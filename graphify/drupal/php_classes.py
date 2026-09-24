"""Read PHP declarations with tree-sitter.

``read_php_class`` is what P2a uses to find Drupal plugin managers and read
the facts their constructor and ``getDiscovery()`` method carry: what they
extend and implement, what they pass to ``parent::__construct()``, what
discovery objects they build, and what ``alterInfo()`` they register.

``read_php_attributes`` and ``read_php_functions`` are P2b's: the attributes
on classes and methods (``#[Hook(...)]``, names resolved, argument source
text kept verbatim) and a file's top-level functions (hook stubs, procedural
hook implementations).
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


def _parse(path: Path) -> "tuple[bytes, tree_sitter.Node] | None":
    """`(source, root node)` of a PHP file, or None when it is missing,
    unreadable, oversized or unparsable."""
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
        state: dict = {"namespace": "", "uses": {}, "found": []}
        _find_class(parsed[1], state)
        out: list[AttributedClass] = []
        for class_node, namespace, uses in state["found"]:
            if class_node.type != "class_declaration":
                continue
            name = _text(class_node.child_by_field_name("name"))
            if not name:
                continue
            methods: list[PhpMethod] = []
            body = class_node.child_by_field_name("body")
            for member in body.named_children if body is not None else ():
                if member.type != "method_declaration":
                    continue
                method_name = _text(member.child_by_field_name("name"))
                if method_name:
                    methods.append(PhpMethod(method_name, member.start_point[0] + 1,
                                             _attributes(member, namespace, uses)))
            out.append(AttributedClass(
                name=name,
                fqcn=resolve_name(name, namespace, uses),
                line=class_node.start_point[0] + 1,
                attributes=_attributes(class_node, namespace, uses),
                methods=tuple(methods),
            ))
        return out
    except Exception:
        return []


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
                args.append(AttrArg(_text(arg_name), _text(value), string))
            found.append(PhpAttribute(resolve_name(_text(name_node), namespace, uses),
                                      attr.start_point[0] + 1, tuple(args)))
    return tuple(found)


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


def _collect_calls(
    node: "tree_sitter.Node", names: "frozenset[str]",
    function: str, class_name: str, method: str, out: list[PhpCall],
) -> None:
    # Closures and arrow functions are not matched below, so they fall through
    # to the last branch and their calls stay attributed to the enclosing
    # named function or method (spec §5.3).
    for child in node.named_children:
        if child.type == "function_definition":
            name = _text(child.child_by_field_name("name"))
            body = child.child_by_field_name("body")
            if body is not None:
                _collect_calls(body, names, name, "", "", out)
            continue
        if child.type == "class_declaration":
            name = _text(child.child_by_field_name("name"))
            body = child.child_by_field_name("body")
            if body is not None:
                _collect_calls(body, names, "", name, "", out)
            continue
        if child.type == "method_declaration":
            name = _text(child.child_by_field_name("name"))
            body = child.child_by_field_name("body")
            if body is not None:
                _collect_calls(body, names, "", class_name, name, out)
            continue
        if child.type in _MEMBER_CALLS:
            name_node = child.child_by_field_name("name")
            call_name = _text(name_node)
            if call_name in names:
                args_node = child.child_by_field_name("arguments")
                out.append(PhpCall(call_name, child.start_point[0] + 1,
                                   _build_call_args(args_node), function, class_name, method,
                                   _text(child.child_by_field_name("object"))))
            _collect_calls(child, names, function, class_name, method, out)
            continue
        _collect_calls(child, names, function, class_name, method, out)


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
