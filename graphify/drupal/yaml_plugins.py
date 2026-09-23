"""YAML-discovered plugins of a learned plugin type (spec §4.1, §4.2, §5.2).

A plugin *type* is learned from the site's own plugin managers (`discovery.py`).
This module reads the YAML files those managers' `YamlDiscovery` actually
consumes -- `<ext>.<yaml_name>.yml` in an extension root -- and emits one
`drupal_plugin` node per top-level key, plus the edges that tie a plugin to the
extension that provides it and the type it implements.

P1's own four `*.links.*.yml` families and `*.breakpoints.yml` are themselves
YAML-discovered plugins (menu links, local tasks, ...), but they were extracted
before any plugin-type registry existed, with their own node types and ids. This
module never re-extracts them: it only adds the `plugin_of_type` edge P1's
extractors could not draw on their own, by looking up the family name in the
registry `plugin_type_for_family` exposes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.discovery import PluginType, Registry, current_registry, type_id
from graphify.drupal.yaml_common import edge, key_lines, load_drupal_yaml, node, plugin_id
from graphify.drupal.yaml_extract import extension_id

#: `registry.by_yaml_name()` built once per registry object, since
#: `learned_family` is on `classify_file`'s hot path (every file in a scan).
#: Cached as an attribute of the registry itself, not in a module-level dict
#: keyed by `id(registry)`: a short-lived `Registry` built by one test (or one
#: `prepare_run`) is freed and a later one can be allocated at the same
#: address, which would silently hand back another run's stale mapping.
_CACHE_ATTR = "_drupal_yaml_name_cache"


def _by_yaml_name(registry: Registry) -> dict[str, PluginType]:
    cached = getattr(registry, _CACHE_ATTR, None)
    if cached is None:
        cached = registry.by_yaml_name()
        setattr(registry, _CACHE_ATTR, cached)
    return cached


def _in_extension_root(path: Path, ext: str) -> bool:
    """The file's directory holds `<ext>.info.yml`, or it is `core/core.<name>.yml`."""
    if (path.parent / f"{ext}.info.yml").is_file():
        return True
    return ext == "core" and path.parent.name == "core"


def learned_family(path: Path) -> tuple[str, PluginType] | None:
    """`(owner, type)` when `path` is `<ext>.<yaml_name>.yml` for a learned,
    non-deferred plugin type that is not one of P1's own family suffixes."""
    name = path.name
    if not name.endswith(".yml"):
        return None
    registry = current_registry()
    if registry is None:
        return None
    ext, sep, rest = name.partition(".")
    if not sep or not rest.endswith(".yml"):
        return None
    middle = rest[: -len(".yml")]
    if not middle or not _in_extension_root(path, ext):
        return None

    # Imported here: `families` imports this module to build its dispatch table.
    from graphify.drupal.families import is_drupal_yaml

    # Re-checked here rather than trusted to `families.drupal_extractor`'s own
    # dispatch order: `learned_family` is a public function callers query
    # directly (see the tests), so it must be correct standing alone, not just
    # as a second opinion after `family_extractor` already ruled a P1 suffix out.
    if is_drupal_yaml(path):
        return None

    # `middle` is fully determined by `ext` (fixed above as the first
    # dot-segment) and the ".yml" suffix, so this is a lookup, not a search --
    # a yaml_name with its own dots (`modeler_api.contexts` in
    # `eca.modeler_api.contexts.yml`) is looked up whole, never split further.
    found = _by_yaml_name(registry).get(middle)
    return (ext, found) if found is not None else None


def plugin_type_for_family(yaml_name: str) -> tuple[str, str] | None:
    """`(type id, plugin type name)` for a P1 family suffix (`links.menu`,
    `breakpoints`, ...), or None when there is no registry or no learned type
    reads that name. Both are needed at the call site: the id is the edge's
    `target`, the name is its `target_name` -- without the latter, an
    unresolved target the resolver later materialises is labelled with its
    raw id (`drupal_plugin_type_bar`) instead of the type's name (`bar`)."""
    registry = current_registry()
    if registry is None:
        return None
    found = _by_yaml_name(registry).get(yaml_name)
    return (type_id(found.plugin_type), found.plugin_type) if found is not None else None


def extract_drupal_yaml_plugins(path: Path) -> dict[str, Any]:
    found = learned_family(path)
    if found is None:
        return {"nodes": [], "edges": []}
    owner, plugin_type = found

    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    owner_id = extension_id(owner)
    tid = type_id(plugin_type.plugin_type)
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for key, definition in data.items():
        if not (definition is None or isinstance(definition, dict)):
            continue
        key = str(key)
        line = lines.get(key, 1)
        pid = plugin_id(plugin_type.plugin_type, key)
        extra: dict[str, Any] = {"provider": owner}
        if isinstance(definition, dict):
            class_name = definition.get("class")
            if isinstance(class_name, str):
                extra["class_name"] = class_name
            deriver = definition.get("deriver")
            if isinstance(deriver, str):
                extra["deriver"] = deriver
        nodes.append(node(pid, key, type="drupal_plugin", layer="plugin", path=path, line=line,
                          plugin_id=key, plugin_type=plugin_type.plugin_type, **extra))
        edges.append(edge(owner_id, pid, "provides_plugin", path=path, line=line))
        edges.append(edge(pid, tid, "plugin_of_type", path=path, line=line,
                          target_name=plugin_type.plugin_type))

    return {"nodes": nodes, "edges": edges}
