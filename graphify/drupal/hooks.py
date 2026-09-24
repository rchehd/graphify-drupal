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
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphify.drupal.php_classes import _parser
from graphify.ids import make_id

_MAX_SIZE = 1024 * 1024  # 1 MiB, matching php_classes.read_php_class
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
    try:
        return _read_hook_stubs(path)
    except Exception:
        return []


def _read_hook_stubs(path: Path) -> list[tuple[str, int]]:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_SIZE:
            return []
        source = path.read_bytes()
    except OSError:
        return []
    try:
        tree = _parser().parse(source)
    except Exception:
        return []
    root = tree.root_node
    if root is None:
        return []
    found: list[tuple[str, int]] = []
    _collect_stubs(root, found)
    return found


def _collect_stubs(node: "Any", found: list[tuple[str, int]]) -> None:
    for child in node.named_children:
        if child.type == "namespace_definition":
            body = child.child_by_field_name("body")
            if body is not None:
                _collect_stubs(body, found)
            continue
        if child.type != "function_definition":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        name = name_node.text.decode("utf-8", errors="replace")
        if name.startswith(_HOOK_PREFIX) and len(name) > len(_HOOK_PREFIX):
            found.append((name[len(_HOOK_PREFIX):], child.start_point[0] + 1))


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
