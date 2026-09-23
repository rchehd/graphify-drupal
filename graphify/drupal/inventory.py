"""What plugin discovery did not recognise (spec §5.7).

`build_inventory` partitions every `<ext>.<name>.yml` file the site's
extension roots hold into what P1/P1b's fixed families, the learned plugin
registry, and the two families P5/P6 will eventually read already claim --
and reports the rest, plus every plugin manager the registry could not
resolve to a type. The seam (`register.py`'s wrapped `detect()`) builds and
writes this once per run, from the same file list `detect()` itself
returned; `report.generate`'s wrapper turns it into the "Drupal coverage"
section appended to `GRAPH_REPORT.md`.

Nothing here raises on bad input (spec §5.8): an unreadable or unparsable
YAML file is simply not counted toward `yaml_plugins`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graphify.drupal.config_stores import in_config_directory
from graphify.drupal.discovery import Registry
from graphify.drupal.families import is_drupal_file
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


def build_inventory(registry: Registry, detected_files: set[str], root: Path) -> dict:
    """The coverage inventory (spec §5.7): what plugin discovery, the P1/P1b
    families and P5/P6's deferred families claim of `detected_files`, and
    what is left over. `root` is the scan root `detected_files`' `examples`
    are made relative to."""
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
        yaml_plugin_count += len(data)

    filtered = 0
    for p in detected:
        if not p.endswith(".yml"):
            continue
        if p in root_yaml_set or p in deferred_paths:
            continue
        if is_drupal_file(Path(p)) or in_config_directory(Path(p)):
            continue
        filtered += 1

    unrecognised_files = sum(e["files"] for e in unrecognised)
    summary = {
        "types": len(registry.types),
        "registered_types": sum(1 for t in registry.types.values() if t.registered),
        "yaml_plugins": yaml_plugin_count,
        "deferred_files": len(deferred_paths),
        "unrecognised_families": len(unrecognised),
        "unrecognised_files": unrecognised_files,
        "filtered": filtered,
    }

    return {
        "unrecognised_yaml": unrecognised,
        "deferred": deferred_entries,
        "managers_unresolved": [dict(u) for u in registry.unresolved],
        "summary": summary,
    }


_SUMMARY_LABELS = (
    ("types", "plugin types"),
    ("registered_types", "registered types"),
    ("yaml_plugins", "YAML plugins"),
    ("deferred_files", "deferred files"),
    ("unrecognised_families", "unrecognised families"),
    ("unrecognised_files", "unrecognised files"),
    ("filtered", "filtered"),
)

_MAX_RENDERED_FAMILIES = 15
_MAX_RENDERED_CLASSES = 5


def render_section(inventory: dict) -> str:
    """Markdown "Drupal coverage" section appended to `GRAPH_REPORT.md`.

    Tolerant of a missing key or an inventory with nothing in it: every list
    renders as "none" and every summary count as 0, rather than raising.
    """
    inventory = inventory or {}
    summary = inventory.get("summary") or {}
    lines = ["## Drupal coverage", "", "| metric | value |", "| --- | --- |"]
    for key, label in _SUMMARY_LABELS:
        lines.append(f"| {label} | {summary.get(key, 0)} |")

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

    return "\n".join(lines)
