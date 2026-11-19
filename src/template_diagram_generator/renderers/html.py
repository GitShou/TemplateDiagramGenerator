"""Render the offline HTML report from the public model and SVG diagrams."""

from functools import partial
from html import escape

from ..architecture import ArchitectureModel, EdgeKind, EdgeOrigin
from ..localization import diagnostic_text, report_text
from ..models import pointer
from ..publication import metadata
from ..views import Diagram, ViewBundle

_STYLE = """
:root{color-scheme:light;font:15px/1.55 system-ui,sans-serif;color:#19342d;background:#f3f6f4}
*{box-sizing:border-box}body{margin:0}a{color:#135f50;text-underline-offset:3px}a:focus-visible,input:focus-visible{outline:3px solid #287665;outline-offset:3px}
.wrap{max-width:1370px;margin:auto;padding:0 28px}header{background:#17372e;color:#fff;padding:26px 0 21px}
h1{font-size:clamp(1.6rem,3vw,2.35rem);margin:0 0 9px;letter-spacing:-.035em}nav{display:flex;gap:.55rem 1.15rem;flex-wrap:wrap}header a{color:#e0f4ea}
h2{font-size:1.55rem;margin:0 0 12px;letter-spacing:-.02em}h3{font-size:1rem;margin:0 0 6px}
section{padding:26px 0;border-bottom:1px solid #cfdbd4;scroll-margin-top:15px}.diagram,.sheet{background:#fff;border:1px solid #d1ddd6;border-radius:10px}
.diagram{padding:12px;margin:0 0 14px}.svg{overflow:auto;min-height:72px}.svg svg{display:block;max-width:none}
.legend,.pages{display:flex;gap:.35rem 1rem;flex-wrap:wrap;margin:0 0 12px}.legend span{display:inline-flex;align-items:center;gap:6px}
.line{display:inline-block;width:22px;border-top:2px solid #14604c}.event{border-color:#8a3b10;border-top-style:dashed}.assoc{border-color:#687884;border-top-style:dotted}.contain{border-color:#426475}.depend{border-color:#744e94;border-top-style:dashed}.conditional{border-color:#936320;border-top-style:dashed}.data{border-color:#18568c;border-top-width:3px}
.dns{border-color:#28577e;border-top-style:dotted}.manual{border-color:#67498e;border-top-style:dotted}
.sheet{overflow:auto}table{width:100%;border-collapse:collapse;text-align:left}td,th{padding:8px 11px;border-bottom:1px solid #e4ebe7;vertical-align:top}th{font-weight:650;background:#eaf1ed;white-space:nowrap}tr:last-child td{border-bottom:0}
code{font: .88em ui-monospace,monospace;overflow-wrap:anywhere}label{display:block;font-weight:650}input{width:min(100%,420px);padding:8px 10px;border:1px solid #8da298;border-radius:6px;font:inherit;margin:4px 0 12px}.muted{color:#4d645c}
summary{cursor:pointer}figure ul,details ul{padding-left:1.25em}dl{margin:0}dt{font-weight:650}dd{margin:0 0 7px}footer{padding:16px 0 22px}
@media(max-width:640px){.wrap{padding:0 13px}section{padding:19px 0}td,th{padding:7px}}
"""
_SCRIPT = """
const filter = document.getElementById('inventory-search');
const rows = Array.from(document.querySelectorAll('#inventory table tbody tr'));
const count = document.getElementById('search-result');
filter.addEventListener('input', () => {
  const search = filter.value.trim().toLowerCase();
  let visible = 0;
  for (const row of rows) {
    row.hidden = !row.textContent.toLowerCase().includes(search);
    if (!row.hidden) visible += 1;
  }
  count.textContent = count.dataset.countFormat
    .replace('{visible}', visible)
    .replace('{total}', rows.length);
});
"""


def _source(source) -> str:
    line = f":{source.line}" if source.line else ""
    return escape(source.template + line)


