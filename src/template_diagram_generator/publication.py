"""Limit the architecture's serialized fields to public identifiers and structural data."""

import json
import re
from collections import Counter
from dataclasses import asdict, replace

from .architecture import ArchitectureModel
from .localization import diagnostic_text
from .models import PropertyPath, ReferenceStatus, SourceLocation, pointer

VERSION = "0.2.0"
_TYPE = re.compile(r"(?:[A-Za-z0-9]{1,64}::){1,2}[A-Za-z0-9]{1,64}\Z")
_SYMBOL = re.compile(r"[A-Za-z0-9]+\Z")
_ENTITY = re.compile(
    r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*"
    r"(?:#(?:[A-Za-z0-9]+|[A-Za-z_][A-Za-z0-9_-]*\.[A-Za-z_][A-Za-z0-9_-]*)"
    r"|@(?:parameter|condition|output|local|module)/[A-Za-z0-9_-]+)?\Z"
)
_HCL_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_LOOP = re.compile(r"Fn::ForEach::[A-Za-z][A-Za-z0-9]*\Z")
_LOOP_IDENTIFIER = re.compile(r"(?:[A-Za-z0-9]|[&$]\{[A-Za-z][A-Za-z0-9]*\})+\Z")
_ATTRIBUTES = {
    "ApiId",
    "Arn",
    "CanonicalHostedZoneID",
    "CidrBlock",
    "DNSName",
    "DistributionDomainName",
    "DistributionHostedZoneId",
    "DomainName",
    "GroupId",
    "IntegrationId",
    "InvokeArn",
    "Ipv6CidrBlocks",
    "HostedZoneId",
    "ListenerArn",
    "LoadBalancerArn",
    "QueueName",
    "RegionalDomainName",
    "RegionalHostedZoneId",
    "ResourceId",
    "RestApiId",
    "RootResourceId",
    "SubnetId",
    "TargetGroupArn",
    "VpcId",
}
_PATH_KEYS = frozenset(
    [
        "Actions",
        "ApiId",
        "Arn",
        "CacheBehaviors",
        "Cluster",
        "Condition",
        "Conditions",
        "CustomOriginConfig",
        "DBSubnetGroupName",
        "Default",
        "DefaultCacheBehavior",
        "DefaultActions",
        "DefaultValue",
        "DependsOn",
        "DistributionConfig",
        "DomainName",
        "Enabled",
        "EventSourceArn",
        "ForwardConfig",
        "FunctionName",
        "Id",
        "IntegrationSubtype",
        "IntegrationType",
        "IntegrationUri",
        "ListenerArn",
        "LoadBalancerArn",
        "LoadBalancers",
        "Mappings",
        "NetworkConfiguration",
        "NoEcho",
        "Origins",
        "Outputs",
        "Parameters",
        "Properties",
        "ProtocolType",
        "Ref",
        "Resources",
        "S3OriginConfig",
        "SecurityGroups",
        "State",
        "SubnetId",
        "SubnetIds",
        "SubnetMappings",
        "Subnets",
        "Target",
        "TargetGroupArn",
        "TargetGroups",
        "TargetOriginId",
        "Targets",
        "TaskDefinition",
        "TemplateURL",
        "Transform",
        "Type",
        "Value",
        "VPCSecurityGroups",
        "VpcId",
        "VpcOriginConfig",
        "Weight",
        "AwsvpcConfiguration",
        "Fn::And",
        "Fn::Cidr",
        "Fn::Equals",
        "Fn::FindInMap",
        "Fn::GetAtt",
        "Fn::If",
        "Fn::Join",
        "Fn::Not",
        "Fn::Or",
        "Fn::Select",
        "Fn::Sub",
    ]
) | frozenset(
    {
        "AliasTarget",
        "Arguments",
        "Branches",
        "Credentials",
        "Definition",
        "DefinitionString",
        "DefinitionSubstitutions",
        "DefinitionS3Location",
        "DNSName",
        "Export",
        "Fn::ImportValue",
        "ActionsEnabled",
        "AccountId",
        "AlarmActions",
        "CidrBlock",
        "Dimensions",
        "Events",
        "Function",
        "Globals",
        "GroupSet",
        "InsufficientDataActions",
        "Metric",
        "Metrics",
        "MetricStat",
        "Namespace",
        "NetworkInterfaces",
        "OKActions",
        "Queue",
        "SecurityGroupIds",
        "VpcConfig",
        "HostedZoneId",
        "Integration",
        "IntegrationHttpMethod",
        "ItemProcessor",
        "Iterator",
        "Name",
        "RecordSets",
        "Resource",
        "ResourceId",
        "RestApiId",
        "States",
        "TableName",
        "TableName.$",
        "Uri",
        "evidence",
        "relationships",
        "resources",
        "views",
    }
)


