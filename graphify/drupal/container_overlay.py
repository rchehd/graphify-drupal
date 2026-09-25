"""The container overlay: the artifact laid over the assembled graph (spec S7).

`apply` mutates the graph core built, in place and idempotently. It first
`undo`es whatever a previous overlay left (graph.json carries it into
`update` and `watch`), then maps the artifact's facts onto the graph:

- a static edge the container also knows gets `confirmed_by: container`;
- an edge only the container knows is added with `origin: container`
  (`_origin` stays `"ast"`, as on everything the Drupal producers emit);
- a static edge of a one-target relation pointing elsewhere is a conflict:
  both edges stay, and the pair is recorded;
- an attribute is added only where absent; a different existing value is
  recorded, never overwritten;
- every node of the runtime types gets `runtime: present | absent`.

The boundary rule is P2b's (spec S7.6): a fact whose subject is custom (or in
`drupal.include`) is applied in full; a fact about core, contrib or vendor
only adds attributes to a node already in the graph. A boundary stub is made
only as the direct target of a custom fact's edge.

Undo markers: `_overlay: True` on nodes the overlay created, and
`_overlay_attrs: [keys]` on every node and edge it touched. The container
never overwrites a value, but the boundary facts (`apply_boundary_facts`,
spec S7.8) replace a stale one; the value they replaced is kept in
`_overlay_prev: {key: value}`, which `undo` puts back -- unless the key no
longer holds what they wrote (`_overlay_set`): a static producer then owns it.

It maps `services`, `aliases`, `routes`, `extensions`, `hooks` (P2b's
implementation shape, plus each implementation's `runtime_order`), `plugins`
and `subscribers`.

`run_for_build` is what the build seam calls on every graph core builds while
a Drupal run is current (spec S7.1): artifact, staleness, `apply`, the
boundary facts, the divergence log and the report's inventory block.

A `drupal_hook_impl` node the overlay makes has its implementation's PHP file
as `source_file` on purpose, as P2b's would: when core re-extracts that file it
evicts the node, and the overlay rebuilds it in the same build.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

from graphify.drupal.boundary import included_realms, install_map, realm_of
from graphify.drupal.container import Artifact, ArtifactError, load_artifact, staleness
from graphify.drupal.discovery import type_id
from graphify.drupal.hooks import hook_id, hook_impl_id
from graphify.drupal.yaml_common import (
    link_id,
    parameter_id,
    permission_id,
    plugin_id,
    route_id,
    service_id,
    tag_id,
)
from graphify.drupal.yaml_extract import extension_id
from graphify.ids import make_id

ORIGIN = "container"

_OVERLAY = "_overlay"
_ATTRS = "_overlay_attrs"
#: `{key: value}` a replaced attribute had before the overlay; `undo` restores it.
_PREV = "_overlay_prev"
#: `{key: value}` the boundary facts wrote; a different value at undo is a static producer's.
_SET = "_overlay_set"
#: The `source_file` an overlay item was created with; see `_owned`.
_OWN_FILE = "_overlay_file"

#: Node types that get `runtime` when an artifact is applied (spec S7.7).
RUNTIME_TYPES = ("drupal_service", "drupal_route", "drupal_extension", "drupal_plugin",
                 "drupal_hook_impl")

#: Relations with one target per source: a different static target is a conflict (S7.5).
_SINGLE_TARGET = frozenset({
    "service_implemented_by", "plugin_implemented_by", "routes_to", "routes_to_form", "decorates",
})

#: Keys an edge's own bookkeeping owns; a tag attribute never overwrites them.
_EDGE_RESERVED = frozenset({
    "source", "target", "relation", "origin", "_origin", "confidence", "confidence_score",
    "source_file", "source_location", "_src", "_tgt", "confirmed_by", _ATTRS, _PREV, _SET, _OWN_FILE,
})

#: Drupal 11.1+'s stand-in class for a procedural hook implementation.
_PROCEDURAL_CALL = "Drupal\\Core\\Extension\\ProceduralCall"

#: P1's links families (`yaml_links`): `yaml_name` -> (link kind, node type).
_LINK_FAMILIES = {
    "links.menu": ("menu_link", "drupal_menu_link"),
    "links.task": ("local_task", "drupal_local_task"),
    "links.action": ("local_action", "drupal_local_action"),
    "links.contextual": ("contextual_link", "drupal_contextual_link"),
}

_PERMISSION_SPLIT = re.compile(r"[+,]")
_PSR4 = re.compile(r"^Drupal\\([A-Za-z0-9_]+)\\(.+)$")


@dataclass
class OverlayResult:
    status: str                                  # "fresh" | "stale" | "unavailable" | "invalid" | "error"
    reasons: list[str] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    edges: dict[str, int] = field(default_factory=lambda: {
        "confirmed": 0, "container_only": 0, "conflict": 0, "pair_taken": 0})
    runtime_absent: dict[str, int] = field(default_factory=dict)
    conflicts: list[dict] = field(default_factory=list)
    seen: dict[str, set[str]] = field(default_factory=lambda: {t: set() for t in RUNTIME_TYPES})
    #: The node ids `G` had before the facts were applied (after `undo`).
    static_ids: set[str] = field(default_factory=set)


# -- undo -----------------------------------------------------------------------


def _strip(data: dict) -> None:
    previous = data.pop(_PREV, None)
    previous = dict(previous) if isinstance(previous, dict) else {}
    written = data.pop(_SET, None)
    written = written if isinstance(written, dict) else {}
    for key in data.pop(_ATTRS, None) or ():
        if key in written and key in data and data[key] != written[key]:
            # A static producer wrote this key since (a re-extracted stub
            # merged over graph.json's copy in core's dedup): its value is
            # current, and the value the overlay once replaced is stale.
            previous.pop(key, None)
            continue
        data.pop(key, None)
    data.update(previous)


def _owned(data: dict) -> bool:
    """The item is still the overlay's own: its `source_file` is the one it was
    created with. A static producer that later emits the same edge pair takes
    it over through `build_from_json`'s `add_edge`, which writes the static
    `source_file` over the overlay's."""
    own = data.get(_OWN_FILE)
    if own is None:
        # Laid before `_overlay_file` existed: the overlay's while it carries `origin`.
        return data.get("origin") == ORIGIN
    return data.get("source_file") == own


