import json
import shutil
from pathlib import Path

import pytest

from template_diagram_generator import ResolveOptions, RootInput, resolve
from template_diagram_generator.architecture import EdgeKind, EdgeOrigin, EdgeStatus
from template_diagram_generator.cli import main
from template_diagram_generator.config import load_config
from template_diagram_generator.models import (
    ReferenceKind,
    ReferenceStatus,
    ValueKind,
)
from template_diagram_generator.publication import public_model
from template_diagram_generator.semantics import analyze
from template_diagram_generator.views import build_views

DEMO = Path(__file__).resolve().parents[2] / "examples" / "extended" / "diagram.yaml"
ACCOUNT = "123456789012"
REGION = "ap-northeast-1"


def _json(root, filename, **sections):
    path = root / filename
    path.write_text(json.dumps(sections), encoding="utf-8")
    return path


def _root(id_, path, account=ACCOUNT, region=REGION):
    return RootInput(id_, path, account_id=account, region=region)


def _edges(model, kind):
    return {
        (edge.source_id, edge.target_id, edge.operation)
        for edge in model.edges
        if edge.kind is kind
    }


def test_extended_example_connects_all_four_features():
    config = load_config(DEMO)
    graph = resolve(config.roots, config.resolution)
    model = public_model(analyze(graph, relationships=config.relationships))
    views = build_views(model, config.view)
    assert not views.diagnostics
    assert graph == resolve(tuple(reversed(config.roots)), config.resolution)
    overview = {node.id: node.label for page in views.overview for node in page.nodes}
    assert overview["orders#Function"] == "注文処理"
    assert "orders#SpareBucket" not in overview
    assert "orders#SpareBucket" in views.inventory_ids
    assert "foundation#AuditTable" in overview
    assert model.nodes["orders#Function"].logical_id == "Function"
    assert _edges(model, EdgeKind.TRAFFIC) == {
        ("orders#OrdersApi", "orders#OrdersMethod", None),
        ("orders#OrdersMethod", "orders#Function", None),
    }
    assert ("foundation#Network", "orders#OrdersSubnet", None) in _edges(model, EdgeKind.CONTAINS)
    assert _edges(model, EdgeKind.DNS) == {("orders#OrdersDns", "orders#OrdersDomain", None)}
    assert _edges(model, EdgeKind.DATA_ACCESS) == {
        ("orders#Workflow", "foundation#AuditTable", "PutItem"),
        ("orders#Function", "foundation#AuditTable", None),
    }
    assert any(
        edge.start == "orders#OrdersApi" and edge.end == "orders#Function"
        for page in views.overview
        for edge in page.edges
    )
    assert {(arc.source_id, arc.target_id) for arc in model.stack_references} == {
        ("orders", "foundation")
    }
    imports = [ref for ref in model.references.values() if ref.kind is ReferenceKind.IMPORT_VALUE]
    assert {ref.direct_target_id for ref in imports} == {
        "foundation@output/SharedVpc",
        "foundation@output/AuditTableName",
    }
    assert all(ref.status is ReferenceStatus.RESOLVED and ref.trace for ref in imports)
    manual = next(edge for edge in model.edges if edge.origin is EdgeOrigin.USER_DEFINED)
    assert manual.source_id == "orders#Function" and manual.status is EdgeStatus.CONFIRMED
    evidence = model.evidence[manual.evidence_ids[0]]
    assert evidence.note and evidence.source.template == "diagram.yaml"
    assert evidence.path == ("relationships", 0, "evidence")


