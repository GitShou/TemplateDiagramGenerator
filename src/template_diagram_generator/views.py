"""Build paged architecture, network detail, stack views, and inventory."""

from collections import defaultdict, deque
from dataclasses import dataclass, field, replace

from .architecture import ArchitectureModel, EdgeKind, EdgeOrigin, EdgeStatus, NodeRole
from .localization import REPORT_LOCALES, report_text
from .models import ConditionState, Diagnostic, ReferenceStatus, SourceLocation
from .semantics.context import stable_id

FLOW = {EdgeKind.TRAFFIC, EdgeKind.DNS, EdgeKind.EVENT, EdgeKind.DATA_ACCESS}
_SUPPLEMENTAL_KINDS = {EdgeKind.ASSOCIATION, EdgeKind.CONTAINS}
_MAX_OVERVIEW_SUPPLEMENTS = 12
_SERVICE_LABELS = {
    "AWS::ApiGatewayV2::Api": "API Gateway",
    "AWS::ApiGatewayV2::Integration": "API integration",
    "AWS::ApiGatewayV2::Route": "API route",
    "AWS::ApiGateway::RestApi": "API Gateway REST API",
    "AWS::ApiGateway::Method": "REST method",
    "AWS::ApiGateway::DomainName": "API Gateway domain",
    "AWS::ApiGatewayV2::DomainName": "API Gateway domain",
    "AWS::CloudFront::Distribution": "CloudFront",
    "AWS::ElasticLoadBalancingV2::Listener": "Listener",
    "AWS::ElasticLoadBalancingV2::ListenerRule": "Listener rule",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "Load balancer",
    "AWS::ElasticLoadBalancingV2::TargetGroup": "Target group",
    "AWS::Events::Rule": "EventBridge rule",
    "AWS::RDS::DBInstance": "RDS instance",
    "AWS::RDS::DBSubnetGroup": "DB subnet group",
    "AWS::Route53::RecordSet": "Route 53 record",
    "AWS::Route53::RecordSetGroup": "Route 53 records",
    "AWS::StepFunctions::StateMachine": "Step Functions",
    "AWS::DynamoDB::Table": "DynamoDB table",
    "AWS::CloudWatch::Alarm": "CloudWatch alarm",
}


@dataclass(frozen=True, slots=True)
class ResourceView:
    label: str | None = None
    overview: str = "default"
    source: SourceLocation = field(default_factory=lambda: SourceLocation("<config>"))


@dataclass(frozen=True, slots=True)
class ViewOptions:
    overview_max_nodes: int = 20
    detail_max_nodes: int = 40
    direction: str = "LR"
    show_conditional: bool = False
    show_network_boundaries: bool = True
    resources: dict[str, ResourceView] = field(default_factory=dict)
    locale: str = "en"
    overview_selection: str = "auto"
    detail_show_conditional: bool = True


@dataclass(frozen=True, slots=True)
class ViewNode:
    id: str
    label: str
    subtitle: str
    state: ConditionState
    group_id: str | None = None
    destination: str | None = None


@dataclass(frozen=True, slots=True)
class ViewEdge:
    start: str
    end: str
    kind: EdgeKind
    status: EdgeStatus
    edge_ids: tuple[str, ...]
    count: int = 1
    origin: EdgeOrigin = EdgeOrigin.AUTOMATIC
    operation: str | None = None


@dataclass(frozen=True, slots=True)
class ViewGroup:
    id: str
    label: str
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class PageConnection:
    edge: ViewEdge
    page: str


@dataclass(frozen=True, slots=True)
class Diagram:
    id: str
    direction: str
    nodes: tuple[ViewNode, ...]
    edges: tuple[ViewEdge, ...]
    groups: tuple[ViewGroup, ...] = ()
    connections: tuple[PageConnection, ...] = ()


@dataclass(frozen=True, slots=True)
class ViewBundle:
    overview: tuple[Diagram, ...]
    detail: tuple[Diagram, ...]
    stacks: tuple[Diagram, ...]
    diagnostics: tuple[Diagnostic, ...]
    inventory_ids: tuple[str, ...]
    locale: str = "en"
    overview_selection: str = "auto"

    def diagrams(self) -> tuple[Diagram, ...]:
        return (*self.overview, *self.detail, *self.stacks)


def _view_edge(edge) -> ViewEdge:
    return ViewEdge(
        edge.source_id,
        edge.target_id,
        edge.kind,
        edge.status,
        (edge.id,),
        origin=edge.origin,
        operation=edge.operation,
    )


