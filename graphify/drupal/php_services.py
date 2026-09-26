"""What the registry says about a service a PHP receiver names (P4 spec §7.3, §7.4).

Shared by the per-file extractor (`php_semantics`, for its
`unresolved_receiver` candidates), the resolver (`resolvers.bind_service_calls`)
and the P3 overlay, so the three cannot disagree:

- `resolve_alias` / `service_class`: a service id through the registry's
  aliases to the class `*.services.yml` gives it;
- `property_service`: which service `$this->p` of a custom class holds, by
  §7.3's rules, first match wins:
  1. `create()` passes `$container->get('x')` at the position of the
     constructor parameter assigned to `p`;
  1b. `create()` assigns `$container->get('x')` to the instance it returns
     (setter injection);
  2. the class is a service whose `arguments:` has `@x` at that position;
  3. the class is an autowired service, and that parameter's type is a
     service id or an alias the registry knows;
  4. a parent's constructor assigns `p` from the parameter the class passes
     through `parent::__construct(...)` -- folded into the positions rules
     1-3 read, so the class's own `create()` and service definition decide;
- `is_service_type`: a declared type some known service answers to (an id,
  an alias, or a service's class), the gate for an `unresolved_receiver`
  candidate, so value objects never become one;
- `class_chain`: a custom class and its custom ancestors, the `extends`
  walk a method lookup follows inside the graph.

Nothing here is guessed: an unknown class, an ambiguous definition (two
services of one class disagreeing) or a missing fact resolves to nothing.
"""
from __future__ import annotations

from typing import Any

#: How far an alias chain or an `extends` chain is followed.
_MAX_DEPTH = 16


def resolve_alias(registry: Any, sid: str) -> str:
    """The service id `sid` names once its aliases are followed."""
    aliases = getattr(registry, "service_aliases", None) or {}
    seen = {sid}
    for _ in range(_MAX_DEPTH):
        target = aliases.get(sid)
        if not target or target in seen:
            break
        seen.add(target)
        sid = target
    return sid


def service_class(registry: Any, sid: str) -> str:
    """The class `*.services.yml` gives service `sid` (aliases followed), else ""."""
    if registry is None or not sid:
        return ""
    found = registry.services.get(resolve_alias(registry, sid))
    return str(found[0]).lstrip("\\") if found else ""


def service_for_type(registry: Any, type_name: str) -> str:
    """The service a declared type names as a service id or an alias, else ""."""
    if registry is None or not type_name:
        return ""
    if type_name in (getattr(registry, "service_aliases", None) or {}):
        return resolve_alias(registry, type_name)
    return type_name if type_name in registry.services else ""


class _Index:
    """Per-registry lookups built once: facts by class, services by class."""

    def __init__(self, registry: Any) -> None:
        from graphify.drupal.php_classes import ClassFacts

        self.facts: dict[str, Any] = {}
        for fqcn, data in (registry.class_facts or {}).items():
            try:
                self.facts[fqcn] = ClassFacts.from_dict(data)
            except Exception:
                continue
        self.service_classes = {str(v[0]).lstrip("\\") for v in registry.services.values() if v}
        self.wired: dict[str, list[dict]] = {}
        for sid, wiring in (getattr(registry, "service_wiring", None) or {}).items():
            cls = service_class(registry, sid)
            if cls:
                self.wired.setdefault(cls, []).append(wiring)


_cache: tuple[Any, _Index] | None = None


def _index(registry: Any) -> _Index:
    global _cache
    if _cache is None or _cache[0] is not registry:
        _cache = (registry, _Index(registry))
    return _cache[1]


def is_service_type(registry: Any, type_name: str) -> bool:
    """`type_name` is a service id, an alias, or the class of a known service."""
    if registry is None or not type_name:
        return False
    return bool(service_for_type(registry, type_name)) \
        or type_name in _index(registry).service_classes


def class_chain(registry: Any, fqcn: str) -> list[Any]:
    """`ClassFacts` of `fqcn` and its ancestors, as far as the registry's
    custom classes go (`[]` for a class it does not know)."""
    if registry is None:
        return []
    facts = _index(registry).facts
    chain: list[Any] = []
    seen: set[str] = set()
    current = fqcn.lstrip("\\")
    while current in facts and current not in seen and len(chain) < _MAX_DEPTH:
        seen.add(current)
        chain.append(facts[current])
        current = facts[current].extends
    return chain


def _positions(chain: list[Any], depth: int = 0) -> tuple[dict[str, int], Any]:
    """`({property: parameter position}, constructor class)` for the
    constructor that builds `chain[0]`: the nearest one up the chain, and,
    through its `parent::__construct(...)` pass-throughs, what the parents'
    constructors assign (rule 4). The class's own assignments win."""
    for k, facts in enumerate(chain):
        if facts.params:
            break
    else:
        return {}, None
    ctor, rest = chain[k], chain[k + 1:]
    names = [p.name for p in ctor.params]
    out: dict[str, int] = {p.name: i for i, p in enumerate(ctor.params) if p.promoted}
    for prop, param in ctor.assigns.items():
        if param in names:
            out.setdefault(prop, names.index(param))
    if ctor.parent_args and rest and depth < _MAX_DEPTH:
        parent, _ = _positions(rest, depth + 1)
        for prop, j in parent.items():
            passed = ctor.parent_args[j] if j < len(ctor.parent_args) else ""
            if passed in names:
                out.setdefault(prop, names.index(passed))
    return out, ctor


def _one(values: set[str]) -> str:
    return next(iter(values)) if len(values) == 1 else ""


def property_service(registry: Any, fqcn: str, prop: str) -> tuple[str, str]:
    """`(service id, declared type)` of `$this->prop` in class `fqcn` (spec
    §7.3). The service is "" when no rule names it; the type is the
    constructor parameter's resolved class or interface, "" when there is
    none (no parameter, a builtin, a union)."""
    try:
        return _property_service(registry, fqcn, prop)
    except Exception:
        return "", ""


def _property_service(registry: Any, fqcn: str, prop: str) -> tuple[str, str]:
    chain = class_chain(registry, fqcn)
    if not chain:
        return "", ""
    positions, ctor = _positions(chain)
    i = positions.get(prop)
    type_name = ctor.params[i].type if i is not None else ""
    # Rule 1: the nearest `new static(...)` up the chain builds this class.
    create_args = next((f.create_args for f in chain if f.create_args), ())
    if i is not None and i < len(create_args) and create_args[i]:
        return create_args[i], type_name
    # Rule 1b: setter injection, the class's own `create()` first.
    for facts in chain:
        if prop in facts.create_props:
            return facts.create_props[prop], type_name
    if i is None:
        return "", type_name
    wirings = _index(registry).wired.get(chain[0].fqcn, [])
    # Rule 2: `@x` at that position, the same in every service of the class.
    service = _one({w["arguments"][i] for w in wirings if i < len(w["arguments"])})
    if service:
        return resolve_alias(registry, service), type_name
    # Rule 3: an autowired service, the parameter typed with a service id or alias.
    if any(w.get("autowire") for w in wirings):
        service = service_for_type(registry, type_name)
        if service:
            return service, type_name
    return "", type_name
