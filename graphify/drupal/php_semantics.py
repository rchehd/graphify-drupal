"""What one custom PHP file says to Drupal (P4 spec §4, §5).

`extract_php_semantics(path, core_result)` is composed onto core's PHP
handler (`register.py`'s dispatch, and `hooks.extract_php_with_hooks` for
`src/Hook/**`) for every in-graph `.php` under an extension's `src/`
(`is_semantics_file`). It reads the file once (`php_classes.read_class_semantics`)
and runs each producer in `_PRODUCERS` over that one reading. P4 Task 2's
producer emits plugin and entity type nodes, Task 3's the forms (a literal
`getFormId()`, and an entity type's `form.<op>` handlers); later tasks add
services and events as further producers.

What a single file cannot settle is emitted as a *pending* edge: its
`pending` attribute names the kind of fact, its `target_name` the thing to
bind (a class FQCN for `derives_plugins` and `entity_handler`), and its
`target` a placeholder id no node carries. The `drupal` resolver
(`resolvers.bind_pending`) binds each one to a node or leaves it pending,
and the seam's `extract` wrapper drops whatever is still pending
(`resolvers.drop_pending`): no `pending` edge ever reaches graph.json.

What looks like Drupal but cannot be read as such is a `php_candidates`
entry (spec §10). The inventory reads them through `find_php_candidates`
from every detected file, as it reads `hook_candidates`, so an incremental
run keeps the candidates of unchanged files.

Readings of spec §5, settled for Task 2:
- a class matches every learned type whose `subdir`, attribute or annotation
  it satisfies: Drupal's `action` and `eca.action` managers both discover a
  `#[Action]` in `src/Plugin/Action`, and the container lists it under both;
- a plugin-looking attribute or annotation is one with a literal `id`; one in
  `src/Plugin/**` naming no learned type's attribute or annotation class is an
  `unknown_plugin_type` candidate (a companion attribute such as `#[EcaAction]`
  has no id and is neither);
- an entity type's handlers are flattened to `name` / `name.sub`
  (`form.default`, `route_provider.html`); each handler class becomes one
  pending `entity_handler` edge whose `handler` lists the names it serves,
  comma-separated, and the node's `handlers` keeps those the resolver could
  not bind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from graphify.ids import make_id

#: The attribute that marks an edge as a raw fact the resolver must bind.
PENDING = "pending"
#: `pending` value: the target is the class node named by `target_name` (a FQCN).
PENDING_CLASS = "class"
#: `pending` values of a `calls` edge (spec §7.4): the method `method` of the
#: class of service `service`, or of the service property `property` of
#: class `class` holds (spec §7.3).
PENDING_SERVICE_CALL = "service_call"
PENDING_PROPERTY_CALL = "property_call"

_UNKNOWN_PLUGIN_TYPE = "unknown_plugin_type"
_NON_LITERAL_SERVICE = "non_literal_service"
_UNRESOLVED_RECEIVER = "unresolved_receiver"
_UNRESOLVED_EVENT = "unresolved_event"
_PLUGIN_DIR = "Plugin/"
_ENTITY_KINDS = {"ContentEntityType": "content", "ConfigEntityType": "config"}
#: Literal entity type attributes copied onto the node (spec §5.3).
_ENTITY_LITERALS = ("bundle_entity_type", "base_table", "admin_permission")
#: An entity type's handler names that are entity forms: `form.<op>`.
_FORM_HANDLER = "form."


def _empty() -> dict[str, Any]:
    return {"nodes": [], "edges": [], "php_candidates": []}


def entity_type_id(entity_type: str) -> str:
    return make_id("drupal", "entity_type", entity_type)


def pending_class_id(fqcn: str) -> str:
    """The placeholder target of a pending class edge: no node carries it."""
    return make_id("drupal", "pending", "class", fqcn)


def _extension_dir(path: Path, registry: Any) -> tuple[str, str]:
    """`(owner, extension directory)` of `path`, or `("", "")`."""
    from graphify.drupal.discovery import registry_owner_of

    owner = registry_owner_of(registry, path)
    directory = registry.extensions.get(owner) if owner else None
    return (owner, directory.rstrip("/")) if directory else ("", "")


def _src_relative(path: Path, registry: Any) -> tuple[str, str]:
    """`(owner, path relative to <extension>/src/)`, or `("", "")`."""
    owner, directory = _extension_dir(path, registry)
    if not owner:
        return "", ""
    prefix = directory + "/src/"
    posix = path.as_posix()
    return (owner, posix[len(prefix):]) if posix.startswith(prefix) else ("", "")


def _where(path: Path, registry: Any) -> tuple[str, str]:
    """`(owner, path relative to <extension>/src/)` of a semantics file: a
    `.php` under an extension's `src/`, or an extension's procedural file
    (`<ext>.module`, `.install`, ..., whose `src` part is ""); `("", "")`
    for any other file."""
    from graphify.drupal.hooks import PROCEDURAL_SUFFIXES, procedural_extension

    if path.suffix == ".php":
        return _src_relative(path.absolute(), registry) if "/src/" in path.as_posix() else ("", "")
    if path.suffix in PROCEDURAL_SUFFIXES:
        return procedural_extension(path), ""
    return "", ""


def is_semantics_file(path: Path) -> bool:
    """A `.php` file under `<extension>/src/`, or an extension's procedural
    file (services are used there too, spec §7.1), of an extension the
    current registry knows. False without a registry, so a non-Drupal run
    pays one substring test per PHP file."""
    from graphify.drupal.hooks import PROCEDURAL_SUFFIXES

    path = Path(path)
    if not ((path.suffix == ".php" and "/src/" in path.as_posix())
            or path.suffix in PROCEDURAL_SUFFIXES):
        return False
    from graphify.drupal.discovery import current_registry

    registry = current_registry()
    return registry is not None and bool(_where(path, registry)[0])


@dataclass
class _File:
    """One file's reading, shared by every producer."""
    path: Path
    registry: Any
    owner: str
    src_rel: str
    core_ids: set[str]
    stem: str
    #: node id -> `source_location` as core emitted it.
    core_lines: dict[str, str] = field(default_factory=dict)
    facts: list = field(default_factory=list)
    annotations: list = field(default_factory=list)
    attributed: list = field(default_factory=list)
    service_uses: list = field(default_factory=list)
    service_calls: list = field(default_factory=list)
    property_uses: list = field(default_factory=list)
    events: list = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    _node_ids: set[str] = field(default_factory=set)
    _pairs: set[tuple[str, str]] = field(default_factory=set)

    def add_node(self, nid: str, label: str, *, type: str, layer: str, line: int,
                 **extra: Any) -> bool:
        from graphify.drupal.yaml_common import node

        if nid in self._node_ids:
            return False
        self._node_ids.add(nid)
        self.nodes.append(node(nid, label, type=type, layer=layer, path=self.path, line=line,
                               **extra))
        return True

    def add_edge(self, source: str, target: str, relation: str, line: int, **extra: Any) -> None:
        """One relation per ordered node pair (a pending edge's pair is its
        placeholder, one per class)."""
        from graphify.drupal.yaml_common import edge

        if (source, target) in self._pairs:
            return
        self._pairs.add((source, target))
        self.edges.append(edge(source, target, relation, path=self.path, line=line, **extra))

    def add_pending_class(self, source: str, relation: str, fqcn: str, line: int,
                          **extra: Any) -> None:
        self.add_edge(source, pending_class_id(fqcn), relation, line,
                      target_name=fqcn, **{PENDING: PENDING_CLASS}, **extra)

    def class_node(self, short_name: str) -> str | None:
        """The class node core emitted for `short_name` in this file (spec §5.2)."""
        from graphify.extractors.base import _make_id

        nid = _make_id(self.stem, short_name)
        return nid if nid in self.core_ids else None

    def candidate(self, kind: str, line: int, **extra: Any) -> None:
        self.candidates.append({"kind": kind, "module": self.owner, **extra,
                                "file": str(self.path), "line": line})


