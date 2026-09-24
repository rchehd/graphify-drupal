"""Every file this fork changes against upstream must be on the allow-list.

The point is to catch an accidental edit to graphify/extract.py at commit time
rather than three months later during a rebase.
"""
from __future__ import annotations

import subprocess
from fnmatch import fnmatch
from pathlib import Path

import pytest

ALLOWLIST = Path("graphify/drupal/allowlist.txt")


def _patterns() -> list[str]:
    return [
        line.strip()
        for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _changed_against_upstream() -> list[str]:
    # From the merge base, not upstream's tip: commits upstream made after the
    # fork are not this fork's changes. Against the working tree, so an
    # uncommitted edit to core is still caught before it is committed.
    base = subprocess.run(
        ["git", "merge-base", "upstream/v8", "HEAD"],
        capture_output=True, text=True,
    )
    if base.returncode != 0:
        pytest.skip("upstream/v8 not available in this checkout")
    proc = subprocess.run(
        ["git", "diff", base.stdout.strip(), "--name-only"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip("upstream/v8 not available in this checkout")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_allowlist_file_is_readable():
    assert _patterns()


def test_every_change_against_upstream_is_allowed():
    offenders = [
        path for path in _changed_against_upstream()
        if not any(fnmatch(path, pattern) for pattern in _patterns())
    ]
    assert offenders == [], (
        "These files diverge from upstream/v8 but are not on the allow-list in "
        f"{ALLOWLIST}: {offenders}. Either revert them or extend the allow-list "
        "deliberately — each entry widens the surface that a rebase can conflict on."
    )


def test_core_files_are_not_silently_editable():
    assert not any(fnmatch("graphify/extract.py", p) for p in _patterns())
    assert not any(fnmatch("graphify/detect.py", p) for p in _patterns())
    assert not any(fnmatch("graphify/build.py", p) for p in _patterns())
