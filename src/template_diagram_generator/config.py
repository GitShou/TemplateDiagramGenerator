"""Read and validate the versioned CLI configuration."""

import re
from dataclasses import dataclass
from pathlib import Path

from .architecture import MANUAL_EDGE_KINDS, ManualRelationship
from .loader import TemplateError, TemplateLoader
from .localization import REPORT_LOCALES
from .models import AstNode, RootInput, TerraformInput
from .renderers import FORMATS
from .resolver import ResolveOptions
from .views import ResourceView, ViewOptions

DEFAULT_FORMATS = ("html", "svg", "json")
ROOT_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_STACK_ID = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9]+)+\Z")
_RESOURCE_ID = re.compile(
    r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*#(?:[A-Za-z0-9]+|[A-Za-z_][A-Za-z0-9_-]*\.[A-Za-z_][A-Za-z0-9_-]*)\Z"
)
_ACCOUNT_ID = re.compile(r"[0-9]{12}\Z")
_REGION = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+-[0-9]+\Z")
_STACK_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,127}\Z")


class ConfigError(ValueError):
    """A config value has a missing, unknown or invalid field."""


@dataclass(frozen=True, slots=True)
class Config:
    roots: tuple[RootInput | TerraformInput, ...]
    resolution: ResolveOptions
    view: ViewOptions
    directory: Path
    formats: tuple[str, ...]
    relationships: tuple[ManualRelationship, ...] = ()
    localize_json_diagnostics: bool = False


def _object(node: AstNode | None, section: str, *keys: str) -> dict[str, AstNode]:
    if node is None:
        return {}
    fields = node.mapping()
    if fields is None or (keys and fields.keys() - set(keys)):
        raise ConfigError(f"{section} must be a mapping containing only supported keys.")
    return fields


def _text(node: AstNode | None, field: str, default: str | None = None) -> str:
    if node is None and default is not None:
        return default
    if (
        node is None
        or not isinstance(node.value, str)
        or not node.value.strip()
        or "\x00" in node.value
    ):
        raise ConfigError(f"{field} must be a nonempty string.")
    return node.value


def _flag(node: AstNode | None, field: str, default: bool) -> bool:
    if node is None:
        return default
    if type(node.value) is not bool:
        raise ConfigError(f"{field} must be true or false.")
    return node.value


def _limit(node: AstNode | None, field: str, default: int) -> int:
    if node is None:
        return default
    if type(node.value) is not int or not 1 <= node.value <= 1000:
        raise ConfigError(f"{field} must be an integer from 1 to 1000.")
    return node.value


def _parameter(node: AstNode):
    if isinstance(node.value, tuple) and all(
        item.mapping() is None and item.sequence() is None for item in node.value
    ):
        return [item.value for item in node.value]
    if node.mapping() is None and node.sequence() is None:
        return node.value
    raise ConfigError("Root parameter overrides must be scalar values or lists of scalar values.")


def _formats(node: AstNode | None) -> tuple[str, ...]:
    if node is None:
        return DEFAULT_FORMATS
    items = node.sequence()
    if not items:
        raise ConfigError("output.formats must be a nonempty list.")
    formats = tuple(_text(item, "output.formats") for item in items)
    if set(formats) - set(FORMATS) or len(set(formats)) != len(formats):
        raise ConfigError("output.formats must contain distinct supported formats.")
    return formats


def _public_text(node: AstNode | None, field: str, limit: int) -> str:
    value = _text(node, field)
    if not value.isprintable() or len(value) > limit:
        raise ConfigError(f"{field} must contain at most {limit} printable characters.")
    return value


