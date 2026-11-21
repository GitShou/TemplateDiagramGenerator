import json
from pathlib import Path
from textwrap import dedent

import pytest

from template_diagram_generator import (
    ResolveError,
    ResolveOptions,
    RootInput,
    TemplateError,
    resolve,
)
from template_diagram_generator.models import (
    ConditionState,
    ReferenceKind,
    ReferenceStatus,
    ResourceAttribute,
    StackStatus,
    ValueKind,
    pointer,
)


@pytest.fixture
def write(tmp_path):
    def template(name, content):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(content), encoding="utf-8")
        return path

    return template


def test_resolves_intrinsics_and_parameter_precedence(tmp_path, write):
    root = write(
        "main.yaml",
        """
        Parameters:
          Stage: {Type: String, Default: dev}
          Numbers: {Type: 'List<Number>', Default: '7, 8'}
        Mappings:
          Names:
            prod: {BucketSuffix: production}
        Conditions:
          IsProd: !Equals [!Ref Stage, prod]
          IsOther: !Not [!Condition IsProd]
          Composite: !And [!Condition IsProd, !Or [!Condition IsProd, !Condition IsOther]]
        Resources:
          Bucket:
            Type: AWS::S3::Bucket
            Condition: Composite
          Other:
            Condition: IsOther
            Type: AWS::S3::Bucket
          Reader:
            Type: Example::Service::Reader
            DependsOn: Bucket
            Properties:
              Direct: !GetAtt Bucket.DomainName
              Values:
                - !If [IsProd, !Ref AWS::NoValue, !Ref Missing]
                - !FindInMap [Names, !Ref Stage, BucketSuffix]
                - !FindInMap [Names, !Ref Stage, None, {DefaultValue: fallback}]
              Text: !Sub ['${Stage}/${!literal}/${Bucket.DomainName}', {Stage: !Join ['-', [explicit, override]]}]
              Region: !Ref 'AWS::Region'
              NumberStrings: !Ref Numbers
    """,
    )
    graph = resolve([RootInput("app", root, {"Stage": "prod"})], ResolveOptions(tmp_path))
    assert graph.resources["app#Bucket"].condition_state is ConditionState.TRUE
    assert graph.resources["app#Other"].condition_state is ConditionState.FALSE
    reader = graph.resources["app#Reader"].evaluated_properties
    assert reader.property("Direct").value == ResourceAttribute("app#Bucket", "DomainName")
    assert [item.value for item in reader.property("Values").value] == ["production", "fallback"]
    assert [part.value for part in reader.property("Text").value] == [
        "explicit-override/${literal}/",
        ResourceAttribute("app#Bucket", "DomainName"),
    ]
    assert reader.property("Region").reason == "PSEUDO_PARAMETER"
    assert [part.value for part in reader.property("NumberStrings").value] == ["7", "8"]
    dep = next(ref for ref in graph.references.values() if ref.kind is ReferenceKind.DEPENDS_ON)
    assert (dep.source_entity, dep.direct_target_id, dep.status, pointer(dep.path)) == (
        "app#Reader",
        "app#Bucket",
        ReferenceStatus.RESOLVED,
        "/Resources/Reader/DependsOn",
    )
    assert not graph.diagnostics


