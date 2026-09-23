"""Read a single PHP class declaration with tree-sitter.

Used by later P2a tasks to find Drupal plugin managers and read the facts
their constructor and ``getDiscovery()`` method carry: what they extend and
implement, what they pass to ``parent::__construct()``, what discovery
objects they build, and what ``alterInfo()`` they register.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter
import tree_sitter_php

_MAX_SIZE = 1024 * 1024  # 1 MiB

_PARSER: tree_sitter.Parser | None = None


def _parser() -> tree_sitter.Parser:
    global _PARSER
    if _PARSER is None:
        language = tree_sitter.Language(tree_sitter_php.language_php())
        _PARSER = tree_sitter.Parser(language)
    return _PARSER


@dataclass(frozen=True)
class Arg:
    kind: str      # "string" | "class" | "other"
    value: str     # string literal text, or resolved FQCN for `X::class`, or "" for other


@dataclass(frozen=True)
class Discovery:
    cls: str       # resolved FQCN of the `new X(...)` class
    args: tuple[Arg, ...]


@dataclass(frozen=True)
class PhpClass:
    fqcn: str
    kind: str                      # "class" | "abstract" | "interface"
    line: int                      # 1-based line of the declaration
    extends: tuple[str, ...]       # resolved FQCNs (class: 0-1, interface: 0-n)
    implements: tuple[str, ...]    # resolved FQCNs
    construct_args: tuple[Arg, ...] | None   # args of `parent::__construct(...)` in __construct, None if absent
    has_get_discovery: bool
    discoveries: tuple[Discovery, ...]       # every `new …Discovery…(...)` inside getDiscovery()
    alter_info: str | None                   # literal of `$this->alterInfo('x')` anywhere in the class


def resolve_name(name: str, namespace: str, uses: dict[str, str]) -> str:
    """Resolve a PHP name to an FQCN with no leading backslash.

    A leading `\\` is fully qualified; otherwise the first segment is looked
    up in `use` aliases, else the name is prefixed with the file's namespace.
    """
    if name.startswith("\\"):
        return name[1:]
    first, sep, rest = name.partition("\\")
    if first in uses:
        base = uses[first]
        return f"{base}\\{rest}" if rest else base
    if namespace:
        return f"{namespace}\\{name}"
    return name


def read_php_class(path: Path) -> PhpClass | None:
    """Read the first class/interface declaration in a PHP file.

    Returns None for missing/unreadable/oversized files, files tree-sitter
    cannot parse, and files with no class or interface declaration. Never
    raises.
    """
    try:
        if not path.is_file() or path.stat().st_size > _MAX_SIZE:
            return None
        source = path.read_bytes()
    except OSError:
        return None

    try:
        tree = _parser().parse(source)
    except Exception:
        return None

    root = tree.root_node
    if root is None:
        return None

    state = {"namespace": "", "uses": {}, "result": None}
    _find_class(root, state)
    found = state["result"]
    if found is None:
        return None
    class_node, namespace, uses = found

    name_node = class_node.child_by_field_name("name")
    if name_node is None:
        return None
    short_name = _text(name_node)
    fqcn = resolve_name(short_name, namespace, uses)

    kind: str
    if class_node.type == "interface_declaration":
        kind = "interface"
    elif any(c.type == "abstract_modifier" for c in class_node.children):
        kind = "abstract"
    else:
        kind = "class"

    body = class_node.child_by_field_name("body")

    base = next((c for c in class_node.named_children if c.type == "base_clause"), None)
    extends = tuple(resolve_name(_text(n), namespace, uses) for n in (base.named_children if base else ()))

    implements: tuple[str, ...] = ()
    if class_node.type != "interface_declaration":
        iface = next((c for c in class_node.named_children if c.type == "class_interface_clause"), None)
        implements = tuple(
            resolve_name(_text(n), namespace, uses) for n in (iface.named_children if iface else ())
        )

    construct_args: tuple[Arg, ...] | None = None
    has_get_discovery = False
    discoveries: tuple[Discovery, ...] = ()
    alter_info: str | None = None

    if body is not None:
        ctor = _find_method(body, "__construct")
        if ctor is not None:
            ctor_body = ctor.child_by_field_name("body")
            if ctor_body is not None:
                parent_call = next(
                    (
                        n
                        for n in _walk(ctor_body)
                        if n.type == "scoped_call_expression"
                        and n.child_by_field_name("scope") is not None
                        and _text(n.child_by_field_name("scope")) == "parent"
                        and n.child_by_field_name("name") is not None
                        and _text(n.child_by_field_name("name")) == "__construct"
                    ),
                    None,
                )
                if parent_call is not None:
                    construct_args = _build_args(
                        parent_call.child_by_field_name("arguments"), namespace, uses
                    )

        discovery_method = _find_method(body, "getDiscovery")
        has_get_discovery = discovery_method is not None
        if discovery_method is not None:
            discovery_body = discovery_method.child_by_field_name("body")
            found_discoveries: list[Discovery] = []
            if discovery_body is not None:
                for node in _walk(discovery_body):
                    if node.type != "object_creation_expression":
                        continue
                    name_children = [c for c in node.named_children if c.type in ("name", "qualified_name")]
                    if not name_children:
                        continue
                    raw_name = _text(name_children[0])
                    resolved = resolve_name(raw_name, namespace, uses)
                    short = resolved.rsplit("\\", 1)[-1]
                    if "Discovery" not in short:
                        continue
                    args_node = next((c for c in node.children if c.type == "arguments"), None)
                    found_discoveries.append(
                        Discovery(cls=resolved, args=_build_args(args_node, namespace, uses))
                    )
            discoveries = tuple(found_discoveries)

        alter_call = next(
            (
                n
                for n in _walk(body)
                if n.type == "member_call_expression"
                and n.child_by_field_name("object") is not None
                and _text(n.child_by_field_name("object")) == "$this"
                and n.child_by_field_name("name") is not None
                and _text(n.child_by_field_name("name")) == "alterInfo"
            ),
            None,
        )
        if alter_call is not None:
            args_node = alter_call.child_by_field_name("arguments")
            args = _build_args(args_node, namespace, uses)
            if args and args[0].kind == "string":
                alter_info = args[0].value

    return PhpClass(
        fqcn=fqcn,
        kind=kind,
        line=class_node.start_point[0] + 1,
        extends=extends,
        implements=implements,
        construct_args=construct_args,
        has_get_discovery=has_get_discovery,
        discoveries=discoveries,
        alter_info=alter_info,
    )


def _find_class(node: "tree_sitter.Node", state: dict) -> None:
    """Populate state['result'] with the first class/interface declaration.

    Walks top-level declarations in source order, tracking the ambient
    namespace and `use` aliases as it goes, and descending into brace-style
    namespace bodies.
    """
    for child in node.named_children:
        if state["result"] is not None:
            return
        if child.type == "namespace_definition":
            name_node = child.child_by_field_name("name")
            state["namespace"] = _text(name_node) if name_node is not None else ""
            body = child.child_by_field_name("body")
            if body is not None:
                _find_class(body, state)
            continue
        if child.type == "namespace_use_declaration":
            _collect_use_decl(child, state["uses"])
            continue
        if child.type in ("class_declaration", "interface_declaration"):
            state["result"] = (child, state["namespace"], dict(state["uses"]))
            return


def _collect_use_decl(decl: "tree_sitter.Node", uses: dict[str, str]) -> None:
    body = decl.child_by_field_name("body")
    if body is not None:
        prefix_node = next(
            (
                decl.child(i)
                for i in range(decl.child_count)
                if decl.field_name_for_child(i) is None and decl.child(i).type == "namespace_name"
            ),
            None,
        )
        prefix = _text(prefix_node) if prefix_node is not None else ""
        for clause in body.named_children:
            if clause.type == "namespace_use_clause":
                _add_use_clause(uses, clause, prefix)
        return

    for clause in decl.named_children:
        if clause.type == "namespace_use_clause":
            _add_use_clause(uses, clause, "")


def _add_use_clause(uses: dict[str, str], clause: "tree_sitter.Node", prefix: str) -> None:
    alias_node = clause.child_by_field_name("alias")
    name_node = next((c for c in clause.named_children if c.type in ("name", "qualified_name")), None)
    if name_node is None:
        return
    name_text = _text(name_node)
    full = f"{prefix}\\{name_text}" if prefix else name_text
    full = full.lstrip("\\")
    alias = _text(alias_node) if alias_node is not None else full.rsplit("\\", 1)[-1]
    uses[alias] = full


def _find_method(body: "tree_sitter.Node", name: str) -> "tree_sitter.Node | None":
    for node in _walk(body):
        if node.type != "method_declaration":
            continue
        name_node = node.child_by_field_name("name")
        if name_node is not None and _text(name_node) == name:
            return node
    return None


def _build_args(
    args_node: "tree_sitter.Node | None", namespace: str, uses: dict[str, str]
) -> tuple[Arg, ...]:
    if args_node is None:
        return ()
    result: list[Arg] = []
    for arg in args_node.named_children:
        if arg.type != "argument":
            continue
        value_node = arg.named_children[0] if arg.named_children else None
        result.append(_arg_from_value(value_node, namespace, uses))
    return tuple(result)


def _arg_from_value(
    value_node: "tree_sitter.Node | None", namespace: str, uses: dict[str, str]
) -> Arg:
    if value_node is None:
        return Arg("other", "")
    if value_node.type in ("string", "encapsed_string"):
        return Arg("string", _string_value(value_node))
    if value_node.type == "class_constant_access_expression":
        children = [c for c in value_node.named_children]
        if len(children) == 2 and _text(children[1]) == "class":
            cls_name = _text(children[0])
            return Arg("class", resolve_name(cls_name, namespace, uses))
    return Arg("other", "")


def _string_value(node: "tree_sitter.Node") -> str:
    parts: list[str] = []
    for child in node.children:
        if child.type == "string_content":
            parts.append(_text(child))
        elif child.type == "escape_sequence":
            text = _text(child)
            if text == "\\\\":
                parts.append("\\")
            elif text == "\\'":
                parts.append("'")
            else:
                parts.append(text)
    return "".join(parts)


def _walk(node: "tree_sitter.Node"):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _text(node: "tree_sitter.Node | None") -> str:
    if node is None:
        return ""
    return node.text.decode("utf-8", errors="replace")