def _hand_over(data: dict) -> None:
    """A static producer now owns this item: drop the overlay's markers and keep it."""
    for key in ("origin", _OVERLAY, _OWN_FILE):
        data.pop(key, None)


def undo(G: nx.Graph) -> None:
    """Remove everything an overlay left in `G` (spec S7.2): the edges and
    nodes it created, and on everything else the attributes named by
    `_overlay_attrs`, then that list itself.

    An item a static producer has since taken over is kept, without the
    overlay's markers (a `graphify update` merges the fresh static
    extraction into the graph.json the overlay was written into):

    - an edge is the overlay's while its `source_file` is still the one it
      was created with; a static re-emission of the pair overwrites it;
    - a node is the overlay's while its `source_file` is still its own and
      every edge on it is the overlay's. `source_file` alone cannot tell:
      core's dedup keeps one record per id by a rank that compares the
      basenames of the two `source_file`s, so the overlay's record often
      survives and the static one is dropped. The static edge that named
      the node (every static producer names a node through an edge) is
      what survives, so a node carrying one is handed over and re-homed to
      that edge's file.
    """
    multi = G.is_multigraph()
    edges = list(G.edges(keys=True, data=True)) if multi else [
        (u, v, None, d) for u, v, d in G.edges(data=True)]
    removed: set = set()
    static_files: dict[str, list[tuple[str, str]]] = {}
    for u, v, k, data in edges:
        if data.get("origin") == ORIGIN and _owned(data):
            removed.add((u, v, k))
            continue
        if data.get("origin") == ORIGIN:
            _hand_over(data)
        _strip(data)
        where = (str(data.get("source_file") or ""), str(data.get("source_location") or "L1"))
        static_files.setdefault(u, []).append(where)
        static_files.setdefault(v, []).append(where)
    G.remove_edges_from([(u, v, k) if multi else (u, v) for u, v, k in removed])

    drop = []
    for nid, data in G.nodes(data=True):
        _strip(data)
        if not data.get(_OVERLAY):
            continue
        if _owned(data) and nid not in static_files:
            drop.append(nid)
            continue
        if _owned(data):
            data["source_file"], data["source_location"] = min(static_files[nid])
        _hand_over(data)
    G.remove_nodes_from(drop)


# -- binding --------------------------------------------------------------------


def _short(name: str) -> str:
    return name.strip().lstrip("\\").rsplit("\\", 1)[-1]


class _Binder:
    """PHP nodes in `G` by (scan-root-relative source file, label). Built once per apply.

    `G`'s `source_file`s are relative to the scan `root` (or absolute); a
    fact's `file` is relative to the composer root (spec S5.1), which is
    `root` itself or an ancestor of it (a scan of `web/`)."""

    def __init__(self, G: nx.Graph, root: Path, composer_root: Path | None = None) -> None:
        self.G = G
        self.root = Path(root).absolute()
        self._resolved_root = self.root.resolve()
        self.composer_root = Path(composer_root).absolute() if composer_root is not None else self.root
        self._by_file: dict[str, dict[str, list[str]]] = {}
        for nid, data in G.nodes(data=True):
            source_file, label = data.get("source_file"), data.get("label")
            if not source_file or not isinstance(label, str):
                continue
            if str(data.get("type") or "").startswith("drupal_"):
                continue
            labels = self._by_file.setdefault(self._norm(str(source_file)), {})
            labels.setdefault(label, []).append(nid)

    def _norm(self, path: str) -> str:
        p = Path(path.replace("\\", "/"))
        if not p.is_absolute():
            return p.as_posix()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            pass
        try:
            return p.resolve().relative_to(self._resolved_root).as_posix()
        except (ValueError, OSError):
            return p.as_posix()

    def _one(self, labels: dict[str, list[str]], name: str) -> str | None:
        for label in (name, f".{name}()", f"{name}()"):
            ids = labels.get(label)
            if ids:
                return ids[0] if len(ids) == 1 else None
        return None

    def php_node(self, file: str | None, name: str) -> str | None:
        """The node whose `source_file` is `file` (relative to the composer
        root, or absolute) and whose label is `name`, `.name()` or `name()`;
        for `Class::method`, the method node under that class (joined to it
        by a `method` edge)."""
        if not file or not name:
            return None
        path = Path(str(file).replace("\\", "/"))
        if not path.is_absolute():
            path = self.composer_root / path
        labels = self._by_file.get(self._norm(os.path.normpath(path)))
        if not labels:
            return None
        if "::" not in name:
            return self._one(labels, _short(name))
        cls, method = name.split("::", 1)
        class_ids = labels.get(_short(cls)) or []
        if len(class_ids) != 1:
            return None
        class_id = class_ids[0]
        for mid in labels.get(f".{method}()", ()):
            if self.G.has_edge(class_id, mid) and self.G.edges[class_id, mid].get("relation") == "method":
                return mid
        return None

    def function_under(self, directory: str | Path | None, name: str) -> str | None:
        """The one function node labelled `name()` in any file under
        `directory` (relative to the composer root, or absolute): a hook
        implementation whose file the collector could not name (an include
        not loaded at collection time)."""
        if not directory or not name:
            return None
        path = Path(str(directory).replace("\\", "/"))
        if not path.is_absolute():
            path = self.composer_root / path
        prefix = self._norm(os.path.normpath(path)).rstrip("/") + "/"
        found = [nid for file, labels in self._by_file.items() if file.startswith(prefix)
                 for nid in labels.get(f"{name}()", ())]
        return found[0] if len(found) == 1 else None