_TERRAFORM_FIELDS = frozenset(
    [
        "resource",
        "module",
        "locals",
        "variable",
        "output",
        "default",
        "value",
        "source",
        "for_each",
        "count",
        "depends_on",
        "enabled",
        "origin",
        "origin_group",
        "origin_id",
        "origin_access_control_id",
        "domain_name",
        "s3_origin_config",
        "vpc_origin_config",
        "custom_origin_config",
        "default_cache_behavior",
        "ordered_cache_behavior",
        "target_origin_id",
        "type",
        "alias",
        "name",
        "zone_id",
        "protocol_type",
        "api_id",
        "target",
        "integration_type",
        "integration_subtype",
        "integration_uri",
        "event_source_arn",
        "function_name",
        "vpc_id",
        "load_balancer_type",
        "subnets",
        "subnet_id",
        "subnet_mapping",
        "security_groups",
        "load_balancer_arn",
        "default_action",
        "listener_arn",
        "action",
        "forward",
        "target_group",
        "target_group_arn",
        "arn",
        "weight",
        "load_balancer",
        "network_configuration",
        "db_subnet_group_name",
        "subnet_ids",
        "vpc_security_group_ids",
        "bucket",
        "failover_criteria",
        "status_codes",
        "member",
        "selection_criteria",
        "state",
    ]
)
_ORIGIN_GROUP_FIELDS = {
    "OriginGroups",
    "OriginId",
    "Items",
    "Members",
    "SelectionCriteria",
    "FailoverCriteria",
    "StatusCodes",
}


def _path(path: PropertyPath) -> PropertyPath:
    result = []
    for index, part in enumerate(path):
        if (
            isinstance(part, int)
            or part in _PATH_KEYS
            or part in _ORIGIN_GROUP_FIELDS
            or path[0] in {"resource", "module", "variable", "output", "locals"}
            and (
                part in _TERRAFORM_FIELDS
                or index in ((1, 2) if path[0] == "resource" else (1,))
                and _HCL_ID.fullmatch(part)
            )
            or _LOOP.fullmatch(part)
            or index == 2
            and path[:2] == ("views", "resources")
            and _ENTITY.fullmatch(part)
            or index == 1
            and path[0] in {"Resources", "Parameters", "Conditions", "Outputs"}
            and _LOOP_IDENTIFIER.fullmatch(part)
            or index > 1
            and isinstance(path[index - 1], int)
            and path[index - 1] == 2
            and (
                isinstance(path[index - 2], str)
                and _LOOP.fullmatch(path[index - 2])
                and _LOOP_IDENTIFIER.fullmatch(part)
            )
        ):
            result.append(part)
        else:
            result.append("[redacted]")
    return tuple(result)


def _source(source: SourceLocation) -> SourceLocation:
    return replace(source, path=_path(source.path))


def _id(id_: str | None) -> str | None:
    if (
        id_ is None
        or _ENTITY.fullmatch(id_)
        or id_
        in {
            "AWS::NoValue",
            "AWS::Region",
            "AWS::Partition",
            "AWS::AccountId",
            "AWS::StackName",
            "AWS::StackId",
            "AWS::URLSuffix",
            "AWS::NotificationARNs",
        }
    ):
        return id_
    return "[redacted]"


def _attribute(attribute: str | None) -> str | None:
    if (
        attribute is None
        or attribute in _ATTRIBUTES
        or (
            attribute.startswith("Outputs.")
            and _SYMBOL.fullmatch(attribute.removeprefix("Outputs."))
        )
    ):
        return attribute
    return "[redacted]"


