"""What plugin discovery did not recognise (spec §5.7).

`build_inventory` partitions every `<ext>.<name>.yml` file the site's
extension roots hold into what P1/P1b's fixed families, the learned plugin
registry, and the two families P5/P6 will eventually read already claim --
and reports the rest, plus every plugin manager the registry could not
resolve to a type. The seam (`register.py`'s wrapped `detect()`) builds and
writes this once per run, from the same file list `detect()` itself
returned; `report.generate`'s wrapper turns it into the "Drupal coverage"
section appended to `GRAPH_REPORT.md`.

It also lists every hook candidate (P2b spec §5.4): what looks like a hook
implementation but names no declared hook literally; and every PHP candidate
(P4 spec §10): what custom PHP says that could not be read as a fact, such as
a plugin attribute of no learned type.

Nothing here raises on bad input (spec §5.8): an unreadable or unparsable
YAML file is simply not counted toward `yaml_plugins`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from graphify.drupal.boundary import boundary_dir, install_map
from graphify.drupal.config_stores import in_config_directory
from graphify.drupal.discovery import Registry
from graphify.drupal.families import is_drupal_file
from graphify.drupal.fingerprint import drupal_fingerprint
from graphify.drupal.hooks import find_hook_candidates, is_procedural_file
from graphify.drupal.php_semantics import find_php_candidates
from graphify.drupal.yaml_common import load_drupal_yaml
from graphify.drupal.yaml_plugins import learned_family

#: Family name -> the phase that will read it (spec §5.7). "migrations" (P6's
#: directory-discovered family, `migrations/*.yml` under an extension) is
#: counted the same way but is not itself a `<ext>.<name>.yml` family name,
#: so it has no entry here -- see `_deferred_entries`.
DEFERRED_FAMILIES = {"component": "P5", "migrate_drupal": "P6"}

_MIGRATIONS_FAMILY = "migrations"
_MIGRATIONS_PHASE = "P6"

_INVENTORY_FILENAME = "drupal-inventory.json"

_MAX_UNRECOGNISED_EXAMPLES = 3

_current_inventory: dict | None = None


def set_current_inventory(inventory: dict | None) -> None:
    """Set this process's in-memory inventory (mirrors `discovery.set_current`)."""
    global _current_inventory
    _current_inventory = inventory


def current_inventory() -> dict | None:
    """This process's inventory, built by the most recent `detect()` in it."""
    return _current_inventory


