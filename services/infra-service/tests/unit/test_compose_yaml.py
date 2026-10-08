"""Compose mappings must parse without silently replacing service definitions."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]


class UniqueKeyLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        node = super().compose_node(parent, index)
        if isinstance(node, yaml.MappingNode):
            keys = set()
            for key, _ in node.value:
                if key.value in keys:
                    raise yaml.constructor.ConstructorError(
                        "while composing a mapping",
                        node.start_mark,
                        f"duplicate key: {key.value}",
                        key.start_mark,
                    )
                keys.add(key.value)
        return node


@pytest.mark.parametrize("path", sorted(ROOT.glob("docker-compose*.yml")), ids=lambda p: p.name)
def test_compose_yaml_has_unique_mapping_keys(path):
    # Compose tags are retained as nodes, including !override and !reset.
    with path.open() as stream:
        document = yaml.compose(stream, Loader=UniqueKeyLoader)
    assert isinstance(document, yaml.MappingNode)