# -- the pass -------------------------------------------------------------------


def _artifact_source_file(artifact: Path, root: Path, out_base: Path | None = None) -> str:
    """The `source_file` of what the overlay adds: the artifact relative to
    the scan root, `../`-relative when it lives outside it
    (`GRAPHIFY_DRUPAL_CONTAINER`).

    Core's incremental `extract` (`cli._stale_graph_sources`) prunes, after
    the build the overlay ran in, every relative `source_file` that lands
    inside the scan root under one of its anchors -- the root, and the
    `--out` directory (`out_base`) when that differs -- and names no file
    there. The bare name would be pruned; a `../` path leaving the root is
    not, unless `--out` sits inside the root and the same path read from
    there lands back in it: then, and only then, the absolute path."""
    artifact, root = Path(artifact).absolute(), Path(root).absolute()
    try:
        return artifact.relative_to(root).as_posix()
    except ValueError:
        pass
    rel = Path(os.path.relpath(artifact, root)).as_posix()
    if out_base is not None:
        out_base = Path(out_base).absolute()
        if out_base != root:
            landed = Path(os.path.normpath(out_base / rel))
            if landed == root or root in landed.parents:
                return artifact.as_posix()
    return rel




def _same_text(key: str, static: str, container: str) -> bool:
    """One value written two ways: a class with or without its leading
    backslash, and a route path with or without its leading slash (Symfony's
    `Route::setPath` prepends it, so a routing.yml `path: 'admin/x'` is the
    container's `/admin/x`)."""
    if key == "route_path":
        return "/" + static.strip().lstrip("/") == "/" + container.strip().lstrip("/")
    return static.lstrip("\\") == container.lstrip("\\")


