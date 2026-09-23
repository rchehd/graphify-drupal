"""A short hash of this package's source, used to namespace core's AST cache.

Core names its AST cache directory after graphify's version, so an entry is
reused for as long as a file's content and graphify's version are unchanged.
This package's extractors change without either moving; without the hash, an
upgraded fork keeps serving whatever an older extractor produced.

Hashing the source rather than a hand-bumped constant means the namespace moves
on every change, including the ones nobody remembers to announce.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _hash(package: Path) -> str:
    digest = hashlib.sha1()
    for source in sorted(package.glob("*.py")):
        digest.update(source.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:10]


def drupal_fingerprint(package: Path | None = None) -> str:
    """Stable for unchanged source; different after any `*.py` in it changes."""
    if package is None:
        return _hash(_PACKAGE)
    # A caller-supplied directory is uncached: tests rewrite it in place.
    return _hash.__wrapped__(package)
