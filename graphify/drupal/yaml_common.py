"""Shared parsing and node/edge construction for the Drupal YAML families.

The loader is the reason this module exists. Drupal service files use Symfony's
custom YAML tags, and `yaml.safe_load` refuses them:

    web/core/core.services.yml                    !tagged_iterator
    web/modules/contrib/modeler_api/...           !service_closure

The first is Drupal core's main service file. Measured on a real 1,140-extension
tree, `safe_load` parses 1,628 services and this loader parses 2,305 -- the
difference is most of core's container. Every family parses through here.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from graphify.drupal.paths import resolve_realm
from graphify.ids import make_id

#: A Drupal YAML file is configuration, not data. Anything past this is not one,
#: and parsing a corpus-supplied file without a cap invites a decompression bomb.
_MAX_YAML_BYTES = 2 * 1024 * 1024


class DrupalYamlLoader(yaml.SafeLoader):
    """SafeLoader that tolerates Symfony's custom service tags."""


def _any_tag(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> Any:
    """Return a custom-tagged node's payload instead of refusing to build it.

    Scoped to `!`-prefixed tags, so structural errors still raise and are
    reported -- core ships a deliberately malformed libraries fixture that must
    keep failing.
    """
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


yaml.add_multi_constructor("!", _any_tag, Loader=DrupalYamlLoader)


def load_drupal_yaml(path: Path) -> tuple[dict | None, str | None]:
    """Parse a Drupal YAML file. Returns (mapping, None) or (None, error)."""
    try:
        if path.stat().st_size > _MAX_YAML_BYTES:
            return None, "yaml too large to index"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"yaml read error: {exc}"
    try:
        data = yaml.load(text, Loader=DrupalYamlLoader)
    except yaml.YAMLError as exc:
        return None, f"yaml parse error: {exc}"
    if not isinstance(data, dict):
        return None, None          # empty or a sequence: nothing to extract
    return data, None


def key_lines(text: str) -> dict[int, dict[str, int]]:
    """Map keys to 1-based line numbers, grouped by indent.

    Indent-scoped on purpose. Entity ids sit at indent 0 for routing,
    permissions, libraries, links and breakpoints, and at indent 2 for services
    (under `services:`) and parameters. A flat map would let a route's `path:`
    property at indent 2 overwrite the line of a route actually named `path`.

    Keys may contain spaces -- a Drupal permission is `administer foo` -- so only
    the colon delimits them. Last occurrence wins, as it does in the parser.
    """
    lines: dict[int, dict[str, int]] = {0: {}, 2: {}}
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.lstrip(" ")
        if not stripped or stripped.startswith(("#", "-")):
            continue
        indent = len(raw) - len(stripped)
        if indent not in lines:
            continue
        key, sep, _rest = stripped.partition(":")
        if not sep:
            continue
        key = key.strip().strip("'\"")
        if key:
            lines[indent][key] = number
    return lines


def node(
    nid: str,
    label: str,
    *,
    type: str,
    layer: str,
    path: Path,
    line: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """A node with every universal attribute filled in the same way."""
    payload: dict[str, Any] = {
        "id": nid,
        "label": label,
        "file_type": "code",
        "type": type,
        "layer": layer,
        "realm": resolve_realm(path),
        "_origin": "static_yaml",
        "source_file": str(path),
        "source_location": f"L{line}",
    }
    payload.update(extra)
    return payload


def edge(
    source: str,
    target: str,
    relation: str,
    *,
    path: Path,
    line: int = 1,
    confidence: str = "EXTRACTED",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": confidence,
        "_origin": "static_yaml",
        "source_file": str(path),
        "source_location": f"L{line}",
    }
    payload.update(extra)
    return payload


def service_id(sid: str) -> str:
    return make_id("drupal", "service", sid)


def route_id(name: str) -> str:
    return make_id("drupal", "route", name)


def permission_id(permission: str) -> str:
    return make_id("drupal", "permission", permission)


def library_id(owner: str, name: str) -> str:
    return make_id("drupal", "library", owner, name)


def tag_id(name: str) -> str:
    return make_id("drupal", "tag", name)


def parameter_id(name: str) -> str:
    return make_id("drupal", "parameter", name)


def menu_id(name: str) -> str:
    return make_id("drupal", "menu", name)


def link_id(kind: str, plugin_id: str) -> str:
    return make_id("drupal", kind, plugin_id)


def breakpoint_id(owner: str, name: str) -> str:
    return make_id("drupal", "breakpoint", owner, name)


def config_id(name: str) -> str:
    return make_id("drupal", "config", name)


#: A type built only from lowercase word characters and `.`, e.g. `system.site`.
#: `make_id` normalises any other type the same way it normalises this one --
#: casefolding, then collapsing every run of non-word characters to `_` -- so a
#: scheme that substitutes characters out of `*`/`+`/`:`/uppercase can still
#: collide with a plain type that already looks like the substitution's output
#: (`views.field.user` and `views_field_user` both normalise to
#: `views_field_user`). No character-substitution scheme built from word
#: characters is injective after that normalisation, so only a plain type keeps
#: the direct id; anything else is disambiguated with a hash of its own name.
_PLAIN_SCHEMA_TYPE = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)*$")


def schema_id(type_: str) -> str:
    if _PLAIN_SCHEMA_TYPE.fullmatch(type_):
        return make_id("drupal", "config_schema", type_)
    digest = hashlib.sha1(type_.encode("utf-8")).hexdigest()[:8]
    return make_id("drupal", "config_schema", type_, digest)


def config_patch_id(split: str, target: str) -> str:
    return make_id("drupal", "config_patch", split, target)


def config_translation_id(language: str, kind: str, split: str, name: str) -> str:
    return make_id("drupal", "config_translation", language, kind, split, name)


def recipe_id(directory: str) -> str:
    return make_id("drupal", "recipe", directory)


def settings_id(site_file: str) -> str:
    return make_id("drupal", "settings", site_file)