@pytest.mark.parametrize(
    ("account", "region", "diagnostic"),
    [
        ("000000000001", REGION, "IMPORT_NOT_FOUND"),
        (ACCOUNT, "us-east-1", "IMPORT_NOT_FOUND"),
        (None, None, "IMPORT_SCOPE_UNKNOWN"),
    ],
)
def test_import_requires_configured_matching_account_and_region(
    tmp_path, account, region, diagnostic
):
    producer = _json(
        tmp_path,
        "producer.json",
        Outputs={"Id": {"Value": "resolved", "Export": {"Name": "Example"}}},
    )
    consumer = _json(
        tmp_path, "consumer.json", Outputs={"Use": {"Value": {"Fn::ImportValue": "Example"}}}
    )
    graph = resolve(
        [_root("consumer", consumer, account, region), _root("producer", producer)],
        ResolveOptions(tmp_path),
    )
    assert graph.stacks["consumer"].outputs["Use"].kind is ValueKind.UNKNOWN
    assert diagnostic in {item.code for item in graph.diagnostics}
    assert not analyze(graph).stack_references


@pytest.mark.parametrize(
    ("other_export", "expected"),
    [
        ("duplicate", ValueKind.UNKNOWN),
        ("different-region", ValueKind.LITERAL),
        ("disabled", ValueKind.LITERAL),
        ("unknown-condition", ValueKind.UNKNOWN),
        ("unknown-name", ValueKind.UNKNOWN),
    ],
)
def test_import_rejects_potential_collisions_and_retains_literal_provenance(
    tmp_path, other_export, expected
):
    producer = _json(
        tmp_path,
        "producer.json",
        Outputs={"Id": {"Value": "static", "Export": {"Name": "Example"}}},
    )
    consumer = _json(
        tmp_path, "consumer.json", Outputs={"Use": {"Value": {"Fn::ImportValue": "Example"}}}
    )
    extra_sections = {}
    extra_output = {"Value": "wrong", "Export": {"Name": "Example"}}
    if other_export == "disabled":
        extra_sections["Conditions"] = {"Active": {"Fn::Equals": ["on", "off"]}}
        extra_output["Condition"] = "Active"
    if other_export in {"unknown-name", "unknown-condition"}:
        extra_sections["Parameters"] = {"Name": {"Type": "String", "NoEcho": True}}
        if other_export == "unknown-name":
            extra_output["Export"]["Name"] = {"Ref": "Name"}
        else:
            extra_sections["Conditions"] = {"Active": {"Fn::Equals": [{"Ref": "Name"}, "on"]}}
            extra_output["Condition"] = "Active"
    extra = _json(tmp_path, "extra.json", Outputs={"Id": extra_output}, **extra_sections)
    roots = [
        _root("consumer", consumer),
        _root("producer", producer),
        _root("extra", extra, region="us-east-1" if other_export == "different-region" else REGION),
    ]
    graph = resolve(roots, ResolveOptions(tmp_path))
    assert graph.stacks["consumer"].outputs["Use"].kind is expected
    if expected is ValueKind.LITERAL:
        assert graph.stacks["consumer"].outputs["Use"].value == "static"
        assert {(ref.source_id, ref.target_id) for ref in analyze(graph).stack_references} == {
            ("consumer", "producer")
        }
    else:
        assert "IMPORT_AMBIGUOUS" in {diag.code for diag in graph.diagnostics}


def test_cross_stack_export_cycles_are_diagnosed(tmp_path):
    first = _json(
        tmp_path,
        "first.json",
        Outputs={"A": {"Value": {"Fn::ImportValue": "B"}, "Export": {"Name": "A"}}},
    )
    second = _json(
        tmp_path,
        "second.json",
        Outputs={"B": {"Value": {"Fn::ImportValue": "A"}, "Export": {"Name": "B"}}},
    )
    graph = resolve([_root("first", first), _root("second", second)], ResolveOptions(tmp_path))
    assert "SYMBOL_CYCLE" in {item.code for item in graph.diagnostics}
    assert graph.stacks["first"].outputs["A"].kind is ValueKind.UNKNOWN
    assert graph.stacks["second"].outputs["B"].kind is ValueKind.UNKNOWN


