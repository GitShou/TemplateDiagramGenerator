"""Project known AWS provider fields onto the semantic analyzer's AWS model."""

from dataclasses import replace

from .models import ResolvedValue, ValueKind
from .semantics.context import (
    ALB,
    API,
    BUCKET,
    CLOUDFRONT,
    DB_SUBNET_GROUP,
    DNS_RECORD,
    DYNAMODB,
    ECS,
    ECS_CLUSTER,
    ECS_TASK,
    EVENT_BUS,
    EVENT_MAPPING,
    INTEGRATION,
    LAMBDA,
    LISTENER,
    LISTENER_RULE,
    RDS,
    ROUTE,
    SCHEDULE,
    SECURITY_GROUP,
    SNS,
    SQS,
    STATE_MACHINE,
    SUBNET,
    TARGET_GROUP,
    VPC,
    symbol,
)

_TYPES = {
    "aws_apigatewayv2_api": API,
    "aws_apigatewayv2_integration": INTEGRATION,
    "aws_apigatewayv2_route": ROUTE,
    "aws_cloudfront_distribution": CLOUDFRONT,
    "aws_cloudwatch_event_bus": EVENT_BUS,
    "aws_db_instance": RDS,
    "aws_db_subnet_group": DB_SUBNET_GROUP,
    "aws_dynamodb_table": DYNAMODB,
    "aws_ecs_cluster": ECS_CLUSTER,
    "aws_ecs_service": ECS,
    "aws_ecs_task_definition": ECS_TASK,
    "aws_iam_role": "AWS::IAM::Role",
    "aws_lambda_event_source_mapping": EVENT_MAPPING,
    "aws_lambda_function": LAMBDA,
    "aws_lb": ALB,
    "aws_lb_listener": LISTENER,
    "aws_lb_listener_rule": LISTENER_RULE,
    "aws_lb_target_group": TARGET_GROUP,
    "aws_route53_record": DNS_RECORD,
    "aws_s3_bucket": BUCKET,
    "aws_s3_bucket_policy": "AWS::S3::BucketPolicy",
    "aws_scheduler_schedule": SCHEDULE,
    "aws_security_group": SECURITY_GROUP,
    "aws_sqs_queue": SQS,
    "aws_sns_topic": SNS,
    "aws_sfn_state_machine": STATE_MACHINE,
    "aws_subnet": SUBNET,
    "aws_vpc": VPC,
}

_ATTRIBUTES = {
    BUCKET: {
        "id": None,
        "bucket": None,
        "bucket_domain_name": "DomainName",
        "bucket_regional_domain_name": "RegionalDomainName",
    },
    CLOUDFRONT: {"id": None, "domain_name": "DomainName", "hosted_zone_id": "HostedZoneId"},
    ALB: {
        "id": "LoadBalancerArn",
        "arn": "LoadBalancerArn",
        "dns_name": "DNSName",
        "zone_id": "CanonicalHostedZoneID",
    },
    LAMBDA: {"id": None, "function_name": None, "invoke_arn": "InvokeArn"},
    API: {"id": None},
    INTEGRATION: {"id": None},
    VPC: {"id": None},
    SUBNET: {"id": None},
    SECURITY_GROUP: {"id": "GroupId"},
    DB_SUBNET_GROUP: {"name": None, "id": None},
    ECS_CLUSTER: {"id": None},
    LISTENER: {"id": "ListenerArn", "arn": "ListenerArn"},
    TARGET_GROUP: {"id": "TargetGroupArn", "arn": "TargetGroupArn"},
    ECS_TASK: {"arn": "TaskDefinitionArn"},
    DYNAMODB: {"name": None, "id": None},
}

_FIELDS = {
    BUCKET: {"BucketName": "bucket"},
    DYNAMODB: {"TableName": "name"},
    API: {"ProtocolType": "protocol_type"},
    INTEGRATION: {
        "ApiId": "api_id",
        "IntegrationType": "integration_type",
        "IntegrationSubtype": "integration_subtype",
        "IntegrationUri": "integration_uri",
    },
    ROUTE: {"ApiId": "api_id", "Target": "target"},
    EVENT_MAPPING: {
        "EventSourceArn": "event_source_arn",
        "FunctionName": "function_name",
        "Enabled": "enabled",
    },
    ALB: {"Type": "load_balancer_type", "SecurityGroups": "security_groups", "Subnets": "subnets"},
    LISTENER: {"LoadBalancerArn": "load_balancer_arn"},
    LISTENER_RULE: {"ListenerArn": "listener_arn"},
    TARGET_GROUP: {"VpcId": "vpc_id"},
    VPC: {},
    SUBNET: {"VpcId": "vpc_id"},
    SECURITY_GROUP: {"VpcId": "vpc_id"},
    ECS: {"Cluster": "cluster", "TaskDefinition": "task_definition"},
    RDS: {
        "DBSubnetGroupName": "db_subnet_group_name",
        "VPCSecurityGroups": "vpc_security_group_ids",
    },
    DB_SUBNET_GROUP: {"SubnetIds": "subnet_ids"},
    DNS_RECORD: {"Type": "type"},
    SCHEDULE: {"State": "state"},
}


def canonical_type(input_type: str) -> str:
    return _TYPES.get(input_type, input_type)


