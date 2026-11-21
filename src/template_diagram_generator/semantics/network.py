"""VPC and subnet placement from declared, typed attachments."""

from ..architecture import EdgeKind
from ..models import ConditionState, ValueKind, conjunction
from .context import (
    ALB,
    DB_SUBNET_GROUP,
    ECS,
    ECS_CLUSTER,
    ECS_TASK,
    SECURITY_GROUP,
    SUBNET,
    VPC,
    Property,
    RuleContext,
)
from .routing import alb_state


def subnet(ctx: RuleContext, resource) -> None:
    vpc = Property.root(resource).get("VpcId")
    owner = ctx.target(vpc, {VPC}, {"VpcId", None})
    if owner is None:
        ctx.issue("SUBNET_VPC", "Subnet VPC is not a supported local VPC reference.", vpc)
        return
    ctx.add("network.subnet_vpc", EdgeKind.CONTAINS, owner, resource, vpc)


def workload(ctx: RuleContext, resource) -> None:
    fields = Property.root(resource)
    through = ()
    state = ConditionState.TRUE
    if resource.type == ALB:
        state = alb_state(resource)
        if state is ConditionState.FALSE:
            return
        list_prop = fields.get("Subnets")
        mappings = fields.get("SubnetMappings")
        if list_prop.value is not None and mappings.value is not None:
            ctx.issue(
                "WORKLOAD_SUBNETS",
                "Load balancer declares both subnet attachment forms.",
                list_prop,
            )
            return
        if list_prop.value is None:
            list_prop = mappings
            entries = tuple(item.get("SubnetId") for item in mappings.entries())
        else:
            entries = list_prop.entries()
        security_groups = fields.get("SecurityGroups")
    elif resource.type == ECS:
        network = fields.get("NetworkConfiguration", "AwsvpcConfiguration")
        list_prop = network.get("Subnets")
        entries = list_prop.entries()
        security_groups = network.get("SecurityGroups")
        _associate(ctx, resource, fields.get("Cluster"), {ECS_CLUSTER}, {None, "Arn"})
        _associate(
            ctx, resource, fields.get("TaskDefinition"), {ECS_TASK}, {None, "TaskDefinitionArn"}
        )
    else:
        group_field = fields.get("DBSubnetGroupName")
        if group_field.value is None:
            return
        group = ctx.target(group_field, {DB_SUBNET_GROUP}, {None})
        if group is None:
            ctx.issue(
                "RDS_SUBNET_GROUP", "RDS subnet group could not be resolved locally.", group_field
            )
            return
        through = (group_field,)
        list_prop = Property.root(group).get("SubnetIds")
        entries = list_prop.entries()
        security_groups = fields.get("VPCSecurityGroups")
        ctx.add("network.rds_subnet_group", EdgeKind.ASSOCIATION, resource, group, group_field)
    for prop in security_groups.entries():
        _associate(ctx, resource, prop, {SECURITY_GROUP}, {"GroupId", None})
    if list_prop.value is None:
        return
    if list_prop.value.kind is not ValueKind.ARRAY or not entries:
        ctx.issue(
            "WORKLOAD_SUBNETS", "Workload subnet list could not be resolved locally.", list_prop
        )
        return

    subnet_ids = set()
    vpcs = set()
    placement_evidence = []
    complete = True
    for entry in entries:
        attached = ctx.target(entry, {SUBNET}, {None, "SubnetId"})
        if attached is None:
            ctx.issue("WORKLOAD_SUBNET", "Workload subnet could not be resolved locally.", entry)
            complete = False
            continue
        subnet_ids.add(attached.id)
        ctx.add(
            "network.subnet_attachment",
            EdgeKind.ASSOCIATION,
            resource,
            attached,
            *through,
            entry,
            state=state,
        )
        vpc = Property.root(attached).get("VpcId")
        owner = ctx.target(vpc, {VPC}, {None, "VpcId"})
        if owner is None:
            complete = False
            continue
        vpcs.add(owner.id)
        placement_evidence.extend((entry, vpc))
        state = conjunction(state, attached.condition_state)
    if complete and len(vpcs) == 1:
        owner = ctx.graph.resources[next(iter(vpcs))]
        ctx.add(
            "network.workload_vpc",
            EdgeKind.CONTAINS,
            owner,
            resource,
            *through,
            *placement_evidence,
            state=state,
        )
        if resource.type == ECS and len(subnet_ids) == 1:
            attached = ctx.graph.resources[next(iter(subnet_ids))]
            ctx.add(
                "network.workload_subnet",
                EdgeKind.CONTAINS,
                attached,
                resource,
                *through,
                *placement_evidence,
                state=state,
            )
    elif len(vpcs) > 1:
        ctx.issue(
            "WORKLOAD_VPC_AMBIGUOUS", "Workload subnets point to more than one VPC.", list_prop
        )


