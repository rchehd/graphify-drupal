"""settings*.php — the `$config[…]` assignments that override configuration.

Read per line with a pattern, not parsed as PHP: conditions around an assignment
are not evaluated, so every edge is AMBIGUOUS (spec §5.4). The right-hand side
is never read; only the config name and the key path are kept.

Registered by composing core's PHP handler with this one (plan decision 1), so
`settings.php` keeps its PHP nodes and is cached and re-read like any file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graphify.drupal.yaml_common import config_id, edge, node, settings_id

_ASSIGNMENT = re.compile(
    r"""^\s*\$config\[\s*['"](?P<name>[^'"]+)['"]\s*\](?P<keys>(?:\s*\[\s*['"][^'"]+['"]\s*\])*)\s*=(?!=)"""
)
_KEY = re.compile(r"""\[\s*['"]([^'"]+)['"]\s*\]""")
_MAX_BYTES = 1024 * 1024


def is_settings_php(path: Path) -> bool:
    return (path.suffix == ".php" and path.name.startswith("settings")
            and path.parent.parent.name == "sites")


def extract_drupal_settings(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return {"nodes": [], "edges": []}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": []}

    found: dict[str, tuple[int, set[str]]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        keys = ".".join(_KEY.findall(match.group("keys")))
        first, seen = found.setdefault(match.group("name"), (number, set()))
        if keys:
            seen.add(keys)
    if not found:
        return {"nodes": [], "edges": []}

    sid = settings_id(f"{path.parent.name}/{path.name}")
    nodes = [node(sid, f"{path.parent.name}/{path.name}", type="drupal_settings",
                  layer="config", path=path, line=1, realm="custom")]
    edges = [
        edge(sid, config_id(name), "overrides_config", path=path, line=line,
             confidence="AMBIGUOUS", override_source="settings_php",
             keys=sorted(keys), target_name=name)
        for name, (line, keys) in found.items()
    ]
    return {"nodes": nodes, "edges": edges}
