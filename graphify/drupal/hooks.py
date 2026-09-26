"""Hooks declared by `*.api.php` stub files (P2b spec §5).

A hook is whatever some extension's `*.api.php` declares as a stub:
`function hook_<name>(...) {}` at the top level of the file. The registry
walk (`discovery.build_registry`) collects every such file, boundary
included, so `Registry.hooks` knows every hook Drupal core, contrib and
custom code declare -- not only the ones a graphed file happens to mention.

`extract_hook_declarations` is the extraction-time half: for an in-graph
`*.api.php` file it re-reads the same stubs and emits one `drupal_hook` node
per stub plus a `declares_hook` edge from the owning extension, composed onto
core's own PHP nodes for that file (see `register.py`'s `_compose`).

`extract_hook_implementations` reads what implements a hook (spec §5.4):
`#[Hook]` attributes in an extension's `src/Hook/**/*.php`
(`is_hook_class_file`, where Drupal's `HookCollectorPass` looks; one
anywhere else is a `misplaced` candidate), and procedural `<ext>_<hook>()`
functions in an extension's procedural files (`is_procedural_file`), which
the seam makes PHP for detection and extraction (spec §5.2). `<ext>_update_N`
and `<ext>_post_update_*` are update functions, never hooks, and are skipped. Only a literal,
declared hook name becomes an implementation; everything else that looks
like one is a `hook_candidates` inventory entry (vocabulary §5.6).

Readings of spec §5.2-§5.4, settled for P2b:
- a procedural `<ext>_<rest>()` naming no declared hook is an `undeclared`
  candidate only when it claims to be a hook -- its docblock reads
  `Implements hook_…`, or it sits in a group file `<ext>.<group>.inc` and
  `<rest>` starts with `<group>_`; any other `<ext>_*` is a helper and
  yields nothing (`_claims_a_hook`);
- `.inc` was already core's (a Pascal include): only `<ext>.<group>.inc`
  beside `<ext>.info.yml` becomes PHP; any other `.inc` keeps core's own
  classification and handler (`register.py`'s `classify_file`);
- the id `drupal:hook_impl:<module>:<hook>` is one node however many
  functions or methods implement that hook for that module: the first names
  it, and each implementation gets its own `hook_implemented_by` edge;
- a non-literal hook name, `module:` or class-level `method:` is a
  `non_literal` candidate, never an implementation;
- `invoke` and `alter` are names other APIs share (`ReflectionMethod::invoke`,
  a class's own `alter()`), so they are invocations only on an explicit
  module- or theme-handler receiver (`_is_handler_receiver`); on any other
  receiver the call is an `unknown_receiver` candidate. `invokeAll`,
  `invokeAllWith`, `hasImplementations` and the `*Deprecated` forms count on
  any receiver;
- an extension implementing a hook it declares itself gets no
  `implements_hook`: that pair already carries `declares_hook`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphify.drupal.php_classes import (
    PhpAttribute,
    PhpCall,
    PhpFunction,
    read_php_attributes,
    read_php_calls,
    read_php_functions,
)
from graphify.ids import make_id

_HOOK_PREFIX = "hook_"


@dataclass(frozen=True)
class HookDecl:
    name: str
    provider: str
    file: str
    line: int
    pattern: str = ""   # UPPERCASE runs -> "*", e.g. "form_*_alter"; "" when fixed


def hook_id(name: str) -> str:
    return make_id("drupal", "hook", name)


def hook_pattern(name: str) -> str:
    """`name` with each run of consecutive ALL-CAPS segments collapsed to `*`.

    `form_FORM_ID_alter` -> `form_*_alter`. Returns `""` for a hook with no
    variable segment -- `HookDecl.pattern`'s own default.
    """
    segments = name.split("_")
    out: list[str] = []
    variable = False
    i, n = 0, len(segments)
    while i < n:
        seg = segments[i]
        if seg.isupper():
            variable = True
            while i < n and segments[i].isupper():
                i += 1
            out.append("*")
        else:
            out.append(seg)
            i += 1
    return "_".join(out) if variable else ""


def read_hook_stubs(path: Path) -> list[tuple[str, int]]:
    """`(name, line)` for every top-level `function hook_<name>(` in `path`.

    Only a direct statement of the file (or of a brace-style namespace body)
    counts -- a function nested in a class, method or conditional block is
    not a hook stub. Never raises: unreadable, oversized or unparsable files
    yield an empty list, like `php_classes.read_php_class`.
    """
    return [
        (f.name[len(_HOOK_PREFIX):], f.line)
        for f in read_php_functions(path)
        if f.name.startswith(_HOOK_PREFIX) and len(f.name) > len(_HOOK_PREFIX)
    ]


def extract_hook_declarations(path: Path) -> dict[str, Any]:
    """One `drupal_hook` node plus a `declares_hook` edge per hook stub `path`
    declares. Meant to be composed onto core's own PHP handler for an
    in-graph `*.api.php` (see `register.py`'s `_compose`); empty when the
    file declares no stub or when no registry is current for this process."""
    # Function-local: discovery.py imports this module at the top level, so a
    # top-level import here would cycle.
    from graphify.drupal.discovery import current_registry, registry_owner_of
    from graphify.drupal.yaml_common import edge, node
    from graphify.drupal.yaml_extract import extension_id

    stubs = read_hook_stubs(path)
    if not stubs:
        return {"nodes": [], "edges": []}
    registry = current_registry()
    provider = registry_owner_of(registry, path) if registry is not None else ""

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for name, line in stubs:
        hid = hook_id(name)
        pattern = hook_pattern(name)
        attrs: dict[str, Any] = {"hook_name": name, "provider": provider}
        if pattern:
            attrs["pattern"] = pattern
        nodes.append(node(hid, name, type="drupal_hook", layer="hook", path=path, line=line, **attrs))
        if provider:
            edges.append(edge(extension_id(provider), hid, "declares_hook", path=path, line=line,
                               owner=provider, target_name=name))
    return {"nodes": nodes, "edges": edges}


# -- implementations (spec §5.2-§5.4) ------------------------------------------

#: What the seam adds to core's `CODE_EXTENSIONS` (spec §5.2). `.inc` is
#: already there -- core reads it as a Pascal include -- so only the other
#: four are new to core; `register.py` keeps them unclassified unless
#: `is_procedural_file` says otherwise.
PROCEDURAL_SUFFIXES = (".module", ".install", ".theme", ".profile", ".inc")
_SINGLE_SUFFIXES = frozenset({".module", ".install", ".theme", ".profile"})

HOOK_ATTRIBUTE = "Drupal\\Core\\Hook\\Attribute\\Hook"
#: `Hook::__construct(string $hook, string $method = "", ?string $module = NULL, ?OrderInterface $order = NULL)`.
_HOOK_PARAMS = ("hook", "method", "module", "order")
_INVOKE = "__invoke"
_IMPLEMENTS_DOC = "Implements hook_"
_UPDATE_FUNCTION = re.compile(r"update_\d+|post_update_.+")


def hook_impl_id(module: str, hook: str) -> str:
    return make_id("drupal", "hook_impl", module, hook)


def procedural_extension(path: Path) -> str:
    """The extension `path` is a procedural file of, or `""`.

    `<ext>.module`, `<ext>.install`, `<ext>.theme`, `<ext>.profile` or
    `<ext>.<group>.inc`, directly in the directory holding `<ext>.info.yml`.
    """
    parts = Path(path).name.split(".")
    if len(parts) < 2 or not parts[0]:
        return ""
    suffix = "." + parts[-1]
    if suffix in _SINGLE_SUFFIXES:
        if len(parts) != 2:
            return ""
    elif suffix != ".inc" or len(parts) < 3 or not all(parts[1:-1]):
        return ""
    ext = parts[0]
    try:
        return ext if (Path(path).parent / f"{ext}.info.yml").is_file() else ""
    except OSError:
        return ""


def is_procedural_file(path: Path) -> bool:
    return bool(procedural_extension(path))


def is_hook_class_file(path: Path) -> bool:
    """A `.php` file under `<extension>/src/Hook/` -- the only place Drupal's
    `HookCollectorPass` collects `#[Hook]` classes from -- for the extension
    that owns it in the current registry. False without a registry."""
    if Path(path).suffix != ".php" or "/src/Hook/" not in Path(path).as_posix():
        return False
    from graphify.drupal.discovery import current_registry

    registry = current_registry()
    return registry is not None and _is_hook_class_file(path, registry)


def _is_hook_class_file(path: Path, registry: Any) -> bool:
    from graphify.drupal.discovery import registry_owner_of

    target = Path(path).absolute()
    owner = registry_owner_of(registry, target)
    directory = registry.extensions.get(owner)
    if not directory:
        return False
    return target.as_posix().startswith(directory.rstrip("/") + "/src/Hook/")


@dataclass(frozen=True)
class _Binding:
    """A variable hook bound through the registry's inventories (P4 spec
    §6.2-§6.3): the declared pattern hook it implements, and the node(s) it
    is about."""
    declared: str                                  # "form_FORM_ID_alter", "ENTITY_TYPE_<op>"
    relation: str                                  # "alters_form" | "hooks_entity_type"
    targets: tuple[tuple[str, str, str], ...]      # (node id, target_name, confidence)
    attrs: tuple[tuple[str, str], ...] = ()        # impl node attributes


@dataclass(frozen=True)
class _Impl:
    module: str
    hook: str
    via: str             # "attribute" | "procedural"
    line: int
    function: str = ""
    class_name: str = ""
    method: str = ""
    order: str = ""
    binding: _Binding | None = None


_PATTERN_CACHE_ATTR = "_drupal_hook_patterns"


def _patterns(registry: Any) -> list[tuple[str, "re.Pattern[str]"]]:
    """The registry's variable hook patterns, most specific first (the most
    literal characters, then alphabetical), cached on the registry object
    (the same pattern `discovery._class_file_types` uses). A pattern with
    no literal segment would match every name, so it is left out."""
    cached = getattr(registry, _PATTERN_CACHE_ATTR, None)
    if cached is None:
        found = {d.pattern for d in registry.hooks.values() if d.pattern}
        ranked = sorted(
            (p for p in found if p.replace("*", "").strip("_")),
            key=lambda p: (-len(p.replace("*", "")), p),
        )
        cached = [(p, re.compile(".+".join(re.escape(s) for s in p.split("*")))) for p in ranked]
        setattr(registry, _PATTERN_CACHE_ATTR, cached)
    return cached


def _classify(name: str, registry: Any) -> tuple[str, str]:
    """`("declared", "")`, `("variable", <pattern>)` or `("undeclared", "")`."""
    if name in registry.hooks:
        return "declared", ""
    for pattern, regex in _patterns(registry):
        if regex.fullmatch(name):
            return "variable", pattern
    return "undeclared", ""


# -- variable hooks bound (P4 spec §6.2-§6.3) -----------------------------------------

FORM_ALTER = "form_FORM_ID_alter"
_FORM_ALTER_PATTERN = hook_pattern(FORM_ALTER)
_FORM_ALTER_NAME = re.compile(r"form_(.+)_alter")
ENTITY_TYPE_PREFIX = "ENTITY_TYPE_"
_ENTITY_OPS_ATTR = "_drupal_entity_type_ops"
#: `_bind`'s answer for a `form_<x>_alter` no inventory knows: `(marker, x)`.
_UNBOUND_FORM = "unbound_form"


def _entity_type_ops(registry: Any) -> frozenset[str]:
    """`<op>` of every declared `ENTITY_TYPE_<op>` hook with no further
    variable segment, cached on the registry object."""
    cached = getattr(registry, _ENTITY_OPS_ATTR, None)
    if cached is None:
        cached = frozenset(
            name[len(ENTITY_TYPE_PREFIX):] for name in registry.hooks
            if name.startswith(ENTITY_TYPE_PREFIX) and len(name) > len(ENTITY_TYPE_PREFIX)
            and not hook_pattern(name[len(ENTITY_TYPE_PREFIX):]))
        try:
            setattr(registry, _ENTITY_OPS_ATTR, cached)
        except Exception:
            pass
    return cached


def _entity_form_regex(pattern: str) -> "re.Pattern[str]":
    """`php_semantics.entity_form_pattern`'s `_*` is the optional `_<bundle>`
    of `EntityForm::getFormId()` (`<entity>[_<bundle>][_<op>]_form`)."""
    return re.compile("(?:_.+)?".join(re.escape(part) for part in pattern.split("_*")))


def _form_targets(x: str, registry: Any) -> tuple[tuple[str, str, str], ...]:
    """The form(s) `form_<x>_alter` alters: a custom form whose literal
    `getFormId()` is `x`, else every custom form whose `getBaseFormId()` is
    `x`, else the registry's boundary form `x`, else the one entity form
    whose pattern matches `x` most literally (`INFERRED`: the bundle is a
    runtime value). Empty when nothing matches or two entity forms tie."""
    from graphify.drupal.php_semantics import entity_form_id, entity_form_operations, \
        entity_form_pattern, form_id

    facts = [f for f in (registry.class_facts or {}).values() if isinstance(f, dict)]
    if any(f.get("form_id") == x for f in facts):
        return ((form_id(x), x, "EXTRACTED"),)
    based = sorted({f["form_id"] for f in facts
                    if f.get("base_form_id") == x and isinstance(f.get("form_id"), str) and f["form_id"]})
    if based:
        return tuple((form_id(i), i, "EXTRACTED") for i in based)
    if x in (registry.forms or {}):
        return ((form_id(x), x, "EXTRACTED"),)
    matches: list[tuple[int, str, str]] = []
    for entity_type in registry.entity_types or {}:
        if not x.startswith(entity_type + "_"):
            continue
        for op in entity_form_operations(registry, entity_type):
            pattern = entity_form_pattern(entity_type, op)
            if _entity_form_regex(pattern).fullmatch(x):
                literal = len(pattern.replace("_*", ""))
                matches.append((literal, entity_form_id(entity_type, op), pattern))
    if not matches:
        return ()
    best = max(m[0] for m in matches)
    winners = {(nid, pattern) for literal, nid, pattern in matches if literal == best}
    if len(winners) != 1:
        return ()
    nid, pattern = next(iter(winners))
    return ((nid, pattern, "INFERRED"),)


def _entity_split(name: str, module: str, registry: Any, function: str) -> tuple[str, str] | None:
    """`(entity type, op)` of `<t>_<op>` implemented by `module`, when the
    entity type list and the op list agree on one split (vocabulary §5.5).
    A procedural `function` must also read that one way across the module
    list: another module `m` with `function == m_<t'>_<op'>` for a known
    `t'` and `op'` makes it ambiguous. None otherwise."""
    ops = _entity_type_ops(registry)
    types = registry.entity_types or {}

    def splits(rest: str) -> list[tuple[str, str]]:
        return [(rest[:i], rest[i + 1:]) for i, ch in enumerate(rest)
                if ch == "_" and rest[:i] in types and rest[i + 1:] in ops]

    found = [(module, t, op) for t, op in splits(name)]
    if function:
        modules = registry.extension_info or registry.extensions or {}
        for other in modules:
            if other != module and function.startswith(other + "_"):
                found += [(other, t, op) for t, op in splits(function[len(other) + 1:])]
    if len(found) != 1 or found[0][0] != module:
        return None
    return found[0][1], found[0][2]


def _bind(pattern: str, name: str, module: str, registry: Any,
          function: str = "") -> _Binding | tuple[str, str] | None:
    """A `variable` hook of the two patterns P4 binds, bound (spec §6.2-§6.3):
    a `_Binding`; `(_UNBOUND_FORM, x)` for a `form_<x>_alter` no form
    inventory knows; None for anything that stays P2b's candidate."""
    from graphify.drupal.php_semantics import entity_type_id

    if pattern == _FORM_ALTER_PATTERN and FORM_ALTER in registry.hooks:
        match = _FORM_ALTER_NAME.fullmatch(name)
        if match is None:
            return None
        x = match.group(1)
        targets = _form_targets(x, registry)
        if not targets:
            return _UNBOUND_FORM, x
        return _Binding(FORM_ALTER, "alters_form", targets, (("form_id", x),))
    ops = _entity_type_ops(registry)
    if not (pattern.startswith("*_") and pattern[2:] in ops):
        return None
    split = _entity_split(name, module, registry, function)
    if split is None:
        return None
    entity_type, op = split
    return _Binding(ENTITY_TYPE_PREFIX + op, "hooks_entity_type",
                    ((entity_type_id(entity_type), entity_type, "EXTRACTED"),),
                    (("entity_type", entity_type), ("operation", op)))


