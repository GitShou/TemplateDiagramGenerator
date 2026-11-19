"""Read CloudFormation without evaluating intrinsics or YAML application tags."""

import json
import math
import re
from pathlib import Path
from typing import ClassVar

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .models import AstNode, PropertyPath, SourceLocation, TemplateDocument

_YAML = "tag:yaml.org,2002:"
_CF_TAG = re.compile(r"![A-Za-z][A-Za-z0-9]*\Z")


class TemplateError(ValueError):
    def __init__(self, code: str, message: str, source: SourceLocation):
        super().__init__(message)
        self.code = code
        self.source = source


class _CloudFormationLoader(yaml.SafeLoader):
    yaml_implicit_resolvers: ClassVar[dict] = {
        first: [
            (tag, rule) for tag, rule in entries if tag not in {_YAML + "timestamp", _YAML + "bool"}
        ]
        for first, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_CloudFormationLoader.add_implicit_resolver(
    _YAML + "bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


class TemplateLoader:
    def __init__(self, *, max_bytes: int = 4 * 1024**2, max_nodes: int = 100_000):
        self.max_bytes = max_bytes
        self.max_nodes = max_nodes

    def load(self, path: Path, *, source_name: str | None = None) -> TemplateDocument:
        name = source_name if source_name is not None else path.name
        source = SourceLocation(name)
        try:
            with path.open("rb") as stream:
                content = stream.read(self.max_bytes + 1)
        except OSError as exc:
            raise TemplateError("TEMPLATE_IO", "Could not read template.", source) from exc
        if len(content) > self.max_bytes:
            raise TemplateError("TEMPLATE_SIZE_LIMIT", "Template exceeds the byte limit.", source)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise TemplateError("TEMPLATE_ENCODING", "Template must use UTF-8.", source) from exc
        if path.suffix.lower() == ".json":
            root = self._load_json(text, source)
        else:
            root = self._load_yaml(text, source)
        if root.mapping() is None:
            raise TemplateError("TEMPLATE_STRUCTURE", "Template must be a mapping.", root.source)
        return TemplateDocument(root)

    def _load_json(self, text: str, source: SourceLocation) -> AstNode:
        def pairs(entries: list[tuple[str, object]]) -> dict[str, object]:
            obj = dict(entries)
            if len(obj) != len(entries):
                raise TemplateError("DUPLICATE_KEY", "Duplicate mapping key.", source)
            return obj

        def invalid_constant(_token: str) -> None:
            raise ValueError("Non-finite JSON number")

        try:
            raw = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
        except TemplateError:
            raise
        except json.JSONDecodeError as exc:
            position = SourceLocation(source.template, line=exc.lineno, column=exc.colno)
            raise TemplateError("TEMPLATE_SYNTAX", "Invalid JSON syntax.", position) from exc
        except (ValueError, RecursionError) as exc:
            raise TemplateError("TEMPLATE_SYNTAX", "Invalid JSON syntax.", source) from exc

        remaining = self.max_nodes

        def convert(value, path: PropertyPath) -> AstNode:
            nonlocal remaining
            remaining -= 1
            location = SourceLocation(source.template, path)
            self._check_limits(remaining, path, location)
            if isinstance(value, dict):
                value = {key: convert(item, (*path, key)) for key, item in value.items()}
            elif isinstance(value, list):
                value = tuple(convert(item, (*path, index)) for index, item in enumerate(value))
            elif isinstance(value, float) and not math.isfinite(value):
                raise TemplateError(
                    "TEMPLATE_SYNTAX", "Non-finite numbers are unsupported.", location
                )
            return AstNode(value, location)

        return convert(raw, ())

    def _load_yaml(self, text: str, source: SourceLocation) -> AstNode:
        loader = _CloudFormationLoader(text)
        remaining = self.max_nodes
        active: set[int] = set()

        def convert(node: Node, path: PropertyPath) -> AstNode:
            nonlocal remaining
            remaining -= 1
            location = SourceLocation(
                source.template, path, node.start_mark.line + 1, node.start_mark.column + 1
            )
            self._check_limits(remaining, path, location)
            if id(node) in active:
                raise TemplateError("YAML_ALIAS_CYCLE", "Cyclic YAML alias.", location)
            active.add(id(node))
            try:
                if _CF_TAG.fullmatch(node.tag):
                    name = node.tag[1:]
                    key = name if name in {"Ref", "Condition"} else f"Fn::{name}"
                    argument = body(node, (*path, key), location, tagged=True)
                    return AstNode({key: argument}, location)
                return body(node, path, location, tagged=False)
            finally:
                active.remove(id(node))

        def body(
            node: Node, path: PropertyPath, location: SourceLocation, *, tagged: bool
        ) -> AstNode:
            if isinstance(node, MappingNode) and (tagged or node.tag == _YAML + "map"):
                result: dict[str, AstNode] = {}
                for key, item in node.value:
                    if not isinstance(key, ScalarNode) or key.tag not in {
                        _YAML + "str",
                        _YAML + "int",
                        _YAML + "float",
                        _YAML + "bool",
                        _YAML + "null",
                    }:
                        raise TemplateError(
                            "YAML_KEY", "Mapping keys must be scalar keys.", location
                        )
                    if key.value in result:
                        key_location = SourceLocation(
                            location.template,
                            path,
                            key.start_mark.line + 1,
                            key.start_mark.column + 1,
                        )
                        raise TemplateError("DUPLICATE_KEY", "Duplicate mapping key.", key_location)
                    result[key.value] = convert(item, (*path, key.value))
                return AstNode(result, location)
            if isinstance(node, SequenceNode) and (tagged or node.tag == _YAML + "seq"):
                return AstNode(
                    tuple(convert(item, (*path, index)) for index, item in enumerate(node.value)),
                    location,
                )
            if isinstance(node, ScalarNode):
                if tagged or node.tag in {_YAML + "str", _YAML + "timestamp"}:
                    return AstNode(node.value, location)
                if node.tag in {_YAML + "int", _YAML + "float", _YAML + "bool", _YAML + "null"}:
                    value = loader.construct_object(node)
                    if isinstance(value, float) and not math.isfinite(value):
                        raise TemplateError(
                            "TEMPLATE_SYNTAX", "Non-finite numbers are unsupported.", location
                        )
                    return AstNode(value, location)
            raise TemplateError("YAML_TAG", "Unsupported YAML tag.", location)

        try:
            node = loader.get_single_node()
            if node is None:
                raise TemplateError("TEMPLATE_STRUCTURE", "Template is empty.", source)
            return convert(node, ())
        except TemplateError:
            raise
        except (yaml.YAMLError, RecursionError, ValueError, KeyError) as exc:
            mark = getattr(exc, "problem_mark", None)
            position = (
                SourceLocation(source.template, line=mark.line + 1, column=mark.column + 1)
                if mark
                else source
            )
            raise TemplateError("TEMPLATE_SYNTAX", "Invalid YAML syntax.", position) from exc
        finally:
            loader.dispose()

    @staticmethod
    def _check_limits(remaining: int, path: PropertyPath, source: SourceLocation) -> None:
        if remaining < 0 or len(path) > 80:
            raise TemplateError(
                "TEMPLATE_NODE_LIMIT", "Template exceeds the structure limit.", source
            )