def _legend(stack: bool, pages: tuple[Diagram, ...], locale: str, mode: str) -> str:
    hierarchy, reference = {
        "cloudformation": ("Nested stack", "Stack reference"),
        "terraform": ("Child module", "Module reference"),
        "mixed": ("Containment hierarchy", "Inter-configuration reference"),
    }[mode]
    items = (
        (("contain", hierarchy), ("depend", reference))
        if stack
        else (
            ("", "Traffic"),
            ("event", "Event"),
            ("data", "Data access"),
            ("assoc", "Association"),
            ("contain", "Containment"),
            ("conditional", "Conditional"),
        )
    )
    if not stack:
        if any(edge.kind is EdgeKind.DNS for page in pages for edge in page.edges):
            items += (("dns", "DNS alias"),)
        if any(edge.origin is EdgeOrigin.USER_DEFINED for page in pages for edge in page.edges):
            items += (("manual", "User-defined"),)
    return (
        f'<div class="legend" aria-label="{report_text(locale, "Legend")}">'
        + "".join(
            f'<span><i class="line {cls}" aria-hidden="true"></i>{report_text(locale, label)}</span>'
            for cls, label in items
        )
        + "</div>"
    )


def _edge_label(edge, locale: str) -> str:
    parts = [report_text(locale, edge.kind.value)]
    if edge.origin is EdgeOrigin.USER_DEFINED:
        parts.append(report_text(locale, "User-defined"))
    if edge.operation:
        parts.append(escape(report_text(locale, edge.operation)))
    return " · ".join(parts)


def _evidence_text(item) -> str:
    note = " · " + escape(item.note) if item.note else ""
    return f"{_source(item.source)} <code>{escape(pointer(item.path))}</code>{note}"


def _diagrams(
    key: str,
    title: str,
    pages: tuple[Diagram, ...],
    svgs: dict[str, str],
    model: ArchitectureModel,
    locale: str,
    mode: str,
) -> str:
    parts = [f'<section id="{key}"><h2>{title}</h2>', _legend(key == "stacks", pages, locale, mode)]
    if len(pages) > 1:
        parts.append(
            '<div class="pages">'
            + "".join(f'<a href="#{page.id}">{index}</a>' for index, page in enumerate(pages, 1))
            + "</div>"
        )
    for index, page in enumerate(pages, 1):
        parts.append(f'<figure class="diagram" id="{page.id}">')
        if len(pages) > 1:
            parts.append(f"<figcaption><h3>{title} · {index} / {len(pages)}</h3></figcaption>")
        parts.append(f'<div class="svg">{svgs[page.id]}</div>')
        if page.connections:
            parts.append(
                f"<details><summary>{report_text(locale, 'Connections to other pages')}</summary><ul>"
            )
            for item in page.connections:
                edge = item.edge
                parts.append(
                    f'<li><a href="#{item.page}">{item.page}</a> · {escape(edge.start)} → '
                    f"{escape(edge.end)} · {_edge_label(edge, locale)}</li>"
                )
            parts.append("</ul></details>")
        used = sorted({id_ for edge in page.edges for id_ in edge.edge_ids})
        if used:
            edges = {edge.id: edge for edge in model.edges}
            parts.append(
                f"<details><summary>{report_text(locale, 'Relationship evidence')}</summary><ul>"
            )
            for id_ in used:
                edge = edges.get(id_)
                if edge is not None:
                    pointers = ", ".join(
                        _evidence_text(model.evidence[e]) for e in edge.evidence_ids
                    )
                    parts.append(
                        f"<li>{escape(edge.source_id)} → {escape(edge.target_id)} · {_edge_label(edge, locale)} · {pointers}</li>"
                    )
                elif (ref := model.references.get(id_)) is not None:
                    parts.append(
                        f"<li>{escape(ref.source_entity)} · {_source(ref.source)} "
                        f"<code>{escape(pointer(ref.path))}</code></li>"
                    )
            parts.append("</ul></details>")
        parts.append("</figure>")
    parts.append("</section>")
    return "".join(parts)


def _table_headers(locale: str, *headings: str) -> str:
    cells = "".join(f"<th>{report_text(locale, heading)}</th>" for heading in headings)
    return f'<div class="sheet"><table><thead><tr>{cells}</tr></thead><tbody>'


