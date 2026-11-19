"""Data exchanged by the loader, resolver and architecture analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

type Scalar = str | int | float | bool | None
type PathPart = str | int
type PropertyPath = tuple[PathPart, ...]


def pointer(path: PropertyPath) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


class ConditionState(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


def conjunction(*states: ConditionState) -> ConditionState:
    if ConditionState.FALSE in states:
        return ConditionState.FALSE
    if ConditionState.UNKNOWN in states:
        return ConditionState.UNKNOWN
    return ConditionState.TRUE


class ValueKind(StrEnum):
    LITERAL = "literal"
    RESOURCE_ATTRIBUTE = "resource_attribute"
    COMPOSITE = "composite"
    OBJECT = "object"
    ARRAY = "array"
    OMITTED = "omitted"
    UNKNOWN = "unknown"
    REDACTED = "redacted"


class ReferenceKind(StrEnum):
    REF = "REF"
    GET_ATT = "GET_ATT"
    SUB = "SUB"
    DEPENDS_ON = "DEPENDS_ON"
    CONDITION = "CONDITION"
    IMPORT_VALUE = "IMPORT_VALUE"
    TERRAFORM_REFERENCE = "TERRAFORM_REFERENCE"


class ReferenceStatus(StrEnum):
    RESOLVED = "resolved"
    CONDITIONAL = "conditional"
    INACTIVE = "inactive"
    UNRESOLVED = "unresolved"
    REDACTED = "redacted"


class StackStatus(StrEnum):
    ANALYZED = "analyzed"
    PARTIAL = "partial"
    INACTIVE = "inactive"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class SourceLocation:
    template: str
    path: PropertyPath = ()
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    source: SourceLocation
    path: PropertyPath = ()
    entity_id: str | None = None
    severity: str = "warning"


@dataclass(frozen=True, slots=True)
class AstNode:
    value: Scalar | dict[str, AstNode] | tuple[AstNode, ...] = field(repr=False)
    source: SourceLocation

    def mapping(self) -> dict[str, AstNode] | None:
        return self.value if isinstance(self.value, dict) else None

    def sequence(self) -> tuple[AstNode, ...] | None:
        return self.value if isinstance(self.value, tuple) else None


@dataclass(frozen=True, slots=True)
class TemplateDocument:
    root: AstNode


@dataclass(frozen=True, slots=True)
class ResourceAttribute:
    resource_id: str
    attribute: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedValue:
    kind: ValueKind
    value: Scalar | ResourceAttribute | tuple[ResolvedValue, ...] | dict[str, ResolvedValue] = (
        field(default=None, repr=False)
    )
    state: ConditionState = ConditionState.TRUE
    reference_ids: tuple[str, ...] = ()
    reason: str | None = None
    source: SourceLocation | None = None
    path: PropertyPath | None = None

    def property(self, name: str) -> ResolvedValue | None:
        if self.kind is ValueKind.OBJECT and isinstance(self.value, dict):
            return self.value.get(name)
        return None


@dataclass(frozen=True, slots=True)
class Reference:
    id: str
    source_entity: str
    source_stack_id: str
    path: PropertyPath
    source: SourceLocation
    kind: ReferenceKind
    direct_target_id: str | None
    direct_target: str | None
    resolved_target_ids: tuple[str, ...]
    attribute: str | None
    status: ReferenceStatus
    trace: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    stack_id: str
    logical_id: str
    type: str
    source: SourceLocation
    condition_state: ConditionState
    properties: AstNode = field(repr=False)
    evaluated_properties: ResolvedValue = field(repr=False)
    input_type: str | None = None


@dataclass(frozen=True, slots=True)
class Stack:
    id: str
    parent_id: str | None
    resource_id: str | None
    source: SourceLocation
    status: StackStatus
    condition_state: ConditionState
    parameters: dict[str, ResolvedValue] = field(default_factory=dict, repr=False)
    outputs: dict[str, ResolvedValue] = field(default_factory=dict, repr=False)
    unexpanded_declarations: tuple[AstNode, ...] = field(default_factory=tuple, repr=False)
    input_format: str | None = None


@dataclass(frozen=True, slots=True)
class RootInput:
    id: str
    template: Path
    parameters: dict[str, Scalar | list[Scalar]] = field(default_factory=dict, repr=False)
    account_id: str | None = None
    region: str | None = None
    stack_name: str | None = None


@dataclass(frozen=True, slots=True)
class TerraformInput:
    id: str
    path: Path
    variables: dict[str, Scalar | list[Scalar]] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class ResourceGraph:
    stacks: dict[str, Stack]
    resources: dict[str, Resource]
    references: dict[str, Reference]
    diagnostics: tuple[Diagnostic, ...]