def _combine(edges) -> tuple[ViewEdge, ...]:
    groups = defaultdict(set)
    for edge in edges:
        groups[
            (edge.start, edge.end, edge.kind, edge.status, edge.origin, edge.operation or "")
        ].update(edge.edge_ids)
    return tuple(
        ViewEdge(start, end, kind, status, tuple(sorted(ids)), len(ids), origin, operation or None)
        for (start, end, kind, status, origin, operation), ids in sorted(groups.items())
    )


def build_views(model: ArchitectureModel, options: ViewOptions = ViewOptions()) -> ViewBundle:
    if min(options.overview_max_nodes, options.detail_max_nodes) < 1 or options.direction not in {
        "LR",
        "TB",
    }:
        raise ValueError("Invalid diagram options.")
    locale = options.locale
    if locale not in REPORT_LOCALES:
        raise ValueError("Unsupported report locale.")
    if options.overview_selection not in {"auto", "primary"}:
        raise ValueError("Unsupported overview selection.")
    overview_status = {EdgeStatus.CONFIRMED}
    if options.show_conditional:
        overview_status.add(EdgeStatus.CONDITIONAL)
    hidden = {key for key, view in options.resources.items() if view.overview == "hidden"}
    forced = {key for key, view in options.resources.items() if view.overview == "primary"}
    labels = {key: view.label for key, view in options.resources.items() if view.label is not None}
    overview_states = {ConditionState.TRUE}
    if options.show_conditional:
        overview_states.add(ConditionState.UNKNOWN)
    eligible = {
        node.id
        for node in model.nodes.values()
        if node.id not in hidden and node.state in overview_states
    }
    overview_edges = [
        edge
        for edge in model.edges
        if edge.kind in FLOW
        and edge.rule_id != "cloudwatch.alarm_action"
        and edge.status in overview_status
        and edge.source_id not in hidden
        and edge.target_id not in hidden
    ]
    primary = {
        node.id
        for node in model.nodes.values()
        if (node.role is NodeRole.PRIMARY or node.id in forced) and node.id in eligible
    }
    overview_links = _overview_edges(primary, overview_edges)
    overview_ids = primary | {id_ for edge in overview_links for id_ in (edge.start, edge.end)}
    if (
        options.overview_selection == "auto"
        and sum(edge.status is EdgeStatus.CONFIRMED for edge in overview_links) <= 1
    ):
        overview_links = _combine(
            (
                *overview_links,
                *_overview_supplements(model, eligible, overview_ids, options.overview_max_nodes),
            )
        )
    overview_nodes = {key: _node(model, key, labels, locale) for key in sorted(overview_ids)}
    overview = _pages(
        "overview",
        overview_nodes,
        overview_links,
        options.overview_max_nodes,
        options.direction,
        locale=locale,
    )

    detail_status = {EdgeStatus.CONFIRMED}
    detail_states = {ConditionState.TRUE}
    if options.detail_show_conditional:
        detail_status.add(EdgeStatus.CONDITIONAL)
        detail_states.add(ConditionState.UNKNOWN)
    detail_edges = [
        edge
        for edge in model.edges
        if edge.kind is not EdgeKind.DEPENDENCY and edge.status in detail_status
    ]
    detail_ids = {
        node.id
        for node in model.nodes.values()
        if node.role is NodeRole.PRIMARY and node.state in detail_states
    }
    detail_ids.update(id_ for edge in detail_edges for id_ in (edge.source_id, edge.target_id))
    detail_ids.update(
        key
        for key, item in model.containers.items()
        if item.axis == "network" and item.state in detail_states
    )
    groups, placement = (
        _network_groups(model, labels, locale) if options.show_network_boundaries else ({}, {})
    )
    detail_nodes = {
        key: replace(_node(model, key, labels, locale), group_id=placement.get(key))
        for key in sorted(detail_ids)
    }
    detail_view_edges = _combine(
        _view_edge(edge)
        for edge in detail_edges
        if not options.show_network_boundaries
        or edge.kind is not EdgeKind.CONTAINS
        or edge.status is not EdgeStatus.CONFIRMED
    )
    detail = _pages(
        "detail",
        detail_nodes,
        detail_view_edges,
        options.detail_max_nodes,
        options.direction,
        groups,
        {key: model.nodes[key].stack_id for key in detail_nodes},
        locale=locale,
    )

    stack_nodes = {
        key: ViewNode(
            key,
            key.split("/")[-1],
            report_text(locale, container.stack_status.value),
            container.state,
        )
        for key, container in model.containers.items()
        if container.axis == "stack"
    }
    hierarchy = [
        ViewEdge(parent, child, EdgeKind.CONTAINS, EdgeStatus.CONFIRMED, ())
        for child in stack_nodes
        for parent in model.containers[child].parent_ids
        if parent in stack_nodes
    ]
    cross_stacks = [
        ViewEdge(
            ref.source_id,
            ref.target_id,
            EdgeKind.DEPENDENCY,
            EdgeStatus.CONFIRMED
            if any(
                model.references[id_].status is ReferenceStatus.RESOLVED
                for id_ in ref.reference_ids
            )
            else EdgeStatus.CONDITIONAL,
            ref.reference_ids,
            len(ref.reference_ids),
        )
        for ref in model.stack_references
        if ref.source_id in stack_nodes and ref.target_id in stack_nodes
    ]
    stack_pages = _pages(
        "stacks",
        stack_nodes,
        (*hierarchy, *cross_stacks),
        options.detail_max_nodes,
        "TB",
        locale=locale,
    )
    missing = tuple(
        Diagnostic(
            "VIEW_RESOURCE_NOT_FOUND",
            "A view override matched no resource.",
            options.resources[key].source,
            options.resources[key].source.path,
            key,
        )
        for key in sorted(options.resources.keys() - model.nodes.keys())
    )
    return ViewBundle(
        overview,
        detail,
        stack_pages,
        (*model.diagnostics, *missing),
        tuple(sorted(model.nodes)),
        locale,
        options.overview_selection,
    )