def render_html(views: ViewBundle, model: ArchitectureModel, svgs: dict[str, str]) -> str:
    locale = views.locale
    text = partial(report_text, locale)
    formats = {
        item.input_format or "cloudformation"
        for item in model.containers.values()
        if item.axis == "stack"
    }
    mode = "cloudformation"
    if "terraform" in formats:
        mode = "mixed" if "cloudformation" in formats else "terraform"
    title, navigation_hierarchy, hierarchy_title, resource_id, group_title, input_title = map(
        text,
        {
            "cloudformation": (
                "CloudFormation architecture",
                "Stacks",
                "Stack overview",
                "Logical ID",
                "Stack",
                "Input templates",
            ),
            "terraform": (
                "Terraform architecture",
                "Modules",
                "Module overview",
                "Terraform address",
                "Modules",
                "Input configurations",
            ),
            "mixed": (
                "Infrastructure architecture",
                "Stacks and modules",
                "Stacks and modules",
                "Resource ID",
                "Stacks and modules",
                "Input configurations",
            ),
        }[mode],
    )
    footer_schema = text("Schema {version}").format(version=model.schema_version)
    navigation = (
        ("overview", "Overview"),
        ("detail", "Detail"),
        ("stacks", navigation_hierarchy),
        ("inventory", "Resources"),
        ("diagnostics", "Diagnostics"),
        ("inputs", "Inputs"),
    )
    parts = [
        f'<!doctype html><html lang="{escape(locale)}"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{title}</title><style>",
        _STYLE,
        "</style></head><body>",
        f'<header><div class="wrap"><h1>{title}</h1><nav aria-label="{text("Report")}">',
        *(f'<a href="#{key}">{text(label)}</a>' for key, label in navigation),
        '</nav></div></header><main class="wrap">',
    ]
    for key, title, pages in (
        ("overview", "Architecture overview", views.overview),
        ("detail", "Architecture detail", views.detail),
        ("stacks", hierarchy_title, views.stacks),
    ):
        parts.append(_diagrams(key, text(title), pages, svgs, model, locale, mode))
    count_format = text("{visible} of {total} resources")
    initial_count = text("{total} resources").format(total=len(views.inventory_ids))
    parts += [
        f'<section id="inventory"><h2>{text("Resource inventory")}</h2>',
        f'<label for="inventory-search">{text("Filter resources")}</label>',
        '<input type="search" id="inventory-search" autocomplete="off">',
        f'<span class="muted" id="search-result" aria-live="polite" data-count-format="{count_format}"> {initial_count}</span>',
        _table_headers(
            locale, resource_id, "Type", group_title, "Condition", "Diagram role", "Source"
        ),
    ]
    for key in views.inventory_ids:
        node = model.nodes[key]
        parts.append(
            f"<tr><td><code>{escape(node.logical_id)}</code></td><td><code>{escape(node.input_type or node.type)}</code></td>"
            f"<td>{escape(node.stack_id)}</td><td>{text(node.state)}</td><td>{text(node.role)}</td><td>{_source(node.source)}</td></tr>"
        )
    parts += [
        f'</tbody></table></div></section><section id="diagnostics"><h2>{text("Diagnostics")}</h2>'
    ]
    if views.diagnostics:
        parts.append(_table_headers(locale, "Severity", "Code", "Message", "Source"))
        for diag in views.diagnostics:
            parts.append(
                f"<tr><td>{escape(text(diag.severity))}</td><td><code>{escape(diag.code)}</code></td>"
                f"<td>{escape(diagnostic_text(locale, diag))}</td><td>{_source(diag.source)} "
                f"<code>{escape(pointer(diag.path))}</code></td></tr>"
            )
        parts.append("</tbody></table></div>")
    else:
        parts.append(f"<p>{text('No diagnostics.')}</p>")
    info = metadata(model)
    parts.append(f'</section><section id="inputs"><h2>{input_title}</h2><dl>')
    for key, status in info["stack_status"].items():
        parts.append(
            f"<dt>{escape(key)}</dt><dd>{_source(model.containers[key].source)} · {text(status)}</dd>"
        )
    parts += [
        "</dl></section>",
        f"<footer>TemplateDiagramGenerator {info['generator_version']} · {footer_schema}</footer>",
        "</main><script>",
        _SCRIPT,
        "</script></body></html>\n",
    ]
    return "".join(parts)
