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

from typing import Any

#: Only relations this package emits are resolved here. Another language's
#: dangling edge is not ours to materialise.
_EXTENSION_RELATIONS = frozenset({"depends_on_module", "base_theme"})


def resolve_missing_extensions(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Materialise an external node for every extension named but not declared."""
    known = {node.get("id") for node in all_nodes}
    created: dict[str, dict[str, Any]] = {}

    for edge in all_edges:
        if edge.get("relation") not in _EXTENSION_RELATIONS:
            continue
        target = edge.get("target")
        if not target or target in known or target in created:
            continue
        created[target] = {
            "id": target,
            "label": edge.get("target_name") or target,
            # Not in the repository, so "concept" + external — the same shape
            # build.py gives its own external nodes, rather than a synthetic
            # source_file invented to satisfy the schema.
            "file_type": "concept",
            "type": "drupal_extension",
            "layer": "extension",
            "realm": "unknown",
            "external": True,
            "_origin": "static_yaml",
            "source_file": edge.get("source_file", ""),
            "source_location": edge.get("source_location", "L1"),
        }

    all_nodes.extend(created.values())
