import json
import os
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from template_diagram_generator.cli import main
from template_diagram_generator.renderers.graphviz import sanitize_svg

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "diagram.yaml"


def test_cli_from_another_directory_without_graphviz(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    argv = ["generate", "--config", str(EXAMPLE), "--format", "json", "--format", "dot"]
    assert main([*argv, "--output", "one", "--fail-on-warning"]) == 0
    assert main([*argv, "--output", "two", "--fail-on-warning"]) == 0
    first = {file.name: file.read_bytes() for file in (tmp_path / "one").iterdir()}
    second = {file.name: file.read_bytes() for file in (tmp_path / "two").iterdir()}
    assert first == second
    assert set(first) == {
        "architecture.json",
        "overview-001.dot",
        "detail-001.dot",
        "stacks-001.dot",
    }
    model = json.loads(first["architecture.json"])
    assert model["schema_version"] == 1
    assert model["metadata"]["roots"] == ["api", "events", "web"]
    assert os.fsencode(tmp_path) not in first["architecture.json"]
    assert b"subgraph" in first["detail-001.dot"]


def test_cli_warning_gate_and_invalid_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "root.yaml").write_text(
        "Resources: {Item: {Type: 'Example::Custom::Type', DependsOn: NullTarget}}"
    )
    assert (
        main(["generate", "--template", "root.yaml", "--format", "json", "--fail-on-warning"]) == 3
    )
    assert (tmp_path / "diagram-output" / "architecture.json").is_file()
    assert "UNRESOLVED_REFERENCE" in capsys.readouterr().err
    (tmp_path / "bad.yaml").write_text("version: 1\nunknown: 3\n")
    assert main(["generate", "--config", "bad.yaml"]) == 2
    assert main(["generate", "--template", "missing.yaml", "--format", "json"]) == 1


def test_missing_graphviz_leaves_no_partially_rendered_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    directory = tmp_path / "missing-dot"
    assert main(["generate", "--config", str(EXAMPLE), "--output", str(directory)]) == 1
    assert not directory.exists()
    assert "Graphviz dot is required" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("dot") is None, reason="requires Graphviz dot")
def test_svg_and_html_are_reproducible_and_self_contained(tmp_path):
    for directory in (tmp_path / "a", tmp_path / "b"):
        assert (
            main(
                [
                    "generate",
                    "--config",
                    str(EXAMPLE),
                    "--output",
                    str(directory),
                    "--fail-on-warning",
                ]
            )
            == 0
        )
    target = tmp_path / "a"
    for file in target.iterdir():
        assert file.read_bytes() == (tmp_path / "b" / file.name).read_bytes()
    html = (target / "index.html").read_text(encoding="utf-8")
    assert all(
        f'id="{id_}"' in html
        for id_ in ("overview", "detail", "stacks", "inventory", "diagnostics", "inputs")
    )
    assert html.count("<svg") == 3
    assert "<script src=" not in html and 'rel="stylesheet"' not in html
    assert html.count("<td><code>") == 58  # logical ID and resource type for each of 29 items
    svg = ET.parse(target / "detail-001.svg").getroot()
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"
    assert {"ApplicationVPC", "WebService", "PrivateB"} <= {
        item.text for item in svg.iter() if item.tag.endswith("}text")
    }


def test_no_echo_dynamic_and_free_text_do_not_leave_analysis(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sample.yaml").write_text(
        """
Parameters:
  Secret: {Type: String, NoEcho: true, Default: noecho-private-fixture}
Metadata: {A: never-publish-metadata}
Resources:
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      Literal: always-private-plaintext
      'never-publish-map-key<svg onload=alert(1)>': !Ref Missing
      Combined: !Sub '${Secret}-private-expression'
      Dynamic: '{{resolve:secretsmanager:dynamic-private-fixture}}'
Outputs:
  Example: {Value: !Ref Secret}
""",
        encoding="utf-8",
    )
    formats = ["json", "dot"] + (["svg", "html"] if shutil.which("dot") else [])
    args = [arg for fmt in formats for arg in ("--format", fmt)]
    assert main(["generate", "--template", "sample.yaml", *args]) == 0
    output = " ".join(
        file.read_text(encoding="utf-8") for file in (tmp_path / "diagram-output").iterdir()
    )
    output += capsys.readouterr().err
    for value in (
        "noecho-private-fixture",
        "never-publish-metadata",
        "always-private-plaintext",
        "private-expression",
        "dynamic-private-fixture",
        "never-publish-map-key",
        "onload=alert(1)",
    ):
        assert value not in output


def test_svg_projection_drops_active_content_and_external_references():
    svg = sanitize_svg(
        b"""
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" onload="a()">
  <script>alert(1)</script><foreignObject/><g id="value" href="http://example.com/">
    <a xlink:href="http://example.com/"><text>link</text></a>
    <text fill="url(https://example.com/)">&lt;script&gt; harmless text</text>
  </g>
</svg>""",
        "overview-001",
    )
    assert all(
        token not in svg for token in ("onload", "foreignObject", "href", "example.com", "<script>")
    )
    assert "&lt;script&gt; harmless text" in svg