def _root(item: AstNode) -> RootInput | TerraformInput:
    root = _object(
        item,
        "input.roots entry",
        "id",
        "template",
        "parameters",
        "account_id",
        "region",
        "stack_name",
        "terraform",
        "variables",
    )
    id_ = _text(root.get("id"), "input.roots.id")
    if not ROOT_ID.fullmatch(id_):
        raise ConfigError("input.roots.id contains an unsupported character.")
    if "terraform" in root:
        if root.keys() - {"id", "terraform", "variables"}:
            raise ConfigError("A Terraform root accepts only id, terraform and variables.")
        variables = _object(root.get("variables"), "input.roots.variables")
        return TerraformInput(
            id_,
            Path(_text(root["terraform"], "input.roots.terraform")),
            {key: _parameter(value) for key, value in variables.items()},
        )
    if "variables" in root:
        raise ConfigError("input.roots.variables belongs to a Terraform root.")
    params = _object(root.get("parameters"), "input.roots.parameters")
    account = _text(root["account_id"], "input.roots.account_id") if "account_id" in root else None
    region = _text(root["region"], "input.roots.region") if "region" in root else None
    stack_name = (
        _text(root["stack_name"], "input.roots.stack_name") if "stack_name" in root else None
    )
    if (account is None) != (region is None):
        raise ConfigError("input.roots.account_id and region must be supplied together.")
    if account is not None and (
        not _ACCOUNT_ID.fullmatch(account) or not _REGION.fullmatch(region)
    ):
        raise ConfigError(
            "input.roots requires a 12-digit account_id string and an AWS region code."
        )
    if stack_name is not None and not _STACK_NAME.fullmatch(stack_name):
        raise ConfigError("input.roots.stack_name must be a valid CloudFormation stack name.")
    return RootInput(
        id_,
        Path(_text(root.get("template"), "input.roots.template")),
        {key: _parameter(value) for key, value in params.items()},
        account,
        region,
        stack_name,
    )


def _resource_views(node: AstNode | None) -> dict[str, ResourceView]:
    result = {}
    for key, entry in _object(node, "views.resources").items():
        if not _RESOURCE_ID.fullmatch(key):
            raise ConfigError(
                "views.resources keys must be fully qualified resource IDs (stack#LogicalId)."
            )
        fields = _object(entry, "views.resources entry", "label", "overview")
        label = (
            _public_text(fields["label"], "views.resources.label", 96)
            if "label" in fields
            else None
        )
        overview = _text(fields.get("overview"), "views.resources.overview", "default")
        if overview not in {"default", "primary", "hidden"}:
            raise ConfigError("views.resources.overview must be default, primary or hidden.")
        result[key] = ResourceView(label, overview, entry.source)
    return result


def _relationships(node: AstNode | None) -> tuple[ManualRelationship, ...]:
    if node is None:
        return ()
    items = node.sequence()
    if items is None:
        raise ConfigError("relationships must be a list.")
    result = []
    seen = set()
    kinds = {kind.value: kind for kind in MANUAL_EDGE_KINDS}
    for item in items:
        fields = _object(item, "relationships entry", "source", "target", "type", "evidence")
        start = _text(fields.get("source"), "relationships.source")
        end = _text(fields.get("target"), "relationships.target")
        if not _RESOURCE_ID.fullmatch(start) or not _RESOURCE_ID.fullmatch(end) or start == end:
            raise ConfigError(
                "relationships requires two distinct fully qualified resource IDs (stack#LogicalId)."
            )
        kind = kinds.get(_text(fields.get("type"), "relationships.type"))
        if kind is None:
            raise ConfigError(
                "relationships.type must be TRAFFIC, EVENT, DATA_ACCESS or ASSOCIATION."
            )
        identity = (start, end, kind)
        if identity in seen:
            raise ConfigError(
                "Each relationship source, target and type combination must be unique."
            )
        seen.add(identity)
        evidence = _public_text(fields.get("evidence"), "relationships.evidence", 300)
        result.append(ManualRelationship(start, end, kind, evidence, fields["evidence"].source))
    return tuple(result)