def declared_hook_of(registry: Any, module: str, name: str) -> str:
    """The declared pattern hook a concrete variable hook `name` of `module`
    implements (`form_FORM_ID_alter`, `ENTITY_TYPE_<op>`) when it binds as
    an attribute implementation would (the module given, not split), else
    "". For the P3 overlay, whose container names the concrete hook."""
    try:
        if registry is None or name in registry.hooks:
            return ""
        kind, pattern = _classify(name, registry)
        bound = _bind(pattern, name, module, registry) if kind == "variable" else None
        return bound.declared if isinstance(bound, _Binding) else ""
    except Exception:
        return ""


def _variable(impls: list[_Impl], candidates: list[dict], impl: _Impl, pattern: str,
              path: Path, registry: Any, function: str = "") -> None:
    """A `variable` hook: an implementation when `_bind` binds it, else an
    `unbound_form` or P2b's `variable` candidate."""
    bound = _bind(pattern, impl.hook, impl.module, registry, function)
    if isinstance(bound, _Binding):
        impls.append(_Impl(impl.module, impl.hook, impl.via, impl.line, impl.function,
                           impl.class_name, impl.method, impl.order, bound))
    elif isinstance(bound, tuple):
        candidates.append({"kind": _UNBOUND_FORM, "module": impl.module, "name": impl.hook,
                           "form_id": bound[1], "file": str(path), "line": impl.line})
    else:
        candidates.append(_candidate("variable", impl.module, impl.hook, path, impl.line, pattern))


