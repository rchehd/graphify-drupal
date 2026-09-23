"""*.permissions.yml and *.routing.yml.

`requires_permission` is the payoff: both endpoints are declared by YAML this
phase reads, so "what does a user need to reach this page" is answerable with no
PHP at all. The `_controller` / `_form` / `_custom_access` values are PHP FQNs
and stay attributes until P4.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graphify.drupal.yaml_common import (
    edge,
    key_lines,
    load_drupal_yaml,
    node,
    permission_id,
    route_id,
)
from graphify.drupal.yaml_extract import extension_id

#: Drupal reads `_permission` as an OR list on `,` and an AND list on `+`. The
#: distinction is about evaluation, not about which permissions are referenced,
#: so both separators produce one edge per permission.
_PERMISSION_SPLIT = re.compile(r"[+,]")


def extract_drupal_permissions(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    # Imported here: `families` imports this module to build its table.
    from graphify.drupal.families import extension_owner

    owner_id = extension_id(extension_owner(path))
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for name, definition in data.items():
        name = str(name)
        # A reserved key, not a permission: the callback is PHP and waits for P4.
        if name == "permission_callbacks":
            continue
        line = lines.get(name, 1)
        pid = permission_id(name)
        extra: dict[str, Any] = {}
        label = name
        if isinstance(definition, dict):
            label = str(definition.get("title") or name)
            if "restrict access" in definition:
                extra["restrict_access"] = definition["restrict access"]
        nodes.append(node(pid, label, type="drupal_permission", layer="routing",
                          path=path, line=line, **extra))
        edges.append(edge(owner_id, pid, "declares_permission", path=path, line=line))

    return {"nodes": nodes, "edges": edges}


def extract_drupal_routing(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    # Imported here: `families` imports this module to build its table.
    from graphify.drupal.families import extension_owner

    owner_id = extension_id(extension_owner(path))
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def add_edge(source: str, target: str, relation: str, line: int) -> None:
        if (source, target) in seen_pairs:
            return
        seen_pairs.add((source, target))
        edges.append(edge(source, target, relation, path=path, line=line))

    for name, definition in data.items():
        name = str(name)
        if not isinstance(definition, dict):
            continue
        line = lines.get(name, 1)
        rid = route_id(name)
        defaults = definition.get("defaults") or {}
        requirements = definition.get("requirements") or {}
        # Not `path`: core reads that key as a legacy alias of `source_file`.
        extra: dict[str, Any] = {"route_path": definition.get("path", "")}
        for key, attr in (("_controller", "controller"), ("_form", "form"),
                          ("_entity_form", "entity_form"), ("_title", "title")):
            if isinstance(defaults, dict) and key in defaults:
                extra[attr] = defaults[key]
        if isinstance(requirements, dict) and "_custom_access" in requirements:
            extra["custom_access"] = requirements["_custom_access"]

        nodes.append(node(rid, name, type="drupal_route", layer="routing",
                          path=path, line=line, **extra))
        add_edge(owner_id, rid, "declares_route", line)

        permission = requirements.get("_permission") if isinstance(requirements, dict) else None
        if isinstance(permission, str):
            for part in _PERMISSION_SPLIT.split(permission):
                part = part.strip()
                if part:
                    add_edge(rid, permission_id(part), "requires_permission", line)

    return {"nodes": nodes, "edges": edges}
