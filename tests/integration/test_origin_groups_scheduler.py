import json

import pytest

from template_diagram_generator import ResolveOptions, RootInput, resolve
from template_diagram_generator.semantics import analyze
from template_diagram_generator.views import build_views


def _model(tmp_path, resources):
    path = tmp_path / "root.json"
    path.write_text(json.dumps({"Resources": resources}), encoding="utf-8")
    return analyze(resolve([RootInput("r", path)], ResolveOptions(tmp_path)))


def _origin(id_, bucket):
    return {
        "Id": id_,
        "DomainName": {"Fn::GetAtt": [bucket, "RegionalDomainName"]},
        "S3OriginConfig": {},
    }


@pytest.mark.parametrize(
    ("variant", "diagnostic"),
    [
        ("valid", None),
        ("unused", None),
        ("unknown-member", "CLOUDFRONT_GROUP_MEMBERS"),
        ("duplicate-member", "CLOUDFRONT_GROUP_MEMBERS"),
        ("bad-code", "CLOUDFRONT_GROUP_CRITERIA"),
        ("non-default", "CLOUDFRONT_GROUP_SELECTION"),
        ("ambiguous-id", "CLOUDFRONT_ORIGIN"),
    ],
)
def test_cloudfront_group_roles_and_rejection_of_ambiguous_groups(tmp_path, variant, diagnostic):
    second = {"unknown-member": "unknown", "duplicate-member": "primary"}.get(variant, "secondary")
    origins = [
        _origin("primary", "Main"),
        _origin("secondary", "Fallback"),
        _origin("other", "Unused"),
    ]
    if variant == "ambiguous-id":
        origins.append(_origin("pair", "Unused"))
    group = {
        "Id": "pair",
        "Members": {"Quantity": 2, "Items": [{"OriginId": "primary"}, {"OriginId": second}]},
        "FailoverCriteria": {
            "StatusCodes": {"Quantity": 2, "Items": [404, 200 if variant == "bad-code" else 503]}
        },
    }
    if variant == "non-default":
        group["SelectionCriteria"] = "media-quality-based"
    resources = {key: {"Type": "AWS::S3::Bucket"} for key in ("Main", "Fallback", "Unused")}
    resources["CDN"] = {
        "Type": "AWS::CloudFront::Distribution",
        "Properties": {
            "DistributionConfig": {
                "Enabled": True,
                "Origins": origins,
                "OriginGroups": {"Quantity": 1, "Items": [group]},
                "DefaultCacheBehavior": {
                    "TargetOriginId": "primary" if variant == "unused" else "pair"
                },
            }
        },
    }
    model = _model(tmp_path, resources)
    paths = {(item.target_id, item.operation) for item in model.edges if item.kind == "TRAFFIC"}
    if variant == "valid":
        assert paths == {("r#Main", "Primary origin"), ("r#Fallback", "Failover origin")}
        assert all(edge.operation for graph in build_views(model).overview for edge in graph.edges)
        assert any("StatusCodes" in proof.path for proof in model.evidence.values())
    elif variant == "unused":
        assert paths == {("r#Main", None)}
    else:
        assert not paths
    assert {item.code for item in model.diagnostics} == ({diagnostic} if diagnostic else set())


def test_scheduler_only_uses_target_arn_and_marks_disabled_links_inactive(tmp_path):
    def schedule(arn, state=None):
        props = {
            "Target": {
                "Arn": arn,
                "DeadLetterConfig": {"Arn": {"Fn::GetAtt": ["Dead", "Arn"]}},
                "RoleArn": {"Fn::GetAtt": ["Role", "Arn"]},
            }
        }
        if state:
            props["State"] = state
        return {"Type": "AWS::Scheduler::Schedule", "Properties": props}

    model = _model(
        tmp_path,
        {
            "Worker": {"Type": "AWS::Lambda::Function"},
            "Dead": {"Type": "AWS::SQS::Queue"},
            "Role": {"Type": "AWS::IAM::Role"},
            "Flow": {"Type": "AWS::StepFunctions::StateMachine"},
            "Daily": schedule({"Fn::GetAtt": ["Worker", "Arn"]}),
            "Off": schedule({"Ref": "Flow"}, "DISABLED"),
            "Unresolved": schedule("arn:aws:scheduler:::aws-sdk:s3:putObject"),
            "InvalidQueue": schedule({"Ref": "Dead"}),
        },
    )
    event = {
        (edge.source_id, edge.target_id, edge.status)
        for edge in model.edges
        if edge.kind == "EVENT"
    }
    assert event == {("r#Daily", "r#Worker", "confirmed"), ("r#Off", "r#Flow", "inactive")}
    assert any(item.code == "SCHEDULER_TARGET" for item in model.diagnostics)
    assert all(
        edge.start != "r#Off" for graph in build_views(model).overview for edge in graph.edges
    )