def load_config(path: Path) -> Config:
    try:
        document = TemplateLoader().load(path)
    except TemplateError as exc:
        raise ConfigError(f"Could not load configuration ({exc.code}).") from exc
    directory = path.resolve().parent
    fields = _object(
        document.root, "Configuration", "version", "input", "views", "output", "relationships"
    )
    if (
        (version := fields.get("version")) is None
        or type(version.value) is not int
        or version.value != 1
    ):
        raise ConfigError("Configuration requires version: 1.")
    inputs = _object(fields.get("input"), "input", "base_directory", "roots", "nested_stack_paths")
    root_items = inputs.get("roots")
    if root_items is None or not root_items.sequence():
        raise ConfigError("input.roots must contain at least one template or Terraform root.")
    roots = tuple(_root(item) for item in root_items.sequence())
    if len({root.id for root in roots}) != len(roots):
        raise ConfigError("input.roots IDs must be distinct.")
    nested = {
        key: Path(_text(value, "input.nested_stack_paths"))
        for key, value in _object(
            inputs.get("nested_stack_paths"), "input.nested_stack_paths"
        ).items()
    }
    if any(not _STACK_ID.fullmatch(key) for key in nested):
        raise ConfigError("input.nested_stack_paths keys must be nested stack instance IDs.")
    views = _object(fields.get("views"), "views", "overview", "detail", "resources", "locale")
    locale = _text(views.get("locale"), "views.locale", "en")
    if locale not in REPORT_LOCALES:
        raise ConfigError("views.locale must be en or ja.")
    overview = _object(
        views.get("overview"),
        "views.overview",
        "max_nodes",
        "direction",
        "show_conditional",
        "selection",
    )
    detail = _object(
        views.get("detail"),
        "views.detail",
        "max_nodes",
        "show_network_boundaries",
        "show_conditional",
    )
    direction = _text(overview.get("direction"), "views.overview.direction", "LR")
    if direction not in {"LR", "TB"}:
        raise ConfigError("views.overview.direction must be LR or TB.")
    selection = _text(overview.get("selection"), "views.overview.selection", "auto")
    if selection not in {"auto", "primary"}:
        raise ConfigError("views.overview.selection must be auto or primary.")
    view_options = ViewOptions(
        _limit(overview.get("max_nodes"), "views.overview.max_nodes", 20),
        _limit(detail.get("max_nodes"), "views.detail.max_nodes", 40),
        direction,
        _flag(overview.get("show_conditional"), "views.overview.show_conditional", False),
        _flag(detail.get("show_network_boundaries"), "views.detail.show_network_boundaries", True),
        _resource_views(views.get("resources")),
        locale=locale,
        overview_selection=selection,
        detail_show_conditional=_flag(
            detail.get("show_conditional"), "views.detail.show_conditional", True
        ),
    )
    out = _object(
        fields.get("output"), "output", "directory", "formats", "localize_json_diagnostics"
    )
    return Config(
        roots,
        ResolveOptions(
            directory / _text(inputs.get("base_directory"), "input.base_directory", "."), nested
        ),
        view_options,
        directory / _text(out.get("directory"), "output.directory", "diagram-output"),
        _formats(out.get("formats")),
        _relationships(fields.get("relationships")),
        _flag(out.get("localize_json_diagnostics"), "output.localize_json_diagnostics", False),
    )


def direct_config(
    templates: list[Path],
    directory: Path,
    *,
    terraform: bool = False,
    input_base: Path | None = None,
) -> Config:
    paths = [(directory / path).resolve() for path in templates]
    locations = [path if terraform and path.is_dir() else path.parent for path in paths]
    base = directory.resolve()
    if input_base is not None:
        base = (directory / input_base).resolve()
    elif not all(path.is_relative_to(base) for path in locations):
        if len(set(locations)) != 1:
            raise ConfigError(
                "Use --input-base for direct inputs in different locations outside the working directory."
            )
        base = locations[0]
    if terraform:
        roots = tuple(
            TerraformInput(path.parent.name if path.is_file() else path.name, path)
            for path in paths
        )
    else:
        roots = tuple(RootInput(path.stem, path) for path in paths)
    if len({root.id for root in roots}) != len(roots) or any(
        not ROOT_ID.fullmatch(root.id) for root in roots
    ):
        raise ConfigError(
            "Template names need distinct supported stems. Use --config to assign root IDs."
        )
    return Config(
        roots,
        ResolveOptions(base),
        ViewOptions(),
        directory / "diagram-output",
        DEFAULT_FORMATS,
    )
