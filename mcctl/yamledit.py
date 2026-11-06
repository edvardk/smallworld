"""Comment-preserving edits of a hand-written YAML file (server.yml), stdlib + PyYAML only (no ruamel).

`set_value(text, "a.b.c", value)` changes exactly the characters of one value and leaves every comment,
blank line and the author's layout alone. PyYAML's composer gives each node its character span
(`start_mark.index` .. `end_mark.index`), so:

- an existing scalar (or flow collection) value is replaced in place, in block or flow style alike
  (`dns: {zone: x}` and `dns:\\n  zone: x` both work);
- an empty value (`ipv4:`) gets the value after its colon;
- a missing key is added to its parent mapping: before the `}` of a flow mapping (`, key: value`), or as a new
  line at the parent's indentation right after its last entry's content in a block mapping (before any
  comments that introduce the next key); missing parents are created
  (block style under block parents, flow style under flow parents).

New values are written as plain scalars when that round-trips, else JSON-quoted (a valid YAML double-quoted
scalar); lists and mappings are written in flow style. Every edit is re-parsed and checked to yield exactly
the requested value, otherwise YamlEditError is raised and nothing should be written.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from mcctl import yamlio

MISSING: Any = object()
_PLAIN = re.compile(r"[A-Za-z0-9_./@+~-][A-Za-z0-9_ ./@+~:'-]*")


class YamlEditError(ValueError):
    pass


def render(value: Any) -> str:  # noqa: ANN401 - any YAML value
    """One value as YAML text (flow style for collections)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    if isinstance(value, str):
        plain = (
            _PLAIN.fullmatch(value)
            and not value.endswith((" ", ":"))
            and ": " not in value
            and " #" not in value
            and yaml.safe_load(value) == value
        )
        return value if plain else json.dumps(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(render(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{render(str(k))}: {render(v)}" for k, v in value.items()) + "}"
    raise YamlEditError(f"can't write a {type(value).__name__} to YAML")


def get_value(text: str, path: str) -> Any:  # noqa: ANN401
    """The value at a dotted path as written in the file (no defaults), or MISSING."""
    node: Any = yamlio.load(text)
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


def _find(mapping: yaml.MappingNode, key: str) -> tuple[yaml.Node, yaml.Node] | None:
    for k, v in mapping.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return k, v
    return None


def _nested_flow(parts: list[str], value: Any) -> str:  # noqa: ANN401
    text = render(value)
    for part in reversed(parts[1:]):
        text = f"{{{render(part)}: {text}}}"
    return f"{render(parts[0])}: {text}"


def _nested_block(parts: list[str], value: Any, indent: int) -> str:  # noqa: ANN401
    lines = []
    for depth, part in enumerate(parts[:-1]):
        lines.append(" " * (indent + 2 * depth) + f"{render(part)}:")
    lines.append(" " * (indent + 2 * (len(parts) - 1)) + f"{render(parts[-1])}: {render(value)}")
    return "\n".join(lines) + "\n"


def _line_end(text: str, pos: int) -> int:
    """Index just past the newline ending the line that contains pos (or len(text))."""
    if pos > 0 and text[pos - 1] == "\n":
        return pos
    nl = text.find("\n", pos)
    return len(text) if nl < 0 else nl + 1


def _content_end(node: yaml.Node) -> int:
    """Where the last entry's own text ends. A block collection's end_mark lies at the next token, i.e.
    after any blank lines and comments that introduce the following key; descend to its last scalar
    instead, so a new key lands right after the parent's content, not under the next section's comment."""
    while isinstance(node, yaml.MappingNode | yaml.SequenceNode) and not node.flow_style and node.value:
        last = node.value[-1]
        node = last[1] if isinstance(last, tuple) else last
    return node.end_mark.index


def _insert(text: str, mapping: yaml.MappingNode, parts: list[str], value: Any) -> str:  # noqa: ANN401
    if mapping.flow_style:
        close = mapping.end_mark.index - 1
        if text[close] != "}":
            raise YamlEditError("unexpected flow mapping layout")
        sep = ", " if mapping.value else ""
        return text[:close] + sep + _nested_flow(parts, value) + text[close:]
    if not mapping.value:
        raise YamlEditError("can't add to an empty block mapping")
    indent = mapping.value[0][0].start_mark.column
    pos = _line_end(text, _content_end(mapping.value[-1][1]))
    if pos == len(text) and not text.endswith("\n"):
        text += "\n"
        pos += 1
    return text[:pos] + _nested_block(parts, value, indent) + text[pos:]


def set_value(text: str, path: str, value: Any) -> str:  # noqa: ANN401
    """Return `text` with the value at dotted `path` set (see module docstring)."""
    parts = path.split(".")
    if not all(parts):
        raise YamlEditError(f"bad path {path!r}")
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is None:
        new = text + ("" if not text or text.endswith("\n") else "\n") + _nested_block(parts, value, 0)
    else:
        if not isinstance(root, yaml.MappingNode):
            raise YamlEditError("the document isn't a mapping")
        node: yaml.MappingNode = root
        new = ""
        for i, part in enumerate(parts):
            found = _find(node, part)
            if found is None:
                new = _insert(text, node, parts[i:], value)
                break
            key, val = found
            if i == len(parts) - 1:
                start, end = val.start_mark.index, val.end_mark.index
                if isinstance(val, yaml.ScalarNode) and start == end:  # `key:` with an empty value
                    colon = text.index(":", key.end_mark.index)
                    new = text[: colon + 1] + " " + render(value) + text[colon + 1 :]
                elif isinstance(val, yaml.ScalarNode) or val.flow_style:
                    new = text[:start] + render(value) + text[end:]
                else:
                    raise YamlEditError(f"{path} is a block {type(val).__name__[:-4].lower()}: set its keys")
                break
            if not isinstance(val, yaml.MappingNode):
                if not (isinstance(val, yaml.ScalarNode) and val.tag == "tag:yaml.org,2002:null"):
                    raise YamlEditError(f"{'.'.join(parts[: i + 1])} isn't a mapping")
                # `parent:` / `parent: null` -> `parent: {child: value}`
                rest = "{" + _nested_flow(parts[i + 1 :], value) + "}"
                start, end = val.start_mark.index, val.end_mark.index
                if start == end:
                    colon = text.index(":", key.end_mark.index)
                    start = end = colon + 1
                    rest = " " + rest
                new = text[:start] + rest + text[end:]
                break
            node = val
    try:
        got = get_value(new, path)
    except yaml.YAMLError as e:
        raise YamlEditError(f"editing {path} would break the file: {e}") from None
    if got != value or (isinstance(value, bool) != isinstance(got, bool)):
        raise YamlEditError(f"editing {path} didn't round-trip (got {got!r})")
    return new