def _overview_supplements(model, eligible, selected, max_nodes) -> tuple[ViewEdge, ...]:
    limit = min(max_nodes, _MAX_OVERVIEW_SUPPLEMENTS)
    initial = frozenset(selected)
    candidates = [
        edge
        for edge in model.edges
        if edge.status is EdgeStatus.CONFIRMED
        and edge.source_id in eligible
        and edge.target_id in eligible
        and (edge.kind in _SUPPLEMENTAL_KINDS or edge.rule_id == "cloudwatch.alarm_action")
    ]
    candidates.sort(
        key=lambda edge: (
            -sum(key in initial for key in (edge.source_id, edge.target_id)),
            edge.rule_id.startswith("cloudwatch."),
            edge.source_id,
            edge.target_id,
            edge.kind,
            edge.id,
        )
    )
    links = []
    for edge in candidates:
        new = {edge.source_id, edge.target_id} - selected
        if len(selected) - len(initial) + len(new) > limit:
            continue
        selected.update(new)
        links.append(_view_edge(edge))
        if len(links) == limit:
            break
    if not selected:
        supported = [key for key in sorted(eligible) if model.nodes[key].role is NodeRole.SUPPORT]
        selected.update(supported[:limit])
    return tuple(links)


def _node(model: ArchitectureModel, key: str, labels: dict[str, str], locale: str) -> ViewNode:
    node = model.nodes[key]
    subtitle = _SERVICE_LABELS.get(node.type, " ".join(node.type.split("::")[1:]) or node.type)
    return ViewNode(
        key, labels.get(key, node.logical_id), report_text(locale, subtitle), node.state
    )


def _overview_edges(primary, edges) -> tuple[ViewEdge, ...]:
    confirmed = defaultdict(list)
    visible = []
    for edge in edges:
        if (
            edge.status is EdgeStatus.CONFIRMED
            and edge.origin is EdgeOrigin.AUTOMATIC
            and edge.kind is not EdgeKind.DNS
            and edge.operation is None
        ):
            confirmed[(edge.source_id, edge.kind)].append(edge)
        else:
            visible.append(_view_edge(edge))
    for start, kind in sorted(confirmed):
        if start not in primary:
            continue
        # Route and listener chains converge. Use the predecessor graph to retain all
        # contributing edges without enumerating every combination of equivalent paths.
        predecessors = defaultdict(list)
        pending = deque([start])
        traversed = {start}
        terminals = set()
        while pending:
            current = pending.popleft()
            for edge in confirmed.get((current, kind), ()):
                end = edge.target_id
                predecessors[end].append(edge)
                if end in primary:
                    terminals.add(end)
                elif end not in traversed:
                    traversed.add(end)
                    pending.append(end)
        for terminal in sorted(terminals):
            queue = deque([terminal])
            seen = {terminal}
            members = set()
            while queue:
                node = queue.popleft()
                for edge in predecessors[node]:
                    members.add(edge.id)
                    if edge.source_id != start and edge.source_id not in seen:
                        seen.add(edge.source_id)
                        queue.append(edge.source_id)
            visible.append(
                ViewEdge(start, terminal, kind, EdgeStatus.CONFIRMED, tuple(sorted(members)))
            )
    return _combine(visible)


