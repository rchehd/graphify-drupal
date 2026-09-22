"""Drupal `*.info.yml` extractor.

Modules, themes and profiles share one id namespace: a `dependencies:` entry
names an extension without saying which kind it is, and the kind is only knowable
after reading that extension's own `*.info.yml`. Separate namespaces would force
a guess at edge-creation time, and every wrong guess is a dangling edge.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.paths import extension_machine_name, resolve_realm
from graphify.ids import make_id

_TYPE_TO_NODE_TYPE = {
    "module": "drupal_module",
    "theme": "drupal_theme",
    "profile": "drupal_profile",
}

#: An info.yml is a handful of lines. Anything larger is not one, and parsing a
#: corpus-supplied file without a cap is how a zip-bomb equivalent gets in.
_MAX_INFO_BYTES = 512 * 1024


def extension_id(machine_name: str) -> str:
    return make_id("drupal", "extension", machine_name)


def _normalise_dependency(raw: Any) -> str:
    """`drupal:node`, `views:views_ui`, `node (>=8.x)` and `node` name one thing."""
    dep = str(raw).strip()
    if ":" in dep:
        dep = dep.split(":", 1)[1]
    return dep.split("(", 1)[0].strip()


def extract_drupal_info(path: Path) -> dict[str, Any]:
    # Function-local so `import graphify` stays at 1 ms.
    import yaml

    try:
        if path.stat().st_size > _MAX_INFO_BYTES:
            return {"nodes": [], "edges": [], "error": "info.yml too large to index"}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": f"info.yml read error: {exc}"}

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {"nodes": [], "edges": [], "error": f"info.yml parse error: {exc}"}

    if not isinstance(data, dict):
        return {"nodes": [], "edges": []}

    machine_name = extension_machine_name(path)
    own_id = extension_id(machine_name)
    str_path = str(path)
    node_type = _TYPE_TO_NODE_TYPE.get(str(data.get("type", "")).strip(), "drupal_module")

    nodes: list[dict[str, Any]] = [{
        "id": own_id,
        "label": str(data.get("name") or machine_name),
        "file_type": "code",
        "type": node_type,
        "layer": "extension",
        "realm": resolve_realm(path),
        "_origin": "static_yaml",
        "source_file": str_path,
        "source_location": "L1",
    }]
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(target_name: str, relation: str) -> None:
        if not target_name or target_name == machine_name:
            return
        target_id = extension_id(target_name)
        if target_id in seen:
            return  # one relation per ordered pair — the reader drops the rest
        seen.add(target_id)
        # No placeholder node for the target. An extension named here is very
        # often declared by another *.info.yml in the same corpus, and emitting
        # a stub for it creates two nodes with one id and two source_files —
        # which extract()'s id-remap pass then disambiguates by prefixing one
        # with its file path, splitting the extension in half. Dangling
        # endpoints are build.py's job: it already materialises them as
        # external nodes with file_type "concept".
        edges.append({
            "source": own_id,
            "target": target_id,
            # Carried so the cross-file resolver can label an external node
            # without reversing make_id.
            "target_name": target_name,
            "relation": relation,
            "confidence": "EXTRACTED",
            "_origin": "static_yaml",
            "source_file": str_path,
            "source_location": "L1",
        })

    raw_deps = data.get("dependencies") or []
    if isinstance(raw_deps, list):
        for raw in raw_deps:
            add(_normalise_dependency(raw), "depends_on_module")

    base = data.get("base theme")
    if isinstance(base, str) and base.strip():
        add(base.strip(), "base_theme")

    return {"nodes": nodes, "edges": edges}
