"""Read PHP declarations with tree-sitter.

``read_php_class`` is what P2a uses to find Drupal plugin managers and read
the facts their constructor and ``getDiscovery()`` method carry: what they
extend and implement, what they pass to ``parent::__construct()``, what
discovery objects they build, and what ``alterInfo()`` they register.

``read_php_attributes`` and ``read_php_functions`` are P2b's: the attributes
on classes and methods (``#[Hook(...)]``, names resolved, argument source
text kept verbatim) and a file's top-level functions (hook stubs, procedural
hook implementations).

P4 adds the facts the registry learns once per run: ``read_drupal_shortcuts``
(core's ``\\Drupal::`` service shortcuts), ``read_class_annotations`` (Doctrine
``@Name(key = value)`` docblocks, a handful of literal keys only) and
``read_class_facts`` (constructor parameters, ``create()`` arguments, literal
form ids and class constants). ``read_class_semantics`` gives all three
per-class readers from one parse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    construct_discoveries: tuple[Discovery, ...] = ()   # every `new …Discovery…(...)` inside __construct


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
    """Read the class/interface declaration named after the file (else the first) in a PHP file.

    Returns None for missing/unreadable/oversized files, files tree-sitter
    cannot parse, and files with no class or interface declaration. Never
    raises: the tree walks recurse, so a pathologically nested expression
    (a `RecursionError`) is one more file that yields None.
    """
    try:
        return _read_php_class(path)
    except Exception:
        return None


def _read_php_class(path: Path) -> PhpClass | None:
    parsed = _parse(path)
    if parsed is None:
        return None
    source, root = parsed

    state: dict = {"namespace": "", "uses": {}, "found": []}
    _find_class(root, state)
    declarations = state["found"]
    if not declarations:
        return None
    # PSR-4: `X.php` declares `X`; a BC shim declared before it must not win.
    class_node, namespace, uses = next(
        (d for d in declarations if _text(d[0].child_by_field_name("name")) == path.stem),
        declarations[0],
    )

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
    construct_discoveries: tuple[Discovery, ...] = ()
    has_get_discovery = False
    discoveries: tuple[Discovery, ...] = ()
    alter_info: str | None = None

    if body is not None:
        ctor = _find_method(body, "__construct")
        if ctor is not None:
            ctor_body = ctor.child_by_field_name("body")
            if ctor_body is not None:
                construct_discoveries = _collect_discoveries(ctor_body, namespace, uses)
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
            if discovery_body is not None:
                discoveries = _collect_discoveries(discovery_body, namespace, uses)

        alter_call = None if b"alterInfo" not in source else next(
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
        construct_discoveries=construct_discoveries,
    )


@dataclass(frozen=True)
class AttrArg:
    name: str            # the named argument's name (`method:` -> "method"), "" when positional
    text: str            # the value's source text, verbatim (`Order::First`)
    string: str | None   # the value of a plain string literal, else None
    #: The literal value, shaped like an annotation's (`Annotation.values`): a
    #: string, `{"class": fqcn}` for `X::class`, a dict for a keyed array and a
    #: list for an unkeyed one (non-literal elements dropped), else None. Never
    #: evaluated; left out of equality so positional construction still compares.
    value: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class PhpAttribute:
    name: str            # resolved FQCN, no leading backslash
    line: int
    args: tuple[AttrArg, ...]


@dataclass(frozen=True)
class PhpMethod:
    name: str
    line: int
    attributes: tuple[PhpAttribute, ...]


@dataclass(frozen=True)
class AttributedClass:
    name: str            # the short class name, as declared
    fqcn: str
    line: int
    attributes: tuple[PhpAttribute, ...]
    methods: tuple[PhpMethod, ...]


@dataclass(frozen=True)
class PhpFunction:
    name: str
    line: int
    doc: str             # the comment directly above the declaration, "" when none


def _parse(path: Path, source: bytes | None = None) -> "tuple[bytes, tree_sitter.Node] | None":
    """`(source, root node)` of a PHP file, or None when it is missing,
    unreadable, oversized or unparsable. A caller that already read the file
    (the registry's text precheck) passes its bytes as `source`."""
    if source is None:
        try:
            if not path.is_file() or path.stat().st_size > _MAX_SIZE:
                return None
            source = path.read_bytes()
        except OSError:
            return None
    elif len(source) > _MAX_SIZE:
        return None
    try:
        tree = _parser().parse(source)
    except Exception:
        return None
    if tree.root_node is None:
        return None
    return source, tree.root_node


def read_php_attributes(path: Path) -> list[AttributedClass]:
    """Every top-level class in `path` with the attributes on it and on each
    of its own methods, attribute names resolved through the file's `use`
    statements. Classes are listed whether or not they carry any attribute.
    Never raises: bad input yields an empty list."""
    try:
        parsed = _parse(path)
        if parsed is None:
            return []
        return [a for a in (_attributed_class(*found) for found in _classes(parsed[1])) if a is not None]
    except Exception:
        return []


def _classes(root: "tree_sitter.Node") -> "list[tuple[tree_sitter.Node, str, dict[str, str]]]":
    """Every top-level `class` declaration (never an interface) with the
    namespace and `use` aliases in force where it appears."""
    state: dict = {"namespace": "", "uses": {}, "found": []}
    _find_class(root, state)
    return [found for found in state["found"] if found[0].type == "class_declaration"]


def _attributed_class(
    class_node: "tree_sitter.Node", namespace: str, uses: dict[str, str],
) -> AttributedClass | None:
    name = _text(class_node.child_by_field_name("name"))
    if not name:
        return None
    methods: list[PhpMethod] = []
    body = class_node.child_by_field_name("body")
    for member in body.named_children if body is not None else ():
        if member.type != "method_declaration":
            continue
        method_name = _text(member.child_by_field_name("name"))
        if method_name:
            methods.append(PhpMethod(method_name, member.start_point[0] + 1,
                                     _attributes(member, namespace, uses)))
    return AttributedClass(
        name=name,
        fqcn=resolve_name(name, namespace, uses),
        line=class_node.start_point[0] + 1,
        attributes=_attributes(class_node, namespace, uses),
        methods=tuple(methods),
    )


def _attributes(decl: "tree_sitter.Node", namespace: str, uses: dict[str, str]) -> tuple[PhpAttribute, ...]:
    attr_list = decl.child_by_field_name("attributes")
    if attr_list is None:
        return ()
    found: list[PhpAttribute] = []
    for group in attr_list.named_children:
        if group.type != "attribute_group":
            continue
        for attr in group.named_children:
            if attr.type != "attribute":
                continue
            name_node = next((c for c in attr.named_children if c.type in ("name", "qualified_name")), None)
            if name_node is None:
                continue
            args: list[AttrArg] = []
            params = attr.child_by_field_name("parameters")
            for arg in params.named_children if params is not None else ():
                if arg.type != "argument":
                    continue
                arg_name = arg.child_by_field_name("name")
                values = [c for c in arg.named_children if arg_name is None or c.id != arg_name.id]
                if not values:
                    continue
                value = values[-1]
                string = _literal_string(value)
                args.append(AttrArg(_text(arg_name), _text(value), string,
                                    _attr_value(value, namespace, uses, 0)))
            found.append(PhpAttribute(resolve_name(_text(name_node), namespace, uses),
                                      attr.start_point[0] + 1, tuple(args)))
    return tuple(found)


#: How deep `_attr_value` follows nested arrays; deeper values are dropped.
_MAX_ATTR_DEPTH = 32


def _attr_value(node: "tree_sitter.Node | None", namespace: str, uses: dict[str, str],
                depth: int) -> Any:
    """An attribute argument's literal value (see `AttrArg.value`), or None."""
    if node is None or depth > _MAX_ATTR_DEPTH:
        return None
    string = _literal_string(node)
    if string is not None:
        return string
    if node.type == "class_constant_access_expression":
        children = node.named_children
        if len(children) == 2 and _text(children[1]) == "class":
            return {"class": resolve_name(_text(children[0]), namespace, uses)}
        return None
    if node.type != "array_creation_expression":
        return None
    keyed: dict[str, Any] = {}
    items: list[Any] = []
    for element in node.named_children:
        if element.type != "array_element_initializer" or not element.named_children:
            continue
        parts = element.named_children
        value = _attr_value(parts[-1], namespace, uses, depth + 1)
        if len(parts) == 2:
            key = _literal_string(parts[0])
            if key is not None and value is not None:
                keyed[key] = value
        elif value is not None:
            items.append(value)
    return keyed if keyed or not items else items


def read_php_functions(path: Path) -> list[PhpFunction]:
    """Every top-level `function name(` in `path` (directly in the file or in
    a brace-style namespace body), in source order -- never a method, a
    closure, or a function declared inside another block. Never raises."""
    try:
        parsed = _parse(path)
        if parsed is None:
            return []
        found: list[PhpFunction] = []
        _collect_functions(parsed[1], found)
        return found
    except Exception:
        return []


@dataclass(frozen=True)
class CallArg:
    kind: str                        # "string" | "array" | "other"
    value: str                       # the string literal's value; "" otherwise
    items: tuple[str, ...] | None = None   # kind=="array": each element's literal string value,
                                            # None when some element is not a plain string literal
    raw: str = ""                    # the argument's verbatim source text


@dataclass(frozen=True)
class PhpCall:
    name: str            # the called method's name (`->name(...)`)
    line: int
    args: tuple[CallArg, ...]
    function: str        # enclosing top-level function name, "" when inside a class
    class_name: str       # enclosing class name, "" when none
    method: str            # enclosing method name, "" when none
    receiver: str = ""     # the called object's verbatim source text (`$this->moduleHandler`)


def read_php_calls(path: Path, names: "frozenset[str]") -> list[PhpCall]:
    """Every `->name(...)` (or `?->name(...)`) member call in `path` whose method name is in
    `names`, attributed to the nearest enclosing named function or method --
    a closure or arrow function nested inside one does not change the
    attribution (P2b spec §5.3). Never raises: bad input yields an empty
    list."""
    try:
        parsed = _parse(path)
        if parsed is None:
            return []
        found: list[PhpCall] = []
        _collect_calls(parsed[1], names, "", "", "", found)
        return found
    except Exception:
        return []


_MEMBER_CALLS = ("member_call_expression", "nullsafe_member_call_expression")
_CLOSURES = ("anonymous_function", "anonymous_function_creation_expression")


@dataclass(frozen=True)
class _Scope:
    """Where a node sits: the nearest enclosing named function or method (a
    closure or arrow function does not change it, P2b spec §5.3), and the
    variable frame -- the innermost function, method or closure node, whose
    locals are its own (an arrow function shares its parent's)."""
    function: str = ""
    class_name: str = ""
    method: str = ""
    frame: int = 0
    #: Inside `create()`: its first parameter's name (the container).
    container: str = ""


def _walk_scoped(node: "tree_sitter.Node", scope: _Scope, visit) -> None:
    """Call `visit(child, scope)` on every named node below `node`, in source
    order, each with the scope it sits in. A function, method or class
    declaration is visited with its own scope and only its body is walked;
    a closure gets its own frame."""
    for child in node.named_children:
        kind = child.type
        if kind in ("function_definition", "method_declaration", "class_declaration"):
            name = _text(child.child_by_field_name("name"))
            if kind == "class_declaration":
                inner = _Scope(class_name=name, frame=child.id)
            elif kind == "function_definition":
                inner = _Scope(function=name, frame=child.id)
            else:
                first = _params(child, "", {})[:1] if name == "create" else ()
                inner = _Scope(class_name=scope.class_name, method=name, frame=child.id,
                               container=first[0].name if first else "")
            visit(child, inner)
            body = child.child_by_field_name("body")
            if body is not None:
                _walk_scoped(body, inner, visit)
            continue
        if kind in _CLOSURES:
            inner = _Scope(scope.function, scope.class_name, scope.method, child.id, scope.container)
            visit(child, inner)
            _walk_scoped(child, inner, visit)
            continue
        visit(child, scope)
        _walk_scoped(child, scope, visit)


def _collect_calls(
    node: "tree_sitter.Node", names: "frozenset[str]",
    function: str, class_name: str, method: str, out: list[PhpCall],
) -> None:
    def visit(child: "tree_sitter.Node", scope: _Scope) -> None:
        if child.type not in _MEMBER_CALLS:
            return
        call_name = _text(child.child_by_field_name("name"))
        if call_name in names:
            out.append(PhpCall(call_name, child.start_point[0] + 1,
                               _build_call_args(child.child_by_field_name("arguments")),
                               scope.function, scope.class_name, scope.method,
                               _text(child.child_by_field_name("object"))))

    _walk_scoped(node, _Scope(function, class_name, method), visit)


def _build_call_args(args_node: "tree_sitter.Node | None") -> tuple[CallArg, ...]:
    if args_node is None:
        return ()
    result: list[CallArg] = []
    for arg in args_node.named_children:
        if arg.type != "argument":
            continue
        value_node = arg.named_children[0] if arg.named_children else None
        result.append(_call_arg_from_value(value_node))
    return tuple(result)


def _call_arg_from_value(value_node: "tree_sitter.Node | None") -> CallArg:
    if value_node is None:
        return CallArg("other", "")
    raw = _text(value_node)
    literal_value = _literal_string(value_node)
    if literal_value is not None:
        return CallArg("string", literal_value, raw=raw)
    if value_node.type == "array_creation_expression":
        items: list[str] = []
        literal = True
        for element in value_node.named_children:
            value = element
            if element.type == "array_element_initializer":
                value = element.named_children[-1] if element.named_children else None
            item = _literal_string(value)
            if item is not None:
                items.append(item)
            else:
                literal = False
                break
        return CallArg("array", "", tuple(items) if literal else None, raw=raw)
    return CallArg("other", "", raw=raw)


def _collect_functions(node: "tree_sitter.Node", found: list[PhpFunction]) -> None:
    for child in node.named_children:
        if child.type == "namespace_definition":
            body = child.child_by_field_name("body")
            if body is not None:
                _collect_functions(body, found)
            continue
        if child.type != "function_definition":
            continue
        name = _text(child.child_by_field_name("name"))
        if not name:
            continue
        doc = ""
        previous = child.prev_named_sibling
        if previous is not None and previous.type == "comment" \
                and previous.end_point[0] >= child.start_point[0] - 1:
            doc = _text(previous)
        found.append(PhpFunction(name, child.start_point[0] + 1, doc))


def _collect_discoveries(
    body: "tree_sitter.Node", namespace: str, uses: dict[str, str]
) -> tuple[Discovery, ...]:
    """Every `new X(...)` in `body` whose short class name contains `Discovery`, outermost first."""
    found: list[Discovery] = []
    for node in _walk(body):
        if node.type != "object_creation_expression":
            continue
        name_children = [c for c in node.named_children if c.type in ("name", "qualified_name")]
        if not name_children:
            continue
        resolved = resolve_name(_text(name_children[0]), namespace, uses)
        if "Discovery" not in resolved.rsplit("\\", 1)[-1]:
            continue
        args_node = next((c for c in node.children if c.type == "arguments"), None)
        found.append(Discovery(cls=resolved, args=_build_args(args_node, namespace, uses)))
    return tuple(found)


def _find_class(node: "tree_sitter.Node", state: dict) -> None:
    """Append every top-level class/interface declaration to state['found'].

    Walks top-level declarations in source order, tracking the ambient
    namespace and `use` aliases as it goes, and descending into brace-style
    namespace bodies. Each declaration keeps the namespace and aliases in
    force where it appears.
    """
    for child in node.named_children:
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
            state["found"].append((child, state["namespace"], dict(state["uses"])))


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
    """The class's own method `name`: a direct member of its body, never a
    method of an anonymous class some other method builds."""
    for node in body.named_children:
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
    literal_value = _literal_string(value_node)
    if literal_value is not None:
        return Arg("string", literal_value)
    if value_node.type == "class_constant_access_expression":
        children = [c for c in value_node.named_children]
        if len(children) == 2 and _text(children[1]) == "class":
            cls_name = _text(children[0])
            return Arg("class", resolve_name(cls_name, namespace, uses))
    return Arg("other", "")


_STRING_TYPES = ("string", "encapsed_string")
_LITERAL_PARTS = frozenset({"string_content", "escape_sequence"})


def _literal_string(node: "tree_sitter.Node | None") -> str | None:
    """The value of a plain PHP string literal, or None for anything else.

    The one place every reader (call arguments, attribute arguments,
    `alterInfo()`) decides what a literal is: a single- or double-quoted
    string whose parts are all text or escapes. A double-quoted string with
    an interpolated part (`"{$type}_presave"`, `"$x"`, `"${x}_y"`) names
    nothing fixed, so it is not a literal; neither is a heredoc or nowdoc.
    """
    if node is None or node.type not in _STRING_TYPES:
        return None
    if any(child.type not in _LITERAL_PARTS for child in node.named_children):
        return None
    parts: list[str] = []
    for child in node.named_children:
        text = _text(child)
        if child.type == "escape_sequence":
            if text == "\\\\":
                text = "\\"
            elif text == "\\'":
                text = "'"
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


# -- P4: the registry's per-class facts ----------------------------------------


def _walk_scope(node: "tree_sitter.Node"):
    """`_walk`, but never into a nested function, closure, arrow function or
    class body: what those contain is not the enclosing method's own code."""
    yield node
    for child in node.named_children:
        if child.type in _NESTED_SCOPES:
            continue
        yield from _walk_scope(child)


_NESTED_SCOPES = frozenset({
    "anonymous_function", "anonymous_function_creation_expression", "arrow_function",
    "function_definition", "class_declaration", "declaration_list",
})


def _statements(body: "tree_sitter.Node | None") -> "list[tree_sitter.Node]":
    """A compound statement's statements, comments left out."""
    if body is None:
        return []
    return [c for c in body.named_children if c.type != "comment"]


def _sole_return(method: "tree_sitter.Node | None") -> "tree_sitter.Node | None":
    """The returned expression when `method`'s body is exactly `return <expr>;`."""
    if method is None:
        return None
    statements = _statements(method.child_by_field_name("body"))
    if len(statements) != 1 or statements[0].type != "return_statement":
        return None
    values = statements[0].named_children
    return values[0] if len(values) == 1 else None


def _modifiers(method: "tree_sitter.Node") -> set[str]:
    return {_text(c) for c in method.children
            if c.type in ("visibility_modifier", "static_modifier", "abstract_modifier",
                          "final_modifier")}


# -- \Drupal:: shortcuts (spec §7.1) --

#: The two receivers a shortcut's `->get('<id>')` may be called on, whitespace removed.
_CONTAINER_RECEIVERS = frozenset({"static::getContainer()", "static::$container"})


def read_drupal_shortcuts(path: Path) -> dict[str, str]:
    """`method -> service id` for core's `core/lib/Drupal.php` (spec §7.1).

    A shortcut is a public static method of class `Drupal` whose body is
    exactly `return static::getContainer()->get('<literal>');` or `return
    static::$container->get('<literal>');`. `service($id)` (non-literal) and
    `request()` (a call chained onto the `get`) are not. Never raises."""
    try:
        parsed = _parse(path)
        if parsed is None:
            return {}
        out: dict[str, str] = {}
        for class_node, _namespace, _uses in _classes(parsed[1]):
            if _text(class_node.child_by_field_name("name")) != "Drupal":
                continue
            body = class_node.child_by_field_name("body")
            for member in body.named_children if body is not None else ():
                if member.type != "method_declaration":
                    continue
                modifiers = _modifiers(member)
                if "static" not in modifiers or modifiers & {"protected", "private"}:
                    continue
                service = _container_get(_sole_return(member), _CONTAINER_RECEIVERS)
                name = _text(member.child_by_field_name("name"))
                if service and name:
                    out.setdefault(name, service)
        return out
    except Exception:
        return {}


def _container_get(node: "tree_sitter.Node | None", receivers: frozenset[str]) -> str:
    """`x` when `node` is `<receiver>->get('x', …)` for one of `receivers`
    (compared with whitespace removed), else ""."""
    if node is None or node.type != "member_call_expression":
        return ""
    if _text(node.child_by_field_name("name")) != "get":
        return ""
    receiver = "".join(_text(node.child_by_field_name("object")).split())
    if receiver not in receivers:
        return ""
    args = node.child_by_field_name("arguments")
    first = next((a for a in (args.named_children if args is not None else ()) if a.type == "argument"),
                 None)
    if first is None or first.child_by_field_name("name") is not None or not first.named_children:
        return ""
    return _literal_string(first.named_children[0]) or ""


# -- Doctrine annotations (spec §5.1) --


@dataclass(frozen=True)
class Annotation:
    """One Doctrine annotation. Drupal's reader matches an annotation by its
    short name, with no `use` needed (spec §5.1): a matcher compares `short`
    with the annotation class's short name, and, when `imported`, also
    requires `name` to be that class."""
    name: str       # resolved FQCN of `@Name` through the file's `use` statements
    line: int       # 1-based line of the `@Name(`
    values: dict    # the literal top-level `_ANNOTATION_KEYS`: str | {"class": fqcn} | dict | list
    short: str = ""        # the last segment of the name as written (`ContentEntityType`)
    imported: bool = False  # the name is fully qualified or resolved through a `use`


#: The only top-level keys an annotation keeps (spec §5.1, §5.3): the plugin
#: id and deriver, and an entity type's handlers and literal attributes. Every
#: other key (labels above all) is skipped, never evaluated.
_ANNOTATION_KEYS = frozenset({
    "id", "deriver", "handlers", "bundle_entity_type", "base_table", "admin_permission",
})
#: The key Doctrine gives an annotation's unnamed first value; kept as `id`
#: when there is no `id =` (`@RenderElement("x")`, Drupal's `PluginID`).
_ANNOTATION_VALUE = "value"
#: A top-level annotation: `@Name(` first on a docblock line.
_ANNOTATION_START = re.compile(r"^[ \t]*@(\\?[A-Za-z_][\w\\]*)\(", re.MULTILINE)
_TOKEN = re.compile(r"""\s*(?:
    (?P<string>"(?:[^"]|"")*")
  | (?P<name>\\?[A-Za-z_][\w\\]*(?:::[A-Za-z_]\w*)?)
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<punct>[(){}=:,@])
)""", re.VERBOSE)
_MAX_ANNOTATION_DEPTH = 32
#: A value parsed and dropped: a nested annotation (`@Translation(...)`), a
#: constant, a number or a boolean.
_SKIP = object()


class _AnnotationError(Exception):
    pass


class _AnnotationParser:
    """Recursive descent over one `@Name(...)`'s arguments, Doctrine style:
    `key = value` or `key: value`, values that are `"strings"` (`""` escapes
    a quote), `X::class`, `{...}` maps or lists, nested annotations, or bare
    constants. Anything else raises `_AnnotationError`."""

    def __init__(self, text: str, pos: int, namespace: str, uses: dict[str, str]) -> None:
        self.text, self.pos, self.namespace, self.uses = text, pos, namespace, uses

    def _peek(self) -> tuple[str, str]:
        match = _TOKEN.match(self.text, self.pos)
        if match is None:
            raise _AnnotationError(self.pos)
        kind = match.lastgroup or ""
        return kind, match.group(kind)

    def _next(self) -> tuple[str, str]:
        match = _TOKEN.match(self.text, self.pos)
        if match is None:
            raise _AnnotationError(self.pos)
        self.pos = match.end()
        kind = match.lastgroup or ""
        return kind, match.group(kind)

    def _expect(self, punct: str) -> None:
        if self._next() != ("punct", punct):
            raise _AnnotationError(self.pos)

    def _keyed(self) -> str | None:
        """The key of a `key = value` / `key: value` item, consumed, or None
        (nothing consumed) when the next item is a bare value."""
        saved = self.pos
        kind, token = self._peek()
        if kind in ("name", "string"):
            self._next()
            if self._peek() in (("punct", "="), ("punct", ":")):
                self._next()
                return token if kind == "name" else token[1:-1].replace('""', '"')
        self.pos = saved
        return None

    def arguments(self, depth: int) -> dict:
        """After `(`: the keyed arguments up to and including `)`."""
        values: dict = {}
        first = True
        while True:
            if self._peek() == ("punct", ")"):
                self._next()
                return values
            key = self._keyed()
            value = self.value(depth)
            if key is None and first and isinstance(value, str):
                # Doctrine's unnamed first value: `@RenderElement("x")`,
                # which a `PluginID` annotation reads as the id.
                key = _ANNOTATION_VALUE
            first = False
            if key is not None and value is not _SKIP:
                values.setdefault(key, value)
            kind, token = self._next()
            if (kind, token) == ("punct", ")"):
                return values
            if (kind, token) != ("punct", ","):
                raise _AnnotationError(self.pos)

    def _collection(self, depth: int) -> dict | list:
        """After `{`: a map when any item is keyed, else a list."""
        keyed: dict = {}
        items: list = []
        while True:
            if self._peek() == ("punct", "}"):
                self._next()
                return keyed if keyed or not items else items
            key = self._keyed()
            value = self.value(depth)
            if value is not _SKIP:
                if key is None:
                    items.append(value)
                else:
                    keyed.setdefault(key, value)
            kind, token = self._next()
            if (kind, token) == ("punct", "}"):
                return keyed if keyed or not items else items
            if (kind, token) != ("punct", ","):
                raise _AnnotationError(self.pos)

    def value(self, depth: int):
        if depth > _MAX_ANNOTATION_DEPTH:
            raise _AnnotationError(self.pos)
        kind, token = self._next()
        if kind == "string":
            return token[1:-1].replace('""', '"')
        if kind == "number":
            return _SKIP
        if kind == "name":
            if token.endswith("::class"):
                return {"class": resolve_name(token[: -len("::class")], self.namespace, self.uses)}
            return _SKIP
        if token == "{":
            return self._collection(depth + 1)
        if token == "@":
            name_kind, _name = self._next()
            if name_kind != "name":
                raise _AnnotationError(self.pos)
            if self._peek() == ("punct", "("):
                self._next()
                self.arguments(depth + 1)
            return _SKIP
        raise _AnnotationError(self.pos)


def _docblock_text(comment: str) -> str:
    """A `/** ... */` comment's text, line for line, without the comment
    markers and each line's leading `*`."""
    body = comment[3:]
    if body.endswith("*/"):
        body = body[:-2]
    lines = body.split("\n")
    return "\n".join(re.sub(r"^\s*\*(?!/)", "", line) for line in lines)


def _annotations(doc: str, first_line: int, namespace: str, uses: dict[str, str]) -> list[Annotation]:
    text = _docblock_text(doc)
    found: list[Annotation] = []
    pos = 0
    while True:
        match = _ANNOTATION_START.search(text, pos)
        if match is None:
            return found
        parser = _AnnotationParser(text, match.end(), namespace, uses)
        try:
            values = parser.arguments(0)
        except (_AnnotationError, RecursionError):
            # This annotation is skipped; the next line may start another.
            pos = text.find("\n", match.end())
            if pos < 0:
                return found
            continue
        line = first_line + text.count("\n", 0, match.start(1))
        kept = {k: v for k, v in values.items() if k in _ANNOTATION_KEYS}
        if "id" not in kept and isinstance(values.get(_ANNOTATION_VALUE), str):
            kept["id"] = values[_ANNOTATION_VALUE]
        written = match.group(1)
        imported = written.startswith("\\") or written.partition("\\")[0] in uses
        found.append(Annotation(resolve_name(written, namespace, uses), line, kept,
                                written.rsplit("\\", 1)[-1], imported))
        pos = parser.pos


def _class_docblock(class_node: "tree_sitter.Node") -> "tree_sitter.Node | None":
    """The `/** */` comment directly above `class_node`: its previous sibling,
    ending on the line just before the class (or its first attribute)."""
    previous = class_node.prev_named_sibling
    if previous is None or previous.type != "comment":
        return None
    if not _text(previous).startswith("/**") or previous.end_point[0] < class_node.start_point[0] - 1:
        return None
    return previous


def _class_annotations(
    class_node: "tree_sitter.Node", namespace: str, uses: dict[str, str],
) -> list[tuple[str, Annotation]]:
    name = _text(class_node.child_by_field_name("name"))
    doc = _class_docblock(class_node)
    if not name or doc is None:
        return []
    fqcn = resolve_name(name, namespace, uses)
    return [(fqcn, a) for a in _annotations(_text(doc), doc.start_point[0] + 1, namespace, uses)]


def read_class_annotations(path: Path) -> list[tuple[str, Annotation]]:
    """`(class FQCN, annotation)` for every `@Name(...)` that starts a line
    of the docblock directly above a top-level class (spec §5.1).

    Only `_ANNOTATION_KEYS` survive, and only as strings, `{"class": fqcn}`
    for `X::class`, or nested `{...}` maps (dict) and lists (list); nested
    annotations, constants and numbers are dropped. An annotation the reader
    cannot parse is skipped. Never raises."""
    try:
        parsed = _parse(path)
        if parsed is None:
            return []
        out: list[tuple[str, Annotation]] = []
        for found in _classes(parsed[1]):
            out.extend(_class_annotations(*found))
        return out
    except Exception:
        return []


# -- constructor, create(), forms and constants (spec §6.1, §7.2, §8) --


@dataclass(frozen=True)
class CtorParam:
    name: str        # without the `$`
    type: str        # the declared class/interface type, resolved FQCN; "" for none, a builtin or a union
    promoted: bool   # a promoted constructor property (`protected Foo $foo`): `$this->name` is the parameter


@dataclass(frozen=True)
class ClassFacts:
    fqcn: str
    file: str                          # POSIX path the class was read from
    extends: str                       # resolved parent class FQCN, "" when none
    params: tuple[CtorParam, ...]      # `__construct` parameters, in order
    assigns: dict[str, str]            # property -> parameter name, `$this->p = $param;` in `__construct`
    parent_args: tuple[str, ...]       # per `parent::__construct(...)` position: a bare parameter's name, else ""
    create_args: tuple[str, ...]       # per `new static|self|<C>(...)` position in `create()`:
                                       # the service id of `$container->get('<literal>')`, else ""
    form_id: str                       # `getFormId()`'s literal return, else ""
    base_form_id: str                  # `getBaseFormId()`'s literal return, else ""
    constants: dict[str, str]          # `const NAME = '<literal>'`
    #: property -> service id, setter injection in `create()`:
    #: `$v->p = $container->get('<literal>')` where `create()` returns `$v`.
    create_props: dict[str, str] = field(default_factory=dict)
    #: `create()` builds with `new static(...)` (so a subclass inheriting it
    #: is what it builds), not `new self(...)` / `new <Class>(...)`.
    create_static: bool = False

    def to_dict(self) -> dict:
        """A JSON-shaped copy: lists for tuples, `params` as dicts."""
        return {
            "fqcn": self.fqcn, "file": self.file, "extends": self.extends,
            "params": [{"name": p.name, "type": p.type, "promoted": p.promoted} for p in self.params],
            "assigns": dict(self.assigns), "parent_args": list(self.parent_args),
            "create_args": list(self.create_args), "form_id": self.form_id,
            "base_form_id": self.base_form_id, "constants": dict(self.constants),
            "create_props": dict(self.create_props),
            "create_static": self.create_static,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ClassFacts:
        return cls(
            fqcn=str(data.get("fqcn") or ""),
            file=str(data.get("file") or ""),
            extends=str(data.get("extends") or ""),
            params=tuple(CtorParam(str(p.get("name") or ""), str(p.get("type") or ""),
                                   bool(p.get("promoted")))
                         for p in data.get("params") or []),
            assigns={str(k): str(v) for k, v in (data.get("assigns") or {}).items()},
            parent_args=tuple(str(a) for a in data.get("parent_args") or []),
            create_args=tuple(str(a) for a in data.get("create_args") or []),
            form_id=str(data.get("form_id") or ""),
            base_form_id=str(data.get("base_form_id") or ""),
            constants={str(k): str(v) for k, v in (data.get("constants") or {}).items()},
            create_props={str(k): str(v) for k, v in (data.get("create_props") or {}).items()},
            create_static=data.get("create_static") is True,
        )


#: Declared types that name no class: a parameter typed with one has type "".
_BUILTIN_TYPES = frozenset({
    "array", "bool", "callable", "false", "float", "int", "iterable", "mixed", "never",
    "null", "object", "parent", "self", "static", "string", "true", "void",
})
_PARAMETERS = ("simple_parameter", "property_promotion_parameter", "variadic_parameter")


def _variable(node: "tree_sitter.Node | None") -> str:
    """`x` for a bare `$x`, else ""."""
    if node is None or node.type != "variable_name":
        return ""
    return _text(node)[1:]


def _param_type(param: "tree_sitter.Node", namespace: str, uses: dict[str, str]) -> str:
    declared = next((c for c in param.named_children
                     if c.type in ("named_type", "optional_type", "union_type", "primitive_type",
                                   "intersection_type", "disjunctive_normal_form_type")), None)
    if declared is not None and declared.type == "optional_type":
        declared = next((c for c in declared.named_children if c.type == "named_type"), None)
    if declared is None or declared.type != "named_type":
        return ""
    name_node = next((c for c in declared.named_children if c.type in ("name", "qualified_name")), None)
    name = _text(name_node)
    if not name or name.lower() in _BUILTIN_TYPES:
        return ""
    return resolve_name(name, namespace, uses)


def _params(method: "tree_sitter.Node | None", namespace: str, uses: dict[str, str]) -> tuple[CtorParam, ...]:
    if method is None:
        return ()
    formal = method.child_by_field_name("parameters")
    out: list[CtorParam] = []
    for param in formal.named_children if formal is not None else ():
        if param.type not in _PARAMETERS:
            continue
        name = _variable(next((c for c in param.named_children if c.type == "variable_name"), None))
        if name:
            out.append(CtorParam(name, _param_type(param, namespace, uses),
                                 param.type == "property_promotion_parameter"))
    return tuple(out)


def _positional(args_node: "tree_sitter.Node | None") -> "list[tree_sitter.Node | None]":
    """Each argument's value node, None for a named or spread argument (its
    position says nothing)."""
    out: list[tree_sitter.Node | None] = []
    for arg in args_node.named_children if args_node is not None else ():
        if arg.type == "variadic_unpacking":
            out.append(None)
            continue
        if arg.type != "argument":
            continue
        if arg.child_by_field_name("name") is not None or not arg.named_children \
                or arg.named_children[0].type == "variadic_unpacking":
            out.append(None)
            continue
        out.append(arg.named_children[0])
    return out


def _assigns(ctor_body: "tree_sitter.Node", params: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in _walk_scope(ctor_body):
        if node.type != "assignment_expression":
            continue
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or left.type != "member_access_expression":
            continue
        if _text(left.child_by_field_name("object")) != "$this":
            continue
        prop = left.child_by_field_name("name")
        param = _variable(right)
        if prop is not None and prop.type == "name" and param in params:
            out.setdefault(_text(prop), param)
    return out


def _parent_args(ctor_body: "tree_sitter.Node") -> tuple[str, ...]:
    for node in _walk_scope(ctor_body):
        if node.type == "scoped_call_expression" \
                and _text(node.child_by_field_name("scope")) == "parent" \
                and _text(node.child_by_field_name("name")) == "__construct":
            return tuple(_variable(v) for v in _positional(node.child_by_field_name("arguments")))
    return ()


def _create_args(create: "tree_sitter.Node | None", fqcn: str,
                 namespace: str, uses: dict[str, str]) -> tuple[str, ...]:
    return _create_new(create, fqcn, namespace, uses)[0]


def _create_new(create: "tree_sitter.Node | None", fqcn: str,
                namespace: str, uses: dict[str, str]) -> tuple[tuple[str, ...], bool]:
    """`(service id per position, built with new static)` of `create()`'s
    `new static|self|<C>(...)`, where `<C>` is the class itself; "" for any
    argument that is not `$container->get('<literal>')` on `create()`'s
    first parameter."""
    if create is None:
        return (), False
    params = _params(create, namespace, uses)
    body = create.child_by_field_name("body")
    if not params or body is None:
        return (), False
    receivers = frozenset({f"${params[0].name}"})
    for node in _walk_scope(body):
        if node.type != "object_creation_expression":
            continue
        name_node = next((c for c in node.named_children if c.type in ("name", "qualified_name")), None)
        name = _text(name_node)
        if name not in ("static", "self") and resolve_name(name, namespace, uses) != fqcn:
            continue
        args = next((c for c in node.named_children if c.type == "arguments"), None)
        return tuple(_container_get(v, receivers) for v in _positional(args)), name == "static"
    return (), False


def _create_props(create: "tree_sitter.Node | None", namespace: str,
                  uses: dict[str, str]) -> dict[str, str]:
    """Setter injection in `create()` (spec §7.3 rule 1b): property -> service
    id for every `$v->p = $container->get('<literal>');` where `$v` is the
    local variable `create()` returns (`return $v;`) and `$container` its
    first parameter. A setter call (`$v->setFoo(...)`) is not read."""
    if create is None:
        return {}
    params = _params(create, namespace, uses)
    body = create.child_by_field_name("body")
    if not params or body is None:
        return {}
    receivers = frozenset({f"${params[0].name}"})
    returned = {_variable(n.named_children[0]) for n in _walk_scope(body)
                if n.type == "return_statement" and len(n.named_children) == 1}
    returned.discard("")
    returned.discard("this")
    out: dict[str, str] = {}
    if not returned:
        return out
    for node in _walk_scope(body):
        if node.type != "assignment_expression":
            continue
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or left.type != "member_access_expression":
            continue
        if _variable(left.child_by_field_name("object")) not in returned:
            continue
        prop = left.child_by_field_name("name")
        service = _container_get(right, receivers)
        if prop is not None and prop.type == "name" and service:
            out.setdefault(_text(prop), service)
    return out


def _literal_return(method: "tree_sitter.Node | None") -> str:
    return _literal_string(_sole_return(method)) or ""


def _constants(body: "tree_sitter.Node") -> dict[str, str]:
    out: dict[str, str] = {}
    for member in body.named_children:
        if member.type != "const_declaration":
            continue
        for element in member.named_children:
            if element.type != "const_element" or len(element.named_children) < 2:
                continue
            name, value = element.named_children[0], element.named_children[-1]
            literal = _literal_string(value)
            if name.type == "name" and literal is not None:
                out.setdefault(_text(name), literal)
    return out


def _class_facts(class_node: "tree_sitter.Node", namespace: str, uses: dict[str, str],
                 file: str) -> ClassFacts | None:
    name = _text(class_node.child_by_field_name("name"))
    if not name:
        return None
    fqcn = resolve_name(name, namespace, uses)
    base = next((c for c in class_node.named_children if c.type == "base_clause"), None)
    parent = base.named_children[0] if base is not None and base.named_children else None
    extends = resolve_name(_text(parent), namespace, uses) if parent is not None else ""
    body = class_node.child_by_field_name("body")
    if body is None:
        return ClassFacts(fqcn, file, extends, (), {}, (), (), "", "", {})
    ctor = _find_method(body, "__construct")
    params = _params(ctor, namespace, uses)
    ctor_body = ctor.child_by_field_name("body") if ctor is not None else None
    assigns = _assigns(ctor_body, {p.name for p in params}) if ctor_body is not None else {}
    parent_args = _parent_args(ctor_body) if ctor_body is not None else ()
    create = _find_method(body, "create")
    create_args, create_static = _create_new(create, fqcn, namespace, uses)
    return ClassFacts(
        fqcn=fqcn, file=file, extends=extends, params=params, assigns=assigns,
        parent_args=parent_args,
        create_args=create_args, create_static=create_static,
        create_props=_create_props(create, namespace, uses),
        form_id=_literal_return(_find_method(body, "getFormId")),
        base_form_id=_literal_return(_find_method(body, "getBaseFormId")),
        constants=_constants(body),
    )


def read_class_facts(path: Path) -> list[ClassFacts]:
    """`ClassFacts` for every top-level class in `path`, in source order.
    Never raises: bad input yields an empty list."""
    try:
        parsed = _parse(path)
        if parsed is None:
            return []
        file = Path(path).as_posix()
        return [f for f in (_class_facts(*found, file) for found in _classes(parsed[1])) if f is not None]
    except Exception:
        return []


def read_class_semantics(
    path: Path, source: bytes | None = None,
) -> tuple[list[ClassFacts], list[tuple[str, Annotation]], list[AttributedClass]]:
    """`read_class_facts`, `read_class_annotations` and `read_php_attributes`
    from one parse -- the registry's walk reads hundreds of files. `source`
    is the file's bytes when the caller already read them. A class whose
    facts cannot be read drops out of all three; never raises."""
    try:
        parsed = _parse(path, source)
    except Exception:
        parsed = None
    if parsed is None:
        return [], [], []
    return _class_semantics(parsed[1], Path(path).as_posix())


def _class_semantics(
    root: "tree_sitter.Node", file: str,
) -> tuple[list[ClassFacts], list[tuple[str, Annotation]], list[AttributedClass]]:
    facts: list[ClassFacts] = []
    annotations: list[tuple[str, Annotation]] = []
    attributed: list[AttributedClass] = []
    try:
        found = _classes(root)
    except Exception:
        return facts, annotations, attributed
    for class_node, namespace, uses in found:
        try:
            one_facts = _class_facts(class_node, namespace, uses, file)
            one_annotations = _class_annotations(class_node, namespace, uses)
            one_attributed = _attributed_class(class_node, namespace, uses)
        except Exception:
            continue
        if one_facts is not None:
            facts.append(one_facts)
        annotations.extend(one_annotations)
        if one_attributed is not None:
            attributed.append(one_attributed)
    return facts, annotations, attributed


# -- service use (spec §7.1, §7.4) --


@dataclass(frozen=True)
class ServiceUse:
    """One place custom code reaches a service (spec §7.1)."""
    via: str         # "service" (`\Drupal::service('x')`) | "shortcut" (`\Drupal::name()`) |
                     # "create" (`$container->get('x')` inside `create()`)
    name: str        # service/create: the literal id, "" when not a literal; shortcut: the method
    raw: str         # the id argument's verbatim source text ("" for a shortcut)
    line: int
    function: str    # enclosing top-level function, "" inside a class
    class_name: str  # enclosing class (short name), "" when none
    method: str      # enclosing method, "" when none


@dataclass(frozen=True)
class PropertyUse:
    """`$this->p` read or called in a class's method (not assigned to)."""
    name: str
    line: int
    class_name: str


@dataclass(frozen=True)
class ServiceCall:
    """`R->name(...)` on a receiver that names a service (spec §7.4)."""
    name: str        # the called method
    line: int
    function: str
    class_name: str
    method: str
    receiver: str    # "service" | "shortcut" | "property"
    target: str      # the literal service id | the `\Drupal::` method | the property
    local: str = ""  # the local variable the receiver was assigned to, "" when direct


def _is_drupal(scope_node: "tree_sitter.Node | None", namespace: str, uses: dict[str, str]) -> bool:
    text = _text(scope_node)
    return bool(text) and resolve_name(text, namespace, uses) == "Drupal"


def _first_literal(args_node: "tree_sitter.Node | None") -> tuple[str, str, bool]:
    """`(literal, raw text, present)` of a call's first positional argument."""
    values = _positional(args_node)
    if not values:
        return "", "", False
    value = values[0]
    return (_literal_string(value) or "") if value is not None else "", _text(value), True


class _Sites:
    """`read_service_sites`' visitor: service uses as they come, receiver
    calls on locals settled once every assignment in the file is counted."""

    def __init__(self) -> None:
        self.namespace = ""
        self.uses: dict[str, str] = {}
        self.found_uses: list[ServiceUse] = []
        self.calls: list[ServiceCall] = []
        #: (frame, variable) -> how often it is written / what it was assigned.
        self.writes: dict[tuple[int, str], int] = {}
        self.sources: dict[tuple[int, str], tuple[str, str]] = {}
        self.local_calls: list[tuple[tuple[int, str], str, int, _Scope]] = []
        self.properties: list[PropertyUse] = []

    def receiver(self, node: "tree_sitter.Node | None") -> tuple[str, str] | None:
        """`(kind, target)` when `node` names a service (spec §7.4), else None."""
        while node is not None and node.type == "parenthesized_expression" and node.named_children:
            node = node.named_children[0]
        if node is None:
            return None
        if node.type == "scoped_call_expression" \
                and _is_drupal(node.child_by_field_name("scope"), self.namespace, self.uses):
            name = _text(node.child_by_field_name("name"))
            if name == "service":
                literal, _raw, _present = _first_literal(node.child_by_field_name("arguments"))
                return ("service", literal) if literal else None
            return ("shortcut", name) if name else None
        if node.type in ("member_access_expression", "nullsafe_member_access_expression") \
                and _text(node.child_by_field_name("object")) == "$this":
            prop = node.child_by_field_name("name")
            if prop is not None and prop.type == "name":
                return "property", _text(prop)
        return None

    def write(self, scope: _Scope, node: "tree_sitter.Node | None") -> None:
        """Every variable in `node` (a bare `$v`, a `list(...)`, a parameter
        list) counts as written in `scope`'s frame."""
        if node is None:
            return
        for found in _walk_scope(node):
            name = _variable(found)
            if name and name != "this":
                key = (scope.frame, name)
                self.writes[key] = self.writes.get(key, 0) + 1

    def visit(self, node: "tree_sitter.Node", scope: _Scope) -> None:
        kind = node.type
        if kind == "namespace_definition":
            self.namespace = _text(node.child_by_field_name("name"))
        elif kind == "namespace_use_declaration":
            _collect_use_decl(node, self.uses)
        elif kind in ("function_definition", "method_declaration") or kind in _CLOSURES:
            self.write(scope, node.child_by_field_name("parameters"))
            for child in node.named_children:
                if child.type == "anonymous_function_use_clause":
                    self.write(scope, child)
        elif kind in ("member_access_expression", "nullsafe_member_access_expression"):
            self.property_use(node, scope)
        elif kind == "scoped_call_expression":
            self.drupal_call(node, scope)
        elif kind in _MEMBER_CALLS:
            self.member_call(node, scope)
        elif kind == "assignment_expression":
            left = node.child_by_field_name("left")
            name = _variable(left)
            if name:
                key = (scope.frame, name)
                self.writes[key] = self.writes.get(key, 0) + 1
                found = self.receiver(node.child_by_field_name("right"))
                if found is not None:
                    self.sources[key] = found
            elif left is not None and left.type in ("list_literal", "array_creation_expression"):
                self.write(scope, left)
        elif kind in ("augmented_assignment_expression", "reference_assignment_expression"):
            self.write(scope, node.child_by_field_name("left"))
        elif kind == "foreach_statement":
            # `foreach ($items as $k => $v)`: everything but the iterated
            # expression and the body is a written variable.
            for child in node.named_children[1:]:
                if child.type not in ("compound_statement", "colon_block") \
                        and not child.type.endswith("_statement"):
                    self.write(scope, child)
        elif kind in ("global_declaration", "function_static_declaration", "catch_clause"):
            self.write(scope, node if kind != "catch_clause"
                       else node.child_by_field_name("name"))

    def property_use(self, node: "tree_sitter.Node", scope: _Scope) -> None:
        if not scope.class_name or _text(node.child_by_field_name("object")) != "$this":
            return
        prop = node.child_by_field_name("name")
        parent = node.parent
        if prop is None or prop.type != "name":
            return
        if parent is not None and parent.type in (
                "assignment_expression", "augmented_assignment_expression",
                "reference_assignment_expression") \
                and parent.child_by_field_name("left") == node:
            return
        self.properties.append(PropertyUse(_text(prop), node.start_point[0] + 1, scope.class_name))

    def drupal_call(self, node: "tree_sitter.Node", scope: _Scope) -> None:
        if not _is_drupal(node.child_by_field_name("scope"), self.namespace, self.uses):
            return
        name = _text(node.child_by_field_name("name"))
        line = node.start_point[0] + 1
        if name == "service":
            literal, raw, present = _first_literal(node.child_by_field_name("arguments"))
            if present:
                self.found_uses.append(ServiceUse("service", literal, raw, line, scope.function,
                                                  scope.class_name, scope.method))
        elif name:
            self.found_uses.append(ServiceUse("shortcut", name, "", line, scope.function,
                                              scope.class_name, scope.method))

    def member_call(self, node: "tree_sitter.Node", scope: _Scope) -> None:
        name = _text(node.child_by_field_name("name"))
        obj = node.child_by_field_name("object")
        line = node.start_point[0] + 1
        if not name:
            return
        if scope.container and scope.method == "create" and name == "get" \
                and _variable(obj) == scope.container:
            literal, raw, present = _first_literal(node.child_by_field_name("arguments"))
            if present:
                self.found_uses.append(ServiceUse("create", literal, raw, line, scope.function,
                                                  scope.class_name, scope.method))
            return
        found = self.receiver(obj)
        if found is not None:
            self.calls.append(ServiceCall(name, line, scope.function, scope.class_name,
                                          scope.method, *found))
            return
        variable = _variable(obj)
        if variable and variable != "this":
            self.local_calls.append(((scope.frame, variable), name, line, scope))

    def finish(self) -> tuple[list[ServiceUse], list[ServiceCall], list[PropertyUse]]:
        for key, name, line, scope in self.local_calls:
            found = self.sources.get(key)
            if found is not None and self.writes.get(key) == 1:
                self.calls.append(ServiceCall(name, line, scope.function, scope.class_name,
                                              scope.method, *found, local=key[1]))
        self.calls.sort(key=lambda c: c.line)
        return self.found_uses, self.calls, self.properties


def _service_sites(root: "tree_sitter.Node",
                   ) -> tuple[list[ServiceUse], list[ServiceCall], list[PropertyUse]]:
    sites = _Sites()
    _walk_scoped(root, _Scope(), sites.visit)
    return sites.finish()


def read_service_sites(path: Path, source: bytes | None = None,
                       ) -> tuple[list[ServiceUse], list[ServiceCall]]:
    """Every service use (`\\Drupal::service()`, any `\\Drupal::name()` --
    the caller keeps the shortcuts it knows --, `$container->get()` in
    `create()`) and every method call on a receiver naming a service: one of
    those, `$this->p`, or a local assigned exactly once in its function from
    one of them (spec §7.4). A chained call past the first is not one.
    Never raises: bad input yields nothing."""
    try:
        parsed = _parse(path, source)
        if parsed is None:
            return [], []
        return _service_sites(parsed[1])[:2]
    except Exception:
        return [], []


@dataclass(frozen=True)
class FileSemantics:
    """`read_class_semantics` plus `read_service_sites`, from one parse."""
    facts: list[ClassFacts]
    annotations: list[tuple[str, Annotation]]
    attributed: list[AttributedClass]
    service_uses: list[ServiceUse]
    service_calls: list[ServiceCall]
    #: every `$this->p` read or called (not assigned), for `via: injected`
    property_uses: list[PropertyUse] = field(default_factory=list)


def read_file_semantics(path: Path) -> FileSemantics:
    """What the per-file extractor reads (P4 spec §4), from one parse. Never raises."""
    try:
        parsed = _parse(path)
    except Exception:
        parsed = None
    if parsed is None:
        return FileSemantics([], [], [], [], [], [])
    facts, annotations, attributed = _class_semantics(parsed[1], Path(path).as_posix())
    root = parsed[1]
    try:
        uses, calls, properties = _service_sites(root)
    except Exception:
        uses, calls, properties = [], [], []
    return FileSemantics(facts, annotations, attributed, uses, calls, properties)
