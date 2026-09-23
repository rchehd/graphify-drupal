"""The Drupal YAML family table -- one source for classification and dispatch.

`graphify.detect.classify_file` promotes a file to CODE and
`graphify.extract._get_extractor` hands it to a handler. If those two ever
disagree, a file is counted as code and then yields nothing, which core reports
only as a warning. Both read this table, so disagreement is not expressible.

A family is switched on by adding one entry here, together with its handler.
Until a handler exists the suffix is absent, and the file keeps whatever
behaviour core gives it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from graphify.drupal.yaml_access import extract_drupal_permissions, extract_drupal_routing
from graphify.drupal.yaml_assets import extract_drupal_breakpoints, extract_drupal_libraries
from graphify.drupal.yaml_extract import extract_drupal_info
from graphify.drupal.yaml_links import (
    extract_drupal_contextual_links,
    extract_drupal_local_actions,
    extract_drupal_local_tasks,
    extract_drupal_menu_links,
)
from graphify.drupal.yaml_services import extract_drupal_services

FAMILY_EXTRACTORS: dict[str, Callable[[Path], dict]] = {
    ".info.yml": extract_drupal_info,
    ".services.yml": extract_drupal_services,
    ".permissions.yml": extract_drupal_permissions,
    ".routing.yml": extract_drupal_routing,
    ".libraries.yml": extract_drupal_libraries,
    ".breakpoints.yml": extract_drupal_breakpoints,
    ".links.menu.yml": extract_drupal_menu_links,
    ".links.task.yml": extract_drupal_local_tasks,
    ".links.action.yml": extract_drupal_local_actions,
    ".links.contextual.yml": extract_drupal_contextual_links,
}


def _suffixes() -> tuple[str, ...]:
    """Longest first, so `*.links.menu.yml` is never matched as some `*.menu.yml`."""
    return tuple(sorted(FAMILY_EXTRACTORS, key=len, reverse=True))


def _match(path: Path) -> str | None:
    name = path.name
    for suffix in _suffixes():
        # `len(name) > len(suffix)` rejects a bare `.info.yml`: no machine name.
        if name.endswith(suffix) and len(name) > len(suffix):
            return suffix
    return None


def is_drupal_yaml(path: Path) -> bool:
    return _match(path) is not None


def family_extractor(path: Path) -> Callable[[Path], dict] | None:
    suffix = _match(path)
    return FAMILY_EXTRACTORS[suffix] if suffix else None


def extension_owner(path: Path) -> str:
    """Machine name of the extension that owns this file, or ''."""
    suffix = _match(path)
    return path.name[: -len(suffix)] if suffix else ""
