"""CloudFormation ordering and inter-stack references."""

from collections import defaultdict

from .architecture import EdgeStatus, StackReference
from .models import Diagnostic, ReferenceKind, ReferenceStatus, ResourceGraph

_STATES = {
    ReferenceStatus.RESOLVED: EdgeStatus.CONFIRMED,
    ReferenceStatus.CONDITIONAL: EdgeStatus.CONDITIONAL,
    ReferenceStatus.INACTIVE: EdgeStatus.INACTIVE,
}


def dependency_references(graph: ResourceGraph):
    for ref in graph.references.values():
        if (
            ref.status in _STATES
            and ref.kind is not ReferenceKind.CONDITION
            and ref.source_entity in graph.resources
            and ref.direct_target_id in graph.resources
        ):
            yield ref, _STATES[ref.status]


def stack_references(graph: ResourceGraph) -> tuple[StackReference, ...]:
    by_pair = defaultdict(set)
    for ref in graph.references.values():
        if ref.status not in {ReferenceStatus.RESOLVED, ReferenceStatus.CONDITIONAL}:
            continue
        targets = {
            graph.resources[id_].stack_id
            for id_ in ref.resolved_target_ids
            if id_ in graph.resources
        }
        for link in (ref, *(graph.references[id_] for id_ in ref.trace if id_ in graph.references)):
            if (
                link.kind in {ReferenceKind.IMPORT_VALUE, ReferenceKind.TERRAFORM_REFERENCE}
                and link.direct_target_id
                and "@output/" in link.direct_target_id
            ):
                targets.add(link.direct_target_id.partition("@output/")[0])
            if (
                link.attribute
                and link.attribute.startswith("Outputs.")
                and link.direct_target_id in graph.resources
            ):
                declaration = graph.resources[link.direct_target_id]
                if declaration.type == "AWS::CloudFormation::Stack":
                    targets.add(f"{declaration.stack_id}/{declaration.logical_id}")
        for target_stack in targets & graph.stacks.keys() - {ref.source_stack_id}:
            by_pair[ref.source_stack_id, target_stack].add(ref.id)
    return tuple(
        StackReference(start, end, tuple(sorted(ids)))
        for (start, end), ids in sorted(by_pair.items())
    )


def dependency_cycles(graph: ResourceGraph):
    # Report DFS back edges, using a stack so long dependency chains are safe.
    successors = defaultdict(dict)
    for ref, status in dependency_references(graph):
        if status is EdgeStatus.CONFIRMED:
            successors[ref.source_entity].setdefault(ref.direct_target_id, ref)
    visited = set()
    active = set()
    for start in sorted(successors):
        if start in visited:
            continue
        visited.add(start)
        active.add(start)
        walk = [(start, iter(sorted(successors[start].items())))]
        while walk:
            node, neighbors = walk[-1]
            next_item = next(neighbors, None)
            if next_item is None:
                active.remove(node)
                walk.pop()
                continue
            next_node, ref = next_item
            if next_node in active:
                yield Diagnostic(
                    "DEPENDENCY_CYCLE",
                    "CloudFormation dependencies contain a cycle.",
                    ref.source,
                    ref.path,
                    ref.source_entity,
                )
            elif next_node not in visited:
                visited.add(next_node)
                active.add(next_node)
                walk.append((next_node, iter(sorted(successors[next_node].items()))))
