"""Which unchanged files a registry change makes stale (P4 spec §9).

P4's per-file facts are read against cross-file registry state: a
`uses_service` target through the aliases, a `via: injected` edge through a
service's wiring, a `form_<x>_alter` bound to a boundary form, an event key
through `event_constants`, a class a plugin or an entity type names. Core's
AST cache is keyed by a file's own bytes, and on an incremental run core
keeps an unchanged file's edges from graph.json and hands the resolvers only
the fresh files -- so a file whose facts depend on a map that changed must be
forced to re-extract (`discovery.affected_files`), or it keeps what the old
registry made.

Each rule compares the two registries and names the files to force: from the
registries themselves where they know the dependents (a class's file, the
hook-dependent files), otherwise from the previous run's graph.json and
inventory (`PreviousRun`, P3's `_invokes_hook_files` pattern generalised):
the `source_file` of the edges or nodes that read the changed fact, and the
file of each candidate that could now bind. An unchanged registry reads
neither file and forces nothing.

Nothing here raises: an unreadable graph.json or inventory adds nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from graphify.drupal.hooks import hook_dependent_files

_INVENTORY_FILENAME = "drupal-inventory.json"

#: Relations whose target is a class another file names, bound in the
#: resolver by that class's file and short name (P4 spec §5, §6.1).
_CLASS_RELATIONS = frozenset({"derives_plugins", "entity_handler", "form_implemented_by",
                              "routes_to_form"})
#: Node attributes naming such a class (a route's `form`, a plugin's
#: `deriver`, an entity form's `class_name`); `handlers` holds what stayed unbound.
_CLASS_ATTRIBUTES = ("deriver", "class_name", "form")


class PreviousRun:
    """The previous run's graph.json and inventory, each read at most once
    and only when a rule asks. Paths come back absolute, resolved and
    existing; graph.json's are relative to `root`."""

    def __init__(self, graph_path: Path | None, root: Path | None) -> None:
        self._graph_path = Path(graph_path) if graph_path is not None else None
        self._root = Path(root) if root is not None else None
        self._graph: tuple[list, list] | None = None
        self._candidates: list[dict] | None = None

    def _read(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def _load(self) -> tuple[list, list]:
        if self._graph is None:
            data = self._read(self._graph_path) if self._graph_path and self._root else None
            nodes, links = [], []
            if isinstance(data, dict):
                nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
                links = data.get("links")
                if not isinstance(links, list):
                    links = data.get("edges") if isinstance(data.get("edges"), list) else []
            self._graph = (nodes, links)
        return self._graph

    def file(self, source_file: Any) -> str | None:
        """`source_file` as an absolute, resolved, existing path, else None."""
        if not source_file or not isinstance(source_file, str) or self._root is None:
            return None
        path = Path(source_file)
        if not path.is_absolute():
            path = self._root / path
        try:
            return path.resolve().as_posix() if path.is_file() else None
        except OSError:
            return None

    def _files(self, items: Iterable[Any], match: Callable[[dict], bool]) -> set[str]:
        out: set[str] = set()
        for item in items:
            try:
                if isinstance(item, dict) and match(item):
                    found = self.file(item.get("source_file"))
                    if found:
                        out.add(found)
            except Exception:
                continue
        return out

    def edge_files(self, match: Callable[[dict], bool]) -> set[str]:
        """The `source_file` of every graph.json edge `match` accepts."""
        return self._files(self._load()[1], match)

    def node_files(self, match: Callable[[dict], bool]) -> set[str]:
        """The `source_file` of every graph.json node `match` accepts."""
        return self._files(self._load()[0], match)

    def candidate_files(self, kinds: Iterable[str]) -> set[str]:
        """The file of every previous inventory candidate (hook or PHP) of `kinds`."""
        if self._candidates is None:
            data = self._read(self._graph_path.parent / _INVENTORY_FILENAME) \
                if self._graph_path else None
            found: list[dict] = []
            if isinstance(data, dict):
                for key in ("hook_candidates", "php_candidates"):
                    items = data.get(key)
                    if isinstance(items, list):
                        found.extend(c for c in items if isinstance(c, dict))
            self._candidates = found
        wanted = set(kinds)
        out: set[str] = set()
        for c in self._candidates:
            if c.get("kind") in wanted:
                found_file = self.file(c.get("file"))
                if found_file:
                    out.add(found_file)
        return out


def _changed(old: dict, new: dict, view: Callable[[Any], Any] = lambda v: v) -> set[str]:
    """Keys added, removed, or whose `view` of the value differs."""
    return {k for k in old.keys() | new.keys()
            if k not in old or k not in new or view(old[k]) != view(new[k])}


def _facts(registry: Any) -> dict[str, dict]:
    return {k: v for k, v in (getattr(registry, "class_facts", None) or {}).items()
            if isinstance(v, dict)}


def _class_files(registries: tuple[Any, Any], fqcns: Iterable[str],
                 subclasses: bool = False) -> set[str]:
    """The files of `fqcns` in either registry's class facts, and with
    `subclasses` those of every class extending one of them."""
    wanted = {f.lstrip("\\") for f in fqcns if f}
    out: set[str] = set()
    for registry in registries:
        facts = _facts(registry)
        found = set(wanted)
        if subclasses:
            children: dict[str, list[str]] = {}
            for fqcn, data in facts.items():
                if data.get("extends"):
                    children.setdefault(str(data["extends"]), []).append(fqcn)
            stack = list(wanted)
            while stack:
                for child in children.get(stack.pop(), ()):
                    if child not in found:
                        found.add(child)
                        stack.append(child)
        for fqcn in found:
            file = (facts.get(fqcn) or {}).get("file")
            if file:
                out.add(str(file))
    return out


def _service_class(registry: Any, sid: str) -> str:
    from graphify.drupal.php_services import service_class

    try:
        return service_class(registry, sid)
    except Exception:
        return ""


def _wiring(registry: Any) -> dict[str, dict]:
    return {k: v for k, v in (getattr(registry, "service_wiring", None) or {}).items()
            if isinstance(v, dict)}


def _service_files(previous: Any, current: Any, prior: PreviousRun) -> set[str]:
    """P4 spec §7.1/§7.3 against a changed service map: see `stale_files`."""
    both = (previous, current)
    services = _changed(previous.services, current.services, lambda v: list(v)[:1])
    aliases = _changed(previous.service_aliases or {}, current.service_aliases or {})
    wiring = _changed(_wiring(previous), _wiring(current))
    hooked = set(previous.hook_services or ()) ^ set(current.hook_services or ())
    out: set[str] = set()
    names = services | aliases
    if names:
        # Every use of the service or alias: a `uses_service` names it as its
        # target or its `alias`, a bound `calls` as its `service`.
        out |= prior.edge_files(lambda e: e.get("relation") in ("uses_service", "calls") and (
            e.get("target_name") in names or e.get("alias") in names or e.get("service") in names))
    # Rule 2: the class a changed service (or its wiring) is, before and after.
    wired = {_service_class(r, sid) for r in both for sid in services | wiring}
    # Rule 1/1b and 2 through a changed alias: the classes whose `create()`
    # or `arguments:` name it, and their subclasses (rule 1 via `new static`).
    if aliases:
        for registry in both:
            for fqcn, data in _facts(registry).items():
                named = {*(data.get("create_args") or ()), *(data.get("create_props") or {}).values(),
                         *(p.get("type") for p in data.get("params") or () if isinstance(p, dict))}
                if named & aliases:
                    wired.add(fqcn)
            for sid, entry in _wiring(registry).items():
                if set(entry.get("arguments") or ()) & aliases:
                    wired.add(_service_class(registry, sid))
    out |= _class_files(both, wired, subclasses=bool(aliases))
    # Rule 3/3b: a type names a service by its id or an alias, so a changed
    # id or alias set moves what any autowired class resolves.
    if set(previous.services) != set(current.services) or aliases:
        autowired = {_service_class(r, sid) for r in both
                     for sid, entry in _wiring(r).items() if entry.get("autowire")}
        autowired |= set(previous.hook_services or ()) | set(current.hook_services or ())
        out |= _class_files(both, autowired)
    # Rule 3b itself: a hook class that became (or stopped being) autowired.
    out |= _class_files(both, hooked)
    return out


def _form_facts(registry: Any) -> dict[str, tuple[str, str]]:
    """`{fqcn: (form id, base form id)}` of the classes that have either: a
    class added or removed without a form changes nothing here."""
    out: dict[str, tuple[str, str]] = {}
    for fqcn, data in _facts(registry).items():
        ids = (str(data.get("form_id") or ""), str(data.get("base_form_id") or ""))
        if any(ids):
            out[fqcn] = ids
    return out


def _binding_files(previous: Any, current: Any) -> set[str]:
    """P4 spec §6.2-§6.3: a variable hook is bound per file from the forms,
    base forms, entity types and their forms and bundles, and the module
    list; any change there forces every hook-dependent file."""
    changed = (
        _changed(previous.forms or {}, current.forms or {})
        or _changed(previous.base_forms or {}, current.base_forms or {})
        or _changed(previous.entity_types or {}, current.entity_types or {})
        or _changed(previous.entity_forms or {}, current.entity_forms or {})
        or _changed(previous.entity_bundles or {}, current.entity_bundles or {})
        or set(previous.extension_info or {}) != set(current.extension_info or {})
        or _form_facts(previous) != _form_facts(current)
    )
    return hook_dependent_files(previous) | hook_dependent_files(current) if changed else set()


def _event_files(previous: Any, current: Any, prior: PreviousRun) -> set[str]:
    """P4 spec §8: the subscribers, bound or not, when the constants move."""
    if not _changed(previous.event_constants or {}, current.event_constants or {}):
        return set()
    return prior.edge_files(lambda e: e.get("relation") == "subscribes_to_event") \
        | prior.candidate_files(("unresolved_event",))


def _type_view(t: Any) -> tuple[str, str, str, str]:
    return (t.subdir, t.attribute_class, t.annotation_class, t.yaml_name)


def _plugin_type_files(previous: Any, current: Any) -> set[str]:
    """P4 spec §5.2: a class is a plugin of the learned type whose `subdir`
    it sits in; a changed type forces the custom classes of both subdirs."""
    changed = _changed(previous.types, current.types, _type_view)
    subdirs = {t.subdir.strip("/") for r in (previous, current) for key, t in r.types.items()
               if key in changed and t.subdir}
    out: set[str] = set()
    if not subdirs:
        return out
    marks = tuple(f"/src/{s}/" for s in subdirs)
    for registry in (previous, current):
        for data in _facts(registry).values():
            file = str(data.get("file") or "")
            if any(m in file for m in marks):
                out.add(file)
    return out


def _class_view(data: dict) -> tuple[str, str]:
    return str(data.get("file") or ""), str(data.get("form_id") or "")


def _named_class_files(previous: Any, current: Any, prior: PreviousRun) -> set[str]:
    """P4 spec §5.4/§6.1: a class that appeared, went, moved or changed its
    form id -- the files naming it (a plugin's deriver, an entity type's
    handlers, an entity form, a route's `_form`) bind it again."""
    changed = _changed(_facts(previous), _facts(current), _class_view)
    if not changed:
        return set()

    def names(node: dict) -> bool:
        if any(isinstance(node.get(a), str) and node[a].strip().lstrip("\\") in changed
               for a in _CLASS_ATTRIBUTES):
            return True
        handlers = node.get("handlers")
        return isinstance(handlers, dict) and any(
            isinstance(v, str) and v.lstrip("\\") in changed for v in handlers.values())

    return prior.edge_files(lambda e: e.get("relation") in _CLASS_RELATIONS
                            and str(e.get("target_name") or "").lstrip("\\") in changed) \
        | prior.node_files(names)


def _shortcut_files(previous: Any, current: Any) -> set[str]:
    """P4 spec §7.1: `\\Drupal::<method>()` names a service through the
    shortcut map; an unknown shortcut leaves no trace, so a changed map
    forces every custom class file and every hook-dependent file."""
    if (previous.shortcuts or {}) == (current.shortcuts or {}):
        return set()
    return {str(d["file"]) for r in (previous, current) for d in _facts(r).values()
            if d.get("file")} | hook_dependent_files(previous) | hook_dependent_files(current)


def stale_files(previous: Any, current: Any, prior: PreviousRun) -> set[str]:
    """Files whose P4 facts the change from `previous` to `current` makes stale:

    - a service whose class changed, or an alias added, removed or
      retargeted: every file with a `uses_service` or bound `calls` naming it
      in the previous graph; the class of a changed service or service
      wiring (rule 2), the classes whose `create()`, `arguments:` or
      parameter types name a changed alias and their subclasses, and, when
      the id or alias set changed, every autowired class (rules 3, 3b); a
      hook class entering or leaving `hook_services`;
    - forms, base forms, entity types, entity forms, bundles, the module
      list or a class's `getFormId()`/`getBaseFormId()`: the hook-dependent files;
    - event constants: the previous `subscribes_to_event` sources and
      `unresolved_event` candidates;
    - a plugin type's subdir, attribute, annotation or YAML name: the custom
      classes in its subdir, before and after;
    - a class added, removed, moved or with another form id: the files whose
      previous edges or nodes name it;
    - the `\\Drupal::` shortcut map: every custom class and hook-dependent file.

    A constructor or `create()` change in a class is `_class_facts_files`' in
    `discovery.affected_files`."""
    out: set[str] = set()
    for rule in (lambda: _service_files(previous, current, prior),
                 lambda: _binding_files(previous, current),
                 lambda: _event_files(previous, current, prior),
                 lambda: _plugin_type_files(previous, current),
                 lambda: _named_class_files(previous, current, prior),
                 lambda: _shortcut_files(previous, current)):
        try:
            out |= rule()
        except Exception:
            continue
    return out
