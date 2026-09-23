"""Configuration objects: one node per config name, and what the file states.

What a view queries or a field formats is P6's. Here a config file contributes
its identity, where it is stored, whether it is active, what it depends on, who
ships it — and, for a few objects, one more fact: `core.extension` installs
extensions, a split entity lists what it splits.

Values never reach the graph (P1b spec §4). The node carries `status` when it is
a boolean and nothing else from the file body.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.config_stores import SPLIT_PREFIX, ConfigStore, config_name, config_store
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

    if type_ == "drupal_config_split":
        for key in ("module", "theme"):
            for ext in _names(data.get(key)):
                edges.add(own, extension_id(ext), "splits_extension", target_name=ext)
        for key, kind in (("complete_list", "complete"), ("partial_list", "partial")):
            for target in _names(data.get(key)):
                if "*" not in target:
                    edges.add(own, config_id(target), "splits_config",
                              split_kind=kind, target_name=target)

    return {"nodes": nodes, "edges": edges.items}


def extract_drupal_config(path: Path) -> dict[str, Any]:
    store = config_store(path)
    if store is None:
        return dict(_EMPTY)
    if store.kind == "schema":
        return dict(_EMPTY)      # Task 4
    if path.name == "recipe.yml":
        return dict(_EMPTY)      # Task 5
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    return _config_object(path, store, config_name(path), data or {})
