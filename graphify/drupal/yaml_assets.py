"""*.libraries.yml and *.breakpoints.yml.

A library dependency is written `<owner>/<name>` and both sides are declared by
a `*.libraries.yml`, so `library_depends_on` stays inside this phase. The css and
js paths point at files whose node ids belong to P5 — graphify has no CSS
extractor and its own convention for JS file nodes — so they are attributes here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.yaml_common import (
    breakpoint_id,
    edge,
    key_lines,
    library_id,
    load_drupal_yaml,
    node,
)
from graphify.drupal.yaml_extract import extension_id


def _js_paths(section: Any) -> list[str]:
    """`js: {path: {options}}` -- one level. Options such as `attributes:` are
    themselves mappings, so the shape alone cannot tell a file from a group."""
    return [str(key) for key in section] if isinstance(section, dict) else []


def _css_paths(section: Any) -> list[str]:
    """`css: {category: {path: {options}}}` -- two levels.

    A path written straight under `css:` with no category is malformed but
    occurs; it is kept as a path rather than dropped.
    """
    if not isinstance(section, dict):
        return []
    paths: list[str] = []
    for key, value in section.items():
        if isinstance(value, dict) and value:
            paths.extend(str(inner_key) for inner_key in value)
        else:
            paths.append(str(key))
    return paths


def extract_drupal_libraries(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    # Imported here: `families` imports this module to build its table.
    from graphify.drupal.families import extension_owner

    owner = extension_owner(path)
    owner_id = extension_id(owner)
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for name, definition in data.items():
        name = str(name)
        line = lines.get(name, 1)
        lid = library_id(owner, name)
        extra: dict[str, Any] = {}
        if isinstance(definition, dict):
            css = _css_paths(definition.get("css"))
            js = _js_paths(definition.get("js"))
            if css:
                extra["css"] = css
            if js:
                extra["js"] = js
            for key in ("version", "license", "remote"):
                if key in definition:
                    extra[key] = definition[key]
        nodes.append(node(lid, f"{owner}/{name}", type="drupal_library",
                          layer="presentation", path=path, line=line, **extra))
        edges.append(edge(owner_id, lid, "declares_library", path=path, line=line))

        if not isinstance(definition, dict):
            continue
        for dependency in definition.get("dependencies") or []:
            # A dependency without a slash is malformed; guessing an owner for it
            # would fabricate an edge to a library that does not exist.
            if not isinstance(dependency, str) or "/" not in dependency:
                continue
            dep_owner, _, dep_name = dependency.partition("/")
            target = library_id(dep_owner, dep_name)
            if (lid, target) in seen_pairs or target == lid:
                continue
            seen_pairs.add((lid, target))
            edges.append(edge(lid, target, "library_depends_on", path=path, line=line,
                              target_name=dependency))

    return {"nodes": nodes, "edges": edges}


def extract_drupal_breakpoints(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    # Imported here: `families` imports this module to build its table.
    from graphify.drupal.families import extension_owner
    # Imported here for the same reason: `yaml_plugins` reads `families`.
    from graphify.drupal.yaml_plugins import plugin_type_for_family

    owner = extension_owner(path)
    owner_id = extension_id(owner)
    plugin_type = plugin_type_for_family("breakpoints")
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for name, definition in data.items():
        name = str(name)
        line = lines.get(name, 1)
        bid = breakpoint_id(owner, name)
        extra: dict[str, Any] = {}
        label = name
        if isinstance(definition, dict):
            label = str(definition.get("label") or name)
            if "mediaQuery" in definition:
                extra["media_query"] = definition["mediaQuery"]
            if "multipliers" in definition:
                extra["multipliers"] = definition["multipliers"]
        nodes.append(node(bid, label, type="drupal_breakpoint",
                          layer="presentation", path=path, line=line, **extra))
        edges.append(edge(owner_id, bid, "declares_breakpoint", path=path, line=line))
        if plugin_type:
            type_id_, type_name = plugin_type
            edges.append(edge(bid, type_id_, "plugin_of_type", path=path, line=line,
                              target_name=type_name))

    return {"nodes": nodes, "edges": edges}
