"""Where the static graph and the container disagree (P3 spec S8).

`compute` compares the graph the overlay was just laid over with the
artifact, and `write` puts the records in `<out>/drupal-divergence.json`,
beside `drupal-discovery.json`, on every build that applied an artifact;
`remove` deletes the file on a build that did not. Each record is

    {"kind": ..., "subject": <node id>, "type": <node type or None>,
     "static": {...} | None, "container": {...} | None, "possibly_stale": bool}

Kinds:

- `static_only`: a node of a runtime type (spec S7.7) the container does not
  know (`runtime: absent`);
- `container_only`: a container fact whose node the static graph did not have
  before the overlay (a `ServiceProvider` service, a derivative plugin);
- `conflict`: a one-target relation, or an attribute, the two sources disagree
  on (`OverlayResult.conflicts`);
- `extension_state`: an extension enabled in one of the sync store's
  `core.extension.yml` and the container but not the other.

Only custom subjects (and `drupal.include`'s realms) are logged, plus boundary
subjects already in the graph. `possibly_stale` marks a record whose subject
lives in a file a `stale` artifact's reasons name, or every record when
`composer.lock` changed. Records are sorted by `(kind, subject)`.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import networkx as nx

from graphify.drupal.container import Artifact
from graphify.drupal.container_overlay import RUNTIME_TYPES, OverlayResult

DIVERGENCE_FILENAME = "drupal-divergence.json"

_CORE_EXTENSION = "core.extension.yml"
_COMPOSER_REASON = "composer.lock changed"
_SOURCES_REASON = re.compile(r"^\d+ container source files changed: (.*)$")

#: Node attributes a record shows of its subject.
_SHOWN = ("label", "realm", "source_file", "class_name", "route_path")


def _shown(data: dict | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return {k: data[k] for k in _SHOWN if data.get(k) is not None}


class _Scope:
    """Which subjects are logged: custom (or included) ones, and boundary
    nodes already in the graph."""

    def __init__(self, G: nx.Graph, root: Path) -> None:
        from graphify.drupal.boundary import included_realms

        self.G = G
        self.included = included_realms(root)

    def custom(self, realm: object) -> bool:
        return realm == "custom" or (isinstance(realm, str) and realm in self.included)

    def logged(self, nid: str) -> bool:
        data = self.G.nodes[nid] if nid in self.G else None
        if data is None:
            return False
        return bool(data.get("boundary")) or self.custom(data.get("realm"))


def _record(kind: str, subject: str, node_type: str | None, static: dict | None,
            container: dict | None) -> dict[str, Any]:
    return {"kind": kind, "subject": subject, "type": node_type, "static": static,
            "container": container, "possibly_stale": False}


def _static_only(G: nx.Graph, scope: _Scope) -> list[dict]:
    return [_record("static_only", nid, data.get("type"), _shown(data), None)
            for nid, data in G.nodes(data=True)
            if data.get("type") in RUNTIME_TYPES and data.get("runtime") == "absent"
            and scope.logged(nid)]


def _container_only(G: nx.Graph, result: OverlayResult, scope: _Scope) -> list[dict]:
    records = []
    for node_type in RUNTIME_TYPES:
        for nid in sorted(result.seen.get(node_type, ())):
            if nid in result.static_ids or nid not in G:
                continue
            data = G.nodes[nid]
            if data.get("boundary") or not scope.custom(data.get("realm")):
                continue
            records.append(_record("container_only", nid, data.get("type"), None, _shown(data)))
    return records


def _conflicts(G: nx.Graph, result: OverlayResult) -> list[dict]:
    records = []
    for c in result.conflicts:
        source = str(c.get("source"))
        # An edge's attribute conflict names the edge `u->v`.
        node = source.split("->", 1)[0]
        node_type = G.nodes[node].get("type") if node in G else None
        relation = c.get("relation")
        records.append(_record("conflict", source, node_type,
                               {"relation": relation, "target": c.get("static_target")},
                               {"relation": relation, "target": c.get("container_target")}))
    return records


# -- extension_state ------------------------------------------------------------


def _core_extension_file(G: nx.Graph, root: Path) -> Path | None:
    """The sync store's `core.extension.yml`: the one the graph's
    `core.extension` config node came from, else one found near the root
    (P1b's config store rules)."""
    from graphify.drupal.boundary import install_map
    from graphify.drupal.config_stores import _nearby_sync_dirs, config_store
    from graphify.drupal.yaml_common import config_id

    candidates: list[Path] = []
    nid = config_id("core.extension")
    if nid in G:
        source_file = G.nodes[nid].get("source_file")
        if source_file:
            path = Path(str(source_file))
            candidates.append(path if path.is_absolute() else Path(root) / path)
    # Only a store of this site: under its composer root (a scan of `web/`
    # keeps its sync store beside `web/`), else under the scan root.
    imap = install_map(Path(root))
    base = Path(imap.project_root) if imap is not None and not imap.error else Path(root)
    base = base.resolve()
    for directory in _nearby_sync_dirs(Path(root)):
        if directory.resolve().is_relative_to(base):
            candidates.append(directory / _CORE_EXTENSION)
    for path in candidates:
        store = config_store(path)
        if path.is_file() and store is not None and store.kind == "sync":
            return path
    return None


def _static_enabled(path: Path) -> dict[str, str] | None:
    """name -> "module" | "theme" from `core.extension.yml`'s `module:`/`theme:` keys."""
    from graphify.drupal.yaml_common import load_drupal_yaml

    data, error = load_drupal_yaml(path)
    if error or not isinstance(data, dict):
        return None
    enabled: dict[str, str] = {}
    for key in ("module", "theme"):
        block = data.get(key)
        if isinstance(block, dict):
            for name in block:
                enabled.setdefault(str(name), key)
    return enabled


def _sha(keys: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()


def _extension_state(G: nx.Graph, artifact: Artifact, root: Path, scope: _Scope) -> list[dict]:
    from graphify.drupal.yaml_extract import extension_id

    path = _core_extension_file(G, root)
    static = _static_enabled(path) if path is not None else None
    if static is None:
        return []
    container: dict[str, str] = {}
    for e in artifact.data.get("extensions") or []:
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e.get("status") == 1:
            container[e["name"]] = str(e.get("type") or "module")
    # The collector's stamp: equal means the same set, nothing to compare.
    if artifact.stamp.get("enabled_extensions_sha") == _sha(
            {f"{t}:{n}" for n, t in static.items()}):
        return []
    records = []
    for name in sorted(static.keys() ^ container.keys()):
        nid = extension_id(name)
        if nid in G and not scope.logged(nid):
            continue
        in_static = name in static
        records.append(_record("extension_state", nid, "drupal_extension",
                               {"enabled": in_static}, {"enabled": not in_static}))
    return records


# -- possibly_stale ---------------------------------------------------------------


def _stale_files(result: OverlayResult) -> tuple[bool, set[str]]:
    """(every record, the files named) from a `stale` result's reasons."""
    if result.status != "stale":
        return False, set()
    everything = _COMPOSER_REASON in result.reasons
    files: set[str] = set()
    for reason in result.reasons:
        m = _SOURCES_REASON.match(reason)
        if m:
            files.update(p.strip() for p in m.group(1).split(",") if p.strip())
    return everything, files


def _subject_file(G: nx.Graph, subject: str) -> str | None:
    node = subject.split("->", 1)[0]
    if node not in G:
        return None
    source_file = G.nodes[node].get("source_file")
    return Path(str(source_file)).as_posix() if source_file else None


def compute(G: nx.Graph, result: OverlayResult, artifact: Artifact, root: Path) -> list[dict]:
    """The divergence records for `G` after `result` (spec S8), sorted by
    `(kind, subject)`."""
    root = Path(root)
    scope = _Scope(G, root)
    records = (_static_only(G, scope) + _container_only(G, result, scope)
               + _conflicts(G, result) + _extension_state(G, artifact, root, scope))
    everything, files = _stale_files(result)
    for record in records:
        if everything:
            record["possibly_stale"] = True
        elif files:
            record["possibly_stale"] = _subject_file(G, record["subject"]) in files
    return sorted(records, key=lambda r: (r["kind"], r["subject"],
                                          json.dumps(r, sort_keys=True, default=str)))


def write(records: list[dict], out: Path) -> Path:
    """`records` to `<out>/drupal-divergence.json` (sorted keys, one-space indent)."""
    target = Path(out) / DIVERGENCE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(records, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return target


def remove(out: Path) -> None:
    """Delete `<out>/drupal-divergence.json` if present; never raises."""
    try:
        (Path(out) / DIVERGENCE_FILENAME).unlink()
    except OSError:
        pass
