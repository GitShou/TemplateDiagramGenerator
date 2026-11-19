import json

import pytest

from template_diagram_generator import ResolveOptions, RootInput, resolve
from template_diagram_generator.architecture import EdgeKind
from template_diagram_generator.semantics import analyze
from template_diagram_generator.views import build_views


def _analyze(tmp_path, resources):
    path = tmp_path / "root.json"
    path.write_text(json.dumps({"Resources": resources}), encoding="utf-8")
    return analyze(resolve([RootInput("r", path)], ResolveOptions(tmp_path)))


def _resource(type_, **props):
    return {"Type": type_, "Properties": props}


def _ref(name):
    return {"Ref": name}


def _attr(name, attr):
    return {"Fn::GetAtt": [name, attr]}


@pytest.mark.parametrize(
    ("reference", "method", "valid"),
    [
        ("same", "POST", True),
        ("foreign", "POST", False),
        ("same", "GET", False),
        ("uri", "POST", False),
    ],
)
def test_rest_lambda_requires_matching_resource_and_invocation_uri(
    tmp_path, reference, method, valid
):
    uri = {
        "Fn::Join": [
            "",
            [
                "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/",
                _attr("Function", "Arn"),
                "/invocations",
            ],
        ]
    }
    model = _analyze(
        tmp_path,
        {
            "Api": _resource("AWS::ApiGateway::RestApi"),
            "Other": _resource("AWS::ApiGateway::RestApi"),
            "Node": _resource(
                "AWS::ApiGateway::Resource",
                RestApiId=_ref("Other" if reference == "foreign" else "Api"),
            ),
            "Method": _resource(
                "AWS::ApiGateway::Method",
                RestApiId=_ref("Api"),
                ResourceId=_ref("Node"),
                Integration={
                    "Type": "AWS",
                    "IntegrationHttpMethod": method,
                    "Uri": _attr("Function", "Arn") if reference == "uri" else uri,
                },
            ),
            "Function": _resource("AWS::Lambda::Function"),
        },
    )
    pairs = {
        (edge.source_id, edge.target_id) for edge in model.edges if edge.kind is EdgeKind.TRAFFIC
    }
    assert pairs == ({("r#Api", "r#Method"), ("r#Method", "r#Function")} if valid else set())


def test_dns_record_and_group_require_a_matching_zone(tmp_path):
    model = _analyze(
        tmp_path,
        {
            "CloudFront": _resource("AWS::CloudFront::Distribution"),
            "AppLB": _resource("AWS::ElasticLoadBalancingV2::LoadBalancer"),
            "OtherLB": _resource("AWS::ElasticLoadBalancingV2::LoadBalancer"),
            "RootRecord": _resource(
                "AWS::Route53::RecordSet",
                Type="A",
                AliasTarget={
                    "DNSName": _attr("CloudFront", "DomainName"),
                    "HostedZoneId": "Z2FDTNDATAQYW2",
                },
            ),
            "WrongRecord": _resource(
                "AWS::Route53::RecordSet",
                Type="A",
                AliasTarget={
                    "DNSName": _attr("AppLB", "DNSName"),
                    "HostedZoneId": _attr("OtherLB", "CanonicalHostedZoneID"),
                },
            ),
            "Group": _resource(
                "AWS::Route53::RecordSetGroup",
                RecordSets=[
                    {
                        "Type": "AAAA",
                        "AliasTarget": {
                            "DNSName": {"Fn::Sub": "dualstack.${AppLB.DNSName}"},
                            "HostedZoneId": _attr("AppLB", "CanonicalHostedZoneID"),
                        },
                    },
                    {"Type": "CNAME", "ResourceRecords": [_attr("AppLB", "DNSName")]},
                ],
            ),
        },
    )
    edges = [edge for edge in model.edges if edge.kind is EdgeKind.DNS]
    assert {(edge.source_id, edge.target_id) for edge in edges} == {
        ("r#RootRecord", "r#CloudFront"),
        ("r#Group", "r#AppLB"),
    }
    group_edge = next(edge for edge in edges if edge.source_id == "r#Group")
    assert any(
        model.evidence[id_].path[3:5] == ("RecordSets", 0) for id_ in group_edge.evidence_ids
    )
    assert {item.code for item in model.diagnostics} == {"ROUTE53_ALIAS_ZONE"}
    overview_ids = {node.id for page in build_views(model).overview for node in page.nodes}
    assert {"r#RootRecord", "r#CloudFront", "r#Group", "r#AppLB"} <= overview_ids
    assert "r#WrongRecord" not in overview_ids


