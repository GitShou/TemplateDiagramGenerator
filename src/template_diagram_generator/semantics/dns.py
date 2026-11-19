"""Route 53 aliases backed by matching DNS and hosted zone identifiers."""

from ..architecture import EdgeKind
from ..models import ResourceAttribute, ValueKind
from .context import (
    ALB,
    CLOUDFRONT,
    DNS_RECORD_GROUP,
    HTTP_DOMAIN,
    REST_DOMAIN,
    Property,
    RuleContext,
    symbol,
)

_TARGET_ZONE_ATTRIBUTES = {
    (ALB, "DNSName"): "CanonicalHostedZoneID",
    (REST_DOMAIN, "RegionalDomainName"): "RegionalHostedZoneId",
    (REST_DOMAIN, "DistributionDomainName"): "DistributionHostedZoneId",
    (HTTP_DOMAIN, "RegionalDomainName"): "RegionalHostedZoneId",
}


def _address(prop: Property) -> tuple[ResourceAttribute | None, bool]:
    value = prop.value
    if value is not None and value.kind is ValueKind.COMPOSITE and len(value.value) == 2:
        prefix, target = value.value
        if prefix.kind is ValueKind.LITERAL and prefix.value == "dualstack.":
            return symbol(target), True
    return symbol(value), False


def route53_alias(ctx: RuleContext, resource) -> None:
    root = Property.root(resource)
    records = root.get("RecordSets").entries() if resource.type == DNS_RECORD_GROUP else (root,)
    for record in records:
        alias = record.get("AliasTarget")
        if alias.value is None:
            continue
        kind, address, zone = (record.get("Type"), alias.get("DNSName"), alias.get("HostedZoneId"))
        target, dualstack = _address(address)
        if kind.literal() not in {"A", "AAAA"} or target is None:
            ctx.issue(
                "ROUTE53_ALIAS_TARGET",
                "Alias is not a supported A/AAAA record with a local DNS target.",
                address,
            )
            continue
        endpoint = ctx.graph.resources.get(target.resource_id)
        if endpoint is None or (dualstack and endpoint.type != ALB):
            ctx.issue(
                "ROUTE53_ALIAS_TARGET", "Alias endpoint is not a supported local resource.", address
            )
            continue
        if endpoint.type == CLOUDFRONT and target.attribute == "DomainName":
            zone_matches = zone.literal() == "Z2FDTNDATAQYW2"
            if endpoint.input_type == "aws_cloudfront_distribution":
                zone_matches |= symbol(zone.value) == ResourceAttribute(endpoint.id, "HostedZoneId")
        else:
            attr = _TARGET_ZONE_ATTRIBUTES.get((endpoint.type, target.attribute))
            zone_matches = attr is not None and symbol(zone.value) == ResourceAttribute(
                endpoint.id, attr
            )
        if not zone_matches:
            ctx.issue(
                "ROUTE53_ALIAS_ZONE",
                "Alias hosted zone could not be verified against its DNS target.",
                zone,
            )
            continue
        ctx.add("route53.alias", EdgeKind.DNS, resource, endpoint, kind, address, zone)
