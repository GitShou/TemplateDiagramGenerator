"""Evaluate the supported static subset of CloudFormation expressions."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Protocol

from .models import (
    AstNode,
    ConditionState,
    PropertyPath,
    ReferenceKind,
    ResolvedValue,
    ResourceAttribute,
    SourceLocation,
    ValueKind,
    conjunction,
)

_SUB_TOKEN = re.compile(r"\$\{([^{}]+)\}")
_DYNAMIC_REFERENCE = re.compile(r"\{\{resolve:[^{}]*\}\}")


@dataclass(frozen=True, slots=True)
class EvaluationSite:
    stack_id: str
    entity_id: str
    path: PropertyPath
    state: ConditionState = ConditionState.TRUE

    def child(self, *path: str | int) -> EvaluationSite:
        return replace(self, path=(*self.path, *path))


class ExpressionScope(Protocol):
    def reference(
        self,
        site: EvaluationSite,
        source: SourceLocation,
        name: str,
        kind: ReferenceKind,
        *,
        attribute: str | None = None,
        occurrence: int = 0,
    ) -> ResolvedValue: ...

    def import_value(
        self, site: EvaluationSite, source: SourceLocation, name: ResolvedValue
    ) -> ResolvedValue: ...

    def mapping(self, stack_id: str, names: tuple[str, str, str]) -> AstNode | None: ...

    def language_extensions(self, stack_id: str) -> bool: ...

    def issue(
        self, code: str, message: str, site: EvaluationSite, source: SourceLocation
    ) -> None: ...


def references(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for value in values for ref in value.reference_ids))


def condition_of(value: ResolvedValue) -> ConditionState:
    if value.kind is ValueKind.LITERAL and type(value.value) is bool:
        return ConditionState.TRUE if value.value else ConditionState.FALSE
    return ConditionState.UNKNOWN


def value_identity(value: ResolvedValue) -> tuple:
    """Compare branch results independently of their source locations."""
    if isinstance(value.value, dict):
        identity = tuple((key, value_identity(item)) for key, item in sorted(value.value.items()))
    elif isinstance(value.value, tuple):
        identity = tuple(value_identity(item) for item in value.value)
    else:
        identity = type(value.value), value.value
    return value.kind, identity, value.reason


def target_ids(value: ResolvedValue) -> tuple[str, ...]:
    if isinstance(value.value, ResourceAttribute):
        return (value.value.resource_id,)
    if isinstance(value.value, (dict, tuple)):
        children = value.value.values() if isinstance(value.value, dict) else value.value
        return tuple(sorted({target for child in children for target in target_ids(child)}))
    return ()


def literal(value, state: ConditionState = ConditionState.TRUE) -> ResolvedValue:
    if isinstance(value, str) and _DYNAMIC_REFERENCE.search(value):
        return ResolvedValue(ValueKind.REDACTED, state=state, reason="DYNAMIC_REFERENCE")
    return ResolvedValue(ValueKind.LITERAL, value, state)


def _composite(parts: list[ResolvedValue], state: ConditionState) -> ResolvedValue:
    refs = references(parts)
    state = conjunction(state, *(part.state for part in parts))
    if any(part.kind is ValueKind.REDACTED for part in parts):
        return ResolvedValue(
            ValueKind.REDACTED, state=state, reference_ids=refs, reason="REDACTED_EXPRESSION"
        )
    if any(
        part.kind in {ValueKind.ARRAY, ValueKind.OBJECT, ValueKind.OMITTED}
        or part.kind is ValueKind.LITERAL
        and type(part.value) not in {str, int, float}
        for part in parts
    ):
        return ResolvedValue(
            ValueKind.UNKNOWN, state=state, reference_ids=refs, reason="INVALID_STRING_PART"
        )
    pieces: list[ResolvedValue] = []
    for part in parts:
        for piece in part.value if part.kind is ValueKind.COMPOSITE else (part,):
            if piece.kind is ValueKind.LITERAL and isinstance(piece.value, (str, int, float)):
                piece = literal(str(piece.value), state)
                if pieces and pieces[-1].kind is ValueKind.LITERAL:
                    piece = literal(str(pieces.pop().value) + str(piece.value), state)
                if piece.kind is ValueKind.REDACTED:
                    return replace(piece, reference_ids=refs)
                if not piece.value:
                    continue
            pieces.append(piece)
    if not pieces:
        return ResolvedValue(ValueKind.LITERAL, "", state, refs)
    if len(pieces) == 1 and pieces[0].kind is ValueKind.LITERAL:
        return replace(pieces[0], state=state, reference_ids=refs)
    return ResolvedValue(ValueKind.COMPOSITE, tuple(pieces), state, refs)


class ExpressionEvaluator:
    def __init__(self, scope: ExpressionScope):
        self.scope = scope

    def evaluate(self, node: AstNode, site: EvaluationSite) -> ResolvedValue:
        result = self._evaluate(node, site)
        return replace(result, source=node.source, path=site.path)

    def _evaluate(self, node: AstNode, site: EvaluationSite) -> ResolvedValue:
        if (mapping := node.mapping()) is not None:
            intrinsic = next(iter(mapping), "") if len(mapping) == 1 else ""
            if intrinsic.startswith("Fn::") or intrinsic in {"Ref", "Condition"}:
                return self._intrinsic(intrinsic, mapping[intrinsic], site, node.source)
            values = {
                key: self.evaluate(item, site.child(key)) for key, item in sorted(mapping.items())
            }
            values = {
                key: value for key, value in values.items() if value.kind is not ValueKind.OMITTED
            }
            return ResolvedValue(ValueKind.OBJECT, values, site.state, references(values.values()))
        if (sequence := node.sequence()) is not None:
            items = tuple(self.evaluate(item, site.child(idx)) for idx, item in enumerate(sequence))
            items = tuple(item for item in items if item.kind is not ValueKind.OMITTED)
            return ResolvedValue(ValueKind.ARRAY, items, site.state, references(items))
        return literal(node.value, site.state)

    def _intrinsic(
        self, key: str, arg: AstNode, site: EvaluationSite, source: SourceLocation
    ) -> ResolvedValue:
        if key in {"Ref", "Condition", "Fn::GetAtt"}:
            return self._lookup(key, arg, site, source)
        if key == "Fn::ImportValue":
            return self.scope.import_value(site, source, self.evaluate(arg, site.child(key)))
        if key == "Fn::Sub":
            return self._sub(arg, site, source)
        if key == "Fn::Join":
            return self._join(arg, site, source)
        if key == "Fn::If":
            return self._if(arg, site, source)
        if key == "Fn::FindInMap":
            return self._find_in_map(arg, site, source)
        if key in {"Fn::Equals", "Fn::And", "Fn::Or", "Fn::Not"}:
            return self._boolean(key, arg, site, source)
        self.scope.issue(
            "UNSUPPORTED_INTRINSIC",
            "Intrinsic is outside the static resolver subset.",
            site,
            source,
        )
        # Keep references in unsupported expressions, with uncertain applicability.
        detail = self.evaluate(
            arg, replace(site.child(key), state=conjunction(site.state, ConditionState.UNKNOWN))
        )
        return ResolvedValue(
            ValueKind.UNKNOWN,
            state=site.state,
            reference_ids=detail.reference_ids,
            reason="UNSUPPORTED_INTRINSIC",
        )

    def _invalid(self, site: EvaluationSite, source: SourceLocation) -> ResolvedValue:
        self.scope.issue(
            "INVALID_INTRINSIC", "Unexpected intrinsic function arguments.", site, source
        )
        return ResolvedValue(ValueKind.UNKNOWN, state=site.state, reason="INVALID_INTRINSIC")

    def _lookup(
        self, key: str, arg: AstNode, site: EvaluationSite, source: SourceLocation
    ) -> ResolvedValue:
        attribute = None
        args = arg.sequence()
        if key == "Fn::GetAtt" and isinstance(arg.value, str):
            name, _, attribute = arg.value.partition(".")
        elif key == "Fn::GetAtt" and args is not None and len(args) == 2:
            name, attribute = (part.value for part in args)
            if self.scope.language_extensions(site.stack_id):
                parts = [self.evaluate(part, site.child(key, idx)) for idx, part in enumerate(args)]
                if any(part.kind is ValueKind.REDACTED for part in parts):
                    return ResolvedValue(
                        ValueKind.REDACTED, state=site.state, reference_ids=references(parts)
                    )
                name, attribute = (
                    part.value if part.kind is ValueKind.LITERAL else None for part in parts
                )
        elif key == "Ref" and arg.mapping() and self.scope.language_extensions(site.stack_id):
            result = self.evaluate(arg, site.child(key))
            if result.kind is ValueKind.REDACTED:
                return result
            name = result.value if result.kind is ValueKind.LITERAL else None
        else:
            name = arg.value
        if (
            not isinstance(name, str)
            or not name
            or (key == "Fn::GetAtt" and (not isinstance(attribute, str) or not attribute))
        ):
            return self._invalid(site, source)
        kind = {
            "Ref": ReferenceKind.REF,
            "Condition": ReferenceKind.CONDITION,
            "Fn::GetAtt": ReferenceKind.GET_ATT,
        }[key]
        return self.scope.reference(site, source, name, kind, attribute=attribute)

    def _sub(self, arg: AstNode, site: EvaluationSite, source: SourceLocation) -> ResolvedValue:
        variables: dict[str, AstNode] = {}
        pattern = arg.value
        if (args := arg.sequence()) is not None:
            if len(args) != 2 or (variable_map := args[1].mapping()) is None:
                return self._invalid(site, source)
            pattern, variables = args[0].value, variable_map
        if not isinstance(pattern, str):
            return self._invalid(site, source)
        # Preserve a dynamic reference as a redacted value before inspecting its contents.
        if _DYNAMIC_REFERENCE.search(pattern):
            return ResolvedValue(ValueKind.REDACTED, state=site.state, reason="DYNAMIC_REFERENCE")
        parts = []
        start = 0
        for index, match in enumerate(_SUB_TOKEN.finditer(pattern)):
            parts.append(literal(pattern[start : match.start()], site.state))
            token = match[1]
            if token.startswith("!"):
                value = literal("${" + token[1:] + "}", site.state)
            elif token in variables:
                value = self.evaluate(variables[token], site.child("Fn::Sub", 1, token))
            else:
                name, _, attr = token.partition(".")
                value = self.scope.reference(
                    site, source, name, ReferenceKind.SUB, attribute=attr or None, occurrence=index
                )
            parts.append(value)
            start = match.end()
        parts.append(literal(pattern[start:], site.state))
        return _composite(parts, site.state)

    def _join(self, arg: AstNode, site: EvaluationSite, source: SourceLocation) -> ResolvedValue:
        args = arg.sequence()
        if args is None or len(args) != 2 or not isinstance(args[0].value, str):
            return self._invalid(site, source)
        values = self.evaluate(args[1], site.child("Fn::Join", 1))
        if values.kind is ValueKind.REDACTED:
            return values
        if values.kind is ValueKind.UNKNOWN:
            return values
        if values.kind is not ValueKind.ARRAY:
            return self._invalid(site, source)
        parts = []
        for index, value in enumerate(values.value):
            if index:
                parts.append(literal(args[0].value, site.state))
            parts.append(value)
        result = _composite(parts, site.state)
        return replace(result, reference_ids=references((values, result)))

    def _if(self, arg: AstNode, site: EvaluationSite, source: SourceLocation) -> ResolvedValue:
        args = arg.sequence()
        if args is None or len(args) != 3 or not isinstance(args[0].value, str):
            return self._invalid(site, source)
        test = self.scope.reference(
            site.child("Fn::If", 0), args[0].source, args[0].value, ReferenceKind.CONDITION
        )
        state = condition_of(test)
        if state is not ConditionState.UNKNOWN:
            index = 1 if state is ConditionState.TRUE else 2
            value = self.evaluate(args[index], site.child("Fn::If", index))
            return replace(value, reference_ids=references((test, value)))
        guarded = replace(site, state=conjunction(site.state, ConditionState.UNKNOWN))
        first, second = [
            self.evaluate(args[index], guarded.child("Fn::If", index)) for index in (1, 2)
        ]
        refs = references((test, first, second))
        if first.kind in {
            ValueKind.RESOURCE_ATTRIBUTE,
            ValueKind.LITERAL,
            ValueKind.OMITTED,
        } and value_identity(first) == value_identity(second):
            state = first.state if first.kind is ValueKind.RESOURCE_ATTRIBUTE else site.state
            return replace(first, state=state, reference_ids=refs)
        if test.kind is ValueKind.REDACTED or ValueKind.REDACTED in {first.kind, second.kind}:
            return ResolvedValue(
                ValueKind.REDACTED,
                state=guarded.state,
                reference_ids=refs,
                reason="REDACTED_BRANCH",
            )
        self.scope.issue(
            "CONDITIONAL_EXPRESSION", "Expression contains an unresolved condition.", site, source
        )
        return ResolvedValue(
            ValueKind.UNKNOWN, (first, second), guarded.state, refs, "CONDITIONAL_EXPRESSION"
        )

    def _find_in_map(
        self, arg: AstNode, site: EvaluationSite, source: SourceLocation
    ) -> ResolvedValue:
        args = arg.sequence()
        if args is None or len(args) not in {3, 4}:
            return self._invalid(site, source)
        fallback = args[3].mapping() if len(args) == 4 else None
        if len(args) == 4 and (fallback is None or set(fallback) != {"DefaultValue"}):
            return self._invalid(site, source)
        keys = [
            self.evaluate(item, site.child("Fn::FindInMap", index))
            for index, item in enumerate(args[:3])
        ]
        if any(key.kind is ValueKind.REDACTED for key in keys):
            return ResolvedValue(
                ValueKind.REDACTED, state=site.state, reference_ids=references(keys)
            )
        if all(key.kind is ValueKind.LITERAL and isinstance(key.value, (str, int)) for key in keys):
            names = tuple(str(key.value) for key in keys)
            item = self.scope.mapping(site.stack_id, names)
            if item is None and fallback is not None:
                result = self.evaluate(
                    fallback["DefaultValue"], site.child("Fn::FindInMap", 3, "DefaultValue")
                )
            elif item is not None:
                result = self.evaluate(item, site)
            else:
                result = None
            if result is not None:
                return replace(result, reference_ids=references((*keys, result)))
        self.scope.issue(
            "UNRESOLVED_MAPPING",
            "Mapping keys or mapping value could not be resolved.",
            site,
            source,
        )
        return ResolvedValue(
            ValueKind.UNKNOWN,
            state=site.state,
            reference_ids=references(keys),
            reason="UNRESOLVED_MAPPING",
        )

    def _boolean(
        self, key: str, arg: AstNode, site: EvaluationSite, source: SourceLocation
    ) -> ResolvedValue:
        args = arg.sequence()
        sizes = {"Fn::Equals": {2}, "Fn::Not": {1}, "Fn::And": range(2, 11), "Fn::Or": range(2, 11)}
        if args is None or len(args) not in sizes[key]:
            return self._invalid(site, source)
        values = [self.evaluate(item, site.child(key, index)) for index, item in enumerate(args)]
        states = [condition_of(value) for value in values]
        result = ConditionState.UNKNOWN
        if key == "Fn::Equals" and all(value.kind is ValueKind.LITERAL for value in values):
            equal = (
                type(values[0].value) is type(values[1].value)
                and values[0].value == values[1].value
            )
            result = ConditionState.TRUE if equal else ConditionState.FALSE
        elif key == "Fn::And":
            result = conjunction(*states)
        elif key == "Fn::Or":
            result = ConditionState.TRUE if ConditionState.TRUE in states else conjunction(*states)
        elif key == "Fn::Not" and states[0] is not ConditionState.UNKNOWN:
            result = (
                ConditionState.TRUE if states[0] is ConditionState.FALSE else ConditionState.FALSE
            )
        refs = references(values)
        if result is not ConditionState.UNKNOWN:
            return ResolvedValue(ValueKind.LITERAL, result is ConditionState.TRUE, site.state, refs)
        kind = (
            ValueKind.REDACTED
            if any(value.kind is ValueKind.REDACTED for value in values)
            else ValueKind.UNKNOWN
        )
        return ResolvedValue(
            kind, state=site.state, reference_ids=refs, reason="UNRESOLVED_CONDITION"
        )
