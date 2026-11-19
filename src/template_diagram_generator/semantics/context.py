"""Shared access to resolved properties and evidence for service rules."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

from ..architecture import Edge, EdgeKind, EdgeOrigin, EdgeStatus, Evidence
from ..models import (
    ConditionState,
    Diagnostic,
    PropertyPath,
    ResolvedValue,
    Resource,
    ResourceAttribute,
    ResourceGraph,
    ValueKind,
    conjunction,
)

STATUS = {
    ConditionState.TRUE: EdgeStatus.CONFIRMED,
    ConditionState.UNKNOWN: EdgeStatus.CONDITIONAL,
    ConditionState.FALSE: EdgeStatus.INACTIVE,
}

VPC = "AWS::EC2::VPC"
SUBNET = "AWS::EC2::Subnet"
SECURITY_GROUP = "AWS::EC2::SecurityGroup"
ALB = "AWS::ElasticLoadBalancingV2::LoadBalancer"
LISTENER = "AWS::ElasticLoadBalancingV2::Listener"
LISTENER_RULE = "AWS::ElasticLoadBalancingV2::ListenerRule"
TARGET_GROUP = "AWS::ElasticLoadBalancingV2::TargetGroup"
ECS = "AWS::ECS::Service"
ECS_CLUSTER = "AWS::ECS::Cluster"
ECS_TASK = "AWS::ECS::TaskDefinition"
RDS = "AWS::RDS::DBInstance"
DB_SUBNET_GROUP = "AWS::RDS::DBSubnetGroup"
LAMBDA = "AWS::Lambda::Function"
SQS = "AWS::SQS::Queue"
EVENT_RULE = "AWS::Events::Rule"
EVENT_MAPPING = "AWS::Lambda::EventSourceMapping"
CLOUDFRONT = "AWS::CloudFront::Distribution"
BUCKET = "AWS::S3::Bucket"
API = "AWS::ApiGatewayV2::Api"
ROUTE = "AWS::ApiGatewayV2::Route"
INTEGRATION = "AWS::ApiGatewayV2::Integration"
REST_API = "AWS::ApiGateway::RestApi"
REST_METHOD = "AWS::ApiGateway::Method"
REST_RESOURCE = "AWS::ApiGateway::Resource"
REST_DOMAIN = "AWS::ApiGateway::DomainName"
HTTP_DOMAIN = "AWS::ApiGatewayV2::DomainName"
DNS_RECORD = "AWS::Route53::RecordSet"
DNS_RECORD_GROUP = "AWS::Route53::RecordSetGroup"
STATE_MACHINE = "AWS::StepFunctions::StateMachine"
SCHEDULE = "AWS::Scheduler::Schedule"
SNS = "AWS::SNS::Topic"
EVENT_BUS = "AWS::Events::EventBus"
DYNAMODB = "AWS::DynamoDB::Table"


def stable_id(prefix: str, *parts) -> str:
    data = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return f"{prefix}-" + hashlib.sha256(data.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class Property:
    resource: Resource
    path: PropertyPath
    value: ResolvedValue | None

    @classmethod
    def root(cls, resource: Resource) -> Property:
        return cls(
            resource,
            resource.evaluated_properties.path or ("Resources", resource.logical_id, "Properties"),
            resource.evaluated_properties,
        )

    def get(self, *keys: str) -> Property:
        result = self.value
        for key in keys:
            result = result.property(key) if result is not None else None
        path = (*self.path, *keys)
        if (
            result is not None
            and result.path is not None
            and result.path[: len(self.path)] == self.path
        ):
            path = result.path
        return Property(self.resource, path, result)

    def entries(self) -> tuple[Property, ...]:
        if self.value is None or self.value.kind is not ValueKind.ARRAY:
            return ()
        return tuple(
            Property(
                self.resource,
                item.path if item.path and item.path[:-1] == self.path else (*self.path, idx),
                item,
            )
            for idx, item in enumerate(self.value.value)
        )

    def literal(self):
        return (
            self.value.value
            if self.value is not None and self.value.kind is ValueKind.LITERAL
            else None
        )


def symbol(value: ResolvedValue | None) -> ResourceAttribute | None:
    if value is not None and value.kind is ValueKind.COMPOSITE and len(value.value) == 1:
        value = value.value[0]
    return value.value if value is not None and value.kind is ValueKind.RESOURCE_ATTRIBUTE else None


class RuleContext:
    def __init__(self, graph: ResourceGraph):
        self.graph = graph
        self.by_type = defaultdict(list)
        for resource in graph.resources.values():
            self.by_type[resource.type].append(resource)
        self.edges: dict[str, Edge] = {}
        self.evidence: dict[str, Evidence] = {}
        self.diagnostics: set[Diagnostic] = set()

    def resources(self, type_: str) -> tuple[Resource, ...]:
        return tuple(self.by_type.get(type_, ()))

    def match(
        self, attribute: ResourceAttribute | None, types: set[str], attributes: set[str | None]
    ) -> Resource | None:
        if attribute is None or attribute.attribute not in attributes:
            return None
        resource = self.graph.resources.get(attribute.resource_id)
        return resource if resource is not None and resource.type in types else None

    def target(
        self, prop: Property, types: set[str], attributes: set[str | None]
    ) -> Resource | None:
        return self.match(symbol(prop.value), types, attributes)

    def issue(self, code: str, message: str, prop: Property) -> None:
        if prop.resource.condition_state is not ConditionState.FALSE:
            source = (
                prop.value.source
                if prop.value is not None and prop.value.source
                else prop.resource.source
            )
            self.diagnostics.add(Diagnostic(code, message, source, prop.path, prop.resource.id))

    def add(
        self,
        rule: str,
        kind: EdgeKind,
        start: Resource,
        end: Resource,
        *props: Property,
        state: ConditionState = ConditionState.TRUE,
        operation: str | None = None,
    ) -> None:
        state = conjunction(
            state,
            start.condition_state,
            end.condition_state,
            *(prop.resource.condition_state for prop in props),
            *(prop.value.state for prop in props if prop.value is not None),
        )
        ids = []
        for prop in props:
            value = prop.value
            refs = tuple(sorted(value.reference_ids)) if value is not None else ()
            source = value.source if value is not None and value.source else prop.resource.source
            ids.append(self.add_evidence(prop.resource.id, prop.path, source, refs))
        self.add_edge(
            rule,
            kind,
            start.id,
            end.id,
            STATUS[state],
            tuple(sorted(set(ids))),
            operation=operation,
        )

    def add_evidence(self, entity_id, path, source, refs, *, note=None) -> str:
        id_ = stable_id("evidence", entity_id, path, refs)
        self.evidence[id_] = Evidence(id_, entity_id, path, source, refs, note)
        return id_

    def add_edge(
        self, rule, kind, start, end, status, ids, *, origin=EdgeOrigin.AUTOMATIC, operation=None
    ) -> None:
        identity = (rule, kind, start, end, status, ids)
        if origin is not EdgeOrigin.AUTOMATIC or operation is not None:
            identity += (origin, operation)
        id_ = stable_id("edge", *identity)
        self.edges[id_] = Edge(id_, start, end, kind, status, rule, ids, origin, operation)
