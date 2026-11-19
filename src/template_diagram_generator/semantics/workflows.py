"""Inspect declared DynamoDB operations in Step Functions state machines."""

import json
import re
from collections import Counter
from dataclasses import replace

from ..architecture import EdgeKind
from ..expressions import references
from ..models import ConditionState, ResolvedValue, ValueKind, conjunction
from .context import DYNAMODB, Property, RuleContext

_ACTION = re.compile(
    r"arn:[a-z0-9-]+:states:::(?:aws-sdk:)?dynamodb:(getItem|putItem|updateItem|deleteItem)\Z"
)
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}\Z")
_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")
_MARKER = re.compile(r"__tdg_asl_symbol_[0-9]+__")


def _literal(value: ResolvedValue | None):
    return value.value if value is not None and value.kind is ValueKind.LITERAL else None


def _from_json(definition: Property) -> Property:
    value = definition.value
    marker_values = {}
    if value.kind is ValueKind.LITERAL and isinstance(value.value, str):
        text = value.value
    elif value.kind is ValueKind.COMPOSITE:
        parts = []
        for part in value.value:
            item = _literal(part)
            if isinstance(item, str):
                parts.append(item)
            else:
                marker = f"__tdg_asl_symbol_{len(marker_values)}__"
                marker_values[marker] = part
                parts.append(marker)
        text = "".join(parts)
    else:
        raise ValueError("Inline ASL is not text")

    def unique_keys(pairs):
        obj = dict(pairs)
        if len(obj) != len(pairs):
            raise ValueError("Duplicate ASL key")
        return obj

    occurrences = Counter()

    def convert(item, path, depth):
        if depth > 64:
            raise ValueError("Inline ASL is too deep")
        metadata = {"state": value.state, "source": value.source, "path": path}
        if isinstance(item, dict):
            if marker_values and any(_MARKER.search(key) for key in item):
                raise ValueError("Symbolic ASL key")
            children = {
                key: convert(child, (*path, key), depth + 1) for key, child in sorted(item.items())
            }
            return ResolvedValue(ValueKind.OBJECT, children, **metadata)
        if isinstance(item, list):
            children = tuple(
                convert(child, (*path, index), depth + 1) for index, child in enumerate(item)
            )
            return ResolvedValue(ValueKind.ARRAY, children, **metadata)
        if isinstance(item, str) and marker_values and (parts := _MARKER.split(item)) != [item]:
            pieces = []
            for index, match in enumerate(_MARKER.finditer(item)):
                token = match[0]
                if token not in marker_values:
                    raise ValueError("Unknown ASL substitution marker")
                occurrences[token] += 1
                if parts[index]:
                    pieces.append(ResolvedValue(ValueKind.LITERAL, parts[index], **metadata))
                pieces.append(marker_values[token])
            if parts[-1]:
                pieces.append(ResolvedValue(ValueKind.LITERAL, parts[-1], **metadata))
            if len(pieces) == 1:
                symbol = pieces[0]
                return replace(
                    symbol,
                    state=conjunction(value.state, symbol.state),
                    source=value.source,
                    path=path,
                )
            return ResolvedValue(
                ValueKind.COMPOSITE,
                tuple(pieces),
                conjunction(value.state, *(p.state for p in pieces)),
                references(pieces),
                source=value.source,
                path=path,
            )
        return ResolvedValue(ValueKind.LITERAL, item, **metadata)

    raw = json.loads(text, object_pairs_hook=unique_keys)
    result = convert(raw, definition.path, 0)
    if marker_values and occurrences != Counter(marker_values.keys()):
        raise ValueError("Ambiguous ASL substitution marker")
    return Property(definition.resource, definition.path, result)


def _tasks(definition: Property):
    machines = [definition]
    while machines:
        states = machines.pop().get("States")
        if states.value is None or states.value.kind is not ValueKind.OBJECT:
            continue
        for name in sorted(states.value.value):
            state = states.get(name)
            kind = state.get("Type").literal()
            if kind == "Task":
                yield state
            elif kind == "Parallel":
                machines.extend(reversed(state.get("Branches").entries()))
            elif kind == "Map":
                processor = state.get("ItemProcessor")
                machines.append(processor if processor.value is not None else state.get("Iterator"))