def _candidate(kind: str, module: str, name: str, path: Path, line: int, pattern: str = "") -> dict:
    entry: dict[str, Any] = {"kind": kind, "module": module, "name": name}
    if pattern:
        entry["pattern"] = pattern
    entry.update(file=str(path), line=line)
    return entry


def _claims_a_hook(function: PhpFunction, rest: str, path: Path) -> bool:
    """A procedural function naming no declared hook is still worth a
    candidate when it says it is one: its docblock reads `Implements hook_…`,
    or it sits in a group file `<ext>.<group>.inc` and its name continues with
    `<group>_` (`foo_views_data` in `foo.views.inc`, `hook_hook_info`'s
    convention). Any other `<ext>_*` function is an ordinary helper."""
    if _IMPLEMENTS_DOC in function.doc:
        return True
    parts = path.name.split(".")
    return len(parts) >= 3 and parts[-1] == "inc" and rest.startswith(parts[1] + "_")


def _procedural(path: Path, ext: str, registry: Any, impls: list[_Impl], candidates: list[dict]) -> None:
    prefix = ext + "_"
    for function in read_php_functions(path):
        if not function.name.startswith(prefix) or len(function.name) == len(prefix):
            continue
        rest = function.name[len(prefix):]
        if _UPDATE_FUNCTION.fullmatch(rest):
            # `hook_update_N` / `hook_post_update_NAME` are run by the update
            # system, not collected as hooks (Drupal's HookCollectorPass skips them).
            continue
        kind, pattern = _classify(rest, registry)
        if kind == "declared":
            impls.append(_Impl(ext, rest, "procedural", function.line, function=function.name))
        elif kind == "variable":
            _variable(impls, candidates, _Impl(ext, rest, "procedural", function.line,
                                               function=function.name),
                      pattern, path, registry, function.name)
        elif _claims_a_hook(function, rest, path):
            candidates.append(_candidate(kind, ext, rest, path, function.line))