def _read(path: Path, core_result: dict | None, registry: Any = None) -> _File | None:
    from graphify.drupal.discovery import current_registry
    from graphify.drupal.php_classes import read_file_semantics
    from graphify.extractors.base import _file_stem

    path = Path(path)
    if registry is None:
        registry = current_registry()
    if registry is None:
        return None
    owner, src_rel = _where(path, registry)
    if not owner:
        return None
    read = read_file_semantics(path)
    return _File(
        path=path, registry=registry, owner=owner, src_rel=src_rel,
        core_ids={n.get("id") for n in (core_result or {}).get("nodes") or ()},
        core_lines={n.get("id"): str(n.get("source_location") or "")
                    for n in (core_result or {}).get("nodes") or ()},
        stem=_file_stem(path), facts=read.facts, annotations=read.annotations,
        attributed=read.attributed, service_uses=read.service_uses,
        service_calls=read.service_calls, property_uses=read.property_uses,
        events=read.events,
    )


# -- plugins and entity types (spec §5) ----------------------------------------------


def _short(name: str) -> str:
    return name.rsplit("\\", 1)[-1]


def _class_ref(value: Any) -> str:
    """A FQCN from an attribute's `X::class` / string or an annotation's string."""
    if isinstance(value, dict) and isinstance(value.get("class"), str):
        value = value["class"]
    if isinstance(value, str):
        value = value.strip().lstrip("\\")
        if value and all(part.isidentifier() for part in value.split("\\")):
            return value
    return ""


