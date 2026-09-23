"""The four *.links.*.yml families.

These are YAML-discovered plugins. Read here as data; recognising them AS
plugins belongs to P2's discovery registry, which learns plugin types from the
project's own plugin managers.

`links_to_route` is what makes the family worth a phase: it joins the UI surface
to the routing table, and both endpoints are declared by YAML P1 reads.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.yaml_common import (
    edge,
    key_lines,
    link_id,
    load_drupal_yaml,
    menu_id,
    node,
    route_id,
)
from graphify.drupal.yaml_extract import extension_id


def _extract_links(path: Path, kind: str, node_type: str, declares: str) -> dict[str, Any]:
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
    menus: set[str] = set()

    def add_edge(source: str, target: str, relation: str, line: int, **extra: Any) -> None:
        # One relation per ordered pair: a local task whose route_name equals its
        # base_route would otherwise emit two, and the reader silently keeps one.
        if source == target or (source, target) in seen_pairs:
            return
        seen_pairs.add((source, target))
        edges.append(edge(source, target, relation, path=path, line=line, **extra))

    for plugin_id, definition in data.items():
        plugin_id = str(plugin_id)
        if not isinstance(definition, dict):
            continue
        line = lines.get(plugin_id, 1)
        lid = link_id(kind, plugin_id)
        extra: dict[str, Any] = {}
        for key in ("weight", "group", "deriver"):
            if key in definition:
                extra[key] = definition[key]
        route = definition.get("route_name")
        base = definition.get("base_route")
        # The tab a base route shows by default. Its links_to_route and
        # base_route edges would share one pair, so the second is a flag.
        if isinstance(base, str) and base and base == route:
            extra["default_tab"] = True
        nodes.append(node(lid, str(definition.get("title") or plugin_id),
                          type=node_type, layer="routing", path=path, line=line, **extra))
        add_edge(owner_id, lid, declares, line)

        if isinstance(route, str) and route:
            add_edge(lid, route_id(route), "links_to_route", line, target_name=route)

        if isinstance(base, str) and base and base != route:
            add_edge(lid, route_id(base), "base_route", line, target_name=base)

        menu = definition.get("menu_name")
        if isinstance(menu, str) and menu:
            # Menus are declared by configuration (P1b), but named here; the
            # seam collapses the copies every links file makes (merge.py).
            mid = menu_id(menu)
            if mid not in menus:
                menus.add(mid)
                nodes.append(node(mid, menu, type="drupal_menu", layer="routing",
                                  path=path, line=line))
            add_edge(lid, mid, "in_menu", line, target_name=menu)

        # Menu links say `parent`, local tasks `parent_id`.
        parent = definition.get("parent") or definition.get("parent_id")
        if isinstance(parent, str) and parent:
            add_edge(lid, link_id(kind, parent), "parent_link", line, target_name=parent)

        for appears in definition.get("appears_on") or []:
            if isinstance(appears, str) and appears:
                add_edge(lid, route_id(appears), "appears_on_route", line, target_name=appears)

    return {"nodes": nodes, "edges": edges}


def extract_drupal_menu_links(path: Path) -> dict[str, Any]:
    return _extract_links(path, "menu_link", "drupal_menu_link", "declares_menu_link")


def extract_drupal_local_tasks(path: Path) -> dict[str, Any]:
    return _extract_links(path, "local_task", "drupal_local_task", "declares_local_task")


def extract_drupal_local_actions(path: Path) -> dict[str, Any]:
    return _extract_links(path, "local_action", "drupal_local_action", "declares_local_action")


def extract_drupal_contextual_links(path: Path) -> dict[str, Any]:
    return _extract_links(
        path, "contextual_link", "drupal_contextual_link", "declares_contextual_link")