def _hook_args(attribute: PhpAttribute) -> dict[str, Any]:
    """The attribute's arguments by `Hook::__construct` parameter name."""
    args: dict[str, Any] = {}
    for position, arg in enumerate(attribute.args):
        name = arg.name or (_HOOK_PARAMS[position] if position < len(_HOOK_PARAMS) else "")
        if name:
            args[name] = arg
    return args


def _attribute(path: Path, owner: str, class_name: str, method: str, class_level: bool,
               attribute: PhpAttribute, placed: bool, registry: Any,
               impls: list[_Impl], candidates: list[dict]) -> None:
    """One `#[Hook]`: an implementation only when it sits in a hook class
    file (`placed`) and its hook, `module:` and (class-level) `method:` are
    literals naming a declared hook; otherwise the candidate saying why."""
    args = _hook_args(attribute)
    hook_arg, module_arg, method_arg = args.get("hook"), args.get("module"), args.get("method")
    if hook_arg is None:
        return
    name = hook_arg.string if hook_arg.string is not None else hook_arg.text
    module = owner if module_arg is None else (
        module_arg.string if module_arg.string is not None else module_arg.text)
    if not placed:
        # Drupal collects `#[Hook]` only from `<extension>/src/Hook/`.
        candidates.append(_candidate("misplaced", module, name, path, attribute.line))
        return
    literal = hook_arg.string is not None \
        and (module_arg is None or module_arg.string is not None) \
        and (not class_level or method_arg is None or method_arg.string is not None)
    if not literal:
        candidates.append(_candidate("non_literal", module, name, path, attribute.line))
        return
    if not module:
        return
    if class_level:
        method = (method_arg.string if method_arg is not None else "") or _INVOKE
    order_arg = args.get("order")
    order = order_arg.text if order_arg is not None else ""
    kind, pattern = _classify(name, registry)
    if kind == "declared":
        impls.append(_Impl(module, name, "attribute", attribute.line,
                           class_name=class_name, method=method, order=order))
    elif kind == "variable":
        _variable(impls, candidates, _Impl(module, name, "attribute", attribute.line,
                                           class_name=class_name, method=method, order=order),
                  pattern, path, registry)
    else:
        candidates.append(_candidate(kind, module, name, path, attribute.line, pattern))


