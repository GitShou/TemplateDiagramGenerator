"""Type-scoped AWS rules and architecture model assembly."""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..architecture import (
    MANUAL_EDGE_KINDS,
    ArchitectureModel,
    ArchitectureNode,
    Container,
    EdgeKind,
    EdgeOrigin,
    EdgeStatus,
    ManualRelationship,
    Membership,
    NodeRole,
)
from ..dependency import dependency_cycles, dependency_references, stack_references
from ..models import ConditionState, Diagnostic, Resource, ResourceGraph, conjunction, pointer
from . import dns, events, network, routing, workflows
from .context import (
    ALB,
    API,
    BUCKET,
    CLOUDFRONT,
    DB_SUBNET_GROUP,
    DNS_RECORD,
    DNS_RECORD_GROUP,
    ECS,
    ECS_CLUSTER,
    ECS_TASK,
    EVENT_BUS,
    EVENT_MAPPING,
    EVENT_RULE,
    HTTP_DOMAIN,
    INTEGRATION,
    LAMBDA,
    LISTENER,
    LISTENER_RULE,
    RDS,
    REST_API,
    REST_DOMAIN,
    REST_METHOD,
    REST_RESOURCE,
    ROUTE,
    SCHEDULE,
    SECURITY_GROUP,
    SNS,
    SQS,
    STATE_MACHINE,
    STATUS,
    SUBNET,
    TARGET_GROUP,
    VPC,
    RuleContext,
)

PRIMARY = {
    ALB,
    API,
    BUCKET,
    CLOUDFRONT,
    ECS,
    EVENT_BUS,
    EVENT_RULE,
    LAMBDA,
    RDS,
    REST_API,
    SCHEDULE,
    SNS,
    SQS,
    STATE_MACHINE,
}
SUPPORT = {
    DB_SUBNET_GROUP,
    DNS_RECORD,
    DNS_RECORD_GROUP,
    ECS_CLUSTER,
    ECS_TASK,
    EVENT_MAPPING,
    HTTP_DOMAIN,
    INTEGRATION,
    LISTENER,
    LISTENER_RULE,
    REST_DOMAIN,
    REST_METHOD,
    REST_RESOURCE,
    ROUTE,
    SECURITY_GROUP,
    SUBNET,
    TARGET_GROUP,
    VPC,
}
INVENTORY = {
    "AWS::CloudFormation::Stack",
    "AWS::DynamoDB::Table",
    "AWS::IAM::Role",
    "AWS::IAM::Policy",
    "AWS::IAM::ManagedPolicy",
    "AWS::S3::BucketPolicy",
    "AWS::Lambda::Permission",
    "AWS::Logs::LogGroup",
    "AWS::SQS::QueuePolicy",
    "AWS::EC2::InternetGateway",
    "AWS::EC2::VPCGatewayAttachment",
    "AWS::EC2::RouteTable",
    "AWS::EC2::Route",
    "AWS::EC2::SubnetRouteTableAssociation",
}


@dataclass(frozen=True, slots=True)
class Rule:
    resource_type: str
    evaluate: Callable[[RuleContext, Resource], None]


DEFAULT_RULES = (
    Rule(CLOUDFRONT, routing.cloudfront),
    Rule(ROUTE, routing.http_api),
    Rule(REST_METHOD, routing.rest_api),
    Rule(DNS_RECORD, dns.route53_alias),
    Rule(DNS_RECORD_GROUP, dns.route53_alias),
    Rule(STATE_MACHINE, workflows.step_functions_dynamodb),
    Rule(LISTENER, routing.alb_listener),
    Rule(LISTENER, network.listener_vpc),
    Rule(LISTENER_RULE, routing.alb_listener_rule),
    Rule(TARGET_GROUP, network.vpc_scoped),
    Rule(ECS, routing.ecs_target),
    Rule(EVENT_RULE, events.eventbridge),
    Rule(SCHEDULE, events.scheduler),
    Rule(EVENT_MAPPING, events.event_mapping),
    Rule(SUBNET, network.subnet),
    Rule(ALB, network.workload),
    Rule(ECS, network.workload),
    Rule(RDS, network.workload),
    Rule(SECURITY_GROUP, network.vpc_scoped),
)


