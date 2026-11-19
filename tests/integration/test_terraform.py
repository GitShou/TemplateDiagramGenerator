import json
import shutil
from pathlib import Path

import pytest

from template_diagram_generator.cli import main
from template_diagram_generator.models import TerraformInput
from template_diagram_generator.publication import public_model
from template_diagram_generator.semantics import analyze
from template_diagram_generator.terraform import resolve_terraform

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "terraform"


def pairs(model, kind):
    return {
        (edge.source_id, edge.target_id, edge.operation, edge.status)
        for edge in model.edges
        if edge.kind == kind
    }


def test_terraform_example_resolves_module_origin_group_dns_and_schedule():
    graph = resolve_terraform((TerraformInput("site", EXAMPLE),), EXAMPLE)
    model = public_model(analyze(graph))
    assert len(model.nodes) == 6
    assert not model.diagnostics
    assert pairs(model, "TRAFFIC") == {
        (
            "site#aws_cloudfront_distribution.site",
            "site/storage#aws_s3_bucket.site",
            "Primary origin",
            "confirmed",
        ),
        (
            "site#aws_cloudfront_distribution.site",
            "site#aws_s3_bucket.fallback",
            "Failover origin",
            "confirmed",
        ),
    }
    assert pairs(model, "DNS") == {
        ("site#aws_route53_record.site", "site#aws_cloudfront_distribution.site", None, "confirmed")
    }
    assert pairs(model, "EVENT") == {
        ("site#aws_scheduler_schedule.daily", "site#aws_lambda_function.worker", None, "confirmed")
    }
    assert any(
        ref.direct_target_id == "site/storage@output/regional_name"
        for ref in model.references.values()
    )
    assert any(
        ref.source_id == "site" and ref.target_id == "site/storage"
        for ref in model.stack_references
    )
    assert any(
        "origin_group" in item.source.path and item.source.line for item in model.evidence.values()
    )


def test_terraform_config_and_cfn_in_same_artifacts(tmp_path):
    (tmp_path / "diagram.yml").write_text(
        f"version: 1\ninput:\n  base_directory: '{EXAMPLE.parent.parent.as_posix()}'\n  roots:\n"
        "    - {id: tf, terraform: examples/terraform}\n"
        "    - {id: cf, template: examples/web.yaml}\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    arguments = [
        "generate",
        "--config",
        str(tmp_path / "diagram.yml"),
        "--format",
        "json",
        "--output",
        str(out),
    ]
    assert main(arguments) == 0
    data = json.loads((out / "architecture.json").read_text(encoding="utf-8"))
    assert data["metadata"]["input_formats"] == {
        "cf": "cloudformation",
        "cf/Application": "cloudformation",
        "tf": "terraform",
        "tf/storage": "terraform",
    }
    assert data["nodes"]["tf#aws_route53_record.site"]["input_type"] == "aws_route53_record"
    assert "input_type" not in data["nodes"]["cf#SiteBucket"]


def test_direct_tf_file_uses_sibling_configuration_and_static_aws_links(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main.tf").write_text(
        """
resource "aws_apigatewayv2_api" "http" { protocol_type = "HTTP" }
resource "aws_apigatewayv2_integration" "link" {
  api_id = aws_apigatewayv2_api.http.id
  integration_type = "AWS_PROXY"
  integration_uri = aws_lambda_function.worker.invoke_arn
}
resource "aws_apigatewayv2_route" "run" {
  api_id = aws_apigatewayv2_api.http.id
  target = "integrations/${aws_apigatewayv2_integration.link.id}"
}
resource "aws_subnet" "web" { vpc_id = aws_vpc.app.id }
resource "aws_subnet" "invalid" { vpc_id = aws_vpc.app }
resource "aws_lb" "front" { subnets = [aws_subnet.web.id] }
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.front.arn
  default_action {
    type = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
resource "aws_ecs_service" "api" {
  load_balancer { target_group_arn = aws_lb_target_group.app.arn }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "workers.tf").write_text(
        """
resource "aws_lambda_function" "worker" {}
resource "aws_vpc" "app" {}
resource "aws_lb_target_group" "app" { vpc_id = aws_vpc.app.id }
""",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert (
        main(["generate", "--terraform", "main.tf", "--format", "json", "--output", str(out)]) == 0
    )
    data = json.loads((out / "architecture.json").read_text(encoding="utf-8"))
    traffic = {
        (edge["source_id"].split("#")[1], edge["target_id"].split("#")[1])
        for edge in data["edges"]
        if edge["kind"] == "TRAFFIC"
    }
    assert ("aws_apigatewayv2_integration.link", "aws_lambda_function.worker") in traffic
    assert ("aws_lb.front", "aws_lb_listener.http") in traffic
    assert ("aws_lb_listener.http", "aws_lb_target_group.app") in traffic
    assert ("aws_lb_target_group.app", "aws_ecs_service.api") in traffic
    assert not any(
        edge["kind"] == "CONTAINS" and edge["target_id"].endswith("aws_subnet.invalid")
        for edge in data["edges"]
    )


def test_remote_dynamic_escaped_and_sensitive_inputs_remain_unresolved(tmp_path):
    (tmp_path / "main.tf").write_text(
        """
variable "private" {
  sensitive = true
  default = "private-test-value"
}
module "external" { source = "https://example.invalid/mod.zip" }
resource "aws_lambda_function" "worker" { count = 0 }
resource "aws_lambda_function" "multiple" { for_each = toset(["a", "b"]) }
resource "aws_scheduler_schedule" "bad" {
  target { arn = "$${aws_lambda_function.worker.arn}" }
}
resource "aws_scheduler_schedule" "private" {
  target { arn = var.private }
}
resource "aws_scheduler_schedule" "computed" {
  target { arn = aws_lambda_function.multiple["a"].arn }
}
resource "example_service" "kept" { source = module.external.foo }
""",
        encoding="utf-8",
    )
    model = public_model(analyze(resolve_terraform((TerraformInput("root", tmp_path),), tmp_path)))
    assert not pairs(model, "EVENT")
    assert model.nodes["root#example_service.kept"].type == "example_service"
    assert model.nodes["root#aws_lambda_function.worker"].state == "false"
    assert model.containers["root/external"].stack_status == "unresolved"
    assert {item.code for item in model.diagnostics} >= {
        "TERRAFORM_REMOTE_MODULE",
        "TERRAFORM_EXPRESSION_UNRESOLVED",
    }
    assert "private-test-value" not in repr(model)


@pytest.mark.skipif(shutil.which("dot") is None, reason="requires Graphviz dot")
def test_terraform_html_and_svg_are_localized(tmp_path):
    assert (
        main(
            [
                "generate",
                "--config",
                str(EXAMPLE / "diagram.yaml"),
                "--output",
                str(tmp_path),
                "--fail-on-warning",
            ]
        )
        == 0
    )
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "<title>Terraform 構成図</title>" in html
    assert "Terraform アドレス" in html and "モジュール構成" in html
    assert "主オリジン" in html and "フェイルオーバー先" in html
    assert "aws_scheduler_schedule.daily" in html
