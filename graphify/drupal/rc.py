"""Shared `.graphifyrc` `key = value` line reader.

Every `drupal.*` rc reader before this file (`container.artifact_path`,
`boundary._included_realms_for`, `paths.load_realm_rules`,
`config_stores._rc_sync_dirs_from`) re-implemented the same `key = value`,
`#`-comment, first-match-wins line format inline. This module factors out
the read of a single key from a single `.graphifyrc` file -- no ancestor
walk: callers that need to look upward (like `boundary.py`) still do that
themselves, one directory at a time, and can reuse `read_rc_value` per
candidate directory.
"""
from __future__ import annotations

from pathlib import Path


def read_rc_value(rc_dir: Path, key: str) -> str | None:
    """The stripped value of the first `key = value` line in
    `<rc_dir>/.graphifyrc`, or `None` when the file is absent, unreadable, or
    has no such line. Blank lines and `#` comments are ignored, as are lines
    with no `=`."""
    rc_path = Path(rc_dir) / ".graphifyrc"
    if not rc_path.is_file():
        return None
    try:
        text = rc_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, val = line.split("=", 1)
        if k.strip() == key:
            return val.strip()
    return None