def test_nested_outputs_and_sibling_parameters_keep_reference_trace(tmp_path, write):
    root = write(
        "infra/root.yaml",
        """
        Resources:
          AConsumer:
            Type: AWS::CloudFormation::Stack
            Properties:
              TemplateURL: https://example.com/not-fetched.yaml
              Parameters:
                SourceArn: !GetAtt ZProducer.Outputs.ProducedArn
          ZProducer:
            Type: AWS::CloudFormation::Stack
            Properties:
              TemplateURL: shared.yaml
              Parameters: {Label: actual}
        Outputs:
          Through: {Value: !GetAtt AConsumer.Outputs.ConsumedArn}
          ProducerStack: {Value: !Ref ZProducer}
    """,
    )
    write(
        "infra/shared.yaml",
        """
        Parameters:
          Label: {Type: String, Default: default}
        Resources:
          Shared:
            Type: AWS::Lambda::Function
            Properties: {Description: !Ref Label}
        Outputs:
          ProducedArn: {Value: !GetAtt Shared.Arn}
    """,
    )
    write(
        "consumer.yaml",
        """
        Parameters:
          SourceArn: {Type: String, Default: no-fallback-when-passed}
        Resources:
          Shared:
            Type: Example::Service::Consumer
            Properties: {Target: !Ref SourceArn}
        Outputs:
          ConsumedArn: {Value: !Ref SourceArn}
    """,
    )
    opts = ResolveOptions(tmp_path, {"root/AConsumer": Path("consumer.yaml")})
    graph = resolve([RootInput("root", root)], opts)
    target = ResourceAttribute("root/ZProducer#Shared", "Arn")
    assert graph.stacks["root"].outputs["Through"].value == target
    assert graph.stacks["root"].outputs["ProducerStack"].value == ResourceAttribute(
        "root#ZProducer"
    )
    assert graph.stacks["root/AConsumer"].parent_id == "root"
    assert (
        graph.resources["root/AConsumer#Shared"].evaluated_properties.property("Target").value
        == target
    )
    assert (
        graph.resources["root/ZProducer#Shared"].evaluated_properties.property("Description").value
        == "actual"
    )
    ref = next(
        ref for ref in graph.references.values() if ref.source_entity == "root/AConsumer#Shared"
    )
    assert ref.direct_target_id == "root/AConsumer@parameter/SourceArn"
    assert ref.resolved_target_ids == ("root/ZProducer#Shared",)
    assert [graph.references[id_].direct_target_id for id_ in ref.trace] == [
        "root#ZProducer",
        "root/ZProducer#Shared",
    ]
    assert not graph.diagnostics
    assert graph == resolve([RootInput("root", root)], opts)


def test_same_template_has_distinct_stack_instances_and_guards_remote_nested(tmp_path, write):
    root = write(
        "root.yaml",
        """
        Conditions:
          Disabled: !Equals [a, b]
        Resources:
          A: {Type: 'AWS::CloudFormation::Stack', Properties: {TemplateURL: leaf.yaml, Parameters: {Label: a}}}
          B: {Type: 'AWS::CloudFormation::Stack', Properties: {TemplateURL: leaf.yaml, Parameters: {Label: b}}}
          Loop: {Type: 'AWS::CloudFormation::Stack', Properties: {TemplateURL: root.yaml}}
          Remote: {Type: 'AWS::CloudFormation::Stack', Properties: {TemplateURL: 's3://example/foo.yaml'}}
          Outside: {Type: 'AWS::CloudFormation::Stack', Properties: {TemplateURL: '../outside.yaml'}}
          Inactive:
            Condition: Disabled
            Type: AWS::CloudFormation::Stack
            Properties: {TemplateURL: absent.yaml}
    """,
    )
    write(
        "leaf.yaml",
        """
        Parameters: {Label: {Type: String}}
        Resources:
          Thing:
            Type: Example::Service::Thing
            Properties: {Label: !Ref Label}
    """,
    )
    options = ResolveOptions(
        tmp_path, {"r/Inactive": Path("no-read.yaml"), "r/Unused": Path("unused.yaml")}
    )
    graph = resolve([RootInput("r", root)], options)
    assert graph.resources["r/A#Thing"].evaluated_properties.property("Label").value == "a"
    assert graph.resources["r/B#Thing"].evaluated_properties.property("Label").value == "b"
    assert graph.stacks["r/Inactive"].status is StackStatus.INACTIVE
    assert graph.stacks["r/Loop"].status is StackStatus.UNRESOLVED
    assert graph.stacks["r/Outside"].status is StackStatus.UNRESOLVED
    assert graph.stacks["r/Remote"].status is StackStatus.UNRESOLVED
    assert {diag.code for diag in graph.diagnostics} == {
        "NESTED_CYCLE",
        "NESTED_PATH",
        "NESTED_UNMAPPED",
        "UNUSED_NESTED_MAPPING",
    }


def test_unresolved_explicit_nested_input_does_not_use_default(tmp_path, write):
    root = write(
        "root.yaml",
        """
        Resources:
          Child:
            Type: AWS::CloudFormation::Stack
            Properties:
              TemplateURL: leaf.yaml
              Parameters: {Value: !Ref Missing}
    """,
    )
    write(
        "leaf.yaml",
        """
        Parameters:
          Value: {Type: String, Default: would-be-incorrect}
        Outputs:
          Result: {Value: !Ref Value}
    """,
    )
    graph = resolve([RootInput("r", root)], ResolveOptions(tmp_path))
    assert graph.stacks["r/Child"].outputs["Result"].kind is ValueKind.UNKNOWN


