"""Renderer-independent architecture and its provenance."""

from dataclasses import dataclass
from enum import StrEnum

from .models import ConditionState, Diagnostic, PropertyPath, Reference, SourceLocation, StackStatus


class EdgeKind(StrEnum):
    TRAFFIC = "TRAFFIC"
    DNS = "DNS"
    EVENT = "EVENT"
    DATA_ACCESS = "DATA_ACCESS"
    CONTAINS = "CONTAINS"
    ASSOCIATION = "ASSOCIATION"
    DEPENDENCY = "DEPENDENCY"


class EdgeStatus(StrEnum):
    CONFIRMED = "confirmed"
    CONDITIONAL = "conditional"
    INACTIVE = "inactive"


class EdgeOrigin(StrEnum):
    AUTOMATIC = "automatic"
    USER_DEFINED = "user_defined"


MANUAL_EDGE_KINDS = frozenset(
    {EdgeKind.TRAFFIC, EdgeKind.EVENT, EdgeKind.DATA_ACCESS, EdgeKind.ASSOCIATION}
)


@dataclass(frozen=True, slots=True)
class ManualRelationship:
    source_id: str
    target_id: str
    kind: EdgeKind
    evidence: str
    source: SourceLocation


class NodeRole(StrEnum):
    PRIMARY = "primary"
    SUPPORT = "support"
    INVENTORY = "inventory"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ArchitectureNode:
    id: str
    logical_id: str
    type: str
    stack_id: str
    state: ConditionState
    role: NodeRole
    source: SourceLocation
    input_type: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    entity_id: str
    path: PropertyPath
    source: SourceLocation
    reference_ids: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Edge:
    id: str
    source_id: str
    target_id: str
    kind: EdgeKind
    status: EdgeStatus
    rule_id: str
    evidence_ids: tuple[str, ...]
    origin: EdgeOrigin = EdgeOrigin.AUTOMATIC
    operation: str | None = None


@dataclass(frozen=True, slots=True)
class Container:
    id: str
    axis: str
    kind: str
    parent_ids: tuple[str, ...]
    state: ConditionState
    source: SourceLocation
    stack_status: StackStatus | None = None
    input_format: str | None = None


@dataclass(frozen=True, slots=True)
class Membership:
    container_id: str
    member_id: str
    axis: str
    status: EdgeStatus
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StackReference:
    source_id: str
    target_id: str
    reference_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchitectureModel:
    schema_version: int
    nodes: dict[str, ArchitectureNode]
    edges: tuple[Edge, ...]
    containers: dict[str, Container]
    memberships: tuple[Membership, ...]
    evidence: dict[str, Evidence]
    references: dict[str, Reference]
    stack_references: tuple[StackReference, ...]
    diagnostics: tuple[Diagnostic, ...]
