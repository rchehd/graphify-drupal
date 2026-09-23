"""Configuration objects: one node per config name, and what the file states.

What a view queries or a field formats is P6's. Here a config file contributes
its identity, where it is stored, whether it is active, what it depends on, who
ships it — and, for a few objects, one more fact: `core.extension` installs
extensions, a split entity lists what it splits.

Values never reach the graph (P1b spec §4). The node carries `status` when it is
a boolean and nothing else from the file body.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graphify.drupal.config_stores import (
    PATCH_PREFIX,
    SPLIT_PREFIX,
    ConfigStore,
    config_name,
    config_store,
)
from graphify.drupal.yaml_common import config_id, edge, load_drupal_yaml, node, recipe_id
from graphify.drupal.yaml_extract import extension_id

#: The collapse keeps the copy with the lowest rank (spec §6.1).
STORE_RANK = {"sync": 0, "split": 1, "recipe": 2, "optional": 3, "install": 4}

_EMPTY: dict[str, Any] = {"nodes": [], "edges": []}


class _Edges:
    """One relation per ordered pair; the first relation added for a pair wins."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: list[dict[str, Any]] = []
        self._pairs: set[tuple[str, str]] = set()

    def add(self, source: str, target: str, relation: str, line: int = 1, **extra: Any) -> None:
        if not source or not target or source == target or (source, target) in self._pairs:
            return
        self._pairs.add((source, target))
        self.items.append(edge(source, target, relation, path=self.path, line=line, **extra))


def _names(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int))]
    return []


def _dependency_targets(block: Any) -> list[tuple[str, str, str]]:
    """(dependency_kind, target id, raw name) for config/module/theme entries."""
    if not isinstance(block, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for kind in ("config", "module", "theme"):
        for name in _names(block.get(kind)):
            out.append((kind, config_id(name) if kind == "config" else extension_id(name), name))
    return out


def _node_type(name: str) -> str:
    if name.startswith(SPLIT_PREFIX):
        return "drupal_config_split"
    if name.startswith("domain.record."):
        return "drupal_domain"
    return "drupal_config"


#: `fr`, `pt-br`, `zh-hans`: a leading segment that may be a langcode.
_LANGCODE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})?")


def _key_paths(value: Any, prefix: str = "") -> list[str]:
    """Dotted leaf key paths; a list is a leaf. Values are never returned."""
    if not isinstance(value, dict):
        return [prefix] if prefix else []
    paths: list[str] = []
    for key, inner in value.items():
        if str(key) == "_core":
            continue
        dotted = f"{prefix}.{key}" if prefix else str(key)
        paths.extend(_key_paths(inner, dotted) if isinstance(inner, dict) else [dotted])
    return sorted(set(paths))


def _split_source(store: ConfigStore) -> str:
    return config_id(f"{SPLIT_PREFIX}{store.split}")


def _patch(path: Path, store: ConfigStore, name: str, data: dict) -> dict[str, Any]:
    target = name[len(PATCH_PREFIX):]
    keys = sorted(set(_key_paths(data.get("adding")) + _key_paths(data.get("removing"))))
    edges = _Edges(path)
    edges.add(_split_source(store), config_id(target), "overrides_config",
              override_source="split", keys=keys, target_name=target)
    return {"nodes": [], "edges": edges.items}


def _language_override(path: Path, store: ConfigStore, name: str, data: dict) -> dict[str, Any]:
    edges = _Edges(path)
    edges.add(config_id(f"language.entity.{store.language}"), config_id(name), "overrides_config",
              override_source="language", keys=_key_paths(data), target_name=name)
    return {"nodes": [], "edges": edges.items}


def _domain_override(edges: _Edges, name: str, data: dict) -> None:
    rest = name[len("domain.config."):]
    domain, _, target = rest.partition(".")
    if not domain or not target:
        return
    extra: dict[str, Any] = {}
    segment, _, after = target.partition(".")
    if after and _LANGCODE.fullmatch(segment):
        # `<domain>.<langcode>.<config>` or `<domain>.<config starting fr.>`:
        # the resolver keeps whichever target the corpus declares (Task 9).
        extra = {"alt_target": config_id(after), "alt_target_name": after}
    edges.add(config_id(f"domain.record.{domain}"), config_id(target), "overrides_config",
              override_source="domain", keys=_key_paths(data), target_name=target, **extra)


