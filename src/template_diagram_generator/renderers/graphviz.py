"""Translate a diagram to DOT and render SVG with the Graphviz CLI."""

import shutil
import subprocess
from collections import defaultdict
from xml.etree import ElementTree as ET

from ..architecture import EdgeKind, EdgeOrigin, EdgeStatus
from ..localization import report_text
from ..models import ConditionState
from ..semantics.context import stable_id
from ..views import FLOW, Diagram

_SVG = "http://www.w3.org/2000/svg"
ET.register_namespace("", _SVG)
_TAGS = {
    "svg",
    "g",
    "title",
    "desc",
    "polygon",
    "polyline",
    "ellipse",
    "path",
    "text",
    "rect",
    "circle",
}
_ATTRIBUTES = {
    "id",
    "class",
    "width",
    "height",
    "viewBox",
    "transform",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-dasharray",
    "stroke-linecap",
    "stroke-linejoin",
    "points",
    "d",
    "x",
    "y",
    "cx",
    "cy",
    "rx",
    "ry",
    "r",
    "font-family",
    "font-size",
    "font-weight",
    "text-anchor",
    "fill-opacity",
    "stroke-opacity",
    "opacity",
}
_COLORS = {
    EdgeKind.TRAFFIC: ("#14604c", "solid"),
    EdgeKind.DNS: ("#28577e", "dotted"),
    EdgeKind.EVENT: ("#8a3b10", "dashed"),
    EdgeKind.DATA_ACCESS: ("#18568c", "bold"),
    EdgeKind.ASSOCIATION: ("#687884", "dotted"),
    EdgeKind.CONTAINS: ("#426475", "solid"),
    EdgeKind.DEPENDENCY: ("#744e94", "dashed"),
}


class RenderError(RuntimeError):
    """Raised when a requested render cannot be produced."""


def _quote(value: str) -> str:
    clean = "".join(
        character if character in "\r\n\t" or ord(character) >= 32 else " " for character in value
    )
    escaped = (
        clean.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", "\\n")
        .replace("\t", " ")
    )
    return '"' + escaped + '"'


def diagram_dot(diagram: Diagram, *, locale: str = "en") -> str:
    lines = [
        "digraph architecture {",
        f"  graph [rankdir={diagram.direction}, bgcolor=transparent, pad=0.22, nodesep=0.32, ranksep=0.62, splines=true];",
        '  node [shape=box, style="rounded,filled", fillcolor="#f7faf9", color="#82988e",',
        '        fontcolor="#19342d", fontname="Arial", fontsize=11, margin="0.15,0.09"];',
        '  edge [fontname="Arial", fontsize=9, arrowsize=0.68];',
    ]
    by_group = defaultdict(list)
    by_parent = defaultdict(list)
    ids = {node.id: stable_id("node", node.id) for node in diagram.nodes}
    for node in diagram.nodes:
        by_group[node.group_id].append(node)
    for group in diagram.groups:
        by_parent[group.parent_id].append(group)

    def emit_group(key, depth):
        indent = "  " * depth
        for node in by_group[key]:
            label = node.label + ("\n" + node.subtitle if node.subtitle else "")
            style = [f"label={_quote(label)}"]
            if node.state is not ConditionState.TRUE:
                style += ['style="rounded,filled,dashed"', 'fillcolor="#fff7e8"']
            if node.destination:
                style += ["shape=tab"]
            if node.state is ConditionState.FALSE:
                style += ['fontcolor="#5b5956"']
            lines.append(f"{indent}{_quote(ids[node.id])} [{', '.join(style)}];")
        for group in by_parent[key]:
            lines.append(
                f"{indent}subgraph {_quote('cluster_' + stable_id('network', group.id))} {{"
            )
            lines.append(f"{indent}  label={_quote(group.label)};")
            lines.append(
                f'{indent}  fontname="Arial"; fontsize=11; color="#a2b7b1"; style=rounded; margin=14;'
            )
            emit_group(group.id, depth + 1)
            lines.append(f"{indent}}}")

    emit_group(None, 1)
    for edge in diagram.edges:
        color, style = _COLORS[edge.kind]
        labels = []
        if edge.origin is EdgeOrigin.USER_DEFINED:
            color, style = "#67498e", "dotted"
            labels.append(report_text(locale, "User-defined"))
        if edge.status is EdgeStatus.CONDITIONAL:
            color, style = "#936320", "dashed"
            labels.append(report_text(locale, "conditional"))
        if edge.kind is EdgeKind.DNS:
            labels.append(report_text(locale, "DNS alias"))
        if edge.operation:
            labels.append(report_text(locale, edge.operation))
        if edge.kind is EdgeKind.DEPENDENCY:
            template = "{count} ref" if edge.count == 1 else "{count} refs"
            labels.append(report_text(locale, template).format(count=edge.count))
        attributes = [f"color={_quote(color)}", f"style={style}"]
        if (
            edge.kind not in FLOW
            and edge.kind is not EdgeKind.CONTAINS
            or edge.status is not EdgeStatus.CONFIRMED
        ):
            attributes.append("constraint=false")
        if labels:
            attributes.append(f"label={_quote(' · '.join(labels))}")
        lines.append(
            f"  {_quote(ids[edge.start])} -> {_quote(ids[edge.end])} [{', '.join(attributes)}];"
        )
    if not diagram.nodes:
        text = report_text(locale, "No matching resources")
        lines.append(f"  empty [label={_quote(text)}, shape=plaintext];")
    lines.append("}")
    return "\n".join(lines) + "\n"


def sanitize_svg(raw: bytes, document_id: str) -> str:
    opening = raw.find(b"<svg")
    if opening < 0:
        raise RenderError("Graphviz did not produce an SVG document.")
    try:
        root = ET.fromstring(raw[opening:])
    except ET.ParseError as exc:
        raise RenderError("Graphviz produced invalid SVG.") from exc
    if root.tag != f"{{{_SVG}}}svg":
        raise RenderError("Graphviz produced an unexpected SVG root.")
    prefix = f"{{{_SVG}}}"

    def project(node):
        for name, value in list(node.attrib.items()):
            if name not in _ATTRIBUTES or "url(" in value.lower() or "javascript:" in value.lower():
                del node.attrib[name]
            elif name == "id":
                node.attrib[name] = stable_id("svg", document_id, value)
        for child in list(node):
            if child.tag not in {prefix + tag for tag in _TAGS}:
                node.remove(child)
            else:
                project(child)

    project(root)
    root.set("role", "img")
    root.set("aria-label", document_id)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True) + "\n"


class GraphvizRenderer:
    def __init__(self, dot_executable: str | None = None):
        self.executable = dot_executable or shutil.which("dot")
        if not self.executable:
            raise RenderError("Graphviz dot is required for SVG or HTML output. Add it to PATH.")

    def svg(self, diagram: Diagram, dot: str) -> str:
        try:
            result = subprocess.run(
                [self.executable, "-Tsvg"],
                input=dot.encode("utf-8"),
                capture_output=True,
                timeout=45,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RenderError(f"Graphviz could not render {diagram.id}.") from exc
        return sanitize_svg(result.stdout, diagram.id)
