import json
from pathlib import Path

import pytest

from template_diagram_generator import ResolveOptions, RootInput, resolve
from template_diagram_generator.architecture import EdgeKind, EdgeStatus
from template_diagram_generator.semantics import analyze
from template_diagram_generator.views import ViewOptions, build_views


def _template(root, name, resources, **sections):
    path = root / name
    path.write_text(json.dumps({"Resources": resources, **sections}), encoding="utf-8")
    return path


def _analyze(root, path):
    return analyze(resolve([RootInput("r", path)], ResolveOptions(root)))


@pytest.mark.parametrize("wrong_owner", [True, False])
def test_deceptive_api_references_do_not_create_traffic(tmp_path, wrong_owner):
    path = _template(
        tmp_path,
        "root.json",
        {
            "Api": {"Type": "AWS::ApiGatewayV2::Api", "Properties": {"ProtocolType": "HTTP"}},
            "Other": {"Type": "AWS::ApiGatewayV2::Api", "Properties": {"ProtocolType": "HTTP"}},
            "Route": {
                "Type": "AWS::ApiGatewayV2::Route",
                "Properties": {
                    "ApiId": {"Ref": "Api"},
                    "Target": {"Fn::Sub": "integrations/${Proxy}"},
                },
            },
            "Proxy": {
                "Type": "AWS::ApiGatewayV2::Integration",
                "Properties": {
                    "ApiId": {"Ref": "Other" if wrong_owner else "Api"},
                    "IntegrationType": "AWS_PROXY",
                    "IntegrationUri": {
                        "Fn::Sub": "${Function.Arn}" if wrong_owner else "invalid/${Function.Arn}"
                    },
                },
            },
            "Function": {"Type": "AWS::Lambda::Function"},
        },
    )
    model = _analyze(tmp_path, path)
    assert not any(edge.kind is EdgeKind.TRAFFIC for edge in model.edges)
    expected = "API_OWNER_MISMATCH" if wrong_owner else "API_LAMBDA_TARGET"
    assert expected in {item.code for item in model.diagnostics}


def test_non_forward_actions_and_zero_weight_do_not_forward(tmp_path):
    path = _template(
        tmp_path,
        "root.json",
        {
            "Balancer": {"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer"},
            "Group": {"Type": "AWS::ElasticLoadBalancingV2::TargetGroup"},
            "Listener": {
                "Type": "AWS::ElasticLoadBalancingV2::Listener",
                "Properties": {
                    "LoadBalancerArn": {"Ref": "Balancer"},
                    "DefaultActions": [
                        {"Type": "redirect", "TargetGroupArn": {"Ref": "Group"}},
                        {
                            "Type": "forward",
                            "ForwardConfig": {
                                "TargetGroups": [{"TargetGroupArn": {"Ref": "Group"}, "Weight": 0}]
                            },
                        },
                    ],
                },
            },
        },
    )
    model = _analyze(tmp_path, path)
    assert {
        (edge.source_id, edge.target_id) for edge in model.edges if edge.kind is EdgeKind.TRAFFIC
    } == {
        ("r#Balancer", "r#Listener"),
    }


def test_conditions_can_enable_event_edges_without_claiming_certainty(tmp_path):
    path = _template(
        tmp_path,
        "root.json",
        {
            "Queue": {"Type": "AWS::SQS::Queue"},
            "Worker": {"Type": "AWS::Lambda::Function"},
            "Mapping": {
                "Type": "AWS::Lambda::EventSourceMapping",
                "Condition": "Enabled",
                "Properties": {
                    "EventSourceArn": {"Fn::GetAtt": ["Queue", "Arn"]},
                    "FunctionName": {"Ref": "Worker"},
                },
            },
        },
        Parameters={"Mode": {"Type": "String"}},
        Conditions={"Enabled": {"Fn::Equals": [{"Ref": "Mode"}, "enabled"]}},
    )
    model = _analyze(tmp_path, path)
    assert {edge.status for edge in model.edges if edge.kind is EdgeKind.EVENT} == {
        EdgeStatus.CONDITIONAL
    }
    assert not build_views(model).overview[0].edges
    assert any(
        edge.status is EdgeStatus.CONDITIONAL
        for page in build_views(model).detail
        for edge in page.edges
    )
    assert not any(
        page.edges for page in build_views(model, ViewOptions(detail_show_conditional=False)).detail
    )
    assert {
        edge.status
        for edge in build_views(model, ViewOptions(show_conditional=True)).overview[0].edges
    } == {
        EdgeStatus.CONDITIONAL,
    }


def test_dependency_cycle_preserves_output_and_reports_the_back_edge(tmp_path):
    path = _template(
        tmp_path,
        "fixture.json",
        {
            "A": {"Type": "AWS::S3::Bucket", "DependsOn": "B"},
            "B": {"Type": "AWS::S3::Bucket", "DependsOn": "A"},
            "C": {"Type": "AWS::S3::Bucket", "DependsOn": "B"},
        },
    )
    model = _analyze(tmp_path, path)
    assert set(model.nodes) == {"r#A", "r#B", "r#C"}
    assert [diag.entity_id for diag in model.diagnostics if diag.code == "DEPENDENCY_CYCLE"] == [
        "r#B"
    ]


def test_literal_nested_output_retains_stack_level_reference(tmp_path):
    _template(tmp_path, "child.json", {}, Outputs={"Value": {"Value": "local"}})
    path = _template(
        tmp_path,
        "root.json",
        {
            "Nested": {
                "Type": "AWS::CloudFormation::Stack",
                "Properties": {"TemplateURL": "child.json"},
            },
        },
        Outputs={"Read": {"Value": {"Fn::GetAtt": ["Nested", "Outputs.Value"]}}},
    )
    model = _analyze(tmp_path, path)
    assert {(item.source_id, item.target_id) for item in model.stack_references} == {
        ("r", "r/Nested")
    }


def test_thousand_resources_split_and_keep_all_hub_connections(tmp_path):
    resources = {f"Queue{i:04d}": {"Type": "AWS::SQS::Queue"} for i in range(999)}
    resources["Router"] = {
        "Type": "AWS::Events::Rule",
        "Properties": {
            "Targets": [{"Id": name, "Arn": {"Fn::GetAtt": [name, "Arn"]}} for name in resources],
        },
    }
    path = _template(tmp_path, "synthetic.json", resources)
    model = _analyze(tmp_path, Path(path.name))
    views = build_views(model)
    assert len(views.inventory_ids) == 1000
    assert len(views.overview) == 50
    assert all(
        sum(node.destination is None for node in page.nodes) <= 20 for page in views.overview
    )
    assert all(
        sum(node.destination is not None for node in page.nodes) <= 8 for page in views.overview
    )
    router = next(
        page for page in views.overview if any(node.id == "r#Router" for node in page.nodes)
    )
    observed = {conn.edge.end for conn in router.connections if conn.edge.start == "r#Router"}
    observed.update(
        edge.end
        for edge in router.edges
        if edge.start == "r#Router" and edge.end.startswith("r#Queue")
    )
    assert len(observed) == 999