class _Overlay:
    def __init__(self, G: nx.Graph, artifact: Artifact, root: Path, result: OverlayResult) -> None:
        from graphify.drupal.discovery import current_registry, current_run

        self.G = G
        self.data = artifact.data
        self.root = Path(root).absolute()
        # Artifact paths are relative to the composer root (spec S5.1), which a
        # scan of `web/` sits below.
        imap = install_map(self.root)
        self.composer_root = Path(imap.project_root) if imap is not None else self.root
        self.result = result
        self.binder = _Binder(G, self.root, self.composer_root)
        self.included = included_realms(self.root)
        self.registry = current_registry()
        run = current_run()
        self.artifact_file = _artifact_source_file(
            Path(artifact.path), self.root, run[1].parent if run is not None else None)

        self.services: dict[str, dict] = {}
        self.class_files: dict[str, str] = {}
        for s in self.data.get("services") or []:
            self.services[s["id"]] = s
            cls, file = s.get("class"), s.get("file")
            if cls and file:
                self.class_files.setdefault(cls.lstrip("\\"), file)
        self.extension_paths = {e["name"]: e.get("path") for e in self.data.get("extensions") or []}
        self._confirmed: set[tuple[str, str]] = set()
        self._conflicted: set[tuple[str, str, str]] = set()
        self._ordered: set[str] = set()
        #: `realm_of` per path: each pass asks once per fact and phase, and
        #: FormsRemote's artifact names ~4,000 files (26k calls, 1.3 s uncached).
        self._realms: dict[str, str | None] = {}

    # -- realms --

    def realm(self, path: Path | str) -> str | None:
        key = str(path)
        if key not in self._realms:
            self._realms[key] = realm_of(Path(path))
        return self._realms[key]

    def custom(self, realm: str | None) -> bool:
        return realm == "custom" or (realm is not None and realm in self.included)

    def _file_realm(self, file: str | None) -> str | None:
        return self.realm(self.composer_root / file) if file else None

    def extension_realm(self, name: str | None) -> str | None:
        if not name:
            return None
        if self.registry is not None:
            info = self.registry.extension_info.get(name)
            directory = info[1] if info else self.registry.extensions.get(name)
            if directory:
                return self.realm(directory)
        return self._file_realm(self.extension_paths.get(name))

    def service_realm(self, sid: str) -> str | None:
        s = self.services.get(sid)
        if s is None:
            return None
        return self._file_realm(s.get("file")) or self.extension_realm(s.get("provider"))

    def class_file(self, fqcn: str) -> str | None:
        fqcn = fqcn.lstrip("\\")
        if fqcn in self.class_files:
            return self.class_files[fqcn]
        m = _PSR4.match(fqcn)
        if not m:
            return None
        ext, rest = m.groups()
        directory = self.extension_paths.get(ext)
        if not directory and self.registry is not None:
            directory = self.registry.extensions.get(ext)
        if not directory:
            return None
        return f"{str(directory).rstrip('/')}/src/{rest.replace(chr(92), '/')}.php"

    # -- binding callables --

    def bind_callable(self, value: str | None) -> str | None:
        """`Class::method` -> the method node, else the class node; a service
        notation `service.id:method` through the service's class; a bare
        class -> its node."""
        if not value:
            return None
        value = value.strip().lstrip("\\")
        if "::" in value:
            cls, method = value.split("::", 1)
        elif value.count(":") == 1:
            sid, method = value.split(":", 1)
            cls = (self.services.get(sid) or {}).get("class") or ""
        else:
            cls, method = value, ""
        if not cls:
            return None
        file = self.class_file(cls)
        if method:
            found = self.binder.php_node(file, f"{_short(cls)}::{method}")
            if found is not None:
                return found
        return self.binder.php_node(file, cls)

    # -- mutation primitives --

    def attr(self, nid: str, data: dict, key: str, value: Any) -> None:
        """Add `key` when absent; a different existing value is a conflict."""
        if key not in data:
            data[key] = value
            touched = data.setdefault(_ATTRS, [])
            if key not in touched:
                touched.append(key)
            return
        existing = data[key]
        same = existing == value or (
            isinstance(existing, str) and isinstance(value, str)
            and _same_text(key, existing, value))
        if not same:
            self.result.conflicts.append({
                "relation": f"attribute:{key}", "source": nid,
                "static_target": existing, "container_target": value})

    def node_attr(self, nid: str, key: str, value: Any) -> None:
        if value is None or nid not in self.G:
            return
        self.attr(nid, self.G.nodes[nid], key, value)

    def ensure(self, nid: str, *, type: str, layer: str, label: str, realm: str | None,
               source_file: str | None = None, source_location: str | None = None,
               **extra: Any) -> str:
        """`nid`, created when absent: a boundary stub unless its realm is
        custom. `extra` attributes (None values left out) go only on a node
        this call creates."""
        if nid in self.G:
            return nid
        realm = realm or "unknown"
        source_file = source_file or self.artifact_file
        attrs: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
        attrs.update({
            "label": label, "file_type": "concept", "type": type, "layer": layer,
            "realm": realm, "source_file": source_file, "source_location": source_location or "L1",
            "_origin": "ast", "origin": ORIGIN, _OVERLAY: True, _OWN_FILE: source_file,
        })
        if not self.custom(realm):
            attrs["boundary"] = True
            attrs["external"] = True
        self.G.add_node(nid, **attrs)
        return nid

    def scan_path(self, file: str | None) -> str | None:
        """An artifact `file` (relative to the composer root) as `G`'s
        `source_file`s are written: relative to the scan root."""
        if not file:
            return None
        try:
            return (self.composer_root / file).relative_to(self.root).as_posix()
        except ValueError:
            return None

    def _static_targets(self, u: str, relation: str) -> list[str]:
        G = self.G
        found: list[str] = []
        if G.is_directed():
            pairs = ((v, d) for _u, v, d in G.out_edges(u, data=True))
        else:
            pairs = ((v, d) for _u, v, d in G.edges(u, data=True))
        for v, d in pairs:
            if d.get("relation") != relation or d.get("origin") == ORIGIN:
                continue
            if not G.is_directed() and d.get("_src", u) != u:
                continue
            found.append(d.get("_tgt", v) if not G.is_directed() else v)
        return found

    def edge(self, u: str, v: str, relation: str, **extra: Any) -> None:
        G, counts = self.G, self.result.edges
        if u == v:
            return
        if G.has_edge(u, v):
            data = G.edges[u, v]
            if data.get("relation") != relation:
                counts["pair_taken"] += 1
                return
            if data.get("origin") == ORIGIN:
                return
            self.attr(f"{u}->{v}", data, "confirmed_by", ORIGIN)
            for key, value in extra.items():
                if key not in _EDGE_RESERVED:
                    self.attr(f"{u}->{v}", data, key, value)
            if (u, v) not in self._confirmed:
                self._confirmed.add((u, v))
                counts["confirmed"] += 1
            return
        if relation in _SINGLE_TARGET:
            for static in self._static_targets(u, relation):
                if static != v and (relation, u, static) not in self._conflicted:
                    self._conflicted.add((relation, u, static))
                    counts["conflict"] += 1
                    self.result.conflicts.append({
                        "relation": relation, "source": u,
                        "static_target": static, "container_target": v})
        attrs = {k: val for k, val in extra.items() if k not in _EDGE_RESERVED}
        attrs.update({
            "relation": relation, "origin": ORIGIN, "_origin": "ast",
            "confidence": "EXTRACTED", "confidence_score": 1.0,
            "source_file": self.artifact_file, "source_location": "L1",
            # As build_from_json stores it: the edge's own direction, whatever the graph.
            "_src": u, "_tgt": v,
            _OWN_FILE: self.artifact_file,
        })
        # Keys a static producer never writes, stripped if one takes the edge over.
        attrs[_ATTRS] = ["confidence_score",
                         *(k for k in extra if k not in _EDGE_RESERVED and k != "target_name")]
        G.add_edge(u, v, **attrs)
        counts["container_only"] += 1

    # -- counting --

    def count(self, source: str, custom: bool) -> None:
        c = self.result.counts.setdefault(source, {"total": 0, "custom": 0, "applied": 0})
        c["total"] += 1
        if custom:
            c["custom"] += 1

    def applied(self, source: str) -> None:
        self.result.counts.setdefault(source, {"total": 0, "custom": 0, "applied": 0})["applied"] += 1

    # -- services --

    def service_target(self, sid: str) -> str:
        return self.ensure(service_id(sid), type="drupal_service", layer="di", label=sid,
                           realm=self.service_realm(sid))

    def services_pass(self, phase: str) -> None:
        for s in self.data.get("services") or []:
            sid = s["id"]
            nid = service_id(sid)
            cls = (s.get("class") or "").lstrip("\\") or None
            custom = self.custom(self.service_realm(sid))
            if phase == "custom":
                self.count("services", custom)
                self.result.seen["drupal_service"].add(nid)
            if not custom:
                if phase == "boundary" and nid in self.G:
                    self.node_attr(nid, "class_name", cls)
                    self.applied("services")
                continue
            if phase != "custom":
                continue
            self.ensure(nid, type="drupal_service", layer="di", label=sid, realm=self.service_realm(sid))
            self.applied("services")
            self.node_attr(nid, "class_name", cls)
            if cls:
                impl = self.binder.php_node(s.get("file"), cls)
                if impl is not None:
                    self.edge(nid, impl, "service_implemented_by")
            provider = s.get("provider")
            if provider:
                ext = self.ensure(extension_id(provider), type="drupal_extension", layer="extension",
                                  label=provider, realm=self.extension_realm(provider))
                self.edge(ext, nid, "declares_service")
            for arg in s.get("arguments") or []:
                if not isinstance(arg, str) or not arg:
                    continue
                if len(arg) > 2 and arg.startswith("%") and arg.endswith("%"):
                    name = arg[1:-1]
                    pid = self.ensure(parameter_id(name), type="drupal_parameter", layer="di",
                                      label=name, realm=None)
                    self.edge(nid, pid, "injects_parameter", target_name=name)
                else:
                    self.edge(nid, self.service_target(arg), "injects_service", target_name=arg)
            for tag in s.get("tags") or []:
                name = tag.get("name") if isinstance(tag, dict) else None
                if not name:
                    continue
                tid = self.ensure(tag_id(name), type="drupal_service_tag", layer="di",
                                  label=name, realm=None)
                attributes = tag.get("attributes") if isinstance(tag.get("attributes"), dict) else {}
                self.edge(nid, tid, "tagged_as", **attributes)
            decorates = s.get("decorates")
            if isinstance(decorates, str) and decorates:
                self.edge(nid, self.service_target(decorates), "decorates", target_name=decorates)

    def aliases_pass(self) -> None:
        by_target: dict[str, list[str]] = {}
        for alias, target in (self.data.get("aliases") or {}).items():
            by_target.setdefault(target, []).append(alias)
            self.result.seen["drupal_service"].add(service_id(alias))
            realm = self.service_realm(target)
            self.count("aliases", self.custom(realm))
            if service_id(target) in self.G:
                self.applied("aliases")
        for target, aliases in by_target.items():
            self.node_attr(service_id(target), "aliases", sorted(aliases))

    # -- routes --

    def route_realm(self, route: dict) -> str | None:
        realm = self.extension_realm(route.get("provider"))
        if realm is not None:
            return realm
        defaults = route.get("defaults") or {}
        for key in ("_controller", "_form"):
            value = defaults.get(key)
            if isinstance(value, str) and value:
                file = self.class_file(value.lstrip("\\").split("::", 1)[0])
                if file:
                    return self._file_realm(file)
        return None

    def routes_pass(self, phase: str) -> None:
        for r in self.data.get("routes") or []:
            name = r["name"]
            nid = route_id(name)
            custom = self.custom(self.route_realm(r))
            if phase == "custom":
                self.count("routes", custom)
                self.result.seen["drupal_route"].add(nid)
            if not custom:
                if phase == "boundary" and nid in self.G:
                    self.node_attr(nid, "route_path", r.get("path"))
                    self.applied("routes")
                continue
            if phase != "custom":
                continue
            self.ensure(nid, type="drupal_route", layer="routing", label=name, realm=self.route_realm(r))
            self.applied("routes")
            # `route_path`, as the routing producer names it: core reads a node's
            # `path` as a legacy alias of `source_file`.
            self.node_attr(nid, "route_path", r.get("path"))
            provider = r.get("provider")
            if provider:
                ext = self.ensure(extension_id(provider), type="drupal_extension", layer="extension",
                                  label=provider, realm=self.extension_realm(provider))
                self.edge(ext, nid, "declares_route")
            defaults = r.get("defaults") or {}
            requirements = r.get("requirements") or {}
            for key, relation, attr in (("_controller", "routes_to", "controller"),
                                        ("_form", "routes_to_form", "form")):
                value = defaults.get(key)
                if not isinstance(value, str) or not value:
                    continue
                target = self.bind_callable(value)
                if target is not None:
                    self.edge(nid, target, relation)
                else:
                    self.node_attr(nid, attr, value)
            permission = requirements.get("_permission")
            if isinstance(permission, str):
                for part in _PERMISSION_SPLIT.split(permission):
                    part = part.strip()
                    if part:
                        pid = self.ensure(permission_id(part), type="drupal_permission",
                                          layer="routing", label=part, realm=None)
                        self.edge(nid, pid, "requires_permission", target_name=part)
            access = requirements.get("_custom_access")
            if isinstance(access, str) and access:
                target = self.bind_callable(access)
                if target is not None:
                    self.edge(nid, target, "access_checked_by")
                else:
                    self.node_attr(nid, "custom_access", access)

    # -- extensions --

    def extensions_pass(self, phase: str) -> None:
        for e in self.data.get("extensions") or []:
            name = e["name"]
            nid = extension_id(name)
            realm = self._file_realm(e.get("path")) or self.extension_realm(name)
            custom = self.custom(realm)
            if phase == "custom":
                self.count("extensions", custom)
                if e.get("status") == 1:
                    self.result.seen["drupal_extension"].add(nid)
            if not custom and not (phase == "boundary" and nid in self.G):
                continue
            if custom and phase != "custom":
                continue
            if custom:
                self.ensure(nid, type="drupal_extension", layer="extension", label=name, realm=realm)
            self.applied("extensions")
            self.node_attr(nid, "weight", e.get("weight"))
            if e.get("status") is not None:
                self.node_attr(nid, "enabled", bool(e.get("status")))
            if not custom:
                continue
            for dep in e.get("dependencies") or []:
                if not isinstance(dep, str) or not dep:
                    continue
                target = self.ensure(extension_id(dep), type="drupal_extension", layer="extension",
                                     label=dep, realm=self.extension_realm(dep))
                self.edge(nid, target, "depends_on_module", target_name=dep)

    # -- hooks --

    def extension_dir(self, name: str) -> str | None:
        directory = self.extension_paths.get(name)
        if not directory and self.registry is not None:
            directory = self.registry.extensions.get(name)
        return str(directory) if directory else None

    def hook_target(self, name: str) -> str:
        decl = self.registry.hooks.get(name) if self.registry is not None else None
        realm = self.realm(decl.file) if decl is not None and decl.file else None
        return self.ensure(hook_id(name), type="drupal_hook", layer="hook", label=name,
                           realm=realm, hook_name=name)

    def impl_order(self, nid: str, order: int) -> None:
        """`runtime_order` on an impl node: its index in the hook's list, once
        per apply (a module with two listeners for one hook keeps the first).
        P2b's `order` (the `#[Hook(order: ...)]` argument text) is its own
        key and is left alone."""
        if nid in self._ordered or nid not in self.G:
            return
        self._ordered.add(nid)
        self.node_attr(nid, "runtime_order", order)

    @staticmethod
    def _listener(value: str) -> tuple[str, str, str]:
        """(function, class, method) of a hook listener identifier."""
        value = value.strip().lstrip("\\")
        if "::" not in value:
            return value, "", ""
        cls, method = value.split("::", 1)
        if cls == _PROCEDURAL_CALL:
            return method, "", ""
        return "", cls, method

    def bind_listener(self, module: str, file: str | None, function: str, cls: str,
                      method: str) -> str | None:
        if function:
            found = self.binder.php_node(file, function)
            return found if found is not None else self.binder.function_under(
                self.extension_dir(module), function)
        return self.binder.php_node(file or self.class_file(cls), f"{_short(cls)}::{method}")

    def hooks_pass(self, phase: str) -> None:
        for hook, entries in (self.data.get("hooks") or {}).items():
            if not isinstance(entries, list):
                continue
            for order, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                module, listener = entry.get("module"), entry.get("callable")
                if not module or not isinstance(listener, str) or not listener:
                    continue
                nid = hook_impl_id(module, hook)
                file = entry.get("file")
                realm = self.extension_realm(module) or self._file_realm(file)
                custom = self.custom(realm)
                if phase == "custom":
                    self.count("hooks", custom)
                    self.result.seen["drupal_hook_impl"].add(nid)
                if not custom:
                    if phase == "boundary" and nid in self.G:
                        self.impl_order(nid, order)
                        self.applied("hooks")
                    continue
                if phase != "custom":
                    continue
                function, cls, method = self._listener(listener)
                if not function and not (cls and method):
                    continue
                if function and "::" in listener:
                    file = None                # ProceduralCall.php is not where the function is
                target = self.bind_listener(module, file, function, cls, method)
                source_file, source_location = self.scan_path(file), None
                if target is not None:
                    bound = self.G.nodes[target]
                    source_file = source_file or bound.get("source_file")
                    source_location = bound.get("source_location")
                self.ensure(nid, type="drupal_hook_impl", layer="hook", label=f"{module}:{hook}",
                            realm=realm, source_file=source_file, source_location=source_location,
                            module=module,
                            hook_name=hook, function=function or None,
                            class_name=_short(cls) if cls else None, method=method or None)
                self.applied("hooks")
                self.impl_order(nid, order)
                decl = self.registry.hooks.get(hook) if self.registry is not None else None
                if decl is None or decl.provider != module:
                    # P2b's rule: an extension declaring the hook already has
                    # `declares_hook` to it, one relation per pair.
                    ext = self.ensure(extension_id(module), type="drupal_extension",
                                      layer="extension", label=module, realm=realm)
                    self.edge(ext, self.hook_target(hook), "implements_hook", target_name=hook)
                if target is not None:
                    self.edge(nid, target, "hook_implemented_by")

    # -- plugins --

    def plugin_type_target(self, plugin_type: str) -> str:
        t = self.registry.types.get(plugin_type) if self.registry is not None else None
        realm = self.realm(t.class_file) if t is not None and t.class_file else None
        return self.ensure(type_id(plugin_type), type="drupal_plugin_type", layer="plugin",
                           label=plugin_type, realm=realm, plugin_type=plugin_type)

    def plugin_node(self, plugin_type: str, name: str) -> tuple[str, str, str]:
        """`(id, type, layer)` of a plugin's node. A type read from a P1 links
        family (`menu.link` from `*.links.menu.yml`, ...) is P1's link node,
        so a static link is confirmed rather than doubled."""
        t = self.registry.types.get(plugin_type) if self.registry is not None else None
        link = _LINK_FAMILIES.get(t.yaml_name) if t is not None else None
        if link is not None:
            kind, node_type = link
            return link_id(kind, name), node_type, "routing"
        return plugin_id(plugin_type, name), "drupal_plugin", "plugin"

    def plugins_pass(self, phase: str) -> None:
        for plugin_type, entries in (self.data.get("plugins") or {}).items():
            if not isinstance(entries, list):
                continue
            for p in entries:
                if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"]:
                    continue
                name = p["id"]
                nid, node_type, layer = self.plugin_node(plugin_type, name)
                file, provider = p.get("file"), p.get("provider")
                file_realm, provider_realm = self._file_realm(file), self.extension_realm(provider)
                custom = self.custom(file_realm) or self.custom(provider_realm)
                if phase == "custom":
                    self.count("plugins", custom)
                    self.result.seen["drupal_plugin"].add(nid)
                cls = (p.get("class") or "").lstrip("\\") or None
                if not custom:
                    if phase == "boundary" and nid in self.G:
                        self.node_attr(nid, "class_name", cls)
                        self.applied("plugins")
                    continue
                if phase != "custom":
                    continue
                deriver = (p.get("deriver") or "").lstrip("\\") or None
                base = p.get("base_plugin_id") or None
                derivative = True if base and base != name else None
                realm = file_realm if self.custom(file_realm) else provider_realm
                self.ensure(nid, type=node_type, layer=layer, label=name, realm=realm,
                            plugin_id=name, plugin_type=plugin_type, provider=provider)
                self.applied("plugins")
                for key, value in (("class_name", cls), ("deriver", deriver),
                                   ("base_plugin_id", base), ("derivative", derivative)):
                    self.node_attr(nid, key, value)
                self.edge(nid, self.plugin_type_target(plugin_type), "plugin_of_type",
                          target_name=plugin_type)
                if provider and self.custom(provider_realm):
                    # A boundary stub is only ever a custom fact's target (S7.6),
                    # never the source of one.
                    ext = self.ensure(extension_id(provider), type="drupal_extension",
                                      layer="extension", label=provider, realm=provider_realm)
                    self.edge(ext, nid, "provides_plugin")
                if cls:
                    impl = self.binder.php_node(file or self.class_file(cls), cls)
                    if impl is not None:
                        self.edge(nid, impl, "plugin_implemented_by")
                if deriver:
                    found = self.binder.php_node(self.class_file(deriver), deriver)
                    if found is not None:
                        self.edge(nid, found, "derives_plugins")

    # -- subscribers --

    def subscribers_pass(self) -> None:
        """`subscribes_to_event` from each custom subscriber class bound in
        `G`. The event node is made by its first custom subscriber, in that
        subscriber's realm: an event is where custom code listens, never a
        boundary stub."""
        for event, entries in (self.data.get("subscribers") or {}).items():
            if not isinstance(entries, list):
                continue
            for s in entries:
                listener = s.get("callable") if isinstance(s, dict) else None
                if not isinstance(listener, str) or "::" not in listener:
                    continue
                cls = listener.lstrip("\\").split("::", 1)[0]
                # The edge's subject is the class, so its own file wins: an
                # inherited listener (`RouteSubscriberBase::onAlterRoutes`) is
                # declared in a core file, which would make every custom route
                # subscriber look like core.
                file = self.class_file(cls) or s.get("file")
                realm = self._file_realm(file)
                custom = self.custom(realm)
                self.count("subscribers", custom)
                if not custom:
                    continue
                source = self.binder.php_node(file, cls)
                if source is None:
                    continue
                eid = self.ensure(make_id("drupal", "event", event), type="drupal_event", layer="di",
                                  label=event, realm=realm)
                self.applied("subscribers")
                priority = s.get("priority")
                self.edge(source, eid, "subscribes_to_event",
                          **({"priority": priority} if priority is not None else {}))

    # -- runtime --

    def runtime(self) -> None:
        # The collector reads module hook lists (`hook_data`, `invokeAllWith`);
        # a theme's implementations are called by the theme registry and are
        # in neither, so the container cannot say whether one runs.
        themes = {e.get("name") for e in self.data.get("extensions") or []
                  if isinstance(e, dict) and e.get("type") == "theme"}
        for nid, data in self.G.nodes(data=True):
            node_type = data.get("type")
            if node_type not in RUNTIME_TYPES:
                continue
            if node_type == "drupal_hook_impl" and data.get("module") in themes:
                continue
            state = "present" if nid in self.result.seen[node_type] else "absent"
            self.attr(nid, data, "runtime", state)
            if state == "absent":
                self.result.runtime_absent[node_type] = self.result.runtime_absent.get(node_type, 0) + 1

    def run(self) -> None:
        # Custom facts first: they make the stubs later boundary facts enrich.
        for phase in ("custom", "boundary"):
            self.services_pass(phase)
            self.routes_pass(phase)
            self.extensions_pass(phase)
            self.hooks_pass(phase)
            self.plugins_pass(phase)
        self.subscribers_pass()
        self.aliases_pass()
        self.runtime()


