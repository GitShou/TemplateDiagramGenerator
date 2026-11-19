"""Event delivery described by AWS resource properties."""

from ..architecture import EdgeKind
from ..models import ConditionState
from .context import EVENT_BUS, LAMBDA, SNS, SQS, STATE_MACHINE, Property, RuleContext
from .routing import _enabled


def eventbridge(ctx: RuleContext, resource) -> None:
    props = Property.root(resource)
    flag = props.get("State")
    state = ConditionState.TRUE
    if flag.literal() == "DISABLED":
        state = ConditionState.FALSE
    elif flag.value is not None and flag.literal() not in {
        "ENABLED",
        "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS",
    }:
        state = ConditionState.UNKNOWN
    for entry in props.get("Targets").entries():
        arn = entry.get("Arn")
        target = ctx.target(arn, {LAMBDA, SQS}, {"Arn"})
        if target is None:
            ctx.issue(
                "EVENTBRIDGE_TARGET",
                "EventBridge target ARN is outside the supported local targets.",
                arn,
            )
            continue
        ctx.add("eventbridge.target", EdgeKind.EVENT, resource, target, arn, flag, state=state)


def event_mapping(ctx: RuleContext, mapping) -> None:
    props = Property.root(mapping)
    queue_field, function_field, enabled = (
        props.get(key) for key in ("EventSourceArn", "FunctionName", "Enabled")
    )
    queue = ctx.target(queue_field, {SQS}, {"Arn"})
    function = ctx.target(function_field, {LAMBDA}, {None, "Arn"})
    if queue is None or function is None:
        ctx.issue(
            "EVENT_MAPPING_TARGET",
            "Event source mapping could not be bound to a supported SQS/Lambda pair.",
            queue_field if queue is None else function_field,
        )
        return
    ctx.add(
        "lambda.event_source_mapping",
        EdgeKind.EVENT,
        queue,
        function,
        queue_field,
        function_field,
        enabled,
        state=_enabled(enabled, ConditionState.TRUE),
    )


def scheduler(ctx: RuleContext, resource) -> None:
    props = Property.root(resource)
    flag = props.get("State")
    state = ConditionState.TRUE
    if flag.literal() == "DISABLED":
        state = ConditionState.FALSE
    elif flag.value is not None and flag.literal() != "ENABLED":
        state = ConditionState.UNKNOWN
    arn = props.get("Target", "Arn")
    endpoint = ctx.target(arn, {LAMBDA, SQS, STATE_MACHINE, SNS, EVENT_BUS}, {"Arn"})
    if endpoint is None:
        endpoint = ctx.target(arn, {STATE_MACHINE, SNS}, {None})
    if endpoint is None:
        ctx.issue(
            "SCHEDULER_TARGET",
            "Schedule target does not identify a supported local target ARN.",
            arn,
        )
        return
    ctx.add("scheduler.target", EdgeKind.EVENT, resource, endpoint, arn, flag, state=state)
