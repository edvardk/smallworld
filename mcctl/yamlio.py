"""YAML reading for mcctl: PyYAML's safe loader, but duplicate keys are an error.

PyYAML silently keeps the last of two equal keys, so a duplicated block in server.yml would drop settings
without a word. Part of the "valid and canonical" check (design §2).
"""

from __future__ import annotations

from typing import Any

import yaml


class _StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:  # unhashable key: the schema rejects it later
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
        return super().construct_mapping(node, deep)


def load(text: str) -> Any:  # noqa: ANN401 - parsed YAML is arbitrary data
    """Parse one YAML document; raises yaml.YAMLError (incl. on duplicate keys)."""
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
