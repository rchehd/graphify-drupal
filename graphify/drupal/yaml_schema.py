"""config/schema/*.schema.yml — the types configuration is validated against.

Each top-level key is a type. A key containing `*` is a pattern that Drupal
matches segment by segment against config names; `schema_for` is drawn by the
resolver (Task 9), which sees every config node.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.config_stores import ConfigStore
from graphify.drupal.yaml_common import edge, key_lines, load_drupal_yaml, node, schema_id
from graphify.drupal.yaml_extract import extension_id


def extract_drupal_schema(path: Path, store: ConfigStore) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    owner_id = extension_id(store.owner) if store.owner else ""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for key in data:
        type_ = str(key)
        sid = schema_id(type_)
        line = lines.get(type_, 1)
        nodes.append(node(sid, type_, type="drupal_config_schema", layer="config",
                          path=path, line=line, schema_type=type_, pattern="*" in type_))
        if owner_id:
            edges.append(edge(owner_id, sid, "defines_schema", path=path, line=line))
    return {"nodes": nodes, "edges": edges}