def test_nested_parameter_map_can_select_a_mapping_or_omit_it(tmp_path, write):
    root = write(
        "root.yaml",
        """
        Parameters:
          Mode: {Type: String, Default: disabled}
        Conditions:
          PassBinding: !Equals [!Ref Mode, enabled]
        Resources:
          Resource:
            Type: AWS::S3::Bucket
          Child:
            Type: AWS::CloudFormation::Stack
            Properties:
              TemplateURL: leaf.yaml
              Parameters: !If [PassBinding, {Value: !GetAtt Resource.Arn}, !Ref AWS::NoValue]
    """,
    )
    write(
        "leaf.yaml",
        """
        Parameters:
          Value: {Type: String, Default: fallback}
        Outputs:
          Result: {Value: !Ref Value}
    """,
    )
    options = ResolveOptions(tmp_path)
    baseline = resolve([RootInput("r", root)], options)
    override = resolve([RootInput("r", root, {"Mode": "enabled"})], options)
    assert baseline.stacks["r/Child"].outputs["Result"].value == "fallback"
    assert override.stacks["r/Child"].outputs["Result"].value == ResourceAttribute(
        "r#Resource", "Arn"
    )
    assert not (*baseline.diagnostics, *override.diagnostics)


def test_conditional_expansion_unknown_branches_and_secret_values(tmp_path, write):
    root = write(
        "root.yaml",
        """
        Parameters:
          Missing: {Type: String}
          Secret: {Type: String, NoEcho: true, Default: private-fixture-value}
        Conditions:
          Unknown: !Equals [!Ref Missing, dev]
          SecretCondition: !Equals [!Ref Secret, guess]
        Resources:
          A: {Type: 'AWS::S3::Bucket', Condition: Unknown}
          B: {Type: 'AWS::S3::Bucket'}
          Device:
            Type: Example::Service::Device
            Properties:
              Target: !If [Unknown, !Ref A, !Ref B]
              Password: !Sub 'password=${Secret}'
              SecretBranch: !If [SecretCondition, first, second]
              Dynamic: !Join ['', ['{{', 'resolve:secretsmanager:private-dynamic-name', '}}']]
    """,
    )
    graph = resolve(
        [RootInput("r", root, {"Secret": "override-private-value"})], ResolveOptions(tmp_path)
    )
    props = graph.resources["r#Device"].evaluated_properties
    assert props.property("Target").kind is ValueKind.UNKNOWN
    assert graph.resources["r#A"].condition_state is ConditionState.UNKNOWN
    for field in ("Password", "SecretBranch", "Dynamic"):
        assert props.property(field).kind is ValueKind.REDACTED
    target_refs = [
        ref
        for ref in graph.references.values()
        if ref.path[3:4] == ("Target",) and ref.kind is ReferenceKind.REF
    ]
    assert {ref.direct_target_id for ref in target_refs} == {"r#A", "r#B"}
    assert all(ref.status is ReferenceStatus.CONDITIONAL for ref in target_refs)
    for private in ("private-fixture-value", "override-private-value", "private-dynamic-name"):
        assert private not in repr(graph)


def test_foreach_handles_nested_loops_and_source_positions(tmp_path, write):
    root = write(
        "root.yaml",
        """
        Transform: AWS::LanguageExtensions
        Parameters:
          Vars: {Type: CommaDelimitedList, Default: 'One,Two'}
        Resources:
          'Fn::ForEach::Groups':
            - Group
            - !Ref Vars
            - 'Fn::ForEach::Entries':
                - Entry
                - [Red, Blue]
                - 'Entry${Group}${Entry}':
                    Type: AWS::S3::Bucket
                    Properties:
                      'Fn::ForEach::Config':
                        - Property
                        - [1.2.3.4]
                        - 'Value&{Property}': !Sub 'group-${Group}-${Entry}-&{Property}'
        Outputs:
          'Fn::ForEach::Results':
            - Token
            - [One, Two]
            - 'Result${Token}':
                Value:
                  Fn::GetAtt:
                    - !Sub 'Entry${Token}Red'
                    - Arn
    """,
    )
    graph = resolve([RootInput("r", root)], ResolveOptions(tmp_path))
    assert set(graph.resources) == {
        "r#EntryOneRed",
        "r#EntryOneBlue",
        "r#EntryTwoRed",
        "r#EntryTwoBlue",
    }
    bucket = graph.resources["r#EntryOneRed"]
    assert bucket.evaluated_properties.property("Value1.2.3.4").value == "group-One-Red-1.2.3.4"
    assert bucket.source.path == (
        "Resources",
        "Fn::ForEach::Groups",
        2,
        "Fn::ForEach::Entries",
        2,
        "Entry${Group}${Entry}",
    )
    assert graph.stacks["r"].outputs["ResultOne"].value == ResourceAttribute("r#EntryOneRed", "Arn")
    assert not graph.diagnostics


