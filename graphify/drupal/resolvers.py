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

from collections.abc import Iterator
from contextlib import contextmanager
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
    # A hook only a boundary `*.api.php` declares (P2b §5.3): a missing stub,
    # named by the edge's `target_name`; Task 6 adds the registry's facts.
    "implements_hook": ("drupal_hook", "hook"),
    "invokes_hook": ("drupal_hook", "hook"),
}


#: Owner -> declared entity. The source is the extension that owns the file;
#: `core.services.yml` or `sites/development.services.yml` has no `*.info.yml`,
#: so its owner can be missing on the source side rather than the target side.
_OWNER_RELATIONS = frozenset({
    "declares_service", "declares_parameter", "declares_route", "declares_permission",
    "declares_library", "declares_breakpoint", "declares_menu_link",
    "declares_local_task", "declares_local_action", "declares_contextual_link",
    "defines_config", "defines_schema", "provides_plugin", "defines_plugin_type",
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
    from graphify.drupal.boundary import realm_of

    source_file = Path(str(edge.get("source_file", "")))
    # `_owner_name` derives an owner from the source file's name/directory
    # (a YAML family suffix, or a config store) -- it cannot do that for a PHP
    # source file, so `defines_plugin_type` carries the owner on the edge itself.
    owner = edge.get("owner") or _owner_name(source_file)
    return {
        "id": edge["source"],
        "label": owner or edge["source"],
        # Named by the file, declared by no *.info.yml: "concept" + external,
        # like a missing dependency, but its realm is known from where it sits.
        "file_type": "concept",
        "type": "drupal_extension",
        "layer": "extension",
        "realm": realm_of(source_file),
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
    "plugin_of_type",
})

#: Relations whose *source* (not target) may be missing from the corpus, and
#: what to materialise it as. `plugin_manager_for`'s source is the P1 service
#: node for the manager; when P1's own service extraction never ran over the
#: file that declares it (e.g. a manager-only incremental run), it is missing
#: rather than merely undeclared, so the materialised node says so.
_SOURCE_RESOLVABLE: dict[str, tuple[str, str]] = {
    "plugin_manager_for": ("drupal_service", "di"),
}


#: Longest first: a schema id also starts with `drupal_config_`.
_PREFIX_TYPES: tuple[tuple[str, str, str], ...] = (
    ("drupal_plugin_type_", "drupal_plugin_type", "plugin"),
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


#: Node types that stand for one config name (spec §4).
_CONFIG_TYPES = frozenset({"drupal_config", "drupal_config_split", "drupal_domain"})


def _draw_schema_for(all_nodes: list[dict], all_edges: list[dict]) -> None:
    # An incremental run hands unchanged nodes over as id/label/type only, so a
    # schema's type and a config's name fall back to the label, which is each.
    exact: dict[str, str] = {}
    # Group patterns by their first segment so each config tests only its own family.
    patterns: dict[str, list[tuple[list[str], str]]] = {}
    for n in all_nodes:
        if n.get("type") != "drupal_config_schema":
            continue
        type_ = str(n.get("schema_type") or n.get("label") or "")
        if "*" in type_:
            parts = type_.split(".")
            patterns.setdefault(parts[0], []).append((parts, n["id"]))
        elif type_:
            exact[type_] = n["id"]
    for config in all_nodes:
        if config.get("type") not in _CONFIG_TYPES:
            continue
        name = config.get("config_name") or config.get("label")
        if not isinstance(name, str) or not name:
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


# -- boundary facts (P2b spec §4.3) ----------------------------------------------

#: The scan root of the `extract()` call the resolver runs inside. The resolver's
#: signature has no root, so the seam's `extract` wrapper sets it (see
#: `scanning`); without one, boundary paths stay absolute.
_scan_root: Path | None = None


@contextmanager
def scanning(root: Path | None) -> Iterator[None]:
    """Resolve boundary paths against `root` for the duration of one `extract()`."""
    global _scan_root
    saved = _scan_root
    _scan_root = Path(root).resolve() if root is not None else None
    try:
        yield
    finally:
        _scan_root = saved


def _portable(path: str) -> str:
    """Relative to the scan root when inside it, else absolute (POSIX)."""
    if _scan_root is not None:
        try:
            return Path(path).resolve().relative_to(_scan_root).as_posix()
        except (ValueError, OSError, RuntimeError):
            pass
    return Path(path).as_posix()


#: A link stub's type -> the P1 family a learned plugin type reads it from.
_LINK_FAMILIES = {
    "drupal_menu_link": "links.menu",
    "drupal_local_task": "links.task",
    "drupal_local_action": "links.action",
    "drupal_contextual_link": "links.contextual",
}


class _BoundaryIndex:
    """Registry facts keyed by the node id a stub carries, each map built on first use.

    Keyed by id rather than label: a stub's label is its edge's `target_name`,
    which is absent on some edges (the label is then the id itself)."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self._maps: dict[str, dict[str, Any]] = {}

    def _map(self, kind: str) -> dict[str, Any]:
        found = self._maps.get(kind)
        if found is None:
            found = self._maps[kind] = self._build(kind)
        return found

    def _build(self, kind: str) -> dict[str, Any]:
        from graphify.drupal.discovery import type_id
        from graphify.drupal.hooks import hook_id
        from graphify.drupal.yaml_common import service_id
        from graphify.drupal.yaml_extract import extension_id

        r = self.registry
        if kind == "drupal_extension":
            return {extension_id(name): name for name in r.extensions}
        if kind == "drupal_service":
            return {service_id(sid): sid for sid in r.services}
        if kind == "drupal_plugin_type":
            return {type_id(pt): t for pt, t in r.types.items()}
        if kind == "drupal_hook":
            return {hook_id(name): decl for name, decl in r.hooks.items()}
        return {}

    def _extension_realm(self, name: str) -> str | None:
        from graphify.drupal.boundary import realm_of

        directory = self.registry.extensions.get(name)
        return realm_of(Path(directory)) if directory else None

    def facts(self, node: dict[str, Any]) -> dict[str, Any]:
        """What the registry knows about the stub `node` (nothing when it knows nothing)."""
        from graphify.drupal.boundary import realm_of
        from graphify.drupal.discovery import type_attributes

        kind = node.get("type")
        found = self._map(str(kind)).get(node.get("id"))
        if found is None:
            return {}
        r = self.registry
        facts: dict[str, Any] = {}
        if kind == "drupal_extension":
            info = r.extension_info.get(found)
            if info is not None:
                facts["extension_type"] = info[0]
            directory = r.extensions[found]
            facts["path"] = _portable(directory)
            facts["realm"] = realm_of(Path(directory))
        elif kind == "drupal_service":
            class_name, provider = r.services[found]
            facts["class_name"], facts["provider"] = class_name, provider
            realm = self._extension_realm(provider)
            if realm is not None:
                facts["realm"] = realm
        elif kind == "drupal_plugin_type":
            facts.update(type_attributes(found))
            facts["realm"] = realm_of(Path(found.class_file))
        elif kind == "drupal_hook":
            facts["provider"] = found.provider
            facts["declared_file"] = _portable(found.file)
            facts["line"] = found.line
            if found.pattern:
                facts["pattern"] = found.pattern
            facts["realm"] = realm_of(Path(found.file))
        return facts


def _mark_boundary(created: dict[str, dict[str, Any]]) -> None:
    """Every materialised node is the boundary; with a registry it gets its facts."""
    from graphify.drupal.discovery import current_registry

    registry = current_registry()
    index = _BoundaryIndex(registry) if registry is not None else None
    for stub in created.values():
        stub["boundary"] = True
        if index is not None:
            stub.update(index.facts(stub))


def _link_type_edge(stub: dict[str, Any], edge: dict[str, Any]) -> dict[str, Any] | None:
    """`plugin_of_type` from a materialised link stub to its family's learned type."""
    from graphify.drupal.yaml_plugins import plugin_type_for_family

    family = _LINK_FAMILIES.get(str(stub.get("type")))
    plugin_type = plugin_type_for_family(family) if family else None
    if plugin_type is None:
        return None
    type_id_, type_name = plugin_type
    return {
        "source": stub["id"], "target": type_id_, "relation": "plugin_of_type",
        "confidence": "EXTRACTED", "_origin": "static_yaml",
        "source_file": edge.get("source_file", ""),
        "source_location": edge.get("source_location", "L1"),
        "target_name": type_name,
    }


def resolve_missing_targets(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Materialise an external node for every endpoint named but never declared.

    Targets of the relations in `_RESOLVABLE` and `_BY_PREFIX_RELATIONS`, and
    owners (sources) of the `declares_*` / `defines_*` relations whose file has no
    `*.info.yml`. Configuration decisions that need the whole corpus run first.

    Every node made here is the boundary (`boundary: true`) and carries what the
    registry knows about it (spec §4.3); a link stub also gets `plugin_of_type`
    to its family's learned type.
    """
    from graphify.drupal.yaml_common import config_id

    _retarget_domain_overrides(all_nodes, all_edges)
    _draw_schema_for(all_nodes, all_edges)

    known = {node.get("id") for node in all_nodes}
    type_of = {node.get("id"): node.get("type") for node in all_nodes}
    core_extension = config_id("core.extension")
    created: dict[str, dict[str, Any]] = {}

    # By index: a link stub's `plugin_of_type` edge is appended below and is
    # resolved in turn (its type may itself be a boundary stub).
    i = 0
    while i < len(all_edges):
        edge = all_edges[i]
        i += 1
        source = edge.get("source")
        relation = edge.get("relation")
        if source and source not in known and source not in created:
            if relation in _OWNER_RELATIONS:
                created[source] = _materialise_owner(edge)
            else:
                source_spec = _SOURCE_RESOLVABLE.get(relation)
                if source_spec is not None:
                    node_type, layer = source_spec
                    created[source] = {
                        "id": source,
                        "label": edge.get("source_name") or source,
                        "file_type": "concept",
                        "type": node_type,
                        "layer": layer,
                        "realm": "unknown",
                        "external": True,
                        "missing": True,
                        "_origin": "static_yaml",
                        "source_file": edge.get("source_file", ""),
                        "source_location": edge.get("source_location", "L1"),
                    }

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
        if node_type == "drupal_hook":
            created[target]["hook_name"] = edge.get("target_name") or target
            created[target]["missing"] = True
        if node_type == "drupal_plugin_type":
            # A type no manager file in the graph defines. On a composer site
            # that is mostly a type the registry knows but whose manager lives
            # in gitignored core/contrib, which the graph leaves out.
            created[target]["missing"] = True
        if relation == "parent_link":
            typed = _link_type_edge(created[target], edge)
            if typed is not None:
                all_edges.append(typed)

    _mark_boundary(created)
    all_nodes.extend(created.values())


#: Kept as the previous name so an older registration keeps working.
resolve_missing_extensions = resolve_missing_targets
