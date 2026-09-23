"""One Drupal id, one node — however many files declare it.

Core salts an id apart when two source files declare it: two `helper()`
functions in two files are two things. Drupal's ids are not like that. The
container, the routing table and the tag vocabulary are global, so a tag used by
forty `*.services.yml` files is one tag, and a service that a test module
redeclares is the same service overridden. Salting either strands every edge
written against the bare id, and core drops those edges silently — on the
reference corpus that was 57 tags becoming over 300 nodes.

The seam runs this immediately before core's pass, so core never sees a Drupal
collision and every other producer's nodes reach it untouched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: Set by `yaml_common.node` on everything the Drupal extractors emit.
_ORIGIN = "static_yaml"

#: Nodes without a store rank sort after every ranked one and among themselves by path.
_NO_RANK = 99


def _relative(source_file: str, root: Path | None) -> str:
    """`declared_in` reaches graph.json as-is, so it must not carry the checkout path."""
    if root is None:
        return source_file
    try:
        return Path(source_file).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return source_file


def collapse_drupal_duplicates(nodes: list[dict[str, Any]], root: Path | None = None) -> None:
    """Keep one node per Drupal id, in place.

    The survivor is the copy with the lowest `_rank`, then the lowest `source_file`; attributes it lacks are taken from the other copies in that order. When there was more than one, every
    declaring file is kept in `declared_in`, relative to `root`.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        if node.get("_origin") == _ORIGIN and isinstance(node.get("id"), str):
            groups.setdefault(node["id"], []).append(node)

    drop: set[int] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        # Configuration ranks its stores (sync > split > recipe > optional >
        # install, P1b spec §6.1); every other type has no rank and keeps the
        # path order.
        group.sort(key=lambda n: (n.get("_rank", _NO_RANK), str(n.get("source_file", ""))))
        survivor = group[0]
        for other in group[1:]:
            for key, value in other.items():
                if key not in survivor and key != "_rank":
                    survivor[key] = value
        survivor["declared_in"] = sorted(
            {_relative(str(n.get("source_file", "")), root) for n in group}
        )
        drop.update(id(n) for n in group[1:])

    if drop:
        nodes[:] = [n for n in nodes if id(n) not in drop]
    for node in nodes:
        if node.get("_origin") == _ORIGIN:
            node.pop("_rank", None)