def _associate(ctx, resource, prop, types, attributes) -> None:
    if prop.value is None:
        return
    target = ctx.target(prop, types, attributes)
    if target is None:
        ctx.issue("NETWORK_ASSOCIATION", "Association target could not be resolved locally.", prop)
        return
    ctx.add("network.association", EdgeKind.ASSOCIATION, resource, target, prop)


def lambda_vpc(ctx: RuleContext, resource) -> None:
    vpc = Property.root(resource).get("VpcConfig")
    for prop in vpc.get("SubnetIds").entries():
        _associate(ctx, resource, prop, {SUBNET}, {"SubnetId", None})
    for prop in vpc.get("SecurityGroupIds").entries():
        _associate(ctx, resource, prop, {SECURITY_GROUP}, {"GroupId", None})


def ec2_network(ctx: RuleContext, resource) -> None:
    props = Property.root(resource)
    attachments = (props, *props.get("NetworkInterfaces").entries())
    for attachment in attachments:
        _associate(ctx, resource, attachment.get("SubnetId"), {SUBNET}, {"SubnetId", None})
        group_key = "SecurityGroupIds" if attachment is props else "GroupSet"
        for group in attachment.get(group_key).entries():
            _associate(ctx, resource, group, {SECURITY_GROUP}, {"GroupId", None})


def vpc_scoped(ctx: RuleContext, resource) -> None:
    vpc = Property.root(resource).get("VpcId")
    if vpc.value is None:
        return
    owner = ctx.target(vpc, {VPC}, {"VpcId", None})
    if owner is None:
        ctx.issue("RESOURCE_VPC", "Resource VPC could not be resolved locally.", vpc)
        return
    ctx.add("network.resource_vpc", EdgeKind.CONTAINS, owner, resource, vpc)


def listener_vpc(ctx: RuleContext, listener) -> None:
    binding = Property.root(listener).get("LoadBalancerArn")
    lb = ctx.target(binding, {ALB}, {None, "LoadBalancerArn"})
    if lb is None or alb_state(lb) is not ConditionState.TRUE:
        return
    fields = Property.root(lb)
    subnets = fields.get("Subnets")
    mappings = fields.get("SubnetMappings")
    if subnets.value is not None and mappings.value is not None:
        return
    attachments = (
        subnets.entries()
        if subnets.value is not None
        else tuple(item.get("SubnetId") for item in mappings.entries())
    )
    owners = set()
    proof = [binding]
    for entry in attachments:
        attached = ctx.target(entry, {SUBNET}, {None, "SubnetId"})
        if attached is None:
            return
        vpc = Property.root(attached).get("VpcId")
        owner = ctx.target(vpc, {VPC}, {None, "VpcId"})
        if owner is None:
            return
        owners.add(owner.id)
        proof.extend((entry, vpc))
    if len(owners) == 1:
        owner = ctx.graph.resources[next(iter(owners))]
        ctx.add("network.listener_vpc", EdgeKind.CONTAINS, owner, listener, *proof)
