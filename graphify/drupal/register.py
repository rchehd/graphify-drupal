"""The single point where graphify.drupal touches graphify core.

Nothing in core is edited. `install()` places a finder on `sys.meta_path` that
wraps the loader for `graphify.detect` and `graphify.extract`, applying the patch
immediately after each module finishes executing — and patches either directly if
it is already in `sys.modules`.

The hook exists rather than an eager `import graphify.extract` because
`graphify/__init__.py` is deliberately lazy: importing it costs 1 ms, importing
`graphify.extract` costs 809 ms, and `graphify install` must keep working before
heavy dependencies are present.

A side effect worth naming: a spawned `ProcessPoolExecutor` worker unpickles
`graphify.extract._extract_single_file`, which imports `graphify.extract`, which
imports the parent package first — so the hook is installed in the worker before
`extract` executes.
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


class DrupalSeamError(RuntimeError):
    """A graphify core structure the seam depends on is missing or changed."""


def _patch_detect(detect: ModuleType) -> None:
    from graphify.drupal.paths import is_drupal_info_yaml

    for attr in ("classify_file", "FileType"):
        if not hasattr(detect, attr):
            raise DrupalSeamError(
                f"graphify.detect.{attr} is missing — graphify core changed shape; "
                "graphify/drupal/register.py must be updated"
            )
    original = detect.classify_file
    if getattr(original, "_drupal_patched", False):
        return

    def classify_file(path: Path):
        if is_drupal_info_yaml(path):
            return detect.FileType.CODE
        return original(path)

    classify_file._drupal_patched = True
    classify_file.__wrapped__ = original
    detect.classify_file = classify_file


def _patch_extract(extract: ModuleType) -> None:
    from graphify.drupal.paths import is_drupal_info_yaml
    from graphify.drupal.yaml_extract import extract_drupal_info

    if not hasattr(extract, "_get_extractor"):
        raise DrupalSeamError(
            "graphify.extract._get_extractor is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )
    # Not mutated in P0, but asserted as a canary: upstream plans to route
    # dispatch through the public registry, and that change must fail here
    # rather than silently produce a Drupal-free graph.
    if not isinstance(getattr(extract, "_DISPATCH", None), dict):
        raise DrupalSeamError(
            "graphify.extract._DISPATCH is missing or no longer a dict — dispatch "
            "was restructured upstream; graphify/drupal/register.py must be updated"
        )
    original = extract._get_extractor
    if getattr(original, "_drupal_patched", False):
        return

    def _get_extractor(path: Path):
        if is_drupal_info_yaml(path):
            return extract_drupal_info
        return original(path)

    _get_extractor._drupal_patched = True
    _get_extractor.__wrapped__ = original
    extract._get_extractor = _get_extractor


_PATCHERS = {
    "graphify.detect": _patch_detect,
    "graphify.extract": _patch_extract,
}


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, inner, fullname: str) -> None:
        self._inner = inner
        self._fullname = fullname

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._inner.exec_module(module)
        _PATCHERS[self._fullname](module)


class _DrupalFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname not in _PATCHERS:
            return None
        # Step aside so find_spec resolves through the real finders.
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader, fullname)
        return spec


def install() -> None:
    """Arrange for core to be patched, without importing it."""
    if not any(isinstance(f, _DrupalFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _DrupalFinder())
    for name, patch in _PATCHERS.items():
        module = sys.modules.get(name)
        if module is not None:
            patch(module)