def public_model(model: ArchitectureModel) -> ArchitectureModel:
    # ArchitectureModel carries no property, parameter, output, AST or description values.
    return replace(
        model,
        nodes={
            key: replace(
                node,
                type=node.type
                if _TYPE.fullmatch(node.type)
                or node.input_type == node.type
                and _HCL_ID.fullmatch(node.type)
                else "[redacted]",
                input_type=node.input_type
                if node.input_type is None
                or _HCL_ID.fullmatch(node.input_type)
                or _TYPE.fullmatch(node.input_type)
                else "[redacted]",
                source=_source(node.source),
            )
            for key, node in model.nodes.items()
        },
        containers={
            key: replace(item, source=_source(item.source))
            for key, item in model.containers.items()
        },
        evidence={
            key: replace(
                item,
                source=_source(item.source),
                path=_path(item.path),
                entity_id=_id(item.entity_id),
            )
            for key, item in model.evidence.items()
        },
        references={
            key: replace(
                item,
                source=_source(item.source),
                path=_path(item.path),
                source_entity=_id(item.source_entity),
                direct_target_id=_id(item.direct_target_id),
                direct_target=_id(item.direct_target),
                attribute=_attribute(item.attribute),
            )
            for key, item in model.references.items()
        },
        diagnostics=tuple(
            replace(
                item,
                source=_source(item.source),
                path=_path(item.path),
                entity_id=_id(item.entity_id),
            )
            for item in model.diagnostics
        ),
    )


def metadata(model: ArchitectureModel) -> dict:
    stacks = [stack for stack in model.containers.values() if stack.axis == "stack"]
    result = {
        "generator_version": VERSION,
        "roots": [stack.id for stack in stacks if not stack.parent_ids],
        "templates": sorted({stack.source.template for stack in stacks}),
        "stack_status": {stack.id: stack.stack_status for stack in stacks},
    }
    if any(stack.input_format == "terraform" for stack in stacks):
        result["input_formats"] = {
            stack.id: stack.input_format or "cloudformation" for stack in stacks
        }
        files = {
            node.source.template
            for node in model.nodes.values()
            if result["input_formats"].get(node.stack_id) == "terraform"
        }
        result["templates"] = sorted(files | set(result["templates"]))
    if missing := missing_parameters(model):
        result["missing_parameters"] = missing
    return result


def missing_parameters(model: ArchitectureModel) -> dict[str, int]:
    """Count affected references once per missing parameter, including alias traces."""
    missing = {
        item.entity_id
        for item in model.diagnostics
        if item.code == "PARAMETER_MISSING" and item.entity_id not in {None, "[redacted]"}
    }
    counts = Counter(dict.fromkeys(missing, 0))
    for ref in model.references.values():
        if ref.status not in {ReferenceStatus.UNRESOLVED, ReferenceStatus.CONDITIONAL}:
            continue
        targets = {ref.direct_target_id}
        targets.update(
            model.references[key].direct_target_id for key in ref.trace if key in model.references
        )
        counts.update(missing & targets)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def architecture_json(model: ArchitectureModel, *, diagnostic_locale: str | None = None) -> str:
    content = asdict(model)
    content["metadata"] = metadata(model)
    if diagnostic_locale is not None:
        content["metadata"]["diagnostic_locale"] = diagnostic_locale
        for item, diagnostic in zip(content["diagnostics"], model.diagnostics, strict=True):
            item["message_localized"] = diagnostic_text(diagnostic_locale, diagnostic)
    for node in content["nodes"].values():
        if node["input_type"] is None:
            del node["input_type"]
    for item in content["containers"].values():
        if item["input_format"] is None:
            del item["input_format"]
    for item in (
        *content["evidence"].values(),
        *content["references"].values(),
        *content["diagnostics"],
    ):
        item["path"] = pointer(item["path"])
    for item in (
        *content["nodes"].values(),
        *content["containers"].values(),
        *content["evidence"].values(),
        *content["references"].values(),
        *content["diagnostics"],
    ):
        item["source"]["path"] = pointer(item["source"]["path"])
    return json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