def apply(G: nx.Graph, artifact: Artifact | None, root: Path, *, status: str = "fresh",
          reasons: list[str] | None = None, boundary_facts: bool = False) -> OverlayResult:
    """Lay `artifact` over `G` in place (spec S7). Undoes a previous overlay
    first; with no artifact, adds nothing (`unavailable`, or the `status`
    the caller already decided, such as `invalid`). Never raises: an internal
    error undoes what it did and becomes status `error`.

    With `boundary_facts`, the registry's facts (`apply_boundary_facts`) are
    re-applied right after the undo, before any container fact, with or
    without an artifact: a container fact about a boundary stub is then
    compared with what the registry says now, not with what it said when the
    stub was materialised (a registry-only change is no conflict)."""
    undo(G)
    try:
        if boundary_facts:
            apply_boundary_facts(G, Path(root))
        if artifact is None:
            return OverlayResult(status="unavailable" if status == "fresh" else status,
                                 reasons=list(reasons or []))
        result = OverlayResult(status=status, reasons=list(reasons or []),
                               static_ids=set(G.nodes))
        _Overlay(G, artifact, Path(root), result).run()
    except Exception as exc:  # noqa: BLE001 -- the overlay never breaks a build
        undo(G)
        return OverlayResult(status="error", reasons=[f"{type(exc).__name__}: {exc}"])
    return result