def _config_object(path: Path, store: ConfigStore, name: str, data: dict) -> dict[str, Any]:
    own = config_id(name)
    extra: dict[str, Any] = {
        "config_name": name,
        "store": store.kind,
        "active": store.kind == "sync",
        "_rank": STORE_RANK[store.kind],
    }
    if store.kind in ("install", "optional"):
        extra["install_mode"] = store.kind
    if store.kind in ("sync", "split"):
        # The site's own export: project-owned whatever module it configures.
        extra["realm"] = "custom"
    if isinstance(data.get("status"), bool):
        extra["status"] = data["status"]
    deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    content = _names(deps.get("content"))
    if content:
        extra["content_dependencies"] = content

    type_ = _node_type(name)
    edges = _Edges(path)
    if type_ == "drupal_config_split":
        if isinstance(data.get("folder"), str):
            extra["folder"] = data["folder"]
        patterns = [n for key in ("complete_list", "partial_list")
                    for n in _names(data.get(key)) if "*" in n]
        if patterns:
            extra["split_patterns"] = patterns

    nodes = [node(own, name, type=type_, layer="config", path=path, line=1, **extra)]

    if store.kind in ("install", "optional") and store.owner:
        edges.add(extension_id(store.owner), own, "defines_config", install_mode=store.kind)
    if store.kind == "recipe" and store.owner:
        edges.add(recipe_id(store.owner), own, "defines_config", install_mode="recipe")

    # What a split splits is more specific than a dependency on it, so it goes first.
    if type_ == "drupal_config_split":
        for key in ("module", "theme"):
            for ext in _names(data.get(key)):
                edges.add(own, extension_id(ext), "splits_extension", target_name=ext)
        for key, kind in (("complete_list", "complete"), ("partial_list", "partial")):
            for target in _names(data.get(key)):
                if "*" not in target:
                    edges.add(own, config_id(target), "splits_config",
                              split_kind=kind, target_name=target)

    # Enforced first: the same pair keeps the stronger relation.
    for kind, target, raw in _dependency_targets(deps.get("enforced")):
        edges.add(own, target, "enforced_dependency", dependency_kind=kind, target_name=raw)
    for kind, target, raw in _dependency_targets(deps):
        edges.add(own, target, "config_depends_on", dependency_kind=kind, target_name=raw)

    if name == "core.extension" and store.kind == "sync":
        for key in ("module", "theme"):
            block = data.get(key)
            if isinstance(block, dict):
                for ext, weight in block.items():
                    edges.add(own, extension_id(str(ext)), "installs_extension",
                              target_name=str(ext),
                              weight=weight if isinstance(weight, int) else None)

    if store.kind == "split":
        edges.add(_split_source(store), own, "overrides_config",
                  override_source="split", whole=True, target_name=name)
    if name.startswith("domain.config.") and store.kind in ("sync", "split"):
        _domain_override(edges, name, data)

    return {"nodes": nodes, "edges": edges.items}


def extract_drupal_config(path: Path) -> dict[str, Any]:
    store = config_store(path)
    if store is None:
        return dict(_EMPTY)
    if store.kind == "schema":
        from graphify.drupal.yaml_schema import extract_drupal_schema

        return extract_drupal_schema(path, store)
    if path.name == "recipe.yml":
        from graphify.drupal.yaml_recipes import extract_drupal_recipe

        return extract_drupal_recipe(path, store)
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    name = config_name(path)
    data = data or {}
    if store.language:
        return _language_override(path, store, name, data)
    if store.kind == "split" and name.startswith(PATCH_PREFIX):
        return _patch(path, store, name, data)
    return _config_object(path, store, name, data)
