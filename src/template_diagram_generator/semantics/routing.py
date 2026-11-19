"""Explicit CloudFront, HTTP API and ALB path rules."""

import re
from collections import defaultdict

from ..architecture import EdgeKind
from ..models import ConditionState, ResolvedValue, ValueKind, conjunction
from .context import (
    ALB,
    API,
    BUCKET,
    INTEGRATION,
    LAMBDA,
    LISTENER,
    REST_API,
    REST_RESOURCE,
    TARGET_GROUP,
    Property,
    RuleContext,
    symbol,
)

_LAMBDA_INVOKE_URI = re.compile(
    r"arn:(?:[a-z0-9-]+|<AWS::Partition>):apigateway:(?:[a-z0-9-]+|<AWS::Region>):"
    r"lambda:path/2015-03-31/functions/<function>/invocations\Z"
)


def _enabled(prop: Property, default: ConditionState = ConditionState.UNKNOWN) -> ConditionState:
    value = prop.literal()
    if prop.value is None:
        return default
    if type(value) is bool:
        return ConditionState.TRUE if value else ConditionState.FALSE
    return ConditionState.UNKNOWN


def alb_state(resource) -> ConditionState:
    kind = Property.root(resource).get("Type")
    if kind.value is None or kind.literal() == "application":
        return ConditionState.TRUE
    return ConditionState.UNKNOWN if kind.literal() is None else ConditionState.FALSE


def cloudfront(ctx: RuleContext, resource) -> None:
    config = Property.root(resource).get("DistributionConfig")
    origins = _cloudfront_entries(config.get("Origins"))
    groups = _cloudfront_entries(config.get("OriginGroups", "Items"))
    choices = (config.get("DefaultCacheBehavior"), *config.get("CacheBehaviors").entries())
    enabled = config.get("Enabled")
    for behavior in choices:
        if behavior.value is None:
            continue
        chosen = behavior.get("TargetOriginId")
        origin_id = chosen.literal()
        direct = origins.get(origin_id, ()) if isinstance(origin_id, str) else ()
        grouped = groups.get(origin_id, ()) if isinstance(origin_id, str) else ()
        if len(direct) + len(grouped) != 1:
            ctx.issue(
                "CLOUDFRONT_ORIGIN",
                "Cache behavior does not select a unique supported local origin.",
                chosen,
            )
            continue
        if direct:
            _cloudfront_link(ctx, resource, direct[0], enabled, chosen)
        else:
            _cloudfront_group(ctx, resource, origins, grouped[0], enabled, chosen)


def _cloudfront_entries(items: Property):
    result = defaultdict(list)
    for item in items.entries():
        key = item.get("Id").literal()
        if isinstance(key, str):
            result[key].append(item)
    return result


_FAILOVER_STATUS = {400, 403, 404, 416, 429, 500, 502, 503, 504}


def _cloudfront_group(ctx, resource, origins, group, enabled, chosen):
    selector = group.get("SelectionCriteria")
    if selector.value is not None and selector.literal() != "default":
        ctx.issue(
            "CLOUDFRONT_GROUP_SELECTION",
            "Origin group does not use a static primary/failover selection.",
            selector,
        )
        return
    statuses = group.get("FailoverCriteria", "StatusCodes", "Items")
    codes = [code.literal() for code in statuses.entries()]
    if not codes or any(type(code) is not int or code not in _FAILOVER_STATUS for code in codes):
        ctx.issue(
            "CLOUDFRONT_GROUP_CRITERIA",
            "Origin group failover status codes are missing or unsupported.",
            statuses,
        )
        return
    members = group.get("Members", "Items")
    ids = [item.get("OriginId") for item in members.entries()]
    names = [item.literal() for item in ids]
    if (
        len(names) != 2
        or any(not isinstance(name, str) or len(origins.get(name, ())) != 1 for name in names)
        or names[0] == names[1]
    ):
        ctx.issue(
            "CLOUDFRONT_GROUP_MEMBERS",
            "Origin group needs two distinct, unambiguous origins.",
            members,
        )
        return
    for name, member, role in zip(names, ids, ("Primary origin", "Failover origin"), strict=True):
        _cloudfront_link(
            ctx,
            resource,
            origins[name][0],
            enabled,
            chosen,
            group.get("Id"),
            member,
            *statuses.entries(),
            selector,
            operation=role,
        )


