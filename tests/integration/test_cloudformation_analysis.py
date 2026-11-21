import json
import os
import subprocess
import sys
from pathlib import Path

from template_diagram_generator.architecture import EdgeKind, EdgeStatus
from template_diagram_generator.cli import main
from template_diagram_generator.models import RootInput, ValueKind
from template_diagram_generator.resolver import ResolveOptions, resolve
from template_diagram_generator.semantics import analyze
from template_diagram_generator.views import ResourceView, ViewOptions, build_views


def _write(path, source):
    path.write_text(source, encoding="utf-8")
    return path


def _resolve(path):
    return resolve([RootInput("root", path)], ResolveOptions(path.parent))


def test_sam_globals_events_and_network_links_use_declared_resources(tmp_path):
    template = _write(
        tmp_path / "sam.yaml",
        """
Transform: [AWS::LanguageExtensions, AWS::Serverless-2016-10-31]
Globals:
  Function:
    VpcConfig:
      SubnetIds: [!Ref SharedSubnet]
      SecurityGroupIds: [!GetAtt Group.GroupId]
Resources:
  Vpc: {Type: 'AWS::EC2::VPC'}
  SharedSubnet: {Type: 'AWS::EC2::Subnet', Properties: {VpcId: !Ref Vpc}}
  PrivateSubnet: {Type: 'AWS::EC2::Subnet', Properties: {VpcId: !Ref Vpc}}
  Group: {Type: 'AWS::EC2::SecurityGroup', Properties: {VpcId: !Ref Vpc}}
  Api: {Type: 'AWS::Serverless::Api'}
  Http: {Type: 'AWS::Serverless::HttpApi'}
  Queue: {Type: 'AWS::SQS::Queue'}
  Function:
    Type: AWS::Serverless::Function
    Properties:
      VpcConfig:
        SubnetIds: [!Ref PrivateSubnet]
      Events:
        Rest: {Type: Api, Properties: {RestApiId: !Ref Api, Path: /test, Method: GET}}
        Default: {Type: HttpApi, Properties: {ApiId: !Ref Http}}
        Queue: {Type: SQS, Properties: {Queue: !GetAtt Queue.Arn, Enabled: false}}
        WrongApi: {Type: HttpApi, Properties: {ApiId: !Ref Api}}
        Implicit: {Type: Api, Properties: {Path: /implicit, Method: GET}}
""",
    )
    graph = _resolve(template)
    model = analyze(graph)
    function = model.nodes["root#Function"]
    assert (function.type, function.input_type, function.role) == (
        "AWS::Lambda::Function",
        "AWS::Serverless::Function",
        "primary",
    )
    assert not model.diagnostics
    links = {
        (edge.source_id, edge.target_id, edge.kind, edge.status)
        for edge in model.edges
        if edge.kind is not EdgeKind.DEPENDENCY
        and "root#Function" in {edge.source_id, edge.target_id}
    }
    assert links == {
        ("root#Api", "root#Function", EdgeKind.TRAFFIC, EdgeStatus.CONFIRMED),
        ("root#Http", "root#Function", EdgeKind.TRAFFIC, EdgeStatus.CONFIRMED),
        ("root#Queue", "root#Function", EdgeKind.EVENT, EdgeStatus.INACTIVE),
        *{
            ("root#Function", f"root#{target}", EdgeKind.ASSOCIATION, EdgeStatus.CONFIRMED)
            for target in ("SharedSubnet", "PrivateSubnet", "Group")
        },
    }
    global_proof = next(
        item for item in model.evidence.values() if item.source.path[:1] == ("Globals",)
    )
    assert global_proof.source.path == ("Globals", "Function", "VpcConfig", "SecurityGroupIds", 0)


def test_cloudwatch_associations_and_actions_have_bounded_overview(tmp_path):
    resources = {
        "Worker": {"Type": "AWS::Lambda::Function", "Properties": {"FunctionName": "local-worker"}},
        "Alert": {"Type": "AWS::SNS::Topic"},
        "Foreign": {"Type": "AWS::SQS::Queue"},
    }
    for index in range(20):
        resources[f"Alarm{index:02}"] = {
            "Type": "AWS::CloudWatch::Alarm",
            "Properties": {
                "Metrics": [
                    {
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/Lambda",
                                "Dimensions": [
                                    {
                                        "Name": "FunctionName",
                                        "Value": "local-worker" if index % 2 else {"Ref": "Worker"},
                                    },
                                    {"Name": "FunctionName", "Value": {"Ref": "Foreign"}},
                                ],
                            }
                        }
                    }
                ],
                "ActionsEnabled": index != 0,
                "AlarmActions": [{"Ref": "Alert"}],
            },
        }
    path = _write(tmp_path / "alarms.json", json.dumps({"Resources": resources}))
    model = analyze(_resolve(path))
    monitored = [edge for edge in model.edges if edge.kind is EdgeKind.ASSOCIATION]
    assert len(monitored) == 20 and {edge.target_id for edge in monitored} == {"root#Worker"}
    actions = [edge for edge in model.edges if edge.kind is EdgeKind.EVENT]
    assert len(actions) == 20 and {edge.target_id for edge in actions} == {"root#Alert"}
    assert (
        next(edge for edge in actions if edge.source_id == "root#Alarm00").status
        is EdgeStatus.INACTIVE
    )
    options = ViewOptions(resources={"root#Alarm01": ResourceView(overview="hidden")})
    overview = build_views(model, options).overview[0]
    assert 0 < len(overview.edges) <= 12
    assert "root#Alarm01" not in {node.id for node in overview.nodes}
    assert not build_views(model, ViewOptions(overview_selection="primary")).overview[0].edges


