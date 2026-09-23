"""recipe.yml — what a recipe installs, applies and does to configuration.

A recipe names recipes by directory (`basic_html_format_editor`) or by path
(`core/recipes/tags_taxonomy`); both reduce to the directory name, which is the
recipe id. `install:` does not say module or theme, so it is one relation.

Per pair, the more specific relation wins: install before import, action before
import. `config.actions` whose arguments use `${…}` depend on recipe input, so
the action is AMBIGUOUS. A config *name* that uses `${…}` names no configuration
until the recipe is applied: it becomes no edge, only an entry of the recipe's
`templated_config` attribute.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.config_stores import ConfigStore
from graphify.drupal.yaml_common import config_id, edge, load_drupal_yaml, node, recipe_id
from graphify.drupal.yaml_extract import extension_id


def extract_drupal_recipe(path: Path, store: ConfigStore) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    data = data or {}
    rid = recipe_id(store.owner)
    extra: dict[str, Any] = {"has_content": (path.parent / "content").is_dir()}
    if isinstance(data.get("type"), str):
        extra["recipe_type"] = data["type"]
    edges: list[dict[str, Any]] = []
    pairs: set[str] = set()
    templated: set[str] = set()

    def add(target: str, relation: str, **attrs: Any) -> None:
        if target in pairs or target == rid:
            return
        pairs.add(target)
        edges.append(edge(rid, target, relation, path=path, line=1, **attrs))

    for ext in data.get("install") or []:
        if isinstance(ext, str) and ext:
            add(extension_id(ext), "installs_extension", target_name=ext)
    for ref in data.get("recipes") or []:
        if isinstance(ref, str) and ref:
            name = Path(ref).name
            add(recipe_id(name), "applies_recipe", target_name=name)

    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    actions = config.get("actions") if isinstance(config.get("actions"), dict) else {}
    for name, action in actions.items():
        if "${" in str(name):
            templated.add(str(name))
            continue
        add(config_id(str(name)), "config_action", target_name=str(name),
            confidence="AMBIGUOUS" if "${" in repr(action) else "EXTRACTED",
            actions=sorted(str(k) for k in action) if isinstance(action, dict) else [])
    imports = config.get("import") if isinstance(config.get("import"), dict) else {}
    for ext, names in imports.items():
        if names == "*":
            add(extension_id(str(ext)), "imports_config", target_name=str(ext), wildcard=True)
        elif isinstance(names, list):
            for name in names:
                if "${" in str(name):
                    templated.add(str(name))
                    continue
                add(config_id(str(name)), "imports_config", target_name=str(name))
    if templated:
        extra["templated_config"] = sorted(templated)
    nodes = [node(rid, str(data.get("name") or store.owner), type="drupal_recipe",
                  layer="extension", path=path, line=1, **extra)]
    return {"nodes": nodes, "edges": edges}
