"""Cross-file passes, registered with graphify's public resolver registry.

A per-file extractor cannot know whether the extension it names is declared
elsewhere in the corpus. Emitting a placeholder from the referencing file is
wrong — two nodes would carry one id and two `source_file` values, and
`extract()`'s id-remap pass then splits the extension in half by prefixing one
with its file path. Leaving the endpoint dangling is also wrong: the edge is
dropped on the way into the graph.

So the decision belongs after every file has been read, which is exactly what
`graphify.resolver_registry` exists for.

Scale, measured on a real 1,140-extension tree: 258 distinct dependency targets,
of which 28 are declared by nothing in the corpus — optional integrations
(`facets`, `paragraphs`, `redirect`) that the site does not vendor. Without this
pass those 28 targets, roughly 11% of the dependency surface, would vanish
silently, which is the exact failure class this project exists to avoid.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: relation -> (node type, layer) for the endpoint this pass may materialise.
#: A relation absent from this table belongs to another producer, and its
#: dangling endpoint is not ours to invent.
_RESOLVABLE: dict[str, tuple[str, str]] = {
    "depends_on_module": ("drupal_extension", "extension"),
    "base_theme": ("drupal_extension", "extension"),
    "injects_service": ("drupal_service", "di"),
    "decorates": ("drupal_service", "di"),
    "parent_service": ("drupal_service", "di"),
    "injects_parameter": ("drupal_parameter", "di"),
    "requires_permission": ("drupal_permission", "routing"),
    "library_depends_on": ("drupal_library", "presentation"),
    "links_to_route": ("drupal_route", "routing"),
    "base_route": ("drupal_route", "routing"),
    "appears_on_route": ("drupal_route", "routing"),
    "in_menu": ("drupal_menu", "routing"),
    # The type is the child's: a menu link's parent is a menu link, a local
    # task's parent a local task. This entry is only the fallback.
    "parent_link": ("drupal_menu_link", "routing"),
}


#: Owner -> declared entity. The source is the extension that owns the file;
#: `core.services.yml` or `sites/development.services.yml` has no `*.info.yml`,
#: so its owner can be missing on the source side rather than the target side.
_OWNER_RELATIONS = frozenset({
    "declares_service", "declares_parameter", "declares_route", "declares_permission",
    "declares_library", "declares_breakpoint", "declares_menu_link",
    "declares_local_task", "declares_local_action", "declares_contextual_link",
    "defines_config", "defines_schema",
})


def _owner_name(source_file: Path) -> str:
    from graphify.drupal.config_stores import config_store
    from graphify.drupal.families import extension_owner

    owner = extension_owner(source_file)
    if owner:
        return owner
    store = config_store(source_file)
    return store.owner if store else ""


def _materialise_owner(edge: dict[str, Any]) -> dict[str, Any]:
    from graphify.drupal.paths import resolve_realm

    source_file = Path(str(edge.get("source_file", "")))
    return {
        "id": edge["source"],
        "label": _owner_name(source_file) or edge["source"],
        # Named by the file, declared by no *.info.yml: "concept" + external,
        # like a missing dependency, but its realm is known from where it sits.
        "file_type": "concept",
        "type": "drupal_extension",
        "layer": "extension",
        "realm": resolve_realm(source_file),
        "external": True,
        "_origin": "static_yaml",
        "source_file": str(source_file),
        "source_location": "L1",
    }


#: P1b relations whose target may be a config, an extension or a recipe; the
#: target's type is read from its id (plan Task 9).
_BY_PREFIX_RELATIONS = frozenset({
    "config_depends_on", "enforced_dependency", "installs_extension", "splits_extension",
    "splits_config", "imports_config", "config_action", "overrides_config", "applies_recipe",
})

#: Longest first: a schema id also starts with `drupal_config_`.
_PREFIX_TYPES: tuple[tuple[str, str, str], ...] = (
    ("drupal_config_schema_", "drupal_config_schema", "config"),
    ("drupal_config_", "drupal_config", "config"),
    ("drupal_extension_", "drupal_extension", "extension"),
    ("drupal_recipe_", "drupal_recipe", "extension"),
)


def _type_from_id(target: str) -> tuple[str, str] | None:
    for prefix, node_type, layer in _PREFIX_TYPES:
        if target.startswith(prefix):
            return node_type, layer
    return None


def _retarget_domain_overrides(all_nodes: list[dict], all_edges: list[dict]) -> None:
    known = {n.get("id") for n in all_nodes}
    for edge in all_edges:
        alt = edge.pop("alt_target", None)
        alt_name = edge.pop("alt_target_name", None)
        if alt is None:
            continue
        if edge.get("target") in known:
            continue
        if alt in known:
            edge["target"], edge["target_name"] = alt, alt_name
        else:
            edge["confidence"] = "INFERRED"


def _segments_match(pattern: list[str], name: list[str]) -> bool:
    return len(pattern) == len(name) and all(p in ("*", n) for p, n in zip(pattern, name))


def _specificity(pattern: list[str]) -> tuple[int, int]:
    """More literal segments first, then a longer literal prefix."""
    literal = sum(1 for p in pattern if p != "*")
    prefix = next((i for i, p in enumerate(pattern) if p == "*"), len(pattern))
    return literal, prefix


def _draw_schema_for(all_nodes: list[dict], all_edges: list[dict]) -> None:
    schemas = [n for n in all_nodes if n.get("type") == "drupal_config_schema"]
    exact = {n.get("schema_type"): n["id"] for n in schemas if not n.get("pattern")}
    # Group patterns by their first segment so each config tests only its own family.
    patterns: dict[str, list[tuple[list[str], str]]] = {}
    for n in schemas:
        if n.get("pattern"):
            parts = str(n.get("schema_type", "")).split(".")
            patterns.setdefault(parts[0], []).append((parts, n["id"]))
    for config in all_nodes:
        name = config.get("config_name")
        if not isinstance(name, str) or not config.get("type", "").startswith("drupal_"):
            continue
        if name in exact:
            all_edges.append(_schema_edge(exact[name], config, "EXTRACTED"))
            continue
        parts = name.split(".")
        family = patterns.get(parts[0], []) + patterns.get("*", [])
        candidates = [(p, sid) for p, sid in family if _segments_match(p, parts)]
        if candidates:
            _, sid = max(candidates, key=lambda c: _specificity(c[0]))
            all_edges.append(_schema_edge(sid, config, "INFERRED"))


def _schema_edge(schema: str, config: dict, confidence: str) -> dict[str, Any]:
    return {
        "source": schema, "target": config["id"], "relation": "schema_for",
        "confidence": confidence, "_origin": "static_yaml",
        "source_file": config.get("source_file", ""), "source_location": "L1",
    }


_EXTENSION_TYPES = frozenset({"drupal_module", "drupal_theme", "drupal_profile", "drupal_extension"})


def _mark_installed(all_nodes: list[dict], all_edges: list[dict]) -> None:
    from graphify.drupal.yaml_common import config_id

    core_extension = config_id("core.extension")
    if not any(n.get("id") == core_extension for n in all_nodes):
        return
    installed = {e["target"] for e in all_edges
                 if e.get("relation") == "installs_extension" and e.get("source") == core_extension}
    for n in all_nodes:
        if n.get("type") in _EXTENSION_TYPES:
            n["installed"] = n.get("id") in installed


def resolve_missing_targets(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Materialise an external node for every endpoint named but never declared.

    Targets of the relations in `_RESOLVABLE` and `_BY_PREFIX_RELATIONS`, and
    owners (sources) of the `declares_*` / `defines_*` relations whose file has no
    `*.info.yml`. Configuration decisions that need the whole corpus run first.
    """
    from graphify.drupal.yaml_common import config_id

    _retarget_domain_overrides(all_nodes, all_edges)
    _draw_schema_for(all_nodes, all_edges)

    known = {node.get("id") for node in all_nodes}
    type_of = {node.get("id"): node.get("type") for node in all_nodes}
    core_extension = config_id("core.extension")
    created: dict[str, dict[str, Any]] = {}

    for edge in all_edges:
        source = edge.get("source")
        relation = edge.get("relation")
        if (relation in _OWNER_RELATIONS and source
                and source not in known and source not in created):
            created[source] = _materialise_owner(edge)

        target = edge.get("target")
        if not target or target in known or target in created:
            continue
        if relation in _BY_PREFIX_RELATIONS:
            spec = _type_from_id(target)
        else:
            spec = _RESOLVABLE.get(relation)
        if spec is None:
            continue
        node_type, layer = spec
        if relation == "parent_link":
            node_type = type_of.get(source) or node_type
        created[target] = {
            "id": target,
            "label": edge.get("target_name") or target,
            # Not in the repository, so "concept" + external — the same shape
            # build.py gives its own external nodes, rather than a synthetic
            # source_file invented to satisfy the schema.
            "file_type": "concept",
            "type": node_type,
            "layer": layer,
            "realm": "unknown",
            "external": True,
            "_origin": "static_yaml",
            "source_file": edge.get("source_file", ""),
            "source_location": edge.get("source_location", "L1"),
        }
        if node_type == "drupal_config":
            created[target]["config_name"] = edge.get("target_name") or target
        if relation == "installs_extension" and source == core_extension:
            # The site installs an extension the code base does not contain.
            created[target]["missing"] = True

    all_nodes.extend(created.values())
    _mark_installed(all_nodes, all_edges)


#: Kept as the previous name so an older registration keeps working.
resolve_missing_extensions = resolve_missing_targets