def test_step_functions_traces_inline_substitutions_and_nested_tasks(tmp_path):
    states = {
        "StartAt": "Parallel",
        "States": {
            "Parallel": {
                "Type": "Parallel",
                "Branches": [
                    {
                        "StartAt": "Fetch",
                        "States": {
                            "Fetch": {
                                "Type": "Task",
                                "Resource": "arn:aws:states:::dynamodb:getItem",
                                "Parameters": {"TableName": "${Table}"},
                                "End": True,
                            },
                        },
                    }
                ],
                "End": True,
            },
            "Map": {
                "Type": "Map",
                "ItemProcessor": {
                    "StartAt": "Update",
                    "States": {
                        "Update": {
                            "Type": "Task",
                            "Resource": "arn:${AWS::Partition}:states:::aws-sdk:dynamodb:updateItem",
                            "Parameters": {"TableName": "${ReadWriteTable.Arn}"},
                            "End": True,
                        },
                    },
                },
                "End": True,
            },
        },
    }
    model = _analyze(
        tmp_path,
        {
            "ReadWriteTable": _resource("AWS::DynamoDB::Table", TableName="audit-local"),
            "Read": _resource(
                "AWS::StepFunctions::StateMachine",
                DefinitionString=json.dumps(states["States"]["Parallel"]["Branches"][0]),
                DefinitionSubstitutions={"Table": _ref("ReadWriteTable")},
            ),
            "Write": _resource(
                "AWS::StepFunctions::StateMachine",
                DefinitionString={
                    "Fn::Sub": json.dumps(
                        {
                            "StartAt": "Map",
                            "States": {"Map": states["States"]["Map"]},
                        }
                    )
                },
            ),
            "Delete": _resource(
                "AWS::StepFunctions::StateMachine",
                Definition={
                    "StartAt": "Task",
                    "States": {
                        "Task": {
                            "Type": "Task",
                            "Resource": "arn:aws:states:::dynamodb:deleteItem",
                            "Arguments": {"TableName": "audit-local"},
                            "End": True,
                        }
                    },
                },
            ),
        },
    )
    edges = [edge for edge in model.edges if edge.kind is EdgeKind.DATA_ACCESS]
    assert {(edge.source_id, edge.target_id, edge.operation) for edge in edges} == {
        ("r#Read", "r#ReadWriteTable", "GetItem"),
        ("r#Write", "r#ReadWriteTable", "UpdateItem"),
        ("r#Delete", "r#ReadWriteTable", "DeleteItem"),
    }
    symbolic = next(edge for edge in edges if edge.operation == "UpdateItem")
    assert any(model.evidence[id_].reference_ids for id_ in symbolic.evidence_ids)
    assert not model.diagnostics


def test_step_functions_leaves_dynamic_or_ambiguous_targets_unconnected(tmp_path):
    tasks = {
        "Dynamic": {"Parameters": {"TableName.$": "$.TableName"}},
        "Ambiguous": {"Parameters": {"TableName": "shared-table"}},
        "CrossAccount": {
            "Parameters": {"TableName": _ref("One")},
            "Credentials": {"RoleArn.$": "$.Role"},
        },
        "Unrelated": {
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {"TableName": _ref("One")},
        },
    }
    model = _analyze(
        tmp_path,
        {
            "One": _resource("AWS::DynamoDB::Table", TableName="shared-table"),
            "Two": _resource("AWS::DynamoDB::Table", TableName="shared-table"),
            "Machine": _resource(
                "AWS::StepFunctions::StateMachine",
                Definition={
                    "StartAt": "Dynamic",
                    "States": {
                        name: {
                            "Type": "Task",
                            "Resource": "arn:aws:states:::dynamodb:putItem",
                            "End": True,
                            **attrs,
                        }
                        for name, attrs in tasks.items()
                    },
                },
            ),
        },
    )
    assert not any(edge.kind is EdgeKind.DATA_ACCESS for edge in model.edges)
    assert {diag.code for diag in model.diagnostics} == {
        "STEP_FUNCTIONS_DYNAMODB_TARGET",
        "STEP_FUNCTIONS_CREDENTIALS",
    }
