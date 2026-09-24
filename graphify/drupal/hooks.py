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
`#[Hook]` attributes in any PHP file, and procedural `<ext>_<hook>()`
functions in an extension's procedural files (`is_procedural_file`), which
the seam makes PHP for detection and extraction (spec §5.2). Only a literal,
declared hook name becomes an implementation; everything else that looks
like one is a `hook_candidates` inventory entry (vocabulary §5.6).

Three readings of spec §5.2-§5.4, settled for P2b:
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
  it, and each implementation gets its own `hook_implemented_by` edge.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphify.drupal.php_classes import (
    PhpAttribute,
    PhpFunction,
    read_php_attributes,
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
    """A `.php` file under some `src/Hook/` directory -- where Drupal 11
    looks for `#[Hook]` classes."""
    if Path(path).suffix != ".php":
        return False
    parts = Path(path).parts
    return any(parts[i] == "src" and parts[i + 1] == "Hook" for i in range(len(parts) - 2))


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
        kind, pattern = _classify(rest, registry)
        if kind == "declared":
            impls.append(_Impl(ext, rest, "procedural", function.line, function=function.name))
        elif kind == "variable":
            candidates.append(_candidate(kind, ext, rest, path, function.line, pattern))
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


def _attribute(path: Path, owner: str, class_name: str, method: str, attribute: PhpAttribute,
               registry: Any, impls: list[_Impl], candidates: list[dict]) -> None:
    args = _hook_args(attribute)
    module_arg = args.get("module")
    module = (module_arg.string if module_arg is not None else None) or owner
    hook_arg = args.get("hook")
    if not module or hook_arg is None:
        return
    order_arg = args.get("order")
    order = order_arg.text if order_arg is not None else ""
    if hook_arg.string is None:
        candidates.append(_candidate("non_literal", module, hook_arg.text, path, attribute.line))
        return
    kind, pattern = _classify(hook_arg.string, registry)
    if kind == "declared":
        impls.append(_Impl(module, hook_arg.string, "attribute", attribute.line,
                           class_name=class_name, method=method, order=order))
    else:
        candidates.append(_candidate(kind, module, hook_arg.string, path, attribute.line, pattern))


def _attributes(path: Path, registry: Any, impls: list[_Impl], candidates: list[dict]) -> None:
    from graphify.drupal.discovery import registry_owner_of

    try:
        if b"Hook" not in path.read_bytes():
            return
    except OSError:
        return
    owner = registry_owner_of(registry, Path(path).absolute())
    for cls in read_php_attributes(path):
        for attribute in cls.attributes:
            if attribute.name == HOOK_ATTRIBUTE:
                method_arg = _hook_args(attribute).get("method")
                method = (method_arg.string if method_arg is not None else None) or _INVOKE
                _attribute(path, owner, cls.name, method, attribute, registry, impls, candidates)
        for method in cls.methods:
            for attribute in method.attributes:
                if attribute.name == HOOK_ATTRIBUTE:
                    _attribute(path, owner, cls.name, method.name, attribute, registry, impls, candidates)


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
    """The `hook_candidates` entries of one file (spec §5.4). Never raises."""
    try:
        return _scan(path, registry)[1]
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
            nodes.append(node(impl_id, f"{impl.module}:{impl.hook}", type="drupal_hook_impl",
                              layer="hook", path=path, line=impl.line, **attrs))
        add_edge(extension_id(impl.module), hook_id(impl.hook), "implements_hook", impl.line,
                 owner=impl.module, target_name=impl.hook)
        if impl.function:
            target = _make_id(stem, impl.function)
        else:
            target = _make_id(_make_id(stem, impl.class_name), impl.method)
        if target in core_ids:
            add_edge(impl_id, target, "hook_implemented_by", impl.line)
    return {"nodes": nodes, "edges": edges, "hook_candidates": candidates}


def extract_php_with_hooks(path: Path) -> dict[str, Any]:
    """Core's own PHP handler for `path`, plus its hook implementations: the
    handler for procedural files and `src/Hook/**/*.php` (spec §5.2).
    `register.py` asserts that core's `.php` handler exists."""
    import graphify.extract as core

    result = dict(core._DISPATCH[".php"](path))
    ours = extract_hook_implementations(path, result)
    result["nodes"] = list(result.get("nodes") or []) + ours["nodes"]
    result["edges"] = list(result.get("edges") or []) + ours["edges"]
    return result


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