def test_unknown_config_resource_warns_and_manual_chain_does_not_create_an_automatic_shortcut(
    tmp_path,
):
    _json(
        tmp_path,
        "root.json",
        Resources={
            "Front": {"Type": "AWS::SQS::Queue"},
            "Middle": {"Type": "AWS::IAM::Role"},
            "End": {"Type": "AWS::Lambda::Function"},
        },
    )
    config = _json(
        tmp_path,
        "diagram.json",
        version=1,
        input={"roots": [{"id": "r", "template": "root.json"}]},
        views={"resources": {"r#Missing": {"label": "typo"}}},
        relationships=[
            {
                "source": "r#Front",
                "target": "r#Middle",
                "type": "EVENT",
                "evidence": "First configured hop",
            },
            {
                "source": "r#Middle",
                "target": "r#End",
                "type": "EVENT",
                "evidence": "Second configured hop",
            },
            {
                "source": "r#Front",
                "target": "r#Missing",
                "type": "EVENT",
                "evidence": "Invalid hop",
            },
        ],
    )
    out = tmp_path / "out"
    assert (
        main(
            [
                "generate",
                "--config",
                str(config),
                "--output",
                str(out),
                "--format",
                "json",
                "--format",
                "dot",
                "--fail-on-warning",
            ]
        )
        == 3
    )
    model = json.loads((out / "architecture.json").read_text(encoding="utf-8"))
    assert {item["code"] for item in model["diagnostics"]} == {
        "VIEW_RESOURCE_NOT_FOUND",
        "RELATIONSHIP_RESOURCE_NOT_FOUND",
    }
    dot = (out / "overview-001.dot").read_text(encoding="utf-8")
    assert dot.count('label="User-defined"') == 2
    assert {(edge["source_id"], edge["target_id"]) for edge in model["edges"]} == {
        ("r#Front", "r#Middle"),
        ("r#Middle", "r#End"),
    }


def test_hiding_a_supporting_route_leaves_the_detailed_and_inventory_models_intact():
    config = load_config(DEMO)
    graph = resolve(config.roots, config.resolution)
    model = analyze(graph, relationships=config.relationships)
    from dataclasses import replace

    from template_diagram_generator.views import ResourceView

    views = build_views(
        model,
        replace(config.view, resources={"orders#OrdersMethod": ResourceView(overview="hidden")}),
    )
    assert not any(edge.kind is EdgeKind.TRAFFIC for page in views.overview for edge in page.edges)
    assert any(node.id == "orders#OrdersMethod" for page in views.detail for node in page.nodes)
    assert "orders#OrdersMethod" in views.inventory_ids


@pytest.mark.skipif(shutil.which("dot") is None, reason="requires Graphviz dot")
def test_html_svg_marks_and_escapes_configured_text(tmp_path):
    original = load_config(DEMO)
    config = _json(
        tmp_path,
        "diagram.json",
        version=1,
        input={
            "base_directory": str(original.resolution.input_base),
            "roots": [
                {
                    "id": root.id,
                    "template": str(root.template),
                    "account_id": root.account_id,
                    "region": root.region,
                    **({"stack_name": root.stack_name} if root.stack_name else {}),
                }
                for root in original.roots
            ],
        },
        views={"resources": {"orders#Function": {"label": '<decoded> "Lambda"'}}},
        relationships=[
            {
                "source": "orders#Function",
                "target": "foundation#AuditTable",
                "type": "DATA_ACCESS",
                "evidence": "<script>not markup</script>",
            }
        ],
    )
    out = tmp_path / "report"
    assert (
        main(["generate", "--config", str(config), "--output", str(out), "--fail-on-warning"]) == 0
    )
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "<script>not markup</script>" not in html
    assert "&lt;script&gt;not markup&lt;/script&gt;" in html
    assert "&lt;decoded&gt;" in html
    assert all(token in html for token in ("User-defined", "DNS alias", "PutItem"))
    model = json.loads((out / "architecture.json").read_text(encoding="utf-8"))
    assert model["metadata"]["generator_version"] == "0.2.0"
    assert ACCOUNT not in (out / "architecture.json").read_text(encoding="utf-8")
    assert model["nodes"]["orders#Function"]["logical_id"] == "Function"
