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
`_overlay_attrs: [keys]` on every node and edge it touched.

This task maps `services`, `aliases`, `routes` and `extensions`; hooks,
plugins and subscribers come next.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

from graphify.drupal.boundary import included_realms, install_map, realm_of
from graphify.drupal.container import Artifact
from graphify.drupal.yaml_common import (
    parameter_id,
    permission_id,
    route_id,
    service_id,
    tag_id,
)
from graphify.drupal.yaml_extract import extension_id

ORIGIN = "container"

_OVERLAY = "_overlay"
_ATTRS = "_overlay_attrs"
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
    "source_file", "source_location", "_src", "_tgt", "confirmed_by", _ATTRS, _OWN_FILE,
})

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


# -- undo -----------------------------------------------------------------------


def _strip(data: dict) -> None:
    for key in data.pop(_ATTRS, None) or ():
        data.pop(key, None)


def _owned(data: dict) -> bool:
    """The item is still the overlay's own: its `source_file` is the one it was
    created with. A static producer that later emits the same edge pair takes
    it over through `build_from_json`'s `add_edge`, which writes the static
    `source_file` over the overlay's."""
    return data.get("source_file") == data.get(_OWN_FILE)


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


# -- the pass -------------------------------------------------------------------


class _Overlay:
    def __init__(self, G: nx.Graph, artifact: Artifact, root: Path, result: OverlayResult) -> None:
        from graphify.drupal.discovery import current_registry

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
        try:
            self.artifact_file = Path(artifact.path).absolute().relative_to(self.root).as_posix()
        except ValueError:
            self.artifact_file = Path(artifact.path).name

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

    # -- realms --

    def custom(self, realm: str | None) -> bool:
        return realm == "custom" or (realm is not None and realm in self.included)

    def _file_realm(self, file: str | None) -> str | None:
        return realm_of(self.composer_root / file) if file else None

    def extension_realm(self, name: str | None) -> str | None:
        if not name:
            return None
        if self.registry is not None:
            info = self.registry.extension_info.get(name)
            directory = info[1] if info else self.registry.extensions.get(name)
            if directory:
                return realm_of(Path(directory))
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
            and existing.lstrip("\\") == value.lstrip("\\"))
        if not same:
            self.result.conflicts.append({
                "relation": f"attribute:{key}", "source": nid,
                "static_target": existing, "container_target": value})

    def node_attr(self, nid: str, key: str, value: Any) -> None:
        if value is None or nid not in self.G:
            return
        self.attr(nid, self.G.nodes[nid], key, value)

    def ensure(self, nid: str, *, type: str, layer: str, label: str, realm: str | None) -> str:
        """`nid`, created when absent: a boundary stub unless its realm is custom."""
        if nid in self.G:
            return nid
        realm = realm or "unknown"
        attrs: dict[str, Any] = {
            "label": label, "file_type": "concept", "type": type, "layer": layer,
            "realm": realm, "source_file": self.artifact_file, "source_location": "L1",
            "_origin": "ast", "origin": ORIGIN, _OVERLAY: True, _OWN_FILE: self.artifact_file,
        }
        if not self.custom(realm):
            attrs["boundary"] = True
            attrs["external"] = True
        self.G.add_node(nid, **attrs)
        return nid

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

    # -- runtime --

    def runtime(self) -> None:
        for nid, data in self.G.nodes(data=True):
            node_type = data.get("type")
            if node_type not in RUNTIME_TYPES:
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
        self.aliases_pass()
        self.runtime()


def apply(G: nx.Graph, artifact: Artifact | None, root: Path, *, status: str = "fresh",
          reasons: list[str] | None = None) -> OverlayResult:
    """Lay `artifact` over `G` in place (spec S7). Undoes a previous overlay
    first; with no artifact, adds nothing (`unavailable`, or the `status`
    the caller already decided, such as `invalid`). Never raises: an internal
    error undoes what it did and becomes status `error`."""
    undo(G)
    if artifact is None:
        return OverlayResult(status="unavailable" if status == "fresh" else status,
                             reasons=list(reasons or []))
    result = OverlayResult(status=status, reasons=list(reasons or []))
    try:
        _Overlay(G, artifact, Path(root), result).run()
    except Exception as exc:  # noqa: BLE001 -- the overlay never breaks a build
        undo(G)
        return OverlayResult(status="error", reasons=[f"{type(exc).__name__}: {exc}"])
    return result
