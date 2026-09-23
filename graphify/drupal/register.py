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


def _wrap(module: ModuleType, name: str, make_wrapper) -> None:
    """Replace `module.name` with a wrapper over it, at most once."""
    original = getattr(module, name)
    if getattr(original, "_drupal_patched", False):
        return
    wrapper = make_wrapper(original)
    wrapper._drupal_patched = True
    wrapper.__wrapped__ = original
    setattr(module, name, wrapper)


def _patch_detect(detect: ModuleType) -> None:
    from graphify.drupal.families import is_drupal_file

    for attr in ("classify_file", "FileType", "_is_graphable_source"):
        if not hasattr(detect, attr):
            raise DrupalSeamError(
                f"graphify.detect.{attr} is missing — graphify core changed shape; "
                "graphify/drupal/register.py must be updated"
            )

    def _classify(original):
        def classify_file(path: Path):
            if is_drupal_file(path):
                return detect.FileType.CODE
            return original(path)
        return classify_file

    def _graphable(original):
        def _is_graphable_source(path: Path):
            # Promoting the file to CODE is not enough on its own. Core's
            # secret screen exempts "genuine programming-language source" via
            # this predicate, and it excludes every data format — `.yml`
            # included — because credentials.yaml is exactly what that screen
            # must catch. A family file or a configuration file (P1b) has
            # a fixed schema and is never a credential store; its values never
            # reach the graph, so it belongs on the exempt side of core's own
            # rule rather than around it.
            #
            # Without this, `token.info.yml` is dropped silently: its stem
            # `token.info` is two words and hits the generic-keyword rule. On a
            # real 1,140-extension tree that was the single casualty while only
            # `*.info.yml` was known — but the `token` module is a dependency of
            # a large share of Drupal sites, so every depends_on_module edge
            # pointing at it would dangle. The exemption tracks the table, so
            # `token.services.yml` is covered the moment that family is
            # registered and not a line sooner.
            if is_drupal_file(path):
                return True
            return original(path)
        return _is_graphable_source

    _wrap(detect, "classify_file", _classify)
    _wrap(detect, "_is_graphable_source", _graphable)


def _patch_extract(extract: ModuleType) -> None:
    from graphify.drupal.families import drupal_extractor
    from graphify.drupal.merge import collapse_drupal_duplicates
    from graphify.drupal.yaml_settings import extract_drupal_settings, is_settings_php

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
    def _dispatch(original):
        def _get_extractor(path: Path):
            handler = drupal_extractor(path)
            if handler is not None:
                return handler
            base = original(path)
            if is_settings_php(path):
                # Keep core's PHP nodes; add the `$config[…]` overrides.
                def settings_handler(p: Path, _base=base):
                    result = dict(_base(p)) if _base else {"nodes": [], "edges": []}
                    ours = extract_drupal_settings(p)
                    result["nodes"] = list(result.get("nodes", [])) + ours["nodes"]
                    result["edges"] = list(result.get("edges", [])) + ours["edges"]
                    return result
                return settings_handler
            return base
        return _get_extractor

    _wrap(extract, "_get_extractor", _dispatch)

    if not callable(getattr(extract, "_disambiguate_colliding_node_ids", None)):
        raise DrupalSeamError(
            "graphify.extract._disambiguate_colliding_node_ids is missing — graphify "
            "core changed shape; graphify/drupal/register.py must be updated"
        )

    def _collapse(original):
        def _disambiguate_colliding_node_ids(nodes, edges, raw_calls, root):
            # Core salts apart an id declared by two files. Drupal ids are
            # global, so collapse ours first; see graphify/drupal/merge.py.
            collapse_drupal_duplicates(nodes, Path(root) if root is not None else None)
            return original(nodes, edges, raw_calls, root)
        return _disambiguate_colliding_node_ids

    _wrap(extract, "_disambiguate_colliding_node_ids", _collapse)
    # Registered here rather than in install() because the registry is imported
    # from graphify.extract's own dependency graph; doing it at this point keeps
    # install() free of any import of core.
    _register_resolvers()


def _register_resolvers() -> None:
    """Register the cross-file pass through graphify's public registry.

    Unlike the two patchers this is a documented extension point, so it widens
    nothing: `resolver_registry.register` exists for exactly this.
    """
    from graphify import resolver_registry
    from graphify.drupal.resolvers import resolve_missing_targets

    if any(r.name == "drupal" for r in resolver_registry.registered_resolvers()):
        return
    resolver_registry.register(
        resolver_registry.LanguageResolver(
            name="drupal",
            suffixes=frozenset({".yml"}),
            resolve=resolve_missing_targets,
        )
    )


def _patch_cache(cache: ModuleType) -> None:
    """Namespace core's AST cache by this package's source as well as graphify's version.

    Core keys an AST entry by graphify's version, a schema number and the file's
    content. A changed Drupal extractor moves none of them, so an unchanged
    `*.services.yml` would keep the nodes an older extractor made. The value is
    read when the cache directory is resolved, so rewriting it here is enough.
    """
    from graphify.drupal.fingerprint import drupal_fingerprint

    version = getattr(cache, "_EXTRACTOR_VERSION", None)
    if not isinstance(version, str):
        raise DrupalSeamError(
            "graphify.cache._EXTRACTOR_VERSION is missing or not a string — the AST "
            "cache namespace changed upstream; graphify/drupal/register.py must be updated"
        )
    if "+drupal." in version:
        return
    cache._EXTRACTOR_VERSION = f"{version}+drupal.{drupal_fingerprint()}"


_PATCHERS = {
    "graphify.cache": _patch_cache,
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