def _cloudfront_link(ctx, resource, origin, enabled, *proof, operation=None):
    domain, s3, custom, vpc = (
        origin.get(key)
        for key in ("DomainName", "S3OriginConfig", "CustomOriginConfig", "VpcOriginConfig")
    )
    endpoint = None
    state = _enabled(enabled)
    if (
        vpc.value is None
        and custom.value is None
        and s3.value is not None
        and s3.value.kind is ValueKind.OBJECT
    ):
        endpoint = ctx.target(domain, {BUCKET}, {"DomainName", "RegionalDomainName"})
        config_kind = s3
    elif (
        vpc.value is None
        and s3.value is None
        and custom.value is not None
        and custom.value.kind is ValueKind.OBJECT
    ):
        endpoint = ctx.target(domain, {ALB}, {"DNSName"})
        config_kind = custom
        if endpoint is not None:
            availability = alb_state(endpoint)
            state = conjunction(state, availability)
            if availability is ConditionState.FALSE:
                endpoint = None
    if endpoint is None:
        ctx.issue(
            "CLOUDFRONT_GROUP_TARGET" if operation else "CLOUDFRONT_TARGET",
            "Selected origin endpoint does not match a supported local resource.",
            domain,
        )
        return
    ctx.add(
        "cloudfront.origin_group" if operation else "cloudfront.origin",
        EdgeKind.TRAFFIC,
        resource,
        endpoint,
        *proof,
        origin.get("Id"),
        enabled,
        config_kind,
        domain,
        state=state,
        operation=operation,
    )


def _route_integration(value: ResolvedValue | None):
    if value is not None and value.kind is ValueKind.COMPOSITE and len(value.value) == 2:
        prefix, id_ = value.value
        if prefix.kind is ValueKind.LITERAL and prefix.value == "integrations/":
            return symbol(id_)
    return None


def _lambda_uri(value: ResolvedValue | None, *, allow_direct: bool = True):
    if allow_direct and (direct := symbol(value)) is not None:
        return direct
    if value is None or value.kind is not ValueKind.COMPOSITE:
        return None
    parts = []
    endpoint = None
    for part in value.value:
        if part.kind is ValueKind.LITERAL and isinstance(part.value, str):
            if "<" in part.value or ">" in part.value:
                return None
            parts.append(part.value)
        elif isinstance(part.value, str) and part.reason == "PSEUDO_PARAMETER":
            parts.append(f"<{part.value}>")
        elif endpoint is None and (target := symbol(part)) is not None:
            endpoint = target
            parts.append("<function>")
        else:
            return None
    return endpoint if _LAMBDA_INVOKE_URI.fullmatch("".join(parts)) else None