def canonical_attribute(input_type: str, attribute: str | None) -> str | None:
    known = _ATTRIBUTES.get(canonical_type(input_type), {})
    if attribute in known:
        return known[attribute]
    if attribute == "arn" and input_type in _TYPES:
        return "Arn"
    if input_type in _TYPES:
        return "TerraformObject" if attribute is None else f"terraform:{attribute}"
    return attribute


def _object(value, fields) -> ResolvedValue:
    return replace(
        value,
        kind=ValueKind.OBJECT,
        value={name: item for name, item in fields.items() if item is not None},
    )


def _select(value, mapping) -> ResolvedValue:
    return _object(value, {key: value.property(original) for key, original in mapping.items()})


def _array(value, project):
    if value is not None and value.kind is ValueKind.ARRAY:
        return replace(value, value=tuple(project(item) for item in value.value))
    return value


def _single_block(value, name):
    items = value.property(name)
    if items is not None and items.kind is ValueKind.ARRAY and len(items.value) == 1:
        return items.value[0]
    return None


def _origin(origin, types):
    domain = origin.property("domain_name")
    endpoint = symbol(domain)
    custom, vpc = (
        _single_block(origin, name) for name in ("custom_origin_config", "vpc_origin_config")
    )
    s3 = _single_block(origin, "s3_origin_config")
    if (
        origin.property("s3_origin_config") is None
        and origin.property("custom_origin_config") is None
        and origin.property("vpc_origin_config") is None
        and endpoint is not None
        and types.get(endpoint.resource_id) == "aws_s3_bucket"
        and endpoint.attribute in {"DomainName", "RegionalDomainName"}
    ):
        # Terraform allows an S3 origin with an OAC and no s3_origin_config block.
        s3 = _object(origin, {"OriginAccessControlId": origin.property("origin_access_control_id")})
    return _object(
        origin,
        {
            "Id": origin.property("origin_id"),
            "DomainName": domain,
            "CustomOriginConfig": custom,
            "S3OriginConfig": s3,
            "VpcOriginConfig": vpc,
        },
    )


def _cloudfront(value, types):
    def behavior(item):
        return _select(item, {"TargetOriginId": "target_origin_id"})

    default = _single_block(value, "default_cache_behavior")
    group_array = _array(value.property("origin_group"), _origin_group)
    return _object(
        value,
        {
            "DistributionConfig": _object(
                value,
                {
                    "Enabled": value.property("enabled"),
                    "Origins": _array(value.property("origin"), lambda item: _origin(item, types)),
                    "DefaultCacheBehavior": behavior(default) if default is not None else None,
                    "CacheBehaviors": _array(value.property("ordered_cache_behavior"), behavior),
                    "OriginGroups": _object(group_array, {"Items": group_array})
                    if group_array is not None
                    else None,
                },
            )
        },
    )


def _origin_group(group):
    members = _array(
        group.property("member"), lambda item: _select(item, {"OriginId": "origin_id"})
    )
    failover = _single_block(group, "failover_criteria")
    criteria = None
    if failover is not None:
        criteria = _object(
            failover,
            {"StatusCodes": _object(failover, {"Items": failover.property("status_codes")})},
        )
    return _object(
        group,
        {
            "Id": group.property("origin_id"),
            "SelectionCriteria": group.property("selection_criteria"),
            "FailoverCriteria": criteria,
            "Members": _object(group, {"Items": members}),
        },
    )


def _forward(item):
    fields = {"Type": item.property("type"), "TargetGroupArn": item.property("target_group_arn")}
    forward = _single_block(item, "forward")
    if forward is not None:
        groups = _array(
            forward.property("target_group"),
            lambda group: _select(group, {"TargetGroupArn": "arn", "Weight": "weight"}),
        )
        fields["ForwardConfig"] = _object(forward, {"TargetGroups": groups})
    return _object(item, fields)


def project_properties(
    input_type: str, value: ResolvedValue, types: dict[str, str]
) -> ResolvedValue:
    canonical = canonical_type(input_type)
    if canonical == CLOUDFRONT:
        return _cloudfront(value, types)
    if canonical not in _FIELDS:
        return value
    fields = _select(value, _FIELDS[canonical]).value
    if canonical == DNS_RECORD and (alias := _single_block(value, "alias")) is not None:
        fields["AliasTarget"] = _select(alias, {"DNSName": "name", "HostedZoneId": "zone_id"})
    if canonical == SCHEDULE and (target := _single_block(value, "target")) is not None:
        fields["Target"] = _select(target, {"Arn": "arn"})
    if canonical == ALB:
        fields["SubnetMappings"] = _array(
            value.property("subnet_mapping"), lambda item: _select(item, {"SubnetId": "subnet_id"})
        )
    if canonical == LISTENER:
        fields["DefaultActions"] = _array(value.property("default_action"), _forward)
    if canonical == LISTENER_RULE:
        fields["Actions"] = _array(value.property("action"), _forward)
    if canonical == ECS:
        fields["LoadBalancers"] = _array(
            value.property("load_balancer"),
            lambda item: _select(item, {"TargetGroupArn": "target_group_arn"}),
        )
        if (network := _single_block(value, "network_configuration")) is not None:
            fields["NetworkConfiguration"] = _object(
                network,
                {
                    "AwsvpcConfiguration": _select(
                        network, {"Subnets": "subnets", "SecurityGroups": "security_groups"}
                    )
                },
            )
    return _object(value, fields)
