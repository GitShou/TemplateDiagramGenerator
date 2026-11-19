import shutil
from pathlib import Path

import pytest

from template_diagram_generator.cli import main

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "extended" / "diagram.yaml"


def test_report_language_changes_dot_without_changing_json(tmp_path):
    common = ["generate", "--config", str(EXAMPLE), "--format", "json", "--format", "dot"]
    ja = tmp_path / "japanese"
    en = tmp_path / "english"
    assert main([*common, "--output", str(ja)]) == 0
    assert main([*common, "--output", str(en), "--locale", "en"]) == 0
    japanese = (ja / "overview-001.dot").read_text(encoding="utf-8")
    english = (en / "overview-001.dot").read_text(encoding="utf-8")
    assert all(
        label in japanese for label in ("利用者定義", "DNS エイリアス", "Lambda 関数", "PutItem")
    )
    assert all(
        label in english for label in ("User-defined", "DNS alias", "Lambda Function", "PutItem")
    )
    assert (ja / "architecture.json").read_bytes() == (en / "architecture.json").read_bytes()


@pytest.mark.skipif(shutil.which("dot") is None, reason="requires Graphviz dot")
def test_direct_cli_japanese_localizes_html_svg_and_diagnostics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "root.yaml").write_text(
        "Resources: {CustomItem: {Type: 'Example::Custom::Item', DependsOn: Missing}}\n",
        encoding="utf-8",
    )
    cmd = ["generate", "--template", "root.yaml", "--locale", "ja", "--fail-on-warning"]
    assert main(cmd) == 3
    out = tmp_path / "diagram-output"
    html = (out / "index.html").read_text(encoding="utf-8")
    assert '<html lang="ja">' in html
    assert "<h2>構成概要</h2>" in html
    assert "<h2>リソース一覧</h2>" in html
    assert 'data-count-format="全 {total} 件中 {visible} 件"' in html
    assert "スタック内で参照先が見つかりません。" in html
    assert "UNRESOLVED_REFERENCE" in html
    assert "Example::Custom::Item" in html
    assert "表示対象のリソースがありません" in (out / "overview-001.svg").read_text(
        encoding="utf-8"
    )
    assert '"message": "Reference target was not found in its stack."' in (
        out / "architecture.json"
    ).read_text(encoding="utf-8")


def test_unknown_config_locale_is_rejected(tmp_path, capsys):
    config = tmp_path / "diagram.yaml"
    config.write_text(
        "version: 1\ninput: {roots: [{id: root, template: root.yaml}]}\nviews: {locale: fr}\n",
        encoding="utf-8",
    )
    assert main(["generate", "--config", str(config)]) == 2
    assert "views.locale must be en or ja" in capsys.readouterr().err
