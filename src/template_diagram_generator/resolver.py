"""Resolve stack-local symbols and explicitly mapped nested templates offline."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath

from .expressions import EvaluationSite, ExpressionEvaluator, condition_of, literal, target_ids
from .foreach import LoopExpander
from .loader import TemplateError, TemplateLoader
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
    RootInput,
    SourceLocation,
    Stack,
    StackStatus,
    ValueKind,
    conjunction,
    pointer,
)

_ROOT_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_LOGICAL_ID = re.compile(r"[A-Za-z0-9]+\Z")
_PSEUDO_PARAMETERS = frozenset(
    {
        "AWS::AccountId",
        "AWS::NotificationARNs",
        "AWS::Partition",
        "AWS::Region",
        "AWS::StackId",
        "AWS::StackName",
        "AWS::URLSuffix",
    }
)
_STACK_TYPE = "AWS::CloudFormation::Stack"
_LANGUAGE_EXTENSIONS = "AWS::LanguageExtensions"


@dataclass(frozen=True, slots=True)
class ResolveOptions:
    input_base: Path
    nested_stack_paths: dict[str, Path] = field(default_factory=dict)
    max_stack_depth: int = 24
    max_stacks: int = 1_000
    max_expanded_nodes: int = 100_000
    max_symbol_depth: int = 40


class ResolveError(ValueError):
    """Invalid public resolver arguments."""


@dataclass(slots=True)
class _Scope:
    id: str
    path: Path
    source: SourceLocation
    parent_id: str | None
    stack_resource_id: str | None
    ancestors: tuple[Path, ...]
    state: ConditionState
    language_extensions: bool
    partial: bool
    parameter_definitions: dict[str, AstNode]
    mappings: dict[str, AstNode]
    conditions: dict[str, AstNode]
    resources: dict[str, AstNode]
    outputs: dict[str, AstNode]
    root_input: RootInput | None = None
    parent_parameters: AstNode | None = None
    omitted_declarations: tuple[AstNode, ...] = ()


def resolve(
    roots: list[RootInput] | tuple[RootInput, ...],
    options: ResolveOptions,
    loader: TemplateLoader | None = None,
) -> ResourceGraph:
    """Resolve fresh per run. Unavailable child stacks are represented as stack records."""
    return _Resolver(options, loader if loader is not None else TemplateLoader()).run(roots)


def _mapping(node: AstNode | None) -> dict[str, AstNode]:
    return (node.mapping() or {}) if node is not None else {}


def _type_of(resource: AstNode) -> str | None:
    item = _mapping(resource).get("Type")
    return item.value if item is not None and isinstance(item.value, str) and item.value else None


def _symbol_id(stack_id: str, kind: str, name: str) -> str:
    return f"{stack_id}@{kind}/{name}"


class _Resolver:
    def __init__(self, options: ResolveOptions, loader: TemplateLoader):
        self.options = options
        self.base = options.input_base.resolve()
        self.loader = loader
        self.evaluator = ExpressionEvaluator(self)
        self.scopes: dict[str, _Scope] = {}
        self.stacks: dict[str, Stack] = {}
        self.resources: dict[str, Resource] = {}
        self.references: dict[str, Reference] = {}
        self.diagnostics: set[Diagnostic] = set()
        self.documents: dict[Path, AstNode] = {}
        self.symbols: dict[tuple[str, str, str], ResolvedValue] = {}
        self.active_symbols: set[tuple[str, str, str]] = set()
        self.opening: set[str] = set()
        self.used_mappings: set[str] = set()
        self.stack_slots: set[str] = set()
        self.exports: dict[tuple[str, str, str], list[tuple[_Scope, str, AstNode]]] | None = None
        self.unknown_export_scopes: set[tuple[str, str]] = set()

    def run(self, roots) -> ResourceGraph:
        if (
            not roots
            or len({root.id for root in roots}) != len(roots)
            or any(not _ROOT_ID.fullmatch(root.id) for root in roots)
        ):
            raise ResolveError(
                "Roots require distinct nonempty IDs using letters, digits, hyphens or underscores."
            )
        if (
            any(
                limit < 1
                for limit in (
                    self.options.max_stack_depth,
                    self.options.max_stacks,
                    self.options.max_expanded_nodes,
                    self.options.max_symbol_depth,
                )
            )
            or len(roots) > self.options.max_stacks
        ):
            raise ResolveError("Resolver limits must be positive and accommodate all roots.")
        # Reserve every root before traversing any children.
        self.stack_slots.update(root.id for root in roots)
        for root in sorted(roots, key=lambda entry: entry.id):
            path = self._within_base(self.base, root.template, allow_absolute=True)
            if path is None:
                raise ResolveError(
                    "A root template is outside the input base or has an invalid path."
                )
            self._load_scope(root.id, path, ConditionState.TRUE, root_input=root)
        self._index_exports()
        for root in sorted(roots, key=lambda entry: entry.id):
            self._visit(self.scopes[root.id])
        for stack_id in sorted(self.options.nested_stack_paths.keys() - self.used_mappings):
            root_id = stack_id.partition("/")[0]
            root = self.scopes.get(root_id)
            self.issue(
                "UNUSED_NESTED_MAPPING",
                "Nested stack mapping did not match an analyzed instance.",
                EvaluationSite(root_id, root_id, ()),
                root.source if root else SourceLocation("<options>"),
            )
        return ResourceGraph(
            dict(sorted(self.stacks.items())),
            dict(sorted(self.resources.items())),
            dict(sorted(self.references.items())),
            tuple(
                sorted(
                    self.diagnostics,
                    key=lambda item: (
                        item.entity_id or "",
                        item.source.template,
                        pointer(item.path),
                        item.code,
                        pointer(item.source.path),
                        item.source.line or 0,
                        item.source.column or 0,
                    ),
                )
            ),
        )

    def _within_base(
        self, directory: Path, location: Path | str, *, allow_absolute: bool = False
    ) -> Path | None:
        try:
            path = Path(location)
            windows = PureWindowsPath(location)
            if not allow_absolute and (path.is_absolute() or windows.anchor):
                return None
            candidate = (directory / path).resolve()
            return candidate if candidate.is_relative_to(self.base) else None
        except (OSError, RuntimeError, ValueError):
            return None

    def _load_scope(
        self,
        stack_id: str,
        path: Path,
        state: ConditionState,
        *,
        root_input: RootInput | None = None,
        parent: _Scope | None = None,
        resource_id: str | None = None,
        parent_parameters: AstNode | None = None,
    ) -> _Scope:
        if path not in self.documents:
            self.documents[path] = self.loader.load(
                path, source_name=path.relative_to(self.base).as_posix()
            ).root
        document = self.documents[path]
        root = _mapping(document)
        transform = root.get("Transform")
        names = (
            ()
            if transform is None
            else (
                tuple(item.value for item in transform.sequence())
                if transform.sequence() is not None
                else (transform.value,)
            )
        )
        opaque = any(name != _LANGUAGE_EXTENSIONS for name in names)
        if opaque:
            self.issue(
                "UNSUPPORTED_TRANSFORM",
                "Transform needs a preprocessed template for confirmed analysis.",
                EvaluationSite(stack_id, stack_id, ("Transform",), state),
                transform.source,
            )
            state = conjunction(state, ConditionState.UNKNOWN)
        sections = {}
        for name in ("Parameters", "Mappings", "Conditions", "Resources", "Outputs"):
            section = root.get(name)
            if section is not None and section.mapping() is None:
                raise TemplateError(
                    "TEMPLATE_STRUCTURE", f"{name} must be a mapping.", section.source
                )
            sections[name] = _mapping(section)
        scope = _Scope(
            stack_id,
            path,
            document.source,
            parent.id if parent else None,
            resource_id,
            (*parent.ancestors, path) if parent else (path,),
            state,
            _LANGUAGE_EXTENSIONS in names,
            opaque,
            sections["Parameters"],
            sections["Mappings"],
            sections["Conditions"],
            sections["Resources"],
            sections["Outputs"],
            root_input,
            parent_parameters,
        )
        self.scopes[stack_id] = scope
        expander = LoopExpander(self.evaluator, max_expanded_nodes=self.options.max_expanded_nodes)
        for name, destination in (
            ("Conditions", "conditions"),
            ("Resources", "resources"),
            ("Outputs", "outputs"),
        ):
            if name in root:
                expr = expander.expand(
                    root[name], EvaluationSite(stack_id, stack_id, (name,), state)
                )
                setattr(scope, destination, _mapping(expr))
        scope.partial |= expander.partial
        scope.omitted_declarations = tuple(expander.unexpanded)
        return scope

    def _visit(self, scope: _Scope) -> None:
        # Child scopes may already exist because a sibling's parameter consumed their output.
        parameters = {
            name: self._parameter(scope, name, node)
            for name, node in sorted(scope.parameter_definitions.items())
        }
        self._check_parameter_inputs(scope)
        for name, node in sorted(scope.conditions.items()):
            self._condition(scope, name, node)
        for name, node in sorted(scope.resources.items()):
            self._visit_resource(scope, name, node)
        outputs = {
            name: self._output(scope, name, node) for name, node in sorted(scope.outputs.items())
        }
        self.stacks[scope.id] = Stack(
            scope.id,
            scope.parent_id,
            scope.stack_resource_id,
            scope.source,
            StackStatus.PARTIAL if scope.partial else StackStatus.ANALYZED,
            scope.state,
            parameters,
            outputs,
            scope.omitted_declarations,
        )
        for name, node in sorted(scope.resources.items()):
            if _type_of(node) == _STACK_TYPE and self._valid_logical_id(name):
                child = self._child(scope, name, node)
                if child is not None:
                    self._visit(child)

    def _visit_resource(self, scope: _Scope, name: str, node: AstNode) -> None:
        id_ = f"{scope.id}#{name}"
        state = self._declaration_state(scope, node, ("Resources", name), id_)
        site = EvaluationSite(scope.id, id_, ("Resources", name), state)
        if not self._valid_logical_id(name) or (type_ := _type_of(node)) is None:
            self.issue(
                "INVALID_RESOURCE",
                "Resource declaration requires a logical ID and a type.",
                site,
                node.source,
            )
            scope.partial = True
            return
        members = _mapping(node)
        properties = members.get("Properties", AstNode({}, node.source))
        evaluated = self.evaluator.evaluate(properties, site.child("Properties"))
        if properties.mapping() is None:
            self.issue(
                "INVALID_PROPERTIES",
                "Resource Properties must be a mapping.",
                site.child("Properties"),
                properties.source,
            )
            scope.partial = True
        if (depends := members.get("DependsOn")) is not None:
            items = depends.sequence()
            for index, item in enumerate(items if items is not None else (depends,)):
                dependency = (
                    site.child("DependsOn", index) if items is not None else site.child("DependsOn")
                )
                if isinstance(item.value, str):
                    self.reference(dependency, item.source, item.value, ReferenceKind.DEPENDS_ON)
                else:
                    self.issue(
                        "INVALID_DEPENDENCY",
                        "DependsOn requires logical IDs.",
                        dependency,
                        item.source,
                    )
        self.resources[id_] = Resource(
            id_, scope.id, name, type_, node.source, state, properties, evaluated
        )

    @staticmethod
    def _valid_logical_id(name: str) -> bool:
        return bool(_LOGICAL_ID.fullmatch(name))

    def _child(self, parent: _Scope, name: str, declaration: AstNode) -> _Scope | None:
        child_id = f"{parent.id}/{name}"
        if child_id in self.opening:
            self.issue(
                "NESTED_EVALUATION_CYCLE",
                "Nested stack inputs or outputs depend on their own resolution.",
                EvaluationSite(parent.id, f"{parent.id}#{name}", ("Resources", name)),
                declaration.source,
            )
            return None
        if child_id in self.scopes or child_id in self.stacks:
            return self.scopes.get(child_id)

        resource_id = f"{parent.id}#{name}"
        state = self._declaration_state(parent, declaration, ("Resources", name), resource_id)
        site = EvaluationSite(
            parent.id, resource_id, ("Resources", name, "Properties", "TemplateURL"), state
        )
        if child_id in self.options.nested_stack_paths:
            self.used_mappings.add(child_id)
        if len(self.stack_slots) >= self.options.max_stacks:
            self.issue(
                "NESTED_LIMIT", "Nested stack record limit reached.", site, declaration.source
            )
            return None
        self.stack_slots.add(child_id)
        if state is ConditionState.FALSE:
            self.stacks[child_id] = Stack(
                child_id, parent.id, resource_id, declaration.source, StackStatus.INACTIVE, state
            )
            return None
        unresolved = Stack(
            child_id, parent.id, resource_id, declaration.source, StackStatus.UNRESOLVED, state
        )
        if len(parent.ancestors) >= self.options.max_stack_depth:
            self.issue(
                "NESTED_LIMIT", "Nested stack traversal limit reached.", site, declaration.source
            )
            self.stacks[child_id] = unresolved
            return None

        self.opening.add(child_id)
        try:
            properties = _mapping(_mapping(declaration).get("Properties"))
            path = self._child_path(
                child_id, parent, properties.get("TemplateURL"), site, declaration.source
            )
            if path is None:
                self.stacks[child_id] = unresolved
                return None
            if path in parent.ancestors:
                self.issue(
                    "NESTED_CYCLE",
                    "Nested template resolves to an ancestor template.",
                    site,
                    declaration.source,
                )
                self.stacks[child_id] = unresolved
                return None
            try:
                return self._load_scope(
                    child_id,
                    path,
                    state,
                    parent=parent,
                    resource_id=resource_id,
                    parent_parameters=properties.get("Parameters"),
                )
            except TemplateError as exc:
                self.issue("NESTED_" + exc.code, str(exc), site, exc.source)
                self.stacks[child_id] = unresolved
                return None
        finally:
            self.opening.remove(child_id)

    def _child_path(
        self,
        child_id: str,
        parent: _Scope,
        url: AstNode | None,
        site: EvaluationSite,
        source: SourceLocation,
    ) -> Path | None:
        if child_id in self.options.nested_stack_paths:
            candidate = self._within_base(self.base, self.options.nested_stack_paths[child_id])
            if candidate is None:
                self.issue(
                    "NESTED_PATH",
                    "Nested template mapping must stay within the input base.",
                    site,
                    source,
                )
            return candidate
        value = self.evaluator.evaluate(url, site) if url is not None else None
        if (
            value is None
            or value.kind is not ValueKind.LITERAL
            or not isinstance(value.value, str)
            or not value.value
            or re.search(r"[:?#]", value.value)
        ):
            self.issue(
                "NESTED_UNMAPPED",
                "A local template mapping is needed for this nested stack.",
                site,
                source,
            )
            return None
        candidate = self._within_base(parent.path.parent, value.value)
        if candidate is None:
            self.issue(
                "NESTED_PATH",
                "Nested template must use a relative path within the input base.",
                site,
                source,
            )
        return candidate

    def _memoized(
        self,
        key: tuple[str, str, str],
        site: EvaluationSite,
        node: AstNode,
        operation: Callable[[], ResolvedValue],
    ) -> ResolvedValue:
        if key in self.symbols:
            return self.symbols[key]
        if key in self.active_symbols or len(self.active_symbols) >= self.options.max_symbol_depth:
            code = "SYMBOL_CYCLE" if key in self.active_symbols else "SYMBOL_LIMIT"
            self.issue(
                code,
                "Symbol evaluation encountered a cycle or exceeded its depth limit.",
                site,
                node.source,
            )
            return ResolvedValue(ValueKind.UNKNOWN, state=site.state, reason=code)
        self.active_symbols.add(key)
        try:
            result = operation()
            self.symbols[key] = result
            return result
        finally:
            self.active_symbols.remove(key)

    def _check_parameter_inputs(self, scope: _Scope) -> None:
        if scope.root_input is not None:
            supplied = scope.root_input.parameters.keys()
        elif scope.parent_parameters is None:
            return
        elif self._ordinary_mapping(scope.parent_parameters) is not None:
            supplied = scope.parent_parameters.mapping().keys()
        else:
            mapping = self._resolve_parent_parameters(scope)
            if mapping.kind is ValueKind.OMITTED:
                return
            if mapping.kind is not ValueKind.OBJECT:
                self.issue(
                    "UNRESOLVED_STACK_PARAMETERS",
                    "Nested stack parameter mapping could not be read.",
                    EvaluationSite(scope.id, scope.id, ("Parameters",), scope.state),
                    scope.parent_parameters.source,
                )
                return
            supplied = mapping.value.keys()
        if supplied - scope.parameter_definitions.keys():
            self.issue(
                "UNKNOWN_PARAMETER_INPUT",
                "A supplied parameter has no template declaration.",
                EvaluationSite(scope.id, scope.id, ("Parameters",), scope.state),
                scope.source,
            )

    def _parameter(self, scope: _Scope, name: str, declaration: AstNode) -> ResolvedValue:
        site = EvaluationSite(
            scope.id, _symbol_id(scope.id, "parameter", name), ("Parameters", name), scope.state
        )
        return self._memoized(
            (scope.id, "parameter", name),
            site,
            declaration,
            lambda: self._parameter_value(scope, name, declaration, site),
        )

    def _parameter_value(
        self, scope: _Scope, name: str, declaration: AstNode, site: EvaluationSite
    ) -> ResolvedValue:
        fields = _mapping(declaration)
        if (no_echo := fields.get("NoEcho")) is not None and (
            no_echo.value is True
            or (isinstance(no_echo.value, str) and no_echo.value.lower() == "true")
        ):
            return ResolvedValue(ValueKind.REDACTED, state=site.state, reason="NO_ECHO")
        value = self._passed_parameter(scope, name)
        if value is not None and value.kind is ValueKind.OMITTED:
            value = None
        if value is None and (default := fields.get("Default")) is not None:
            value = self.evaluator.evaluate(default, site.child("Default"))
        if value is None:
            self.issue(
                "PARAMETER_MISSING",
                "Parameter has no supplied value or default.",
                site,
                declaration.source,
            )
            return ResolvedValue(ValueKind.UNKNOWN, state=site.state, reason="PARAMETER_MISSING")
        return self._coerce_parameter(value, fields.get("Type"), site, declaration.source)

    def _passed_parameter(self, scope: _Scope, name: str) -> ResolvedValue | None:
        if scope.root_input is not None and name in scope.root_input.parameters:
            candidate = scope.root_input.parameters[name]
            return (
                ResolvedValue(
                    ValueKind.ARRAY,
                    tuple(literal(item, scope.state) for item in candidate),
                    scope.state,
                )
                if isinstance(candidate, list)
                else literal(candidate, scope.state)
            )
        if scope.parent_parameters is None:
            return None
        fields = self._ordinary_mapping(scope.parent_parameters)
        if fields is not None:
            return (
                self.evaluator.evaluate(fields[name], self._parameter_site(scope).child(name))
                if name in fields
                else None
            )
        whole = self._resolve_parent_parameters(scope)
        if whole.kind is ValueKind.OBJECT:
            return whole.property(name)
        if whole.kind in {ValueKind.OMITTED, ValueKind.REDACTED}:
            return whole
        return replace(
            whole, kind=ValueKind.UNKNOWN, value=None, reason="UNRESOLVED_STACK_PARAMETERS"
        )

    @staticmethod
    def _ordinary_mapping(node: AstNode) -> dict[str, AstNode] | None:
        fields = node.mapping()
        if fields is not None and len(fields) == 1:
            key = next(iter(fields))
            if key.startswith("Fn::") or key in {"Ref", "Condition"}:
                return None
        return fields

    def _parameter_site(self, scope: _Scope) -> EvaluationSite:
        parent = self.scopes[scope.parent_id]
        logical_id = scope.stack_resource_id.partition("#")[2]
        path = ("Resources", logical_id)
        state = self._declaration_state(
            parent, parent.resources[logical_id], path, scope.stack_resource_id
        )
        return EvaluationSite(
            parent.id, scope.stack_resource_id, (*path, "Properties", "Parameters"), state
        )

    def _resolve_parent_parameters(self, scope: _Scope) -> ResolvedValue:
        site = self._parameter_site(scope)
        return self._memoized(
            (scope.id, "passed_parameters", ""),
            site,
            scope.parent_parameters,
            lambda: self.evaluator.evaluate(scope.parent_parameters, site),
        )

    def _coerce_parameter(
        self,
        value: ResolvedValue,
        type_node: AstNode | None,
        site: EvaluationSite,
        source: SourceLocation,
    ) -> ResolvedValue:
        value = replace(value, state=conjunction(value.state, site.state))
        type_ = type_node.value if type_node is not None else None
        if isinstance(type_, str) and type_.startswith("AWS::SSM::Parameter::Value<"):
            self.issue(
                "SSM_PARAMETER_VALUE",
                "SSM parameter values require deployment context.",
                site,
                source,
            )
            return ResolvedValue(
                ValueKind.REDACTED,
                state=site.state,
                reference_ids=value.reference_ids,
                reason="SSM_PARAMETER_VALUE",
            )
        if value.kind in {
            ValueKind.REDACTED,
            ValueKind.UNKNOWN,
            ValueKind.RESOURCE_ATTRIBUTE,
            ValueKind.COMPOSITE,
        }:
            return value
        if (
            type_ in ("CommaDelimitedList", "List<Number>")
            or isinstance(type_, str)
            and type_.startswith("List<")
        ):
            if value.kind is ValueKind.LITERAL and isinstance(value.value, str):
                entries = (
                    ()
                    if not value.value
                    else tuple(
                        literal(part.strip(), value.state) for part in value.value.split(",")
                    )
                )
            elif value.kind is ValueKind.ARRAY:
                entries = value.value
            else:
                entries = None
            if entries is not None:
                items = tuple(
                    self._scalar_parameter(item, number=type_ == "List<Number>") for item in entries
                )
                if all(item is not None for item in items):
                    return ResolvedValue(ValueKind.ARRAY, items, value.state, value.reference_ids)
        elif (converted := self._scalar_parameter(value, number=type_ == "Number")) is not None:
            return converted
        self.issue(
            "PARAMETER_TYPE",
            "Parameter value cannot be interpreted as its declared type.",
            site,
            source,
        )
        return ResolvedValue(
            ValueKind.UNKNOWN,
            state=site.state,
            reference_ids=value.reference_ids,
            reason="PARAMETER_TYPE",
        )

    @staticmethod
    def _scalar_parameter(value: ResolvedValue, *, number: bool) -> ResolvedValue | None:
        if value.kind in {
            ValueKind.REDACTED,
            ValueKind.UNKNOWN,
            ValueKind.RESOURCE_ATTRIBUTE,
            ValueKind.COMPOSITE,
        }:
            return value
        if value.kind is not ValueKind.LITERAL or not isinstance(
            value.value, (str, int, float, bool)
        ):
            return None
        text = str(value.value).lower() if isinstance(value.value, bool) else str(value.value)
        if number:
            try:
                if not Decimal(text).is_finite():
                    return None
            except InvalidOperation:
                return None
        return replace(value, value=text)

    def _condition(self, scope: _Scope, name: str, declaration: AstNode) -> ResolvedValue:
        site = EvaluationSite(
            scope.id, _symbol_id(scope.id, "condition", name), ("Conditions", name), scope.state
        )

        def evaluate() -> ResolvedValue:
            value = self.evaluator.evaluate(declaration, site)
            if (
                condition_of(value) is ConditionState.UNKNOWN
                and value.kind is not ValueKind.REDACTED
            ):
                self.issue(
                    "UNRESOLVED_CONDITION",
                    "Condition could not be evaluated to a boolean.",
                    site,
                    declaration.source,
                )
            return value

        return self._memoized((scope.id, "condition", name), site, declaration, evaluate)

    def _output(self, scope: _Scope, name: str, declaration: AstNode) -> ResolvedValue:
        id_ = _symbol_id(scope.id, "output", name)
        path = ("Outputs", name)
        site = EvaluationSite(
            scope.id, id_, path, self._declaration_state(scope, declaration, path, id_)
        )

        def evaluate() -> ResolvedValue:
            value = _mapping(declaration).get("Value")
            if value is None:
                self.issue("INVALID_OUTPUT", "Output requires a Value.", site, declaration.source)
                return ResolvedValue(ValueKind.UNKNOWN, state=site.state, reason="INVALID_OUTPUT")
            return self.evaluator.evaluate(value, site.child("Value"))

        return self._memoized((scope.id, "output", name), site, declaration, evaluate)

    def _index_exports(self) -> None:
        exports = {}
        for scope in tuple(self.scopes.values()):
            root = scope.root_input
            if root is None or root.account_id is None or root.region is None:
                continue
            for name, declaration in sorted(scope.outputs.items()):
                export = _mapping(_mapping(declaration).get("Export")).get("Name")
                if export is None:
                    continue
                path = ("Outputs", name)
                id_ = _symbol_id(scope.id, "output", name)
                state = self._declaration_state(scope, declaration, path, id_)
                if state is ConditionState.FALSE:
                    continue
                site = EvaluationSite(scope.id, id_, (*path, "Export", "Name"), state)
                value = self.evaluator.evaluate(export, site)
                if (
                    value.kind is not ValueKind.LITERAL
                    or not isinstance(value.value, str)
                    or not value.value
                ):
                    self.issue(
                        "EXPORT_NAME_UNRESOLVED",
                        "Export name is not a static string.",
                        site,
                        export.source,
                    )
                    self.unknown_export_scopes.add((root.account_id, root.region))
                    continue
                exports.setdefault((root.account_id, root.region, value.value), []).append(
                    (scope, name, declaration)
                )
        self.exports = exports

    def import_value(
        self, site: EvaluationSite, source: SourceLocation, name: ResolvedValue
    ) -> ResolvedValue:
        direct = None
        output_name = None
        root = self.scopes[site.stack_id.partition("/")[0]].root_input
        if name.kind is ValueKind.REDACTED or site.state is ConditionState.FALSE:
            value = name if name.kind is ValueKind.REDACTED else ResolvedValue(ValueKind.UNKNOWN)
        else:
            issue = self._import_issue(root, name)
            if issue is None:
                matches = self.exports.get((root.account_id, root.region, name.value), ())
                if not matches:
                    issue = (
                        "IMPORT_NOT_FOUND",
                        "No supplied root exports this name in the configured account and region.",
                    )
                elif (
                    len(matches) != 1
                    or (root.account_id, root.region) in self.unknown_export_scopes
                ):
                    issue = (
                        "IMPORT_AMBIGUOUS",
                        "The export could not be uniquely verified among the supplied outputs.",
                    )
                elif matches[0][0].id == site.stack_id:
                    issue = ("IMPORT_SAME_STACK", "An import must refer to another stack.")
            if issue is None:
                scope, output_name, declaration = matches[0]
                direct = _symbol_id(scope.id, "output", output_name)
                value = self._output(scope, output_name, declaration)
            else:
                self.issue(*issue, site, source)
                value = ResolvedValue(ValueKind.UNKNOWN, reason=issue[0])
        value = replace(
            value,
            state=conjunction(value.state, name.state),
            reference_ids=tuple(dict.fromkeys((*name.reference_ids, *value.reference_ids))),
        )
        return self._record_reference(
            site, source, ReferenceKind.IMPORT_VALUE, direct, output_name, value
        )

    def _import_issue(self, root: RootInput, name: ResolvedValue) -> tuple[str, str] | None:
        if name.kind is not ValueKind.LITERAL or not isinstance(name.value, str) or not name.value:
            return "IMPORT_NAME_UNRESOLVED", "Import name is not a static string."
        if root.account_id is None or root.region is None:
            return (
                "IMPORT_SCOPE_UNKNOWN",
                "Set account_id and region on the importing and exporting roots.",
            )
        if self.exports is None:
            return (
                "IMPORT_INDEX_UNAVAILABLE",
                "Imports cannot determine export names during indexing.",
            )
        return None

    def _declaration_state(
        self, scope: _Scope, node: AstNode, path, entity_id: str
    ) -> ConditionState:
        if (flag := _mapping(node).get("Condition")) is None:
            return scope.state
        site = EvaluationSite(scope.id, entity_id, (*path, "Condition"), scope.state)
        if not isinstance(flag.value, str):
            self.issue(
                "INVALID_CONDITION_NAME",
                "Condition must name a declared condition.",
                site,
                flag.source,
            )
            return conjunction(scope.state, ConditionState.UNKNOWN)
        value = self.reference(site, flag.source, flag.value, ReferenceKind.CONDITION)
        return conjunction(scope.state, condition_of(value))

    def reference(
        self,
        site: EvaluationSite,
        source: SourceLocation,
        name: str,
        kind: ReferenceKind,
        *,
        attribute: str | None = None,
        occurrence: int = 0,
    ) -> ResolvedValue:
        scope = self.scopes[site.stack_id]
        direct, value = self._target(scope, name, kind, attribute, site, source)
        display = name if direct is not None or self._valid_logical_id(name) else None
        return self._record_reference(
            site, source, kind, direct, display, value, attribute, occurrence
        )

    def _record_reference(
        self,
        site: EvaluationSite,
        source: SourceLocation,
        kind: ReferenceKind,
        direct: str | None,
        display: str | None,
        value: ResolvedValue,
        attribute: str | None = None,
        occurrence: int = 0,
    ) -> ResolvedValue:
        effective = conjunction(site.state, value.state)
        if effective is ConditionState.FALSE:
            status = ReferenceStatus.INACTIVE
        elif value.kind is ValueKind.REDACTED:
            status = ReferenceStatus.REDACTED
        elif effective is ConditionState.UNKNOWN:
            status = ReferenceStatus.CONDITIONAL
        elif value.kind is ValueKind.UNKNOWN:
            status = ReferenceStatus.UNRESOLVED
        else:
            status = ReferenceStatus.RESOLVED
        # Reference IDs use the expanded path and occurrence, so equal template files in
        # sibling stacks and repeated substitutions remain distinguishable.
        identity = json.dumps([site.entity_id, site.path, kind, occurrence], ensure_ascii=True)
        ref_id = "ref-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        trace = tuple(id_ for id_ in value.reference_ids if id_ != ref_id)
        self.references[ref_id] = Reference(
            ref_id,
            site.entity_id,
            site.stack_id,
            site.path,
            source,
            kind,
            direct,
            display,
            target_ids(value),
            attribute if attribute is None or re.fullmatch(r"[A-Za-z0-9_.]+", attribute) else None,
            status,
            trace,
        )
        return replace(value, state=effective, reference_ids=(ref_id, *trace))

    def _target(
        self,
        scope: _Scope,
        name: str,
        kind: ReferenceKind,
        attribute: str | None,
        site: EvaluationSite,
        source: SourceLocation,
    ) -> tuple[str | None, ResolvedValue]:
        if kind is ReferenceKind.CONDITION:
            if name in scope.conditions:
                return _symbol_id(scope.id, "condition", name), self._condition(
                    scope, name, scope.conditions[name]
                )
        elif attribute is None and kind is not ReferenceKind.DEPENDS_ON:
            if name in scope.parameter_definitions:
                return _symbol_id(scope.id, "parameter", name), self._parameter(
                    scope, name, scope.parameter_definitions[name]
                )
            if name == "AWS::NoValue":
                return name, ResolvedValue(ValueKind.OMITTED)
            if name in _PSEUDO_PARAMETERS:
                root = self.scopes[scope.id.partition("/")[0]].root_input
                setting = {
                    "AWS::AccountId": root.account_id,
                    "AWS::Region": root.region,
                    "AWS::StackName": root.stack_name if scope.parent_id is None else None,
                }.get(name)
                if setting is not None:
                    return name, literal(setting)
                return name, ResolvedValue(ValueKind.UNKNOWN, name, reason="PSEUDO_PARAMETER")
        if (
            kind is not ReferenceKind.CONDITION
            and name in scope.resources
            and self._valid_logical_id(name)
        ):
            declaration = scope.resources[name]
            if _type_of(declaration) is None:
                self.issue(
                    "UNRESOLVED_REFERENCE",
                    "Reference points to an invalid resource declaration.",
                    site,
                    source,
                )
                return None, ResolvedValue(ValueKind.UNKNOWN, reason="UNRESOLVED_REFERENCE")
            resource_id = f"{scope.id}#{name}"
            state = self._declaration_state(scope, declaration, ("Resources", name), resource_id)
            if (
                attribute
                and attribute.startswith("Outputs.")
                and _type_of(declaration) == _STACK_TYPE
            ):
                output_name = attribute.removeprefix("Outputs.")
                child = (
                    self._child(scope, name, declaration)
                    if site.state is not ConditionState.FALSE
                    else None
                )
                if child is not None and output_name in child.outputs:
                    return resource_id, self._output(child, output_name, child.outputs[output_name])
                self.issue(
                    "UNRESOLVED_NESTED_OUTPUT",
                    "Nested stack output could not be located.",
                    site,
                    source,
                )
                return resource_id, ResolvedValue(
                    ValueKind.UNKNOWN, state=state, reason="UNRESOLVED_NESTED_OUTPUT"
                )
            return resource_id, ResolvedValue(
                ValueKind.RESOURCE_ATTRIBUTE, ResourceAttribute(resource_id, attribute), state
            )
        self.issue(
            "UNRESOLVED_REFERENCE", "Reference target was not found in its stack.", site, source
        )
        return None, ResolvedValue(ValueKind.UNKNOWN, reason="UNRESOLVED_REFERENCE")

    def mapping(self, stack_id: str, names: tuple[str, str, str]) -> AstNode | None:
        first, second, third = names
        return _mapping(_mapping(self.scopes[stack_id].mappings.get(first)).get(second)).get(third)

    def language_extensions(self, stack_id: str) -> bool:
        return self.scopes[stack_id].language_extensions

    def issue(self, code: str, message: str, site: EvaluationSite, source: SourceLocation) -> None:
        if site.state is not ConditionState.FALSE:
            self.diagnostics.add(Diagnostic(code, message, source, site.path, site.entity_id))