# -- the boundary facts (spec S7.8) ------------------------------------------------


def _replace(data: dict, key: str, value: Any) -> None:
    """Set `key` to `value`, recorded so `undo` gives the node back as it was:
    the key in `_overlay_attrs`, and a value it replaces in `_overlay_prev`
    (unless the overlay itself set that value this build)."""
    touched = data.setdefault(_ATTRS, [])
    if key not in touched:
        if key in data:
            data.setdefault(_PREV, {})[key] = data[key]
        touched.append(key)
    data[key] = value
    data.setdefault(_SET, {})[key] = value


def apply_boundary_facts(G: nx.Graph, root: Path) -> None:
    """Re-apply the current registry's facts to every boundary stub in `G`
    (P2b's `boundary: true`), whether or not an artifact is applied.

    The facts are the resolver's own (`resolvers._BoundaryIndex.facts`, the
    ones a stub gets when it is materialised). A stub carried over from
    graph.json keeps what the registry said when its referencing file was
    last extracted; a registry-only change (a core service's class) reaches
    it here, on the next build, without re-extracting anything. A key is
    written only where the registry's value differs from the node's or the
    node lacks it."""
    from graphify.drupal.discovery import current_registry
    from graphify.drupal.resolvers import _BoundaryIndex, scanning

    registry = current_registry()
    if registry is None:
        return
    index = _BoundaryIndex(registry)
    # Paths in the facts are relative to the scan root, as the resolver writes them.
    with scanning(Path(root)):
        for nid, data in G.nodes(data=True):
            if not data.get("boundary"):
                continue
            for key, value in index.facts({**data, "id": nid}).items():
                if value is None or (key in data and data[key] == value):
                    continue
                _replace(data, key, value)


