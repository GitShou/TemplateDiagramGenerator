from pathlib import Path

from template_diagram_generator import ResolveOptions, RootInput, resolve
from template_diagram_generator.architecture import EdgeKind, EdgeStatus, NodeRole
from template_diagram_generator.config import load_config
from template_diagram_generator.models import StackStatus
from template_diagram_generator.publication import public_model
from template_diagram_generator.semantics import analyze
from template_diagram_generator.views import build_views

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def sample():
    config = load_config(EXAMPLES / "diagram.yaml")
    return public_model(analyze(resolve(config.roots, config.resolution)))


def pairs(model, kind, status=EdgeStatus.CONFIRMED):
    return {
        (edge.source_id, edge.target_id)
        for edge in model.edges
        if edge.kind is kind and edge.status is status
    }


def test_static_origin_with_unused_origin_and_cross_stack_alb():
    model = sample()
    actual = {pair for pair in pairs(model, EdgeKind.TRAFFIC) if pair[0] == "web#CDN"}
    assert actual == {("web#CDN", "web#SiteBucket"), ("web#CDN", "web/Application#LoadBalancer")}
    edge = next(
        edge
        for edge in model.edges
        if edge.kind is EdgeKind.TRAFFIC
        and edge.source_id == "web#CDN"
        and edge.target_id == "web/Application#LoadBalancer"
    )
    objects = [model.evidence[id_] for id_ in edge.evidence_ids]
    assert {item.path[-1] for item in objects} >= {"DomainName", "TargetOriginId"}
    reference_ids = {id_ for item in objects for id_ in item.reference_ids}
    assert any(
        model.references[id_].attribute == "Outputs.LoadBalancerDnsName" for id_ in reference_ids
    )
    assert any(
        ref.source_id == "web" and ref.target_id == "web/Application"
        for ref in model.stack_references
    )


def test_api_lambda_dynamodb():
    model = sample()
    assert {
        (source, target)
        for source, target in pairs(model, EdgeKind.TRAFFIC)
        if source.startswith("api#")
    } == {
        ("api#HttpApi", "api#GetItems"),
        ("api#GetItems", "api#ItemsIntegration"),
        ("api#ItemsIntegration", "api#ItemsFunction"),
    }
    assert model.nodes["api#Table"].role is NodeRole.INVENTORY
    assert ("api#ItemsFunction", "api#Table") in pairs(model, EdgeKind.DEPENDENCY)
    assert not pairs(model, EdgeKind.DATA_ACCESS)


def test_alb_ecs_rds_without_assumed_database_connection():
    model = sample()
    assert {
        (source, target)
        for source, target in pairs(model, EdgeKind.TRAFFIC)
        if source.startswith("web/Application#")
    } == {
        ("web/Application#LoadBalancer", "web/Application#WebListener"),
        ("web/Application#WebListener", "web/Application#WebTargets"),
        ("web/Application#WebTargets", "web/Application#WebService"),
    }
    assert ("web/Application#ApplicationVPC", "web/Application#Database") in pairs(
        model, EdgeKind.CONTAINS
    )
    overview = build_views(model).overview[0]
    shown = {(edge.start, edge.end) for edge in overview.edges}
    assert ("web/Application#LoadBalancer", "web/Application#WebService") in shown
    assert ("api#HttpApi", "api#ItemsFunction") in shown
    assert "web/Application#WebTargets" not in {node.id for node in overview.nodes}


def test_eventbridge_queue_lambda_and_disabled_mapping():
    model = sample()
    assert pairs(model, EdgeKind.EVENT) == {
        ("events#Schedule", "events#Queue"),
        ("events#Schedule", "events#Worker"),
        ("events#Queue", "events#Worker"),
    }
    assert pairs(model, EdgeKind.EVENT, EdgeStatus.INACTIVE) == {
        ("events#FailureQueue", "events#Worker")
    }
    assert not any(
        edge.end == "events#Worker" and edge.start == "events#FailureQueue"
        for page in build_views(model).detail
        for edge in page.edges
    )


def test_subnet_containment_has_explicit_evidence_and_no_publicness_guesses():
    model = sample()
    containment = pairs(model, EdgeKind.CONTAINS)
    assert ("web/Application#ApplicationVPC", "web/Application#PublicA") in containment
    assert ("web/Application#ApplicationVPC", "web/Application#PrivateB") in containment
    assert ("web/Application#PrivateB", "web/Application#WebService") in containment
    assert ("web/Application#PublicA", "web/Application#LoadBalancer") not in containment
    assert ("web/Application#PrivateB", "web/Application#LoadBalancer") not in containment
    groups = {group.id: group for page in build_views(model).detail for group in page.groups}
    assert groups["web/Application#PublicA"].parent_id == "web/Application#ApplicationVPC"
    assert groups["web/Application#PrivateB"].parent_id == "web/Application#ApplicationVPC"


def test_nested_stack_condition_changes_topology():
    opts = ResolveOptions(EXAMPLES, {"web/Application": Path("stacks/application.yaml")})
    model = analyze(resolve([RootInput("web", Path("web.yaml"))], opts))
    assert model.containers["web/Application"].stack_status is StackStatus.INACTIVE
    assert "web#Application" in model.nodes
    assert not any(key.startswith("web/Application#") for key in model.nodes)
    assert pairs(model, EdgeKind.TRAFFIC) == {("web#CDN", "web#SiteBucket")}
    assert not model.diagnostics


def test_unknown_resource_and_reference_are_preserved():
    model = analyze(resolve([RootInput("r", Path("unknown.json"))], ResolveOptions(EXAMPLES)))
    assert model.nodes["r#FutureResource"].role is NodeRole.UNSUPPORTED
    assert pairs(model, EdgeKind.DEPENDENCY) == {("r#Bucket", "r#FutureResource")}
    assert not pairs(model, EdgeKind.TRAFFIC)
    assert {diag.code for diag in model.diagnostics} == {
        "UNSUPPORTED_RESOURCE_TYPE",
        "UNRESOLVED_REFERENCE",
    }