def _attribute_values(attribute: Any) -> dict[str, Any]:
    """An attribute's named literal arguments, the first positional string as `id`."""
    values: dict[str, Any] = {}
    positional = next((a for a in attribute.args if not a.name), None)
    if positional is not None and positional.string is not None:
        values["id"] = positional.string
    for arg in attribute.args:
        if arg.name and arg.value is not None:
            values[arg.name] = arg.value
    return values


@dataclass(frozen=True)
class _Declared:
    """One attribute or annotation on a class, normalised."""
    class_fqcn: str
    class_name: str
    line: int
    values: dict
    attribute: str = ""     # resolved attribute FQCN, "" for an annotation
    short: str = ""         # annotation: the name as written, last segment
    name: str = ""          # annotation: the resolved name
    imported: bool = False  # annotation: fixed by a `use` or a leading `\`


def _declarations(f: _File) -> list[_Declared]:
    return _declarations_of(f.attributed, f.annotations)


def _declarations_of(attributed: list, annotations: list) -> list[_Declared]:
    found: list[_Declared] = []
    names = {cls.fqcn: cls.name for cls in attributed}
    for cls in attributed:
        for attribute in cls.attributes:
            found.append(_Declared(cls.fqcn, cls.name, attribute.line,
                                   _attribute_values(attribute), attribute=attribute.name))
    for fqcn, annotation in annotations:
        found.append(_Declared(fqcn, names.get(fqcn, _short(fqcn)), annotation.line,
                               dict(annotation.values), short=annotation.short,
                               name=annotation.name, imported=annotation.imported))
    return found


def _annotation_matches(d: _Declared, annotation_class: str) -> bool:
    """The Task 1 contract: by short name, unless a `use` fixes another class."""
    return bool(annotation_class) and d.short == _short(annotation_class) \
        and (not d.imported or d.name == annotation_class)


def _entity_kind(d: _Declared) -> str:
    from graphify.drupal.discovery import (
        _ENTITY_TYPE_ANNOTATION_CLASSES,
        _ENTITY_TYPE_ATTRIBUTES,
    )

    if d.attribute:
        return _ENTITY_KINDS.get(_short(d.attribute), "") \
            if d.attribute in _ENTITY_TYPE_ATTRIBUTES else ""
    kind = _ENTITY_KINDS.get(d.short, "")
    if kind and d.imported and d.name not in _ENTITY_TYPE_ANNOTATION_CLASSES:
        return ""
    return kind


def _types(registry: Any) -> list[Any]:
    """Learned types a class can be a plugin of: a `subdir` and an attribute
    or annotation class, and not read from a P1 links family (whose plugins
    stay P1's link nodes)."""
    from graphify.drupal.resolvers import link_family_names

    links = link_family_names()
    return [t for t in registry.types.values()
            if t.subdir and (t.attribute_class or t.annotation_class)
            and t.yaml_name not in links]