def test_cidr_select_resolves_known_networks_and_preserves_missing_inputs(tmp_path):
    template = _write(
        tmp_path / "network.yaml",
        """
Parameters:
  Block: {Type: String}
  Unused: {Type: String}
  Protected: {Type: String, NoEcho: true}
Conditions:
  Include: !Equals [!Ref Block, '127.0.0.0/8']
Resources:
  Vpc:
    Type: AWS::EC2::VPC
    Properties: {CidrBlock: 10.42.0.12/16}
Outputs:
  Selected: {Value: !Select [1, !Cidr [!GetAtt Vpc.CidrBlock, '2', 8]]}
  Ipv6: {Value: !Select [1, !Cidr ['2001:db8::/56', 2, 64]]}
  Unknown: {Value: !Select [0, !Cidr [!Ref Block, 1, 8]]}
  Hidden: {Value: !Cidr [!Ref Protected, 1, 8]}
  InvalidBlock: {Value: !Cidr ['192.168.0.0/24', 2, 8]}
  InvalidIndex: {Value: !Select [3, [a, b]]}
  InvalidNull: {Value: !Select [0, [a, null]]}
  ConditionalIndex: {Value: !Select [1, [!If [Include, a, !Ref 'AWS::NoValue'], b, c]]}
""",
    )
    graph = _resolve(template)
    outputs = graph.stacks["root"].outputs
    assert outputs["Selected"].value == "10.42.1.0/24"
    assert outputs["Ipv6"].value == "2001:db8:0:1::/64"
    assert outputs["Unknown"].reason == "PARAMETER_MISSING"
    assert outputs["Unknown"].reference_ids
    assert outputs["Hidden"].kind is ValueKind.REDACTED
    assert outputs["ConditionalIndex"].kind is ValueKind.UNKNOWN
    assert [item.code for item in graph.diagnostics].count("INVALID_INTRINSIC") == 3
    assert not any(item.code == "UNSUPPORTED_INTRINSIC" for item in graph.diagnostics)


def test_external_template_json_locale_and_missing_parameter_summary(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    templates = tmp_path / "templates"
    templates.mkdir()
    template = _write(
        templates / "stack.yaml",
        """
Parameters:
  Missing: {Type: String}
  Unused: {Type: String}
Resources:
  Function:
    Type: AWS::Lambda::Function
    Properties: {Role: !Ref Missing, FunctionName: !Ref Missing}
""",
    )
    monkeypatch.chdir(app)
    cmd = ["generate", "--template", str(template), "--format", "json", "--locale", "ja"]
    assert main([*cmd, "--localize-json"]) == 0
    output = app / "diagram-output" / "architecture.json"
    model = json.loads(output.read_text(encoding="utf-8"))
    assert model["metadata"]["templates"] == ["stack.yaml"]
    assert model["metadata"]["diagnostic_locale"] == "ja"
    assert model["metadata"]["missing_parameters"] == {
        "stack@parameter/Missing": 2,
        "stack@parameter/Unused": 0,
    }
    diagnostic = next(item for item in model["diagnostics"] if item["code"] == "PARAMETER_MISSING")
    assert "パラメータ" in diagnostic["message_localized"]
    assert diagnostic["message"] == "Parameter has no supplied value or default."
    assert main([*cmd, "--input-base", str(app)]) == 2
    assert main([*cmd, "--input-base", str(tmp_path)]) == 0
    model = json.loads(output.read_text(encoding="utf-8"))
    assert model["metadata"]["templates"] == ["templates/stack.yaml"]
    assert "message_localized" not in model["diagnostics"][0]


def test_cloudformation_cli_can_run_with_hcl2_unavailable(tmp_path):
    _write(tmp_path / "stack.yaml", "Resources: {Worker: {Type: 'AWS::Lambda::Function'}}")
    _write(tmp_path / "main.tf", 'resource "aws_lambda_function" "worker" {}')
    runner = (
        "import sys; sys.modules['hcl2'] = None; "
        "from template_diagram_generator.cli import main; sys.exit(main())"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    common = [sys.executable, "-c", runner, "generate", "--format", "json"]
    result = subprocess.run(
        [*common, "--template", "stack.yaml"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [*common, "--terraform", "main.tf"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 2
    assert "template-diagram-generator[terraform]" in result.stderr