def _substitution(field: Property, substitutions: Property) -> Property:
    value = field.literal()
    if isinstance(value, str) and (match := _PLACEHOLDER.fullmatch(value)):
        return substitutions.get(match[1])
    return field


def _operation(field: Property) -> str | None:
    value = field.value
    uri = field.literal()
    if not isinstance(uri, str) and value is not None and value.kind is ValueKind.COMPOSITE:
        parts = []
        for item in value.value:
            if isinstance(_literal(item), str):
                parts.append(item.value)
            elif (
                item.kind is ValueKind.UNKNOWN
                and item.reason == "PSEUDO_PARAMETER"
                and item.value == "AWS::Partition"
            ):
                parts.append("aws")
            else:
                return None
        uri = "".join(parts)
    match = _ACTION.fullmatch(uri) if isinstance(uri, str) else None
    if match:
        operation = match[1]
        return operation[0].upper() + operation[1:]
    return None


def _table(ctx: RuleContext, field: Property):
    if (resource := ctx.target(field, {DYNAMODB}, {None, "Arn"})) is not None:
        return resource, None
    name = field.literal()
    if not isinstance(name, str) or not _TABLE_NAME.fullmatch(name):
        return None, None
    matches = []
    for candidate in ctx.resources(DYNAMODB):
        if (
            candidate.stack_id != field.resource.stack_id
            or candidate.condition_state is ConditionState.FALSE
        ):
            continue
        name_property = Property.root(candidate).get("TableName")
        if name_property.literal() == name:
            matches.append((candidate, name_property))
    return matches[0] if len(matches) == 1 else (None, None)


def step_functions_dynamodb(ctx: RuleContext, resource) -> None:
    fields = Property.root(resource)
    locations = [
        fields.get(key) for key in ("Definition", "DefinitionString", "DefinitionS3Location")
    ]
    definitions = [item for item in locations if item.value is not None]
    if len(definitions) != 1 or definitions[0] == locations[2]:
        if definitions:
            ctx.issue(
                "STEP_FUNCTIONS_DEFINITION",
                "A single local Step Functions definition is required for analysis.",
                definitions[0],
            )
        return
    definition = definitions[0]
    if definition == locations[1]:
        try:
            definition = _from_json(definition)
        except (ValueError, RecursionError):
            ctx.issue(
                "STEP_FUNCTIONS_DEFINITION",
                "Inline DefinitionString could not be read as static JSON.",
                definition,
            )
            return
    states = definition.get("States")
    if states.value is None or states.value.kind is not ValueKind.OBJECT:
        ctx.issue(
            "STEP_FUNCTIONS_DEFINITION",
            "The local definition does not contain a static States object.",
            definition,
        )
        return
    substitutions = fields.get("DefinitionSubstitutions")
    for task in _tasks(definition):
        action = task.get("Resource")
        resolved_action = _substitution(action, substitutions)
        operation = _operation(resolved_action)
        if operation is None:
            continue
        credentials = task.get("Credentials")
        if credentials.value is not None:
            ctx.issue(
                "STEP_FUNCTIONS_CREDENTIALS",
                "Task credentials may select a different DynamoDB account.",
                credentials,
            )
            continue
        arguments, parameters = task.get("Arguments"), task.get("Parameters")
        table_args = arguments if arguments.value is not None else parameters
        table = table_args.get("TableName")
        resolved_table = _substitution(table, substitutions)
        if (
            table_args.get("TableName.$").value is not None
            or arguments.value is not None
            and parameters.value is not None
        ):
            endpoint, matched_name = None, None
        else:
            endpoint, matched_name = _table(ctx, resolved_table)
        if endpoint is None:
            ctx.issue(
                "STEP_FUNCTIONS_DYNAMODB_TARGET",
                "DynamoDB table could not be uniquely resolved from the declared task.",
                table,
            )
            continue
        proof = [task.get("Type"), action, table]
        if resolved_action is not action:
            proof.append(resolved_action)
        if resolved_table is not table:
            proof.append(resolved_table)
        if matched_name is not None:
            proof.append(matched_name)
        ctx.add(
            "stepfunctions.dynamodb_task",
            EdgeKind.DATA_ACCESS,
            resource,
            endpoint,
            *proof,
            operation=operation,
        )