def _in_subdir(src_rel: str, subdir: str) -> bool:
    return src_rel.startswith(subdir.strip("/") + "/")


def _plugin(f: _File, d: _Declared, plugin_type: Any, plugin_name: str) -> None:
    from graphify.drupal.discovery import type_id
    from graphify.drupal.yaml_common import plugin_id
    from graphify.drupal.yaml_extract import extension_id

    pid = plugin_id(plugin_type.plugin_type, plugin_name)
    deriver = _class_ref(d.values.get("deriver"))
    extra: dict[str, Any] = {"deriver": deriver} if deriver else {}
    f.add_node(pid, plugin_name, type="drupal_plugin", layer="plugin", line=d.line,
               plugin_id=plugin_name, plugin_type=plugin_type.plugin_type,
               class_name=d.class_fqcn, provider=f.owner, **extra)
    f.add_edge(extension_id(f.owner), pid, "provides_plugin", d.line, owner=f.owner)
    f.add_edge(pid, type_id(plugin_type.plugin_type), "plugin_of_type", d.line,
               target_name=plugin_type.plugin_type)
    implementation = f.class_node(d.class_name)
    if implementation is not None:
        f.add_edge(pid, implementation, "plugin_implemented_by", d.line)
    if deriver:
        f.add_pending_class(pid, "derives_plugins", deriver, d.line)


def _handlers(value: Any) -> dict[str, str]:
    """`{name: fqcn}` from an entity type's `handlers`, one level of nesting."""
    out: dict[str, str] = {}
    if not isinstance(value, dict):
        return out
    for key, item in value.items():
        fqcn = _class_ref(item)
        if fqcn:
            out[key] = fqcn
        elif isinstance(item, dict) and "class" not in item:
            for sub, nested in item.items():
                fqcn = _class_ref(nested)
                if fqcn:
                    out[f"{key}.{sub}"] = fqcn
    return out


def _entity_type(f: _File, d: _Declared, kind: str, entity_type: str) -> None:
    from graphify.drupal.yaml_common import permission_id
    from graphify.drupal.yaml_extract import extension_id

    eid = entity_type_id(entity_type)
    extra: dict[str, Any] = {}
    for key in _ENTITY_LITERALS:
        value = d.values.get(key)
        if isinstance(value, str) and value:
            extra[key] = value
    handlers = _handlers(d.values.get("handlers"))
    if handlers:
        extra["handlers"] = dict(handlers)
    if not f.add_node(eid, entity_type, type="drupal_entity_type", layer="model", line=d.line,
                      entity_kind=kind, class_name=d.class_fqcn, provider=f.owner, **extra):
        return
    f.add_edge(extension_id(f.owner), eid, "defines_entity_type", d.line, owner=f.owner)
    # One edge per class (one relation per node pair): `form.add` and
    # `form.edit` served by one form class are one edge, `handler: "form.add,form.edit"`.
    names: dict[str, list[str]] = {}
    for name, fqcn in handlers.items():
        names.setdefault(fqcn, []).append(name)
    for fqcn, served in names.items():
        f.add_pending_class(eid, "entity_handler", fqcn, d.line, handler=",".join(served))
    permission = extra.get("admin_permission")
    if permission:
        f.add_edge(eid, permission_id(permission), "requires_permission", d.line,
                   target_name=permission)
    for name, fqcn in handlers.items():
        if name.startswith(_FORM_HANDLER):
            _entity_form(f, entity_type, name[len(_FORM_HANDLER):], fqcn, d.line)


# -- forms (spec §6.1) -----------------------------------------------------------------


def form_id(form: str) -> str:
    return make_id("drupal", "form", form)


def entity_form_id(entity_type: str, operation: str) -> str:
    return make_id("drupal", "form", "entity", entity_type, operation)


def entity_form_pattern(entity_type: str, operation: str) -> str:
    """The form ids an entity form answers to: `EntityForm::getFormId()` is
    `<entity>[_<bundle>][_<op>]_form`, the `default` operation left out."""
    if operation == "default":
        return f"{entity_type}_*_form"
    return f"{entity_type}_*_{operation}_form"


