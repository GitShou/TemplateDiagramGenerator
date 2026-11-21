"""Create a set of artifacts from the public architecture and view models."""

from ..architecture import ArchitectureModel
from ..publication import architecture_json
from ..views import ViewBundle
from .graphviz import GraphvizRenderer, RenderError, diagram_dot
from .html import render_html

FORMATS = ("html", "svg", "json", "dot")


def render(
    model: ArchitectureModel,
    views: ViewBundle,
    formats: tuple[str, ...],
    *,
    diagnostic_locale: str | None = None,
) -> dict[str, str]:
    if not formats or set(formats) - set(FORMATS):
        raise RenderError("Unknown or missing output format.")
    artifacts = {}
    if "json" in formats:
        artifacts["architecture.json"] = architecture_json(
            model, diagnostic_locale=diagnostic_locale
        )
    if set(formats) & {"html", "svg", "dot"}:
        graphviz = GraphvizRenderer() if set(formats) & {"html", "svg"} else None
        svgs = {}
        for diagram in views.diagrams():
            dot = diagram_dot(diagram, locale=views.locale)
            if "dot" in formats:
                artifacts[diagram.id + ".dot"] = dot
            if graphviz:
                svg = graphviz.svg(diagram, dot)
                if "html" in formats:
                    svgs[diagram.id] = svg
                if "svg" in formats:
                    artifacts[diagram.id + ".svg"] = svg
        if "html" in formats:
            artifacts["index.html"] = render_html(views, model, svgs)
    return artifacts


__all__ = ["FORMATS", "RenderError", "render"]