def rest_api(ctx: RuleContext, method) -> None:
    fields = Property.root(method)
    integration = fields.get("Integration")
    if integration.value is None:
        return
    kind = integration.get("Type")
    if kind.literal() not in {"AWS", "AWS_PROXY"}:
        return
    uri = integration.get("Uri")
    # REST AWS integrations can call many services. Only an exact Lambda invocation
    # URI with a typed Lambda ARN establishes this relationship.
    endpoint = ctx.match(_lambda_uri(uri.value, allow_direct=False), {LAMBDA}, {"Arn"})
    if endpoint is None:
        ctx.issue(
            "REST_LAMBDA_TARGET",
            "REST AWS integration has no supported local Lambda invocation URI.",
            uri,
        )
        return
    http = integration.get("IntegrationHttpMethod")
    if http.literal() != "POST":
        ctx.issue(
            "REST_LAMBDA_METHOD",
            "REST Lambda integration requires a POST integration method.",
            http,
        )
        return
    api_id = fields.get("RestApiId")
    api = ctx.target(api_id, {REST_API}, {None, "RestApiId"})
    resource_id = fields.get("ResourceId")
    if api is None:
        ctx.issue("REST_API_OWNER", "REST method API could not be resolved locally.", api_id)
        return
    proof = [api_id, resource_id, kind, http, uri]
    if ctx.target(resource_id, {REST_API}, {"RootResourceId"}) != api:
        resource = ctx.target(resource_id, {REST_RESOURCE}, {None, "ResourceId"})
        resource_owner = Property.root(resource).get("RestApiId") if resource else None
        if (
            resource_owner is None
            or ctx.target(resource_owner, {REST_API}, {None, "RestApiId"}) != api
        ):
            ctx.issue(
                "REST_RESOURCE_OWNER",
                "REST method resource is unresolved or belongs to a different API.",
                resource_id,
            )
            return
        proof.append(resource_owner)
    ctx.add("apigateway.rest_lambda", EdgeKind.TRAFFIC, api, method, *proof)
    ctx.add("apigateway.rest_lambda", EdgeKind.TRAFFIC, method, endpoint, *proof)


def http_api(ctx: RuleContext, route) -> None:
    properties = Property.root(route)
    owner = properties.get("ApiId")
    api = ctx.target(owner, {API}, {None, "ApiId"})
    target = properties.get("Target")
    if target.value is None:
        return
    integration = ctx.match(
        _route_integration(target.value), {INTEGRATION}, {None, "IntegrationId"}
    )
    if api is None or integration is None:
        ctx.issue(
            "API_ROUTE_TARGET", "Route could not be bound to a local API and integration.", target
        )
        return
    protocol = Property.root(api).get("ProtocolType")
    if protocol.literal() != "HTTP":
        ctx.issue("API_PROTOCOL", "The route is outside the supported HTTP API scope.", protocol)
        return
    integration_properties = Property.root(integration)
    integration_owner = integration_properties.get("ApiId")
    if ctx.target(integration_owner, {API}, {None, "ApiId"}) != api:
        ctx.issue(
            "API_OWNER_MISMATCH",
            "Route and integration do not resolve to the same API.",
            integration_owner,
        )
        return
    kind = integration_properties.get("IntegrationType")
    subtype = integration_properties.get("IntegrationSubtype")
    uri = integration_properties.get("IntegrationUri")
    if kind.literal() != "AWS_PROXY" or subtype.value is not None:
        ctx.issue(
            "API_INTEGRATION_TYPE", "Integration is outside the supported Lambda proxy scope.", kind
        )
        return
    endpoint = ctx.match(_lambda_uri(uri.value), {LAMBDA}, {"Arn"})
    if endpoint is None:
        invocation = ctx.target(uri, {LAMBDA}, {"InvokeArn"})
        if invocation is not None and invocation.input_type == "aws_lambda_function":
            endpoint = invocation
    if endpoint is None:
        ctx.issue(
            "API_LAMBDA_TARGET",
            "Integration URI is not a supported local Lambda ARN expression.",
            uri,
        )
        return
    evidence = (owner, target, protocol, integration_owner, kind, uri)
    ctx.add("apigatewayv2.lambda_integration", EdgeKind.TRAFFIC, api, route, *evidence)
    ctx.add("apigatewayv2.lambda_integration", EdgeKind.TRAFFIC, route, integration, *evidence)
    ctx.add("apigatewayv2.lambda_integration", EdgeKind.TRAFFIC, integration, endpoint, *evidence)