# -- the build seam (spec S7.1) ------------------------------------------------------

_log = logging.getLogger(__name__)
_logged_errors: set[str] = set()

#: The vocabulary's S7 list: what a graph without the container cannot know.
NOT_STATIC = (
    ("plugin derivatives", "definitions are generated by code at runtime"),
    ("container changes from `*ServiceProvider` and compiler passes",
     "the container is assembled programmatically"),
    ("routes added by `RouteSubscriber`", "same"),
    ("actual hook execution order", "depends on weights and alters"),
    ("`$config[…]` overrides in `settings.php`", "PHP assignment, not declaration"),
)

_MAX_REPORTED_RECORDS = 10
_STAMP_KEYS = ("git_commit", "created_at", "runner", "drupal_version")


def _summary(result: OverlayResult, artifact: Artifact | None, records: list[dict]) -> dict:
    divergence: dict[str, int] = {}
    for record in records:
        divergence[record["kind"]] = divergence.get(record["kind"], 0) + 1
    stamp = artifact.stamp if artifact is not None else {}
    errors = artifact.data.get("errors") if artifact is not None else []
    return {
        "status": result.status,
        "reasons": list(result.reasons),
        "stamp": {k: stamp.get(k) for k in _STAMP_KEYS if stamp.get(k) is not None},
        "counts": result.counts,
        "edges": result.edges,
        "runtime_absent": result.runtime_absent,
        "divergence": divergence,
        "records": records[:_MAX_REPORTED_RECORDS],
        "errors": errors if isinstance(errors, list) else [],
    }