def analyze(
    graph: ResourceGraph,
    rules: Iterable[Rule] = DEFAULT_RULES,
    *,
    relationships: Iterable[ManualRelationship] = (),
) -> ArchitectureModel:
    context = RuleContext(graph)
    by_type = defaultdict(list)
    for rule in rules:
        by_type[rule.resource_type].append(rule.evaluate)
    nodes = {}
    for key, resource in sorted(graph.resources.items()):
        role = _role(resource, by_type)
        nodes[key] = ArchitectureNode(
            key,
            resource.logical_id,
            resource.type,
            resource.stack_id,
            resource.condition_state,
            role,
            resource.source,
            resource.input_type,
        )
        if role is NodeRole.UNSUPPORTED:
            context.diagnostics.add(
                Diagnostic(
                    "UNSUPPORTED_RESOURCE_TYPE",
                    "Type is retained in the inventory.",
                    resource.source,
                    resource.source.path,
                    key,
                    "info",
                )
            )
        for evaluate in by_type.get(resource.type, ()):
            evaluate(context, resource)

    for relationship in relationships:
        _add_manual_relationship(context, relationship)
    for ref, status in dependency_references(graph):
        id_ = context.add_evidence(ref.source_entity, ref.path, ref.source, (ref.id,))
        context.add_edge(
            "cloudformation.reference",
            EdgeKind.DEPENDENCY,
            ref.source_entity,
            ref.direct_target_id,
            status,
            (id_,),
        )
    edges = tuple(
        sorted(
            context.edges.values(),
            key=lambda edge: (edge.source_id, edge.target_id, edge.kind, edge.id),
        )
    )
    containers = {
        stack.id: Container(
            stack.id,
            "stack",
            "Stack",
            (stack.parent_id,) if stack.parent_id else (),
            stack.condition_state,
            stack.source,
            stack.status,
            stack.input_format,
        )
        for stack in graph.stacks.values()
    }
    memberships = [
        Membership(node.stack_id, node.id, "stack", STATUS[node.state]) for node in nodes.values()
    ]
    parents = defaultdict(set)
    for edge in edges:
        if edge.kind is EdgeKind.CONTAINS and nodes[edge.source_id].type in {VPC, SUBNET}:
            memberships.append(
                Membership(
                    edge.source_id, edge.target_id, "network", edge.status, edge.evidence_ids
                )
            )
            if edge.status is EdgeStatus.CONFIRMED:
                parents[edge.target_id].add(edge.source_id)
    for node in nodes.values():
        if node.type in {VPC, SUBNET}:
            containers[node.id] = Container(
                node.id,
                "network",
                node.type.rsplit("::", 1)[-1],
                tuple(sorted(parents[node.id])),
                node.state,
                node.source,
            )
    diagnostics = (*graph.diagnostics, *context.diagnostics, *dependency_cycles(graph))
    return ArchitectureModel(
        1,
        nodes,
        edges,
        dict(sorted(containers.items())),
        tuple(memberships),
        dict(sorted(context.evidence.items())),
        graph.references,
        stack_references(graph),
        tuple(
            sorted(
                diagnostics,
                key=lambda item: (
                    item.entity_id or "",
                    item.source.template,
                    pointer(item.path),
                    item.code,
                    pointer(item.source.path),
                    item.source.line or 0,
                    item.source.column or 0,
                    item.message,
                ),
            )
        ),
    )


def _add_manual_relationship(context: RuleContext, relationship: ManualRelationship) -> None:
    if (
        relationship.kind not in MANUAL_EDGE_KINDS
        or relationship.source_id == relationship.target_id
    ):
        raise ValueError(
            "User-defined relationships require a supported kind and distinct endpoints."
        )
    start = context.graph.resources.get(relationship.source_id)
    end = context.graph.resources.get(relationship.target_id)
    source = relationship.source
    if start is None or end is None:
        context.diagnostics.add(
            Diagnostic(
                "RELATIONSHIP_RESOURCE_NOT_FOUND",
                "A user-defined relationship endpoint was not found.",
                source,
                source.path,
                relationship.source_id if start is None else relationship.target_id,
            )
        )
        return
    id_ = context.add_evidence(start.id, source.path, source, (), note=relationship.evidence)
    status = STATUS[conjunction(start.condition_state, end.condition_state)]
    context.add_edge(
        "configuration.relationship",
        relationship.kind,
        start.id,
        end.id,
        status,
        (id_,),
        origin=EdgeOrigin.USER_DEFINED,
    )


def _role(resource: Resource, by_type) -> NodeRole:
    if resource.type in PRIMARY:
        return (
            NodeRole.INVENTORY
            if resource.type == ALB and routing.alb_state(resource) is ConditionState.FALSE
            else NodeRole.PRIMARY
        )
    if resource.type in SUPPORT or resource.type in by_type:
        return NodeRole.SUPPORT
    if resource.type in INVENTORY:
        return NodeRole.INVENTORY
    return NodeRole.UNSUPPORTED
