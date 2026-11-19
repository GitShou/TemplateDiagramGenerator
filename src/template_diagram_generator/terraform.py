"""Resolve a static subset of Terraform configuration without running Terraform."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import hcl2
from lark.exceptions import LarkError

from .expressions import EvaluationSite, literal, references, target_ids
from .loader import TemplateError
from .models import (
    AstNode,
    ConditionState,
    Diagnostic,
    Reference,
    ReferenceKind,
    ReferenceStatus,
    ResolvedValue,
    Resource,
    ResourceAttribute,
    ResourceGraph,
    SourceLocation,
    Stack,
    StackStatus,
    TerraformInput,
    ValueKind,
    conjunction,
    pointer,
)
from .resolver import ResolveError
from .semantics.context import stable_id
from .terraform_aws import canonical_attribute, canonical_type, project_properties

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_-]*"
_NAME = re.compile(_IDENTIFIER + r"\Z")
_ADDRESS = re.compile(rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*\Z")
_ROOT_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_INTERPOLATION = re.compile(r"(?<!\$)\$\{([^{}]+)\}")
_TEMPLATE = re.compile(r"(?<!\$)\$\{|(?<!%)%\{")
_UNKNOWN = ConditionState.UNKNOWN
_TRUE = ConditionState.TRUE
_FALSE = ConditionState.FALSE


@dataclass(slots=True)
class _Module:
    id: str
    directory: Path
    source: SourceLocation
    state: ConditionState
    parent: _Module | None = None
    call: AstNode | None = None
    inputs: dict = field(default_factory=dict, repr=False)
    resources: dict[str, AstNode] = field(default_factory=dict)
    variables: dict[str, AstNode] = field(default_factory=dict)
    locals: dict[str, AstNode] = field(default_factory=dict)
    outputs: dict[str, AstNode] = field(default_factory=dict)


def _fields(node: AstNode | None) -> dict[str, AstNode]:
    return (node.mapping() or {}) if node is not None else {}


def _raw(node: AstNode | None):
    return node.value if node is not None else None


def _ast(value, source: SourceLocation) -> AstNode:
    if len(source.path) > 80:
        raise TemplateError(
            "TERRAFORM_SIZE", "Terraform configuration exceeds the analysis depth limit.", source
        )
    if isinstance(value, dict):
        source = replace(source, line=value.get("__start_line__", source.line))
        value = {
            key: _ast(item, replace(source, path=(*source.path, key)))
            for key, item in value.items()
            if key not in {"__start_line__", "__end_line__"}
        }
    elif isinstance(value, list):
        value = tuple(
            _ast(item, replace(source, path=(*source.path, index)))
            for index, item in enumerate(value)
        )
    return AstNode(value, source)


def _existence(fields) -> ConditionState:
    if "for_each" in fields:
        each = fields["for_each"].value
        return _FALSE if isinstance(each, (dict, tuple)) and not each else _UNKNOWN
    if "count" in fields:
        count = fields["count"].value
        if type(count) is int and count in (0, 1):
            return _TRUE if count else _FALSE
        return _UNKNOWN
    return _TRUE


def _blocks(parsed, name, source):
    items = parsed.get(name, [])
    if not isinstance(items, list) or any(not isinstance(block, dict) for block in items):
        raise TemplateError(
            "TERRAFORM_DECLARATION", "Terraform block has an invalid structure.", source
        )
    return items


def resolve_terraform(roots: tuple[TerraformInput, ...], input_base: Path) -> ResourceGraph:
    return _TerraformResolver(input_base.resolve()).run(roots)


class _TerraformResolver:
    def __init__(self, base: Path):
        self.base = base
        self.modules: dict[str, _Module] = {}
        self.stacks: dict[str, Stack] = {}
        self.refs: dict[str, Reference] = {}
        self.issues: set[Diagnostic] = set()
        self.memo: dict[tuple[str, str, str], ResolvedValue] = {}
        self.active: set[tuple[str, str, str]] = set()
        self.resource_types: dict[str, str] = {}

    def run(self, roots):
        if (
            not roots
            or len({item.id for item in roots}) != len(roots)
            or any(not _ROOT_ID.fullmatch(item.id) for item in roots)
        ):
            raise ResolveError("Terraform roots require distinct alphanumeric IDs.")
        for root in sorted(roots, key=lambda item: item.id):
            path = self._path(self.base / root.path)
            if path is None or (
                not path.is_dir() and not (path.is_file() and path.suffix == ".tf")
            ):
                raise ResolveError(
                    "Terraform input must be a directory or .tf file within the input base."
                )
            directory = path if path.is_dir() else path.parent
            self._load(root.id, directory, _TRUE, (), inputs=root.variables)

        resources = {}
        for module in self.modules.values():
            for address in module.resources:
                self.resource_types[f"{module.id}#{address}"] = address.partition(".")[0]
        for module in self.modules.values():
            for address, node in sorted(module.resources.items()):
                id_ = f"{module.id}#{address}"
                type_ = self.resource_types[id_]
                state = conjunction(module.state, _existence(_fields(node)))
                site = EvaluationSite(module.id, id_, node.source.path, state)
                if _existence(_fields(node)) is _UNKNOWN:
                    self.issue(
                        "TERRAFORM_INSTANCES",
                        "Resource instances require a Terraform plan; one declaration is shown.",
                        site,
                        node.source,
                    )
                parsed = self._evaluate(node, site, depth=0)
                resources[id_] = Resource(
                    id_,
                    module.id,
                    address,
                    canonical_type(type_),
                    node.source,
                    state,
                    node,
                    project_properties(type_, parsed, self.resource_types),
                    type_,
                )
        for module in self.modules.values():
            outputs = {
                name: self._symbol(module, "output", name, node)
                for name, node in sorted(module.outputs.items())
            }
            unknown = module.inputs.keys() - module.variables.keys()
            if unknown:
                self.issue(
                    "UNKNOWN_PARAMETER_INPUT",
                    "Terraform override has no variable declaration.",
                    EvaluationSite(module.id, module.id, ("variable",)),
                    module.source,
                )
            status = {
                _TRUE: StackStatus.ANALYZED,
                _UNKNOWN: StackStatus.PARTIAL,
                _FALSE: StackStatus.INACTIVE,
            }[module.state]
            self.stacks[module.id] = Stack(
                module.id,
                module.parent.id if module.parent else None,
                None,
                module.source,
                status,
                module.state,
                outputs=outputs,
                input_format="terraform",
            )
        diagnostics = sorted(
            self.issues,
            key=lambda item: (
                item.entity_id or "",
                item.source.template,
                pointer(item.path),
                item.code,
            ),
        )
        return ResourceGraph(
            dict(sorted(self.stacks.items())),
            dict(sorted(resources.items())),
            dict(sorted(self.refs.items())),
            tuple(diagnostics),
        )

    def _path(self, value: Path) -> Path | None:
        try:
            path = value.resolve()
            return path if path.is_relative_to(self.base) else None
        except (OSError, RuntimeError, ValueError):
            return None

    def _load(self, id_, directory, state, ancestors, *, parent=None, call=None, inputs=None):
        source = SourceLocation(directory.relative_to(self.base).as_posix())
        paths = sorted(directory.glob("*.tf"))
        regular = [
            path
            for path in paths
            if path.name != "override.tf" and not path.name.endswith("_override.tf")
        ]
        if not regular:
            raise TemplateError(
                "TERRAFORM_INPUT",
                "No supported .tf files were found in this Terraform module.",
                source,
            )
        incomplete = len(paths) != len(regular) or any(directory.glob("*.tf.json"))
        if incomplete:
            self.issue(
                "TERRAFORM_OVERRIDE",
                "Terraform override and .tf.json files require a plan and are not evaluated.",
                EvaluationSite(id_, id_, (), state),
                source,
            )
            state = conjunction(state, _UNKNOWN)
        module = _Module(id_, directory, source, state, parent, call, inputs or {})
        calls = {}
        for path in regular:
            self._read(module, path, calls)
        self.modules[id_] = module
        for name, node in sorted(calls.items()):
            self._load_child(module, name, node, (*ancestors, directory))

    def _read(self, module: _Module, path: Path, calls) -> None:
        file_name = path.relative_to(self.base).as_posix()
        source = SourceLocation(file_name)
        if self._path(path) is None:
            raise TemplateError(
                "TERRAFORM_INPUT", "Terraform file resolves outside the input base.", source
            )
        try:
            with path.open("rb") as stream:
                data = stream.read(4 * 1024**2 + 1)
            if len(data) > 4 * 1024**2:
                raise TemplateError(
                    "TERRAFORM_SIZE", "Terraform file exceeds the input size limit.", source
                )
            parsed = hcl2.loads(data.decode("utf-8-sig").replace("\r\n", "\n"), with_meta=True)
        except (LarkError, UnicodeError, RecursionError) as exc:
            position = replace(
                source, line=getattr(exc, "line", None), column=getattr(exc, "column", None)
            )
            raise TemplateError(
                "TERRAFORM_SYNTAX", "Invalid Terraform HCL configuration.", position
            ) from exc
        except OSError as exc:
            raise TemplateError(
                "TERRAFORM_INPUT", "Could not read Terraform configuration.", source
            ) from exc

        def add(destination, key, raw, parts):
            if (
                not isinstance(raw, dict)
                or not all(_NAME.fullmatch(part) for part in parts[1:])
                or key in destination
            ):
                raise TemplateError(
                    "TERRAFORM_DECLARATION", "Invalid or duplicate Terraform declaration.", source
                )
            destination[key] = _ast(raw, replace(source, path=parts))

        for block in _blocks(parsed, "resource", source):
            for type_, instances in block.items():
                if not isinstance(instances, dict):
                    raise TemplateError(
                        "TERRAFORM_DECLARATION",
                        "Terraform resource has an invalid structure.",
                        source,
                    )
                for name, properties in instances.items():
                    add(module.resources, f"{type_}.{name}", properties, ("resource", type_, name))
        for key, destination in (
            ("variable", module.variables),
            ("output", module.outputs),
            ("module", calls),
        ):
            for block in _blocks(parsed, key, source):
                for name, properties in block.items():
                    add(destination, name, properties, (key, name))
        for block in _blocks(parsed, "locals", source):
            start = replace(source, line=block.get("__start_line__"))
            for name, value in block.items():
                if name not in {"__start_line__", "__end_line__"}:
                    if not _NAME.fullmatch(name) or name in module.locals:
                        raise TemplateError(
                            "TERRAFORM_DECLARATION", "Invalid or duplicate Terraform local.", start
                        )
                    module.locals[name] = _ast(value, replace(start, path=("locals", name)))

    def _load_child(self, parent, name, node, ancestors):
        call_fields = _fields(node)
        id_ = f"{parent.id}/{name}"
        state = conjunction(parent.state, _existence(call_fields))
        site = EvaluationSite(parent.id, id_, node.source.path, state)
        source = _raw(call_fields.get("source"))
        if state is _FALSE:
            self.stacks[id_] = Stack(
                id_,
                parent.id,
                None,
                node.source,
                StackStatus.INACTIVE,
                state,
                input_format="terraform",
            )
            return
        reason = None
        if (
            not isinstance(source, str)
            or not source.startswith(("./", "../"))
            or _TEMPLATE.search(source)
        ):
            reason = (
                "TERRAFORM_REMOTE_MODULE",
                "Remote Terraform modules are not fetched during static analysis.",
            )
        else:
            directory = self._path(parent.directory / source)
            if (
                directory is None
                or directory in ancestors
                or len(ancestors) >= 32
                or len(self.modules) >= 1000
            ):
                reason = (
                    "TERRAFORM_MODULE_PATH",
                    "Local module path is outside the input base, circular or above the analysis limit.",
                )
            else:
                try:
                    self._load(id_, directory, state, ancestors, parent=parent, call=node)
                except TemplateError as exc:
                    reason = (
                        "TERRAFORM_MODULE_UNAVAILABLE",
                        f"Local Terraform module could not be read ({exc.code}).",
                    )
        if reason is not None:
            self.issue(*reason, site, node.source)
            self.stacks[id_] = Stack(
                id_,
                parent.id,
                None,
                node.source,
                StackStatus.UNRESOLVED,
                _UNKNOWN,
                input_format="terraform",
            )

    def _symbol(self, module, category, name, node):
        cache_key = (module.id, category, name)
        if cache_key in self.memo:
            return self.memo[cache_key]
        site = EvaluationSite(
            module.id, f"{module.id}@{category}/{name}", node.source.path, module.state
        )
        if cache_key in self.active or len(self.active) >= 48:
            return self._unknown(
                "TERRAFORM_SYMBOL_CYCLE",
                "Terraform value resolution is circular or too deep.",
                site,
                node.source,
            )
        self.active.add(cache_key)
        try:
            value = self._symbol_value(module, category, name, node, site)
            value = replace(value, state=conjunction(value.state, module.state))
            self.memo[cache_key] = value
            return value
        finally:
            self.active.remove(cache_key)

    def _symbol_value(self, module, category, name, node, site):
        if category == "local":
            return self._evaluate(node, site)
        fields = _fields(node)
        if "sensitive" in fields and _raw(fields["sensitive"]) is not False:
            return ResolvedValue(
                ValueKind.REDACTED, state=module.state, reason="TERRAFORM_SENSITIVE"
            )
        key = "value" if category == "output" else "default"
        if category == "parameter":
            if name in module.inputs:
                item = module.inputs[name]
                return (
                    ResolvedValue(
                        ValueKind.ARRAY, tuple(literal(v, site.state) for v in item), site.state
                    )
                    if isinstance(item, list)
                    else literal(item, site.state)
                )
            if argument := _fields(module.call).get(name):
                parent = module.parent
                origin = EvaluationSite(
                    parent.id,
                    f"{parent.id}@module/{module.id.rsplit('/', 1)[-1]}",
                    argument.source.path,
                    parent.state,
                )
                return self._evaluate(argument, origin)
        if key in fields:
            return self._evaluate(fields[key], site.child(key))
        return self._unknown(
            "TERRAFORM_VARIABLE_MISSING",
            "Terraform input has no supplied value or default.",
            site,
            node.source,
        )

    def _evaluate(
        self, node: AstNode, site: EvaluationSite, depth=0, kind=ReferenceKind.TERRAFORM_REFERENCE
    ):
        path = site.path
        if depth > 80:
            return self._unknown(
                "TERRAFORM_SIZE", "Terraform expression exceeds the depth limit.", site, node.source
            )
        if (obj := node.mapping()) is not None:
            values = {
                key: self._evaluate(
                    value,
                    site.child(key),
                    depth + 1,
                    ReferenceKind.DEPENDS_ON
                    if depth == 0 and path[:1] == ("resource",) and key == "depends_on"
                    else kind,
                )
                for key, value in sorted(obj.items())
            }
            result = ResolvedValue(
                ValueKind.OBJECT, values, site.state, references(values.values())
            )
        elif (items := node.sequence()) is not None:
            values = tuple(
                self._evaluate(item, site.child(index), depth + 1, kind)
                for index, item in enumerate(items)
            )
            result = ResolvedValue(ValueKind.ARRAY, values, site.state, references(values))
        elif isinstance(node.value, str):
            result = self._text(node.value, site, node.source, kind)
        else:
            result = literal(node.value, site.state)
        return replace(result, source=node.source, path=path)

    def _text(self, text, site, source, kind):
        literals = _INTERPOLATION.split(text)[::2]
        if any(_TEMPLATE.search(part) for part in literals):
            return self._unknown(
                "TERRAFORM_EXPRESSION_UNRESOLVED",
                "Terraform expression requires runtime evaluation.",
                site,
                source,
            )
        parts = []
        position = 0
        for index, match in enumerate(_INTERPOLATION.finditer(text)):
            if match.start() > position:
                parts.append(
                    literal(text[position : match.start()].replace("$${", "${"), site.state)
                )
            parts.append(self._reference(match[1].strip(), site, source, kind, index))
            position = match.end()
        if position < len(text):
            parts.append(literal(text[position:].replace("$${", "${"), site.state))
        if not parts:
            return literal("", site.state)
        if len(parts) == 1:
            return parts[0]
        state = conjunction(site.state, *(part.state for part in parts))
        if any(part.kind is ValueKind.REDACTED for part in parts):
            return ResolvedValue(ValueKind.REDACTED, state=state, reference_ids=references(parts))
        if all(
            part.kind is ValueKind.LITERAL and type(part.value) in {str, int, float, bool}
            for part in parts
        ):
            joined = "".join(
                str(part.value).lower() if type(part.value) is bool else str(part.value)
                for part in parts
            )
            return replace(literal(joined, state), reference_ids=references(parts))
        return ResolvedValue(ValueKind.COMPOSITE, tuple(parts), state, references(parts))

    def _reference(self, address, site, source, kind, index):
        module = self.modules[site.stack_id]
        if not _ADDRESS.fullmatch(address):
            return self._unknown(
                "TERRAFORM_EXPRESSION_UNRESOLVED",
                "Terraform expression requires runtime evaluation.",
                site,
                source,
            )
        segments = address.split(".")
        direct, attribute, value = self._lookup(module, segments, site, source)
        state = conjunction(site.state, value.state)
        if state is _FALSE:
            status = ReferenceStatus.INACTIVE
        elif value.kind is ValueKind.REDACTED:
            status = ReferenceStatus.REDACTED
        elif value.kind is ValueKind.UNKNOWN:
            status = ReferenceStatus.UNRESOLVED
        else:
            status = ReferenceStatus.CONDITIONAL if state is _UNKNOWN else ReferenceStatus.RESOLVED
        id_ = stable_id("tf-ref", site.entity_id, site.path, kind, index)
        trace = tuple(key for key in value.reference_ids if key != id_)
        self.refs[id_] = Reference(
            id_,
            site.entity_id,
            site.stack_id,
            site.path,
            source,
            kind,
            direct,
            None,
            target_ids(value),
            attribute,
            status,
            trace,
        )
        return replace(value, state=state, reference_ids=(id_, *trace))

    def _lookup(self, module, segments, site, source):
        first = segments[0]
        if len(segments) == 2 and first in {"var", "local"}:
            category = "parameter" if first == "var" else "local"
            symbols = module.variables if first == "var" else module.locals
            name = segments[1]
            if name in symbols:
                return (
                    f"{module.id}@{category}/{name}",
                    None,
                    self._symbol(module, category, name, symbols[name]),
                )
        if len(segments) == 3 and first == "module":
            child = self.modules.get(f"{module.id}/{segments[1]}")
            name = segments[2]
            if child is not None and name in child.outputs:
                return (
                    f"{child.id}@output/{name}",
                    None,
                    self._symbol(child, "output", name, child.outputs[name]),
                )
        if len(segments) in {2, 3}:
            address = ".".join(segments[:2])
            if address in module.resources:
                id_ = f"{module.id}#{address}"
                attribute = canonical_attribute(first, segments[2] if len(segments) == 3 else None)
                state = conjunction(module.state, _existence(_fields(module.resources[address])))
                return (
                    id_,
                    attribute,
                    ResolvedValue(
                        ValueKind.RESOURCE_ATTRIBUTE, ResourceAttribute(id_, attribute), state
                    ),
                )
        value = self._unknown(
            "TERRAFORM_REFERENCE_UNRESOLVED",
            "Terraform reference is outside the loaded static configuration.",
            site,
            source,
        )
        return None, None, value

    def _unknown(self, code, message, site, source):
        self.issue(code, message, site, source)
        return ResolvedValue(ValueKind.UNKNOWN, state=site.state, reason=code)

    def issue(self, code, message, site, source):
        if site.state is not _FALSE:
            self.issues.add(Diagnostic(code, message, source, site.path, site.entity_id))
