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
})


def _materialise_owner(edge: dict[str, Any]) -> dict[str, Any]:
    from graphify.drupal.families import extension_owner
    from graphify.drupal.paths import resolve_realm

    source_file = Path(str(edge.get("source_file", "")))
    return {
        "id": edge["source"],
        "label": extension_owner(source_file) or edge["source"],
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


def resolve_missing_targets(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Materialise an external node for every endpoint named but never declared.

    Targets of the relations in `_RESOLVABLE`, and owners (sources) of the
    `declares_*` relations whose file has no `*.info.yml`.
    """
    known = {node.get("id") for node in all_nodes}
    type_of = {node.get("id"): node.get("type") for node in all_nodes}
    created: dict[str, dict[str, Any]] = {}

    for edge in all_edges:
        source = edge.get("source")
        if (edge.get("relation") in _OWNER_RELATIONS and source
                and source not in known and source not in created):
            created[source] = _materialise_owner(edge)

        spec = _RESOLVABLE.get(edge.get("relation"))
        if spec is None:
            continue
        target = edge.get("target")
        if not target or target in known or target in created:
            continue
        node_type, layer = spec
        if edge.get("relation") == "parent_link":
            node_type = type_of.get(edge.get("source")) or node_type
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

    all_nodes.extend(created.values())


#: Kept as the previous name so an older registration keeps working.
resolve_missing_extensions = resolve_missing_targets