def alb_listener(ctx: RuleContext, listener) -> None:
    properties = Property.root(listener)
    binding = properties.get("LoadBalancerArn")
    lb = ctx.target(binding, {ALB}, {None, "LoadBalancerArn"})
    if lb is None:
        ctx.issue(
            "ALB_LISTENER_TARGET", "Listener load balancer could not be resolved locally.", binding
        )
        return
    state = alb_state(lb)
    if state is ConditionState.FALSE:
        return
    ctx.add(
        "elbv2.listener",
        EdgeKind.TRAFFIC,
        lb,
        listener,
        binding,
        Property.root(lb).get("Type"),
        state=state,
    )
    _forward(ctx, listener, properties.get("DefaultActions"), (binding,), state)


def alb_listener_rule(ctx: RuleContext, rule) -> None:
    properties = Property.root(rule)
    listener_ref = properties.get("ListenerArn")
    listener = ctx.target(listener_ref, {LISTENER}, {None, "ListenerArn"})
    if listener is None:
        ctx.issue(
            "ALB_RULE_LISTENER",
            "Listener rule could not be bound to a local listener.",
            listener_ref,
        )
        return
    binding = Property.root(listener).get("LoadBalancerArn")
    lb = ctx.target(binding, {ALB}, {None, "LoadBalancerArn"})
    if lb is None:
        return
    state = alb_state(lb)
    if state is ConditionState.FALSE:
        return
    forward = _forward(ctx, rule, properties.get("Actions"), (listener_ref, binding), state)
    if forward:
        ctx.add(
            "elbv2.listener_rule",
            EdgeKind.TRAFFIC,
            listener,
            rule,
            listener_ref,
            binding,
            state=state,
        )


def _forward(ctx, source, actions, evidence, state) -> bool:
    found = False
    for action in actions.entries():
        kind = action.get("Type")
        if kind.literal() != "forward":
            if kind.literal() is None:
                ctx.issue("ALB_ACTION", "Listener action type is unresolved.", kind)
            continue
        direct = action.get("TargetGroupArn")
        weighted = action.get("ForwardConfig", "TargetGroups")
        if weighted.value is not None and weighted.value.kind is not ValueKind.ARRAY:
            ctx.issue(
                "ALB_TARGET_GROUP", "ForwardConfig target groups could not be resolved.", weighted
            )
            continue
        targets = weighted.entries()
        if (
            direct.value is not None
            and targets
            and (
                len(targets) != 1
                or symbol(direct.value) is None
                or symbol(direct.value) != symbol(targets[0].get("TargetGroupArn").value)
            )
        ):
            ctx.issue(
                "ALB_TARGET_GROUP", "Forward action declares conflicting target groups.", direct
            )
            continue
        configured = [(item.get("TargetGroupArn"), item.get("Weight")) for item in targets]
        for target, weight in configured or [(direct, None)]:
            group = ctx.target(target, {TARGET_GROUP}, {None, "TargetGroupArn"})
            if group is None:
                ctx.issue(
                    "ALB_TARGET_GROUP",
                    "Forward target group could not be resolved locally.",
                    target,
                )
                continue
            active = state
            if weight is not None and weight.value is not None:
                number = weight.literal()
                if type(number) is int and number == 0:
                    continue
                if type(number) is not int or number < 0:
                    active = conjunction(active, ConditionState.UNKNOWN)
            props = (
                (*evidence, kind, target) if weight is None else (*evidence, kind, target, weight)
            )
            ctx.add("elbv2.forward", EdgeKind.TRAFFIC, source, group, *props, state=active)
            found = True
    return found


def ecs_target(ctx: RuleContext, service) -> None:
    for entry in Property.root(service).get("LoadBalancers").entries():
        arn = entry.get("TargetGroupArn")
        if arn.value is None:
            continue
        group = ctx.target(arn, {TARGET_GROUP}, {None, "TargetGroupArn"})
        if group is None:
            ctx.issue("ECS_TARGET_GROUP", "ECS target group could not be resolved locally.", arn)
            continue
        ctx.add("ecs.target_group", EdgeKind.TRAFFIC, group, service, arn)