def _attributes(path: Path, registry: Any, impls: list[_Impl], candidates: list[dict]) -> None:
    from graphify.drupal.discovery import registry_owner_of

    try:
        if b"Hook" not in path.read_bytes():
            return
    except OSError:
        return
    owner = registry_owner_of(registry, Path(path).absolute())
    placed = _is_hook_class_file(path, registry)
    for cls in read_php_attributes(path):
        for attribute in cls.attributes:
            if attribute.name == HOOK_ATTRIBUTE:
                _attribute(path, owner, cls.name, "", True, attribute, placed, registry,
                           impls, candidates)
        for method in cls.methods:
            for attribute in method.attributes:
                if attribute.name == HOOK_ATTRIBUTE:
                    _attribute(path, owner, cls.name, method.name, False, attribute, placed,
                               registry, impls, candidates)


def _scan(path: Path, registry: Any) -> tuple[list[_Impl], list[dict]]:
    impls: list[_Impl] = []
    candidates: list[dict] = []
    path = Path(path)
    ext = procedural_extension(path)
    if ext:
        _procedural(path, ext, registry, impls, candidates)
    if ext or path.suffix == ".php":
        _attributes(path, registry, impls, candidates)
    return impls, candidates


def find_hook_candidates(path: Path, registry: Any) -> list[dict]:
    """The `hook_candidates` entries of one file (spec §5.4), implementation
    and invocation alike. Never raises."""
    try:
        return _scan(path, registry)[1] + _invocation_candidates(path, registry)
    except Exception:
        return []


