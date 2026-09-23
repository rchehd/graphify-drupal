"""*.services.yml — the container as the codebase declares it.

Edges stay inside this phase: a service injects a service, carries a tag, or
extends another service, and every one of those endpoints is declared by a
`*.services.yml` somewhere in the corpus. The `class:` value is a PHP FQN, which
belongs to a layer that does not exist yet, so it is recorded as an attribute
and becomes an edge in P4.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.yaml_common import (
    edge,
    key_lines,
    load_drupal_yaml,
    node,
    parameter_id,
    service_id,
    tag_id,
)
from graphify.drupal.yaml_extract import extension_id


#: Keys under `services:` that configure the file rather than name a service.
_RESERVED_SERVICE_KEYS = frozenset({"_defaults", "_instanceof"})


def _service_reference(value: Any) -> tuple[str, bool] | None:
    """('database', required) for '@database', ('x', optional) for '@?x'."""
    if not isinstance(value, str) or not value.startswith("@"):
        return None
    ref = value[1:]
    optional = ref.startswith("?")
    ref = ref.lstrip("?")
    # '@@' is an escaped literal, and a closure reference is not an injection.
    if not ref or ref.startswith("@"):
        return None
    return ref, optional


def _parameter_reference(value: Any) -> str | None:
    if isinstance(value, str) and len(value) > 2 and value.startswith("%") and value.endswith("%"):
        return value[1:-1]
    return None


def extract_drupal_services(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    # Imported here: `families` imports this module to build its table.
    from graphify.drupal.families import extension_owner

    owner = extension_owner(path)
    owner_id = extension_id(owner)
    # Services and parameters are entity ids at indent 2, under their section key.
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[2]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    emitted: set[str] = set()

    def add_edge(source: str, target: str, relation: str, line: int, **extra: Any) -> None:
        if source == target or (source, target) in seen_pairs:
            return
        seen_pairs.add((source, target))
        edges.append(edge(source, target, relation, path=path, line=line, **extra))

    def add_node(nid: str, label: str, type_: str, line: int, **extra: Any) -> None:
        if nid in emitted:
            return
        emitted.add(nid)
        nodes.append(node(nid, label, type=type_, layer="di", path=path, line=line, **extra))

    for name in (data.get("parameters") or {}):
        line = lines.get(str(name), 1)
        pid = parameter_id(str(name))
        add_node(pid, str(name), "drupal_parameter", line)
        add_edge(owner_id, pid, "declares_parameter", line)

    services = data.get("services")
    if not isinstance(services, dict):
        return {"nodes": nodes, "edges": edges}

    for sid, definition in services.items():
        sid = str(sid)
        # Symfony's file-level settings, not services.
        if sid in _RESERVED_SERVICE_KEYS:
            continue
        line = lines.get(sid, 1)
        own = service_id(sid)
        extra: dict[str, Any] = {}
        if isinstance(definition, dict):
            for key, attr in (("class", "class_name"), ("abstract", "abstract"),
                              ("deprecated", "deprecated"), ("autowire", "autowire")):
                if key in definition:
                    extra[attr] = definition[key]
        add_node(own, sid, "drupal_service", line, **extra)
        add_edge(owner_id, own, "declares_service", line)

        if not isinstance(definition, dict):
            continue

        for argument in definition.get("arguments") or []:
            reference = _service_reference(argument)
            if reference is not None:
                ref, optional = reference
                add_edge(own, service_id(ref), "injects_service", line,
                         confidence="INFERRED" if optional else "EXTRACTED")
                continue
            parameter = _parameter_reference(argument)
            if parameter is not None:
                add_edge(own, parameter_id(parameter), "injects_parameter", line)

        for tag in definition.get("tags") or []:
            name = tag.get("name") if isinstance(tag, dict) else tag
            if not name:
                continue
            tid = tag_id(str(name))
            add_node(tid, str(name), "drupal_service_tag", line)
            add_edge(own, tid, "tagged_as", line)

        if isinstance(definition.get("decorates"), str):
            add_edge(own, service_id(definition["decorates"]), "decorates", line)
        if isinstance(definition.get("parent"), str):
            add_edge(own, service_id(definition["parent"]), "parent_service", line)

    return {"nodes": nodes, "edges": edges}