def _network_groups(model, labels, locale):
    network = {key: item for key, item in model.containers.items() if item.axis == "network"}
    parents = defaultdict(set)
    for member in model.memberships:
        if member.axis == "network" and member.status is EdgeStatus.CONFIRMED:
            parents[member.member_id].add(member.container_id)
    placement = {}
    for key, ancestors in parents.items():
        subnets = sorted(parent for parent in ancestors if network[parent].kind == "Subnet")
        vpcs = sorted(parent for parent in ancestors if network[parent].kind == "VPC")
        if len(subnets) == 1:
            placement[key] = subnets[0]
        elif len(vpcs) == 1:
            placement[key] = vpcs[0]
    groups = {
        key: ViewGroup(
            key,
            f"{report_text(locale, item.kind)} · {labels.get(key, model.nodes[key].logical_id)}",
            placement.get(key),
        )
        for key, item in network.items()
    }
    # Boundary anchors remain inside their own cluster for references and inventory links.
    placement.update({key: key for key in groups})
    return groups, placement


def _pages(
    prefix, nodes, edges, limit, direction, groups=None, stacks=None, *, locale="en"
) -> tuple[Diagram, ...]:
    groups = groups or {}
    stacks = stacks or {}
    if not nodes:
        return (Diagram(f"{prefix}-001", direction, (), ()),)
    neighbors = defaultdict(set)
    for edge in edges:
        if edge.kind in FLOW or prefix == "stacks" and edge.kind is EdgeKind.CONTAINS:
            neighbors[edge.start].add(edge.end)
            neighbors[edge.end].add(edge.start)

    def grouping(key):
        return (nodes[key].group_id or "", stacks.get(key, ""), key)

    unseen = set(nodes)
    batches = []
    page = []
    for start in sorted(nodes, key=grouping):
        if start not in unseen:
            continue
        unseen.remove(start)
        pending = deque([start])
        component = []
        while pending:
            current = pending.popleft()
            component.append(current)
            near = sorted(neighbors[current] & unseen, key=grouping)
            unseen.difference_update(near)
            pending.extend(near)
        if page and len(page) + len(component) > limit:
            batches.append(page)
            page = []
        for offset in range(0, len(component), limit):
            page.extend(component[offset : offset + limit])
            if len(page) == limit:
                batches.append(page)
                page = []
    if page:
        batches.append(page)
    page_ids = [f"{prefix}-{index:03d}" for index in range(1, len(batches) + 1)]
    node_pages = {node: id_ for id_, batch in zip(page_ids, batches, strict=True) for node in batch}
    local = defaultdict(list)
    crosses = defaultdict(list)
    for edge in edges:
        left, right = node_pages[edge.start], node_pages[edge.end]
        if left == right:
            local[left].append(edge)
        else:
            crosses[left].append(PageConnection(edge, right))
            crosses[right].append(PageConnection(edge, left))

    diagrams = []
    for id_, batch in zip(page_ids, batches, strict=True):
        connections = tuple(crosses[id_])
        remotes = sorted({item.page for item in connections})[:8]
        stubs = {
            remote: ViewNode(
                stable_id("stub", id_, remote),
                remote,
                report_text(locale, "Continues on page"),
                ConditionState.UNKNOWN,
                destination=remote,
            )
            for remote in remotes
        }
        page_edges = list(local[id_])
        for connection in connections:
            if connection.page in stubs:
                edge = connection.edge
                stub = stubs[connection.page].id
                page_edges.append(
                    replace(edge, end=stub)
                    if node_pages[edge.start] == id_
                    else replace(edge, start=stub)
                )
        needed = set()
        for key in batch:
            group = nodes[key].group_id
            while group and group not in needed:
                needed.add(group)
                group = groups[group].parent_id
        # Hierarchy edges carry no architecture-edge IDs, so preserve their count.
        diagram_edges = (
            *_combine(edge for edge in page_edges if edge.edge_ids),
            *(edge for edge in page_edges if not edge.edge_ids),
        )
        diagrams.append(
            Diagram(
                id_,
                direction,
                tuple(nodes[key] for key in batch) + tuple(stubs.values()),
                diagram_edges,
                tuple(groups[key] for key in sorted(needed)),
                connections,
            )
        )
    return tuple(diagrams)