def extract_hook_implementations(path: Path, core_result: dict) -> dict[str, Any]:
    """`drupal_hook_impl` nodes with their `implements_hook` and
    `hook_implemented_by` edges, plus the file's `hook_candidates`.

    `core_result` is core's own PHP extraction of the same `path`: the
    `hook_implemented_by` target is computed with core's id helpers and
    emitted only when core emitted that very id (spec §5.3). One node per
    (module, hook) in a file -- the first implementation names it -- and one
    `hook_implemented_by` edge per implementing function or method. Never
    raises: bad input, or no current registry, yields nothing.
    """
    try:
        return _extract_hook_implementations(Path(path), core_result)
    except Exception:
        return {"nodes": [], "edges": [], "hook_candidates": []}


def _extract_hook_implementations(path: Path, core_result: dict) -> dict[str, Any]:
    from graphify.drupal.discovery import current_registry
    from graphify.drupal.yaml_common import edge, node
    from graphify.drupal.yaml_extract import extension_id
    from graphify.extractors.base import _file_stem, _make_id

    registry = current_registry()
    if registry is None:
        return {"nodes": [], "edges": [], "hook_candidates": []}
    impls, candidates = _scan(path, registry)

    core_ids = {n.get("id") for n in (core_result or {}).get("nodes") or ()}
    stem = _file_stem(path)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()

    def add_edge(source: str, target: str, relation: str, line: int, **extra: Any) -> None:
        if (source, target) not in seen_pairs:
            seen_pairs.add((source, target))
            edges.append(edge(source, target, relation, path=path, line=line, **extra))

    for impl in impls:
        impl_id = hook_impl_id(impl.module, impl.hook)
        if impl_id not in seen_nodes:
            seen_nodes.add(impl_id)
            attrs: dict[str, Any] = {"module": impl.module, "hook_name": impl.hook, "via": impl.via}
            if impl.function:
                attrs["function"] = impl.function
            else:
                attrs.update(class_name=impl.class_name, method=impl.method)
            if impl.order:
                attrs["order"] = impl.order
            if impl.binding is not None:
                attrs["declared_hook"] = impl.binding.declared
                attrs.update(impl.binding.attrs)
            nodes.append(node(impl_id, f"{impl.module}:{impl.hook}", type="drupal_hook_impl",
                              layer="hook", path=path, line=impl.line, **attrs))
        # A bound variable hook implements its declared pattern hook
        # (`form_FORM_ID_alter`), never a hook node of its concrete name.
        implemented = impl.binding.declared if impl.binding is not None else impl.hook
        declaration = registry.hooks.get(implemented)
        if declaration is None or declaration.provider != impl.module:
            # One relation per ordered node pair: an extension implementing
            # its own hook already has `declares_hook` to it, so the pair is
            # not doubled (the impl node and `hook_implemented_by` still say it).
            add_edge(extension_id(impl.module), hook_id(implemented), "implements_hook", impl.line,
                     owner=impl.module, target_name=implemented)
        if impl.binding is not None:
            for target, target_name, confidence in impl.binding.targets:
                add_edge(impl_id, target, impl.binding.relation, impl.line,
                         target_name=target_name, confidence=confidence)
        if impl.function:
            target = _make_id(stem, impl.function)
        else:
            target = _make_id(_make_id(stem, impl.class_name), impl.method)
        if target in core_ids:
            add_edge(impl_id, target, "hook_implemented_by", impl.line)
    return {"nodes": nodes, "edges": edges, "hook_candidates": candidates}


