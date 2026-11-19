import pytest

from template_diagram_generator import TemplateError, TemplateLoader


def test_yaml_intrinsics_and_source_locations(tmp_path):
    path = tmp_path / "root.yaml"
    path.write_text(
        "AWSTemplateFormatVersion: 2010-09-09\n"
        "Resources:\n  Item:\n    Type: Example::Service::Thing\n    Properties:\n"
        "      State: on\n      Flag: true\n      Attribute: !GetAtt Nested.Outputs.Name\n"
        "      Other: !SomeFutureFunction [!Ref Item]\n",
        encoding="utf-8",
    )
    root = TemplateLoader().load(path, source_name="fixtures/root.yaml").root.mapping()
    assert root["AWSTemplateFormatVersion"].value == "2010-09-09"
    props = root["Resources"].mapping()["Item"].mapping()["Properties"].mapping()
    assert props["State"].value == "on"
    assert props["Flag"].value is True
    assert props["Attribute"].mapping()["Fn::GetAtt"].value == "Nested.Outputs.Name"
    assert (
        props["Other"].mapping()["Fn::SomeFutureFunction"].sequence()[0].mapping()["Ref"].value
        == "Item"
    )
    source = props["Attribute"].source
    assert source.path == ("Resources", "Item", "Properties", "Attribute")
    assert (source.template, source.line, source.column) == ("fixtures/root.yaml", 8, 18)


@pytest.mark.parametrize(
    ("suffix", "content", "code"),
    [
        (".yaml", "Resources: [\n", "TEMPLATE_SYNTAX"),
        (".yaml", "Resources: {}\nResources: {}\n", "DUPLICATE_KEY"),
        (".json", '{"Resources": {}, "Resources": {}}', "DUPLICATE_KEY"),
        (".json", '{"Resources":{},}', "TEMPLATE_SYNTAX"),
        (".json", '{"Number":1e400}', "TEMPLATE_SYNTAX"),
        (".yaml", "Resources: !!python/object/apply:builtins.print [unsafe]\n", "YAML_TAG"),
        (".yaml", "Resources: &a {Alias: *a}\n", "YAML_ALIAS_CYCLE"),
        (".yaml", "- first\n", "TEMPLATE_STRUCTURE"),
    ],
)
def test_loader_rejects_invalid_input(tmp_path, suffix, content, code):
    path = tmp_path / f"root{suffix}"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(TemplateError) as error:
        TemplateLoader().load(path)
    assert error.value.code == code


def test_loader_enforces_size_and_alias_expansion_limits(tmp_path):
    path = tmp_path / "root.yaml"
    path.write_text("one: &one [A, B, C]\ntwo: [*one, *one, *one, *one]\n", encoding="utf-8")
    with pytest.raises(TemplateError, match="byte limit"):
        TemplateLoader(max_bytes=10).load(path)
    with pytest.raises(TemplateError, match="structure limit"):
        TemplateLoader(max_nodes=10).load(path)