_ENTITY_FORM_OPS_ATTR = "_drupal_entity_form_ops"


def entity_form_operations(registry: Any, entity_type: str) -> tuple[str, ...]:
    """The `form.<op>` operations entity type `entity_type` declares, custom
    or boundary: its class (the registry's `entity_types`) re-read on first
    use and cached on the registry object. The same reading as the per-file
    extractor's entity forms (`_entity_form`). Empty when the class file is
    unknown or unreadable; never raises."""
    cache = getattr(registry, _ENTITY_FORM_OPS_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(registry, _ENTITY_FORM_OPS_ATTR, cache)
        except Exception:
            pass
    found = cache.get(entity_type)
    if found is None:
        try:
            found = cache[entity_type] = _read_entity_form_operations(registry, entity_type)
        except Exception:
            found = cache[entity_type] = ()
    return found


def _read_entity_form_operations(registry: Any, entity_type: str) -> tuple[str, ...]:
    from graphify.drupal.php_classes import read_class_semantics
    from graphify.drupal.resolvers import _class_file

    entry = (registry.entity_types or {}).get(entity_type)
    if not entry or len(entry) < 2:
        return ()
    fqcn = str(entry[1])
    file = _class_file(registry, fqcn)
    if not file:
        return ()
    _facts, annotations, attributed = read_class_semantics(Path(file))
    ops: list[str] = []
    for d in _declarations_of(attributed, annotations):
        if d.class_fqcn != fqcn or d.values.get("id") != entity_type or not _entity_kind(d):
            continue
        for name in _handlers(d.values.get("handlers")):
            op = name[len(_FORM_HANDLER):] if name.startswith(_FORM_HANDLER) else ""
            if op and "." not in op and op not in ops:
                ops.append(op)
    return tuple(ops)


def _entity_form(f: _File, entity_type: str, operation: str, fqcn: str, line: int) -> None:
    """A `form.<op>` handler: its id is built at runtime, so the node is keyed
    by entity type and operation, and its class binds in the resolver."""
    if not operation or "." in operation:
        return
    fid = entity_form_id(entity_type, operation)
    pattern = entity_form_pattern(entity_type, operation)
    if f.add_node(fid, pattern, type="drupal_form", layer="hook", line=line, entity_form=True,
                  pattern=pattern, entity_type=entity_type, operation=operation,
                  class_name=fqcn):
        f.add_pending_class(fid, "form_implemented_by", fqcn, line)


def _forms(f: _File) -> None:
    """A class whose `getFormId()` returns a literal is a `drupal_form`; a
    computed id is legitimate and yields nothing (no candidate). Only a
    class under `src/`: a procedural file's is no form Drupal loads."""
    if not f.src_rel:
        return
    for facts in f.facts:
        if not facts.form_id:
            continue
        name = _short(facts.fqcn)
        implementation = f.class_node(name)
        line = _class_line(f, name)
        extra: dict[str, Any] = {"base_form_id": facts.base_form_id} if facts.base_form_id else {}
        fid = form_id(facts.form_id)
        if not f.add_node(fid, facts.form_id, type="drupal_form", layer="hook", line=line,
                          form_id=facts.form_id, class_name=facts.fqcn, **extra):
            continue
        if implementation is not None:
            f.add_edge(fid, implementation, "form_implemented_by", line)


def _class_line(f: _File, name: str) -> int:
    """The line core gave the class node, else 1."""
    from graphify.extractors.base import _make_id

    location = f.core_lines.get(_make_id(f.stem, name), "")
    digits = location[1:] if location.startswith("L") else ""
    return int(digits) if digits.isdigit() else 1


def _plugins_and_entity_types(f: _File) -> None:
    if not f.src_rel:
        return
    types = _types(f.registry)
    attribute_classes = {t.attribute_class for t in f.registry.types.values() if t.attribute_class}
    annotation_shorts = {_short(t.annotation_class) for t in f.registry.types.values()
                         if t.annotation_class}
    for d in _declarations(f):
        declared_id = d.values.get("id")
        if not isinstance(declared_id, str) or not declared_id:
            continue
        kind = _entity_kind(d)
        if kind:
            _entity_type(f, d, kind, declared_id)
            continue
        matched = False
        for t in types:
            if not _in_subdir(f.src_rel, t.subdir):
                continue
            if (d.attribute == t.attribute_class) if d.attribute \
                    else _annotation_matches(d, t.annotation_class):
                matched = True
                _plugin(f, d, t, declared_id)
        known = d.attribute in attribute_classes if d.attribute else d.short in annotation_shorts
        if not matched and not known and f.src_rel.startswith(_PLUGIN_DIR):
            f.candidate(_UNKNOWN_PLUGIN_TYPE, d.line, **{
                "class": d.class_fqcn,
                "attribute": d.attribute or f"@{d.short}",
            })


# -- services (spec §7) ----------------------------------------------------------------


def pending_call_id(*parts: str) -> str:
    """The placeholder target of a pending `calls` edge: one per source and
    (service, method) or (class, property, method), so one ordered pair."""
    return make_id("drupal", "pending", "call", *parts)


def _caller(f: _File, site: Any) -> str | None:
    """The node core emitted for the function or method enclosing `site`."""
    from graphify.extractors.base import _make_id

    if site.function:
        nid = _make_id(f.stem, site.function)
    elif site.class_name and site.method:
        nid = _make_id(_make_id(f.stem, site.class_name), site.method)
    else:
        return None
    return nid if nid in f.core_ids else None


def _caller_name(site: Any) -> str:
    if site.function:
        return site.function
    return f"{site.class_name}::{site.method}" if site.class_name else ""


def _services(f: _File) -> None:
    """`uses_service` from each of spec §7.1's forms, and a pending
    `calls` edge for each method call on a receiver naming a service (§7.4):
    `service_call` when the service is known here, `property_call` for
    `$this->p`, which the resolver settles with §7.3's rules. A non-literal
    service id is a `non_literal_service` candidate; a call on a property
    no rule resolves is an `unresolved_receiver` candidate when the
    property's declared type is one a known service answers to."""
    from graphify.drupal.php_services import is_service_type, property_service, resolve_alias
    from graphify.drupal.yaml_common import service_id

    registry = f.registry
    shortcuts = registry.shortcuts or {}
    for use in f.service_uses:
        sid = shortcuts.get(use.name, "") if use.via == "shortcut" else use.name
        if use.via == "shortcut" and not sid:
            continue
        if not sid:
            f.candidate(_NON_LITERAL_SERVICE, use.line, via=use.via, argument=use.raw,
                        caller=_caller_name(use))
            continue
        source = f.class_node(use.class_name) if use.via == "create" else _caller(f, use)
        if source is None:
            continue
        target = resolve_alias(registry, sid)
        extra: dict[str, Any] = {"alias": sid} if target != sid else {}
        if use.via == "shortcut":
            extra["shortcut"] = use.name
        f.add_edge(source, service_id(target), "uses_service", use.line, via=use.via,
                   target_name=target, **extra)

    classes = {_short(facts.fqcn): facts.fqcn for facts in f.facts}
    _injected(f, classes)
    for call in f.service_calls:
        source = _caller(f, call)
        if call.receiver == "property":
            fqcn = classes.get(call.class_name)
            if not fqcn:
                continue
            service, declared = property_service(registry, fqcn, call.target)
            if not service and declared and is_service_type(registry, declared):
                f.candidate(_UNRESOLVED_RECEIVER, call.line, **{
                    "class": fqcn, "property": call.target, "type": declared,
                    "method": call.name})
            if source is None:
                continue
            f.add_edge(source, pending_call_id(fqcn, call.target, call.name), "calls", call.line,
                       **{PENDING: PENDING_PROPERTY_CALL, "class": fqcn}, property=call.target,
                       method=call.name)
            continue
        sid = call.target if call.receiver == "service" else shortcuts.get(call.target, "")
        if not sid or source is None:
            continue
        sid = resolve_alias(registry, sid)
        f.add_edge(source, pending_call_id(sid, call.name), "calls", call.line,
                   **{PENDING: PENDING_SERVICE_CALL}, service=sid, method=call.name)


def _injected(f: _File, classes: dict[str, str]) -> None:
    """`uses_service` `via: injected` from a class to each service a
    property it uses (reads or calls, never only assigns) holds by §7.3's
    rules: one edge per (class, service), `properties` listing them. The
    pair a `via: create` edge already has keeps that edge (emitted first):
    this carries the calls on a service the class did not get in its own
    `create()` -- services.yml arguments, autowiring, a parent's
    constructor or `create()`."""
    from graphify.drupal.php_services import property_service
    from graphify.drupal.yaml_common import service_id

    found: dict[tuple[str, str], tuple[int, set[str]]] = {}
    seen: set[tuple[str, str]] = set()
    for use in f.property_uses:
        key = (use.class_name, use.name)
        fqcn = classes.get(use.class_name)
        if key in seen or not fqcn:
            continue
        seen.add(key)
        service = property_service(f.registry, fqcn, use.name)[0]
        if not service:
            continue
        line, props = found.setdefault((use.class_name, service), (use.line, set()))
        props.add(use.name)
    for (class_name, service), (line, props) in found.items():
        source = f.class_node(class_name)
        if source is not None:
            f.add_edge(source, service_id(service), "uses_service", line, via="injected",
                       target_name=service, properties=sorted(props))


# -- events (spec §8) --------------------------------------------------------------------


def event_id(name: str) -> str:
    return make_id("drupal", "event", name)


def _events(f: _File) -> None:
    """`subscribes_to_event` (class -> `drupal_event`) for each key of a
    class's `getSubscribedEvents()` that is a string literal or a class
    constant the registry's `event_constants` resolves; any other key is an
    `unresolved_event` candidate. `method` lists the key's listeners
    (comma-joined, one edge per class and event); `priority` is the
    listener's integer priority when there is one listener, `priorities`
    the list when there are several."""
    if not f.src_rel or not f.events:
        return
    constants = f.registry.event_constants or {}
    for key in f.events:
        name = key.event or constants.get(key.constant, "")
        methods = [m for m, _p in key.listeners]
        if not name:
            f.candidate(_UNRESOLVED_EVENT, key.line, **{
                "class": key.fqcn, "event": key.raw, "method": ",".join(methods)})
            continue
        source = f.class_node(key.class_name)
        if source is None or not methods:
            continue
        priorities = [p for _m, p in key.listeners]
        extra: dict[str, Any] = {"method": ",".join(methods)}
        if len(priorities) == 1:
            if priorities[0] is not None:
                extra["priority"] = priorities[0]
        elif all(p is not None for p in priorities):
            extra["priorities"] = priorities
        f.add_edge(source, event_id(name), "subscribes_to_event", key.line, target_name=name, **extra)


#: Every producer, run in order over one file's reading. Tasks 3-5 add theirs.
_PRODUCERS: tuple[Callable[[_File], None], ...] = (_plugins_and_entity_types, _forms, _services,
                                                   _events)


def _run(path: Path, core_result: dict | None, registry: Any = None) -> dict[str, Any]:
    f = _read(path, core_result, registry)
    if f is None:
        return _empty()
    for producer in _PRODUCERS:
        producer(f)
    return {"nodes": f.nodes, "edges": f.edges, "php_candidates": f.candidates}


def extract_php_semantics(path: Path, core_result: dict) -> dict[str, Any]:
    """The Drupal nodes and edges one custom PHP file declares, plus its
    `php_candidates`. `core_result` is core's own extraction of `path`: an
    edge to a class node is emitted only when core emitted that id. Never
    raises: bad input, or no current registry, yields nothing."""
    try:
        return _run(Path(path), core_result)
    except Exception:
        return _empty()


def find_php_candidates(path: Path, registry: Any = None) -> list[dict[str, Any]]:
    """The `php_candidates` of one file (spec §10), for the inventory: the
    same reading the extractor makes, against `registry` (else the current
    one), so the two cannot disagree. Never raises."""
    try:
        return _run(Path(path), None, registry)["php_candidates"]
    except Exception:
        return []