def extract_php_with_hooks(path: Path) -> dict[str, Any]:
    """Core's own PHP handler for `path`, plus its hook implementations and
    invocation sites: the handler for procedural files and
    `src/Hook/**/*.php` (spec §5.2). `register.py` asserts that core's
    `.php` handler exists."""
    import graphify.extract as core
    from graphify.drupal.merge import compose_handlers
    from graphify.drupal.php_semantics import extract_php_semantics, is_semantics_file

    extras = [extract_hook_implementations, extract_hook_invocations]
    if is_semantics_file(path):
        extras.append(extract_php_semantics)
    return compose_handlers(core._DISPATCH[".php"], extras)(path)


# -- invocations (spec §5.3) ----------------------------------------------------

#: `ModuleHandlerInterface`'s hook-name argument position, 0-based, for each
#: method that takes a hook name directly (`web/core/lib/Drupal/Core/Extension/
#: ModuleHandlerInterface.php`).
_INVOCATION_HOOK_POS = {
    "hasImplementations": 0,
    "invokeAllWith": 0,
    "invokeAll": 0,
    "invoke": 1,
    "invokeAllDeprecated": 1,
    "invokeDeprecated": 2,
}
#: Same, for the two `alter()` forms -- their argument is a plugin/form
#: `$type`, not a hook name; the target hook is `<type>_alter`.
_ALTER_TYPE_POS = {
    "alter": 0,
    "alterDeprecated": 1,
}
# `_invocation_hook_names` reads the alter table first: a name in both would
# silently lose its hook-name reading.
assert not set(_INVOCATION_HOOK_POS) & set(_ALTER_TYPE_POS)
_INVOCATION_METHODS = frozenset(_INVOCATION_HOOK_POS) | frozenset(_ALTER_TYPE_POS)
#: Names other APIs share (`ReflectionMethod::invoke`, a class's own `alter()`):
#: a call counts only on a receiver that is explicitly a module or theme
#: handler (`_is_handler_receiver`). The other names are Drupal's alone.
_GENERIC_INVOCATION_METHODS = frozenset({"invoke", "alter"})
_HANDLER_EXPRESSIONS = frozenset({
    "Drupal::moduleHandler()",
    "Drupal::service('module_handler')",
    'Drupal::service("module_handler")',
    "Drupal::service('theme.manager')",
    'Drupal::service("theme.manager")',
    "Drupal::theme()",
})
_HANDLER_NAMES = frozenset({"modulehandler", "thememanager"})
_RECEIVER_NAME = re.compile(r"(?:^\$|->|::\$?)(\w+)(?:\(\))?$")
_INVOCATION_MARKERS = (b"->invoke", b"->alter", b"->hasImplementations")