def _report(summary: dict, out: Path) -> None:
    """`summary` as the inventory's `container` block, in process and on disk."""
    from graphify.drupal.inventory import current_inventory, write_inventory

    inventory = current_inventory()
    if inventory is None:
        return
    inventory["container"] = summary
    try:
        write_inventory(inventory, out)
    except OSError:
        pass


def _log_once(message: str) -> None:
    if message not in _logged_errors:
        _logged_errors.add(message)
        _log.warning("graphify-drupal: the container overlay failed: %s", message)


def run_for_build(G: nx.Graph) -> OverlayResult | None:
    """The overlay for one build (spec S7.1): `None`, touching nothing, when no
    Drupal run is current (`query`, `path` and the other readers). Otherwise
    load the artifact, decide its staleness, `apply` it with the boundary
    facts (re-applied first, see `apply`), write (or remove) the divergence
    log and put the report block in the inventory. Never raises: a broken artifact is `invalid` (boundary
    facts still applied), any other failure undoes the overlay and is
    `error`."""
    from graphify.drupal import divergence
    from graphify.drupal.discovery import current_run

    run = current_run()
    if run is None:
        return None
    root, out = run
    artifact: Artifact | None = None
    records: list[dict] = []
    try:
        try:
            artifact = load_artifact(root)
        except ArtifactError as exc:
            result = apply(G, None, root, status="invalid", reasons=[str(exc)],
                           boundary_facts=True)
        else:
            if artifact is None:
                result = apply(G, None, root, boundary_facts=True)
            else:
                status, reasons = staleness(artifact, root)
                result = apply(G, artifact, root, status=status, reasons=reasons,
                               boundary_facts=True)
        if result.status == "error":
            _log_once("; ".join(result.reasons))
            artifact = None
        if artifact is not None:
            records = divergence.compute(G, result, artifact, root)
            divergence.write(records, out)
        else:
            divergence.remove(out)
    except Exception as exc:  # noqa: BLE001 -- the overlay never breaks a build
        message = f"{type(exc).__name__}: {exc}"
        _log_once(message)
        try:
            undo(G)
        except Exception:  # noqa: BLE001
            pass
        result, artifact, records = OverlayResult(status="error", reasons=[message]), None, []
        divergence.remove(out)
    try:
        _report(_summary(result, artifact, records), out)
    except Exception as exc:  # noqa: BLE001
        _log_once(f"{type(exc).__name__}: {exc}")
    return result
