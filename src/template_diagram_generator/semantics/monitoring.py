"""CloudWatch metric dimensions and alarm action targets."""

from ..architecture import EdgeKind
from ..models import ConditionState
from .context import DYNAMODB, EC2, LAMBDA, RDS, SNS, SQS, Property, RuleContext
from .routing import _enabled

_DIMENSIONS = {
    ("AWS/DynamoDB", "TableName"): (DYNAMODB, {None}),
    ("AWS/EC2", "InstanceId"): (EC2, {None}),
    ("AWS/Lambda", "FunctionName"): (LAMBDA, {None}),
    ("AWS/RDS", "DBInstanceIdentifier"): (RDS, {None}),
    ("AWS/SQS", "QueueName"): (SQS, {"QueueName"}),
}


def metric_alarm(ctx: RuleContext, alarm) -> None:
    props = Property.root(alarm)
    _metric(ctx, alarm, props)
    for query in props.get("Metrics").entries():
        if query.get("AccountId").value is None:
            _metric(ctx, alarm, query.get("MetricStat", "Metric"))

    enabled = props.get("ActionsEnabled")
    state = _enabled(enabled, ConditionState.TRUE)
    for key, transition in (
        ("AlarmActions", "ALARM"),
        ("OKActions", "OK"),
        ("InsufficientDataActions", "INSUFFICIENT_DATA"),
    ):
        for action in props.get(key).entries():
            target = ctx.target(action, {LAMBDA, SNS}, {"Arn"}) or ctx.target(action, {SNS}, {None})
            if target is not None:
                ctx.add(
                    "cloudwatch.alarm_action",
                    EdgeKind.EVENT,
                    alarm,
                    target,
                    action,
                    enabled,
                    state=state,
                    operation=transition,
                )


def _metric(ctx: RuleContext, alarm, metric: Property) -> None:
    namespace = metric.get("Namespace")
    name = namespace.literal()
    if not isinstance(name, str):
        return
    for dimension in metric.get("Dimensions").entries():
        key, value = dimension.get("Name"), dimension.get("Value")
        dimension_name = key.literal()
        if not isinstance(dimension_name, str) or (name, dimension_name) not in _DIMENSIONS:
            continue
        type_, attributes = _DIMENSIONS[name, dimension_name]
        target = ctx.target(value, {type_}, attributes)
        proof = (namespace, key, value)
        if target is None and type_ != EC2:
            target, declared = ctx.named_local(value, type_, dimension_name)
            if declared is not None:
                proof = (*proof, declared)
        if target is not None:
            ctx.add(
                "cloudwatch.monitored_resource",
                EdgeKind.ASSOCIATION,
                alarm,
                target,
                *proof,
            )