def load_inventory(out: Path) -> dict | None:
    """The inventory written to `<out>/drupal-inventory.json`, or None."""
    try:
        return json.loads((Path(out) / _INVENTORY_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_inventory(inventory: dict, out: Path) -> Path:
    """Write `inventory` to `<out>/drupal-inventory.json`; return that path."""
    target = Path(out) / _INVENTORY_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(inventory, sort_keys=True, indent=1), encoding="utf-8")
    return target


def remove_inventory(out: Path) -> None:
    """Delete `<out>/drupal-inventory.json` if present.

    Called when a run finds no Drupal marker (`prepare_run` returned None):
    without this, a STALE inventory a previous Drupal run left in the same
    out dir would keep being picked up by `report.generate`'s file fallback
    (`load_inventory`) for a tree that is no longer Drupal, or no longer
    scanned here, at all. Tolerant of a missing file or any other OSError,
    same as everything else in this module (spec §5.8): nothing here raises.
    """
    try:
        (Path(out) / _INVENTORY_FILENAME).unlink()
    except OSError:
        pass


def _owner_and_family(path: str, registry: Registry) -> tuple[str, str] | None:
    """`(owner, family)` for a `registry.root_yaml` entry: `<owner>.<family>.yml`,
    `owner` being whichever extension's root directory holds it (`core` for
    `core/core.<family>.yml`). None if no extension in the registry claims it."""
    p = Path(path)
    name = p.name
    parent = p.parent.as_posix()
    for ext, directory in registry.extensions.items():
        if Path(directory).as_posix() != parent or not name.startswith(f"{ext}."):
            continue
        prefix_len = len(ext) + 1
        if name.endswith(".yml") and len(name) > prefix_len + len(".yml"):
            return ext, name[prefix_len:-len(".yml")]
    return None


def _under_extension_tree(path: str, ext_dirs: list[str]) -> bool:
    posix = Path(path).as_posix()
    return any(posix == d or posix.startswith(d + "/") for d in ext_dirs)


def _relative(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return Path(path).as_posix()


def _deferred_entries(
    registry: Registry, detected: set[str], root_yaml_detected: list[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """The `deferred` list and the set of files it accounts for.

    `component` files are `*.component.yml` anywhere under an extension
    directory; `migrate_drupal` files are root-scoped (like every other
    `<ext>.<name>.yml` family); `migrations` files are `migrations/*.yml`
    anywhere under an extension directory (P6's directory discovery).
    """
    ext_dirs = sorted({Path(d).as_posix() for d in registry.extensions.values()})

    component_files = {
        p for p in detected
        if p.endswith(".component.yml") and _under_extension_tree(p, ext_dirs)
    }
    migrate_drupal_files = set()
    for p in root_yaml_detected:
        found = _owner_and_family(p, registry)
        if found is not None and found[1] == "migrate_drupal":
            migrate_drupal_files.add(p)
    migrations_files = {
        p for p in detected
        if p.endswith(".yml") and Path(p).parent.name == "migrations"
        and _under_extension_tree(p, ext_dirs)
    }

    entries: list[dict[str, Any]] = []
    if component_files:
        entries.append({"family": "component", "phase": DEFERRED_FAMILIES["component"],
                        "files": len(component_files)})
    if migrate_drupal_files:
        entries.append({"family": "migrate_drupal", "phase": DEFERRED_FAMILIES["migrate_drupal"],
                        "files": len(migrate_drupal_files)})
    if migrations_files:
        entries.append({"family": _MIGRATIONS_FAMILY, "phase": _MIGRATIONS_PHASE,
                        "files": len(migrations_files)})
    entries.sort(key=lambda e: (e["phase"], e["family"]))

    return entries, component_files | migrate_drupal_files | migrations_files


#: `files` is not a realm: it counts each site's public files directory.
_BOUNDARY_KINDS = ("core", "contrib", "vendor", "files")


def _resolved(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(path).absolute()


def _boundary_counts(registry: Registry, root: Path) -> tuple[dict[str, int], dict[str, int], str]:
    """`(boundary, boundary_reasons, composer error)` for the scan `root` (spec §4.2).

    Counts the directories `detect()` prunes as boundary trees, read from the
    composer install map rather than from a walk: every install dir that
    exists under `root`, is still a boundary after `drupal.include`, and is
    not inside another counted one (a package under `vendor/` is pruned with
    it). Without a usable install map the pruning follows P0's path rules, so
    the candidates are the registry's extension directories plus the vendor
    directory beside the web root. Every site's `sites/<site>/files` counts
    under `files`.
    """
    imap = install_map(root)
    if imap is not None and not imap.error:
        candidates = [Path(d) for d, _realm, _reason in imap.paths]
    else:
        candidates = [Path(d) for d in registry.extensions.values()]
        if registry.web_root:
            candidates.append(Path(registry.web_root).parent / "vendor")
    if registry.web_root:
        try:
            candidates.extend(sorted((Path(registry.web_root) / "sites").glob("*/files")))
        except OSError:
            pass

    scan_root = _resolved(root)
    pruned: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        resolved = _resolved(candidate)
        try:
            resolved.relative_to(scan_root)
        except ValueError:
            continue
        if not candidate.is_dir():
            continue
        found = boundary_dir(candidate)
        if found is not None:
            pruned[resolved.as_posix()] = found
    outermost = [
        d for d in pruned
        if not any(d != other and d.startswith(other + "/") for other in pruned)
    ]

    boundary = dict.fromkeys(_BOUNDARY_KINDS, 0)
    reasons: dict[str, int] = {}
    for d in outermost:
        realm, reason = pruned[d]
        boundary[realm] += 1
        reasons[reason] = reasons.get(reason, 0) + 1
    return boundary, dict(sorted(reasons.items())), imap.error if imap is not None else ""


def _hook_candidates(registry: Registry, detected: set[str], root: Path) -> list[dict[str, Any]]:
    """Every `hook_candidates` entry (P2b spec §5.4) of the detected PHP and
    procedural files, `file` relative to `root`.

    Read here, from the full file list every `detect()` returns, rather than
    collected from extraction results: an incremental run extracts only the
    changed files, and a candidate in an unchanged one must not drop out of
    the inventory. The same `hooks.find_hook_candidates` the extractor uses
    decides, so the two cannot disagree.
    """
    found: list[dict[str, Any]] = []
    for p in sorted(detected):
        path = Path(p)
        if path.suffix != ".php" and not is_procedural_file(path):
            continue
        for entry in find_hook_candidates(path, registry):
            found.append({**entry, "file": _relative(entry["file"], root)})
    found.sort(key=lambda e: (e["kind"], e["module"], e["name"], e["file"], e["line"]))
    return found


_PHP_CANDIDATES_FILENAME = "drupal-php-candidates.json"


def _cache_digest(registry: Registry) -> str:
    """What every cached entry was read against: this package's code (the
    fingerprint core's AST cache is namespaced with, see
    `register._patch_cache`) and the whole registry."""
    digest = hashlib.sha1(drupal_fingerprint().encode("utf-8"))
    digest.update(b"\0")
    digest.update(json.dumps(registry.to_json(), sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def _stat_key(path: Path) -> list[int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino]


def _php_candidates(registry: Registry, detected: set[str], root: Path,
                    cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every `php_candidates` entry (P4 spec §10) of the detected PHP and
    procedural files (services are used in both), `file` relative to
    `root`: read from the full file list, like `_hook_candidates`, with the
    extractor's own `find_php_candidates`.

    Parsing every custom PHP file on each `detect()` costs about half a
    second on the reference corpus, so with a `cache_dir` a file's entries
    are kept in `<cache_dir>/drupal-php-candidates.json`, keyed by its
    mtime, ctime, size and inode, and valid only for the code and the
    registry they were read against (`_cache_digest`): a changed registry
    or an edited extractor reads every file again."""
    digest = _cache_digest(registry) if cache_dir is not None else ""
    cached: dict[str, Any] = {}
    if cache_dir is not None:
        try:
            data = json.loads((Path(cache_dir) / _PHP_CANDIDATES_FILENAME).read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("registry") == digest \
                    and isinstance(data.get("files"), dict):
                cached = data["files"]
        except (OSError, ValueError, TypeError):
            cached = {}
    fresh: dict[str, Any] = {}
    found: list[dict[str, Any]] = []
    for p in sorted(detected):
        path = Path(p)
        if path.suffix != ".php" and not is_procedural_file(path):
            continue
        key = _stat_key(path) if cache_dir is not None else None
        entry = cached.get(p)
        if key is not None and isinstance(entry, dict) and entry.get("stat") == key \
                and isinstance(entry.get("found"), list):
            entries = entry["found"]
        else:
            entries = find_php_candidates(path, registry)
        if key is not None:
            fresh[p] = {"stat": key, "found": entries}
        for item in entries:
            found.append({**item, "file": _relative(item["file"], root)})
    if cache_dir is not None and fresh != cached:
        try:
            target = Path(cache_dir) / _PHP_CANDIDATES_FILENAME
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"registry": digest, "files": fresh}, sort_keys=True),
                              encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass
    found.sort(key=lambda e: (e["kind"], e["module"], e["file"], e["line"]))
    return found


def build_inventory(registry: Registry, detected_files: set[str], root: Path,
                    cache_dir: Path | None = None) -> dict:
    """The coverage inventory (spec §5.7): what plugin discovery, the P1/P1b
    families and P5/P6's deferred families claim of `detected_files`, and
    what is left over. `root` is the scan root `detected_files`' `examples`
    are made relative to; `cache_dir` (the out dir) keeps the PHP
    candidates between runs (`_php_candidates`)."""
    root = Path(root).resolve()
    detected = {str(Path(p).absolute()) for p in detected_files}
    root_yaml = [str(Path(p).absolute()) for p in registry.root_yaml]
    root_yaml_set = set(root_yaml)
    root_yaml_detected = [p for p in root_yaml if p in detected]

    deferred_entries, deferred_paths = _deferred_entries(registry, detected, root_yaml_detected)
    deferred_family_names = {e["family"] for e in deferred_entries} | set(DEFERRED_FAMILIES)

    groups: dict[str, dict[str, Any]] = {}
    for p in root_yaml_detected:
        if is_drupal_file(Path(p)):
            continue
        found = _owner_and_family(p, registry)
        if found is None:
            continue
        owner, family = found
        if family in deferred_family_names:
            continue
        group = groups.setdefault(family, {"family": family, "files": 0, "owners": set(), "examples": []})
        group["files"] += 1
        group["owners"].add(owner)
        group["examples"].append(p)

    unrecognised = []
    for family, group in groups.items():
        examples = sorted(_relative(p, root) for p in group["examples"])[:_MAX_UNRECOGNISED_EXAMPLES]
        unrecognised.append({
            "family": family,
            "files": group["files"],
            "owners": sorted(group["owners"]),
            "examples": examples,
        })
    unrecognised.sort(key=lambda e: (-e["files"], e["family"]))

    yaml_plugin_count = 0
    for p in root_yaml_detected:
        if learned_family(Path(p)) is None:
            continue
        data, error = load_drupal_yaml(Path(p))
        if error or not data:
            continue
        # Exactly what `extract_drupal_yaml_plugins` emits: a mapping or a null
        # definition is a plugin; any other top-level value is skipped there.
        yaml_plugin_count += sum(1 for v in data.values() if v is None or isinstance(v, dict))

    filtered = 0
    for p in detected:
        if not p.endswith(".yml"):
            continue
        if p in root_yaml_set or p in deferred_paths:
            continue
        if is_drupal_file(Path(p)) or in_config_directory(Path(p)):
            continue
        filtered += 1

    candidates = _hook_candidates(registry, detected, root)
    php_candidates = _php_candidates(registry, detected, root, cache_dir)

    unrecognised_files = sum(e["files"] for e in unrecognised)
    boundary, boundary_reasons, composer_error = _boundary_counts(registry, root)
    summary = {
        "types": len(registry.types),
        "registered_types": sum(1 for t in registry.types.values() if t.registered),
        "yaml_plugins": yaml_plugin_count,
        "deferred_files": len(deferred_paths),
        "unrecognised_families": len(unrecognised),
        "unrecognised_files": unrecognised_files,
        "filtered": filtered,
        "boundary": boundary,
        "boundary_reasons": boundary_reasons,
        "hook_candidates": len(candidates),
        "php_candidates": len(php_candidates),
    }

    inventory = {
        "unrecognised_yaml": unrecognised,
        "deferred": deferred_entries,
        "managers_unresolved": [dict(u) for u in registry.unresolved],
        "hook_candidates": candidates,
        "php_candidates": php_candidates,
        "summary": summary,
    }
    if composer_error:
        inventory["composer_unreadable"] = composer_error
    return inventory


#: Edge relations P4 counts in the graph (spec §10).
_P4_RELATIONS = ("alters_form", "hooks_entity_type", "subscribes_to_event")


def graph_counts(graph: Any) -> dict[str, Any]:
    """What P4 put in the graph (spec §10): plugins by type, entity types,
    forms and entity forms (boundary stubs left out), `uses_service` by
    `via`, `calls` bound from a resolved receiver (static, and the P3
    overlay's separately), and the `alters_form`, `hooks_entity_type`,
    `subscribes_to_event` edges. `graph` is a networkx graph or a
    graph.json-shaped dict; anything else counts nothing. Never raises."""
    counts: dict[str, Any] = {
        "plugins": {}, "entity_types": 0, "forms": 0, "entity_forms": 0,
        "uses_service": {}, "calls_bound": 0, "calls_container": 0,
        **{r: 0 for r in _P4_RELATIONS},
    }
    try:
        nodes, edges = _graph_items(graph)
        for n in nodes:
            if not isinstance(n, dict) or n.get("boundary"):
                continue
            kind = n.get("type")
            if kind == "drupal_plugin":
                ptype = str(n.get("plugin_type") or "")
                counts["plugins"][ptype] = counts["plugins"].get(ptype, 0) + 1
            elif kind == "drupal_entity_type":
                counts["entity_types"] += 1
            elif kind == "drupal_form":
                counts["entity_forms" if n.get("entity_form") else "forms"] += 1
        for e in edges:
            if not isinstance(e, dict):
                continue
            relation = e.get("relation")
            if relation == "uses_service":
                via = str(e.get("via") or "")
                counts["uses_service"][via] = counts["uses_service"].get(via, 0) + 1
            elif relation == "calls" and e.get("service"):
                counts["calls_container" if e.get("origin") == "container" else "calls_bound"] += 1
            elif relation in _P4_RELATIONS:
                counts[relation] += 1
    except Exception as exc:
        # Recorded, so the report can tell a failed count from a zero.
        counts["counts_error"] = f"{type(exc).__name__}: {exc}"
    counts["plugins"] = dict(sorted(counts["plugins"].items()))
    counts["uses_service"] = dict(sorted(counts["uses_service"].items()))
    return counts


def _graph_items(graph: Any) -> tuple[list, list]:
    if isinstance(graph, dict):
        links = graph.get("links")
        return list(graph.get("nodes") or []), list(links if isinstance(links, list)
                                                    else graph.get("edges") or [])
    nodes = [dict(data) | {"id": nid} for nid, data in graph.nodes(data=True)]
    edges = [dict(data) for _u, _v, data in graph.edges(data=True)]
    return nodes, edges


def _render_graph_counts(counts: dict) -> list[str]:
    plugins = counts.get("plugins") or {}
    lines = ["### PHP semantics", "", "| metric | value |", "| --- | --- |",
             f"| plugins | {sum(plugins.values()) if isinstance(plugins, dict) else 0} |",
             f"| plugins by type | {_counts(plugins)} |",
             f"| entity types | {counts.get('entity_types', 0)} |",
             f"| forms | {counts.get('forms', 0)} |",
             f"| entity forms | {counts.get('entity_forms', 0)} |",
             f"| uses_service by via | {_counts(counts.get('uses_service'))} |",
             f"| calls bound (static) | {counts.get('calls_bound', 0)} |",
             f"| calls bound (container) | {counts.get('calls_container', 0)} |"]
    lines += [f"| {r} | {counts.get(r, 0)} |" for r in _P4_RELATIONS]
    if counts.get("counts_error"):
        lines += ["", f"counts incomplete: {counts['counts_error']}"]
    return lines


_SUMMARY_LABELS = (
    ("types", "plugin types"),
    ("registered_types", "registered types"),
    ("yaml_plugins", "YAML plugins"),
    ("deferred_files", "deferred files"),
    ("unrecognised_families", "unrecognised families"),
    ("unrecognised_files", "unrecognised files"),
    ("filtered", "filtered"),
    ("hook_candidates", "hook candidates"),
    ("php_candidates", "PHP candidates"),
)

_MAX_RENDERED_FAMILIES = 15
_MAX_RENDERED_CLASSES = 5


def _counts(counts: object, order: tuple[str, ...] = ()) -> str:
    """`core 1, contrib 101` -- `order`'s keys first, then the rest by name;
    "none" for a missing or empty mapping."""
    if not isinstance(counts, dict) or not counts:
        return "none"
    keys = [k for k in order if k in counts] + sorted(k for k in counts if k not in order)
    return ", ".join(f"{k} {counts[k]}" for k in keys)


def render_section(inventory: dict) -> str:
    """Markdown "Drupal coverage" section appended to `GRAPH_REPORT.md`.

    The graph's P4 counts (`graph_counts`) render when the inventory
    carries them under `graph` (the report seam adds them).

    Tolerant of a missing key or an inventory with nothing in it: every list
    renders as "none" and every summary count as 0, rather than raising.
    """
    inventory = inventory or {}
    summary = inventory.get("summary") or {}
    lines = ["## Drupal coverage", "", "| metric | value |", "| --- | --- |"]
    for key, label in _SUMMARY_LABELS:
        lines.append(f"| {label} | {summary.get(key, 0)} |")
    lines.append(f"| boundary dirs | {_counts(summary.get('boundary'), _BOUNDARY_KINDS)} |")
    lines.append(f"| boundary reasons | {_counts(summary.get('boundary_reasons'))} |")
    if inventory.get("composer_unreadable"):
        lines += ["", f"composer: {inventory['composer_unreadable']} -- P0's path rules "
                      "decided the realms and the boundary instead."]

    lines += ["", "### Unrecognised YAML families"]
    unrecognised = inventory.get("unrecognised_yaml") or []
    if unrecognised:
        for entry in unrecognised[:_MAX_RENDERED_FAMILIES]:
            owners = ", ".join(entry.get("owners") or [])
            examples = entry.get("examples") or []
            example = f" (e.g. `{examples[0]}`)" if examples else ""
            lines.append(f"- `{entry.get('family')}` — {entry.get('files', 0)} file(s), "
                         f"owners: {owners}{example}")
    else:
        lines.append("- none")

    lines += ["", "### Deferred"]
    deferred = inventory.get("deferred") or []
    if deferred:
        for entry in deferred:
            lines.append(f"- `{entry.get('family')}` -> {entry.get('phase')} "
                         f"({entry.get('files', 0)} file(s))")
    else:
        lines.append("- none")

    lines += ["", "### Hook candidates"]
    candidates = inventory.get("hook_candidates") or []
    if candidates:
        by_kind: dict[str, int] = {}
        for c in candidates:
            by_kind[c.get("kind", "")] = by_kind.get(c.get("kind", ""), 0) + 1
        for kind in sorted(by_kind):
            lines.append(f"- {kind}: {by_kind[kind]}")
    else:
        lines.append("- none")

    lines += ["", "### PHP candidates"]
    php_candidates = inventory.get("php_candidates") or []
    if php_candidates:
        php_by_kind: dict[str, int] = {}
        for c in php_candidates:
            php_by_kind[c.get("kind", "")] = php_by_kind.get(c.get("kind", ""), 0) + 1
        for kind in sorted(php_by_kind):
            lines.append(f"- {kind}: {php_by_kind[kind]}")
    else:
        lines.append("- none")

    graph = inventory.get("graph")
    if isinstance(graph, dict):
        lines += ["", *_render_graph_counts(graph)]

    lines += ["", "### Unresolved managers"]
    unresolved = inventory.get("managers_unresolved") or []
    if unresolved:
        by_reason: dict[str, list[str]] = {}
        for u in unresolved:
            by_reason.setdefault(u.get("reason", ""), []).append(u.get("class", ""))
        for reason in sorted(by_reason):
            classes = [c for c in by_reason[reason] if c]
            shown = ", ".join(f"`{c}`" for c in classes[:_MAX_RENDERED_CLASSES])
            suffix = f": {shown}" if shown else ""
            lines.append(f"- {reason} ({len(by_reason[reason])}){suffix}")
    else:
        lines.append("- none")

    container = inventory.get("container")
    if isinstance(container, dict):
        lines += ["", *_render_container(container)]

    return "\n".join(lines)


_CONTAINER_SOURCES = ("services", "aliases", "routes", "extensions", "hooks", "plugins",
                      "subscribers")
_EDGE_OUTCOMES = ("confirmed", "container_only", "conflict")


def container_status(container: dict) -> str:
    """`fresh`, `unavailable`, or `stale|invalid|error (reasons)` (P3 spec S9)."""
    status = str(container.get("status") or "unavailable")
    reasons = [str(r) for r in container.get("reasons") or []]
    return f"{status} ({'; '.join(reasons)})" if reasons else status


def _render_container(container: dict) -> list[str]:
    """The "Container" block (P3 spec S9): what the artifact laid over the
    graph, or, without one, what a static-only graph cannot know."""
    from graphify.drupal.container_overlay import NOT_STATIC

    lines = ["### Container", "", f"- status: {container_status(container)}"]
    if container.get("status") in ("unavailable", "invalid", "error"):
        lines += ["", "Not statically knowable without the container "
                      "(`graphify drupal container`):"]
        lines += [f"- {what}: {why}" for what, why in NOT_STATIC]
        return lines

    stamp = container.get("stamp") or {}
    lines.append("- stamp: " + ", ".join(
        f"{key}: {stamp[key]}" for key in ("git_commit", "created_at", "runner", "drupal_version")
        if stamp.get(key) is not None) if stamp else "- stamp: none")
    counts = container.get("counts") or {}
    lines += ["", "| source | in artifact | custom | applied |", "| --- | --- | --- | --- |"]
    for source in _CONTAINER_SOURCES:
        c = counts.get(source) or {}
        lines.append(f"| {source} | {c.get('total', 0)} | {c.get('custom', 0)} | "
                     f"{c.get('applied', 0)} |")
    edges = container.get("edges") or {}
    lines += ["", "- edges: " + ", ".join(f"{k} {edges.get(k, 0)}" for k in _EDGE_OUTCOMES)]
    lines.append(f"- runtime: absent: {_counts(container.get('runtime_absent'))}")
    lines.append(f"- divergences: {_counts(container.get('divergence'))}")
    for record in container.get("records") or []:
        stale = " (possibly stale)" if record.get("possibly_stale") else ""
        lines.append(f"  - {record.get('kind')}: `{record.get('subject')}`{stale}")
    errors = container.get("errors") or []
    lines.append(f"- collector errors: {len(errors)}")
    for error in errors:
        if isinstance(error, dict):
            lines.append(f"  - {error.get('source')}: {error.get('class')}: "
                         f"{error.get('message')}")
    return lines