@pytest.mark.parametrize(
    ("transform", "parameter", "max_nodes", "code"),
    [
        ("", "Foo,Bar", 100, "FOREACH_TRANSFORM"),
        ("Transform: AWS::LanguageExtensions", "Foo,Foo", 100, "FOREACH_COLLISION"),
        ("Transform: AWS::LanguageExtensions", "Foo,Bar", 1, "FOREACH_LIMIT"),
    ],
)
def test_foreach_failures_retain_unexpanded_declarations(
    tmp_path, write, transform, parameter, max_nodes, code
):
    root = write(
        "root.yaml",
        f"""
        {transform}
        Parameters:
          Values: {{Type: CommaDelimitedList, Default: '{parameter}'}}
        Resources:
          Fixed: {{Type: 'AWS::S3::Bucket'}}
          'Fn::ForEach::Loop':
            - Item
            - !Ref Values
            - 'Bucket${{Item}}': {{Type: 'AWS::S3::Bucket'}}
    """,
    )
    graph = resolve([RootInput("r", root)], ResolveOptions(tmp_path, max_expanded_nodes=max_nodes))
    assert set(graph.resources) == {"r#Fixed"}
    assert graph.stacks["r"].status is StackStatus.PARTIAL
    assert graph.stacks["r"].unexpanded_declarations
    assert code in {diag.code for diag in graph.diagnostics}


def test_conditions_and_outputs_cycles_are_reported(tmp_path, write):
    root = write(
        "root.yaml",
        """
        Conditions:
          A: !Condition B
          B: !Condition A
        Resources:
          Child:
            Type: AWS::CloudFormation::Stack
            Properties:
              TemplateURL: leaf.yaml
              Parameters: {Value: !GetAtt Child.Outputs.Result}
          Thing:
            Type: Example::Service::Thing
            Condition: A
    """,
    )
    write(
        "leaf.yaml",
        """
        Parameters: {Value: {Type: String}}
        Outputs: {Result: {Value: !Ref Value}}
    """,
    )
    graph = resolve([RootInput("r", root)], ResolveOptions(tmp_path))
    assert graph.resources["r#Thing"].condition_state is ConditionState.UNKNOWN
    assert graph.stacks["r/Child"].outputs["Result"].kind is ValueKind.UNKNOWN
    assert "SYMBOL_CYCLE" in {diag.code for diag in graph.diagnostics}


def test_unknown_transform_marks_declarations_uncertain(tmp_path, write):
    path = write(
        "root.json",
        json.dumps(
            {
                "Transform": ["AWS::Serverless-2016-10-31", "ExampleCustomMacro"],
                "Resources": {"A": {"Type": "AWS::S3::Bucket"}},
            }
        ),
    )
    graph = resolve([RootInput("r", path)], ResolveOptions(tmp_path))
    assert graph.resources["r#A"].condition_state is ConditionState.UNKNOWN
    assert graph.stacks["r"].status is StackStatus.PARTIAL
    assert graph.diagnostics[0].code == "UNSUPPORTED_TRANSFORM"


def test_argument_validation_and_root_error(tmp_path, write):
    path = write("bad.yaml", "Resources: invalid")
    with pytest.raises(ResolveError):
        resolve([RootInput("bad/id", path)], ResolveOptions(tmp_path))
    with pytest.raises(ResolveError):
        resolve([RootInput("good", Path("../outside.yaml"))], ResolveOptions(tmp_path))
    with pytest.raises(TemplateError, match="Resources must be a mapping"):
        resolve([RootInput("good", path)], ResolveOptions(tmp_path))