def has_hook_invocation_marker(path: Path) -> bool:
    """Cheap text pre-check: does `path` contain a byte sequence any of the
    invocation methods' calls would produce? False for unreadable files."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    return any(marker in data for marker in _INVOCATION_MARKERS)


def _is_handler_receiver(receiver: str) -> bool:
    """`\\Drupal::moduleHandler()`, `\\Drupal::service('module_handler')`,
    `\\Drupal::service('theme.manager')`, `\\Drupal::theme()`, or a variable,
    property or zero-argument method named `moduleHandler`/`themeManager`
    (case-insensitive, ignoring `_`: `$this->moduleHandler`, `$module_handler`,
    `$this?->moduleHandler`, `self::$moduleHandler`, `$this->moduleHandler()`)."""
    text = re.sub(r"\s+", "", receiver).lstrip("\\")
    if text in _HANDLER_EXPRESSIONS:
        return True
    match = _RECEIVER_NAME.search(text)
    return match is not None and match.group(1).replace("_", "").lower() in _HANDLER_NAMES


def _unknown_receiver(call: PhpCall) -> bool:
    return call.name in _GENERIC_INVOCATION_METHODS and not _is_handler_receiver(call.receiver)


def _invocation_calls(path: Path) -> list[PhpCall]:
    path = Path(path)
    if not has_hook_invocation_marker(path):
        return []
    return read_php_calls(path, _INVOCATION_METHODS)


def _invocation_hook_names(call: PhpCall) -> list[str] | None:
    """The literal hook name(s) `call` targets, or None when its relevant
    argument is missing or not a literal (spec §5.3-§5.4)."""
    if call.name in _ALTER_TYPE_POS:
        pos = _ALTER_TYPE_POS[call.name]
        if pos >= len(call.args):
            return None
        arg = call.args[pos]
        if arg.kind == "string":
            return [f"{arg.value}_alter"]
        if arg.kind == "array" and arg.items:
            return [f"{item}_alter" for item in arg.items]
        return None
    pos = _INVOCATION_HOOK_POS.get(call.name)
    if pos is None or pos >= len(call.args):
        return None
    arg = call.args[pos]
    return [arg.value] if arg.kind == "string" else None


def _invocation_raw_name(call: PhpCall) -> str:
    pos = _ALTER_TYPE_POS.get(call.name, _INVOCATION_HOOK_POS.get(call.name))
    if pos is None or pos >= len(call.args):
        return ""
    return call.args[pos].raw


def _invocation_site_candidate(kind: str, call: PhpCall, path: Path, registry: Any) -> dict:
    """One invocation candidate; every kind carries the same fields."""
    from graphify.drupal.discovery import registry_owner_of

    module = registry_owner_of(registry, Path(path).absolute()) if registry is not None else ""
    return {"kind": kind, "module": module or "", "name": _invocation_raw_name(call),
            "method": call.name, "file": str(path), "line": call.line}


def _invocation_candidate(call: PhpCall, path: Path, registry: Any) -> dict | None:
    """The candidate `call` is instead of an edge, or None when it is an edge:
    `unknown_receiver`, `non_literal`, or `top_level` for a call outside any
    function or method (there is no source node for the edge)."""
    if _unknown_receiver(call):
        kind = "unknown_receiver"
    elif _invocation_hook_names(call) is None:
        kind = "non_literal"
    elif not call.function and not (call.class_name and call.method):
        kind = "top_level"
    else:
        return None
    return _invocation_site_candidate(kind, call, path, registry)


def _invocation_candidates(path: Path, registry: Any) -> list[dict]:
    found = (_invocation_candidate(call, path, registry) for call in _invocation_calls(path))
    return [c for c in found if c is not None]


def extract_hook_invocations(path: Path, core_result: dict) -> dict[str, Any]:
    """`invokes_hook` edges from the enclosing PHP function/method to the
    hook(s) each call targets, plus the file's non-literal invocation
    `hook_candidates` (spec §5.3-§5.4). `core_result` is core's own PHP
    extraction of the same `path`: the source id is computed with core's id
    helpers and emitted only when core emitted that very id. Never raises."""
    try:
        return _extract_hook_invocations(Path(path), core_result)
    except Exception:
        return {"nodes": [], "edges": [], "hook_candidates": []}


def _extract_hook_invocations(path: Path, core_result: dict) -> dict[str, Any]:
    from graphify.drupal.discovery import current_registry
    from graphify.drupal.yaml_common import edge
    from graphify.extractors.base import _file_stem, _make_id

    registry = current_registry()
    if registry is None:
        return {"nodes": [], "edges": [], "hook_candidates": []}
    calls = _invocation_calls(path)
    if not calls:
        return {"nodes": [], "edges": [], "hook_candidates": []}

    core_ids = {n.get("id") for n in (core_result or {}).get("nodes") or ()}
    stem = _file_stem(path)
    edges: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for call in calls:
        candidate = _invocation_candidate(call, path, registry)
        if candidate is not None:
            candidates.append(candidate)
            continue
        names = _invocation_hook_names(call) or []
        if call.function:
            source = _make_id(stem, call.function)
        else:   # a method: `_invocation_candidate` took every top-level call
            source = _make_id(_make_id(stem, call.class_name), call.method)
        if source not in core_ids:
            continue
        for name in names:
            pair = (source, hook_id(name))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            # Whether `name` is declared is the target's fact, not the edge's:
            # an edge flag would go stale on an incremental run that re-reads
            # the declaring `*.api.php` but not this file (final review I2).
            edges.append(edge(source, hook_id(name), "invokes_hook", path=path, line=call.line,
                              target_name=name))
    return {"nodes": [], "edges": edges, "hook_candidates": candidates}


def hook_dependent_files(registry: Any) -> set[str]:
    """Every procedural file and `src/Hook/**/*.php` of the registry's
    in-graph (non-boundary) extensions: what a changed hook set makes stale
    (spec §5.5). Absolute POSIX paths."""
    from graphify.drupal.boundary import boundary_dir

    out: set[str] = set()
    if registry is None:
        return out
    for directory in set(registry.extensions.values()):
        base = Path(directory)
        try:
            if boundary_dir(base) is not None:
                continue
            with os.scandir(base) as entries:
                for entry in entries:
                    if entry.is_file() and is_procedural_file(Path(entry.path)):
                        out.add(Path(entry.path).as_posix())
            hook_dir = base / "src" / "Hook"
            if hook_dir.is_dir():
                out.update(p.as_posix() for p in hook_dir.rglob("*.php") if p.is_file())
        except OSError:
            continue
    return out
