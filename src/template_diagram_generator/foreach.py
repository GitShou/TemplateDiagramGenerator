"""Expand AWS::LanguageExtensions loops with bounded, source-preserving substitution."""

import re
from dataclasses import replace

from .expressions import EvaluationSite, ExpressionEvaluator
from .models import AstNode, ValueKind

_LOOP_PREFIX = "Fn::ForEach::"
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*\Z")


class _ExpansionLimit(Exception):
    pass


class LoopExpander:
    def __init__(self, evaluator: ExpressionEvaluator, *, max_expanded_nodes: int):
        self.evaluator = evaluator
        self.remaining = max_expanded_nodes
        self.partial = False
        self.unexpanded: list[AstNode] = []
        self.loop_names: set[str] = set()

    def expand(self, section: AstNode, site: EvaluationSite) -> AstNode:
        return self._expand(section, site, 0)

    def _expand(self, node: AstNode, site: EvaluationSite, depth: int) -> AstNode:
        if (mapping := node.mapping()) is not None:
            result = {}
            for key, item in sorted(mapping.items()):
                if not key.startswith(_LOOP_PREFIX):
                    result[key] = self._expand(item, site.child(key), depth)
            for key, item in sorted(mapping.items()):
                if key.startswith(_LOOP_PREFIX):
                    candidates = self._loop(key, item, site, depth)
                    if result.keys() & candidates.keys():
                        self._issue(
                            "FOREACH_COLLISION",
                            "Loop would create a duplicate key.",
                            site.child(key),
                            item,
                        )
                    else:
                        result.update(candidates)
            return replace(node, value=result)
        if (sequence := node.sequence()) is not None:
            return replace(
                node,
                value=tuple(
                    self._expand(item, site.child(index), depth)
                    for index, item in enumerate(sequence)
                ),
            )
        return node

    def _loop(
        self, key: str, node: AstNode, site: EvaluationSite, depth: int
    ) -> dict[str, AstNode]:
        loop_site = site.child(key)
        if not self.evaluator.scope.language_extensions(site.stack_id):
            self._issue(
                "FOREACH_TRANSFORM", "Loop requires AWS::LanguageExtensions.", loop_site, node
            )
            return {}
        if depth >= 5:
            self._issue("FOREACH_LIMIT", "Loop exceeds the nesting limit.", loop_site, node)
            return {}
        args = node.sequence()
        loop_name = key.removeprefix(_LOOP_PREFIX)
        if (
            not _IDENTIFIER.fullmatch(loop_name)
            or args is None
            or len(args) != 3
            or not isinstance(args[0].value, str)
            or not _IDENTIFIER.fullmatch(args[0].value)
            or args[2].mapping() is None
        ):
            self._issue(
                "FOREACH_INVALID",
                "Loop requires an identifier, a collection, and a mapping.",
                loop_site,
                node,
            )
            return {}
        # A nested loop declaration is visited once for each enclosing iteration.
        # Only the outermost declarations are evaluated as distinct names here.
        if depth == 0:
            if loop_name in self.loop_names:
                self._issue(
                    "FOREACH_COLLISION", "Loop name is declared more than once.", loop_site, node
                )
                return {}
            self.loop_names.add(loop_name)
        collection = self.evaluator.evaluate(args[1], loop_site.child(1))
        if collection.kind is not ValueKind.ARRAY or any(
            item.kind is not ValueKind.LITERAL or not isinstance(item.value, str)
            for item in collection.value
        ):
            self._issue(
                "FOREACH_COLLECTION",
                "Loop collection could not be statically resolved.",
                loop_site,
                node,
            )
            return {}
        result = {}
        for index, item in enumerate(collection.value):
            try:
                fragment = self._substitute(args[2], args[0].value, item.value)
            except _ExpansionLimit:
                self._issue("FOREACH_LIMIT", "Loop exceeds the expansion limit.", loop_site, node)
                return {}
            if fragment is None:
                self._issue(
                    "FOREACH_COLLISION", "Loop would create a duplicate key.", loop_site, node
                )
                return {}
            iteration = self._expand(fragment, site.child(key, 2, index), depth + 1).mapping()
            if result.keys() & iteration.keys():
                self._issue(
                    "FOREACH_COLLISION", "Loop would create a duplicate key.", loop_site, node
                )
                return {}
            result.update(iteration)
        return result

    def _substitute(self, node: AstNode, identifier: str, value: str) -> AstNode | None:
        self.remaining -= 1
        if self.remaining < 0:
            raise _ExpansionLimit

        def substitute(text: str) -> str:
            return text.replace("${" + identifier + "}", value).replace(
                "&{" + identifier + "}", value
            )

        if isinstance(node.value, str):
            return replace(node, value=substitute(node.value))
        if (mapping := node.mapping()) is not None:
            items = {}
            for key, item in mapping.items():
                new_key = substitute(key)
                transformed = self._substitute(item, identifier, value)
                if transformed is None or new_key in items:
                    return None
                items[new_key] = transformed
            return replace(node, value=items)
        if (sequence := node.sequence()) is not None:
            items = tuple(self._substitute(item, identifier, value) for item in sequence)
            return None if None in items else replace(node, value=items)
        return node

    def _issue(self, code: str, message: str, site: EvaluationSite, node: AstNode) -> None:
        self.partial = True
        self.unexpanded.append(node)
        self.evaluator.scope.issue(code, message, site, node.source)
