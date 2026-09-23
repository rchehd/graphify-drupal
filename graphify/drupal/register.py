"""The single point where graphify.drupal touches graphify core.

Nothing in core is edited. `install()` places a finder on `sys.meta_path` that
wraps the loader for `graphify.cache`, `graphify.detect`, `graphify.extract` and
`graphify.report`, applying the patch immediately after each module finishes
executing — and patches either directly if it is already in `sys.modules`.

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

    for attr in ("classify_file", "FileType", "_is_graphable_source", "detect"):
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

    def _detect(original):
        # The registry must exist before core scans a single file — extraction
        # workers spawned later in the same run read it through
        # `current_registry()`, and `prepare_run` is what makes that possible.
        def detect_(root, *, follow_symlinks=None, google_workspace=None,
                    extra_excludes=None, cache_root=None, gitignore=True):
            from graphify.drupal.discovery import current_registry, out_dir, prepare_run
            from graphify.drupal.inventory import build_inventory, set_current_inventory, write_inventory

            root_path = Path(root)
            prepare_run(root_path, cache_root,
                        extra_excludes=extra_excludes, gitignore=gitignore)
            result = original(
                root,
                follow_symlinks=follow_symlinks,
                google_workspace=google_workspace,
                extra_excludes=extra_excludes,
                cache_root=cache_root,
                gitignore=gitignore,
            )
            # Built from the same file list detect() just returned (every
            # "files" category plus "unclassified"), after the scan so every
            # category is known (spec §5.7). No registry -> no inventory, same
            # as prepare_run's own "not a Drupal tree" outcome.
            registry = current_registry()
            if registry is not None:
                detected: set[str] = set()
                for paths in (result.get("files") or {}).values():
                    detected.update(paths)
                detected.update(result.get("unclassified") or [])
                inventory = build_inventory(registry, detected, root_path)
                write_inventory(inventory, out_dir(root_path, cache_root))
                set_current_inventory(inventory)
            return result
        detect_.__name__ = detect_.__qualname__ = "detect"
        detect_.__doc__ = original.__doc__
        return detect_

    _wrap(detect, "classify_file", _classify)
    _wrap(detect, "_is_graphable_source", _graphable)
    _wrap(detect, "detect", _detect)


def _patch_extract(extract: ModuleType) -> None:
    from graphify.drupal.config_stores import clear_caches
    from graphify.drupal.discovery import clear_force_miss
    from graphify.drupal.discovery import extract_plugin_types, is_manager_class_file
    from graphify.drupal.families import drupal_extractor
    from graphify.drupal.merge import collapse_drupal_duplicates, collision_group
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
    def _compose(base, extra):
        """A handler that runs core's `base` first, then appends `extra(p)`'s
        nodes and edges -- core's PHP nodes are kept either way, and a file
        that is both cases at once cannot occur (settings.php is never a
        manager class file), so one helper serves both compositions."""
        def handler(p: Path, _base=base, _extra=extra):
            result = dict(_base(p)) if _base else {"nodes": [], "edges": []}
            ours = _extra(p)
            result["nodes"] = list(result.get("nodes", [])) + ours["nodes"]
            result["edges"] = list(result.get("edges", [])) + ours["edges"]
            return result
        return handler

    def _dispatch(original):
        def _get_extractor(path: Path):
            handler = drupal_extractor(path)
            if handler is not None:
                return handler
            base = original(path)
            if is_settings_php(path):
                # Keep core's PHP nodes; add the `$config[…]` overrides.
                return _compose(base, extract_drupal_settings)
            if path.suffix == ".php" and is_manager_class_file(path):
                # Keep core's PHP nodes; add the type's node and edges (spec §5.4).
                return _compose(base, extract_plugin_types)
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
            collapse_drupal_duplicates(nodes, Path(root) if root is not None else None, edges)
            return original(nodes, edges, raw_calls, root)
        return _disambiguate_colliding_node_ids

    _wrap(extract, "_disambiguate_colliding_node_ids", _collapse)

    if not callable(getattr(extract, "extract", None)):
        raise DrupalSeamError(
            "graphify.extract.extract is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )

    def _run(original):
        # The CLI, `graphify watch` and the hooks all do
        # `from graphify.extract import extract` inside the calling function,
        # so they read the module attribute at call time and get this wrapper.
        def extract_(paths, cache_root=None, **kwargs):
            clear_caches()
            context = kwargs.get("resolution_context_nodes")
            if context:
                # An incremental run. Pull in the unchanged files a changed
                # plugin registry affects (spec §5.5), then each collision group
                # (see merge.collision_group), and keep every pulled file out of
                # the read-only context, as core does for every file it extracts.
                root = kwargs.get("root")
                anchor = root if root is not None else cache_root
                given = list(paths)
                widened = collision_group(
                    given + _registry_widening(given, context, anchor), context, anchor)
                if len(widened) > len(given):
                    _strip_context(kwargs, widened[len(given):], anchor)
                    paths = widened
            result = original(paths, cache_root, **kwargs)
            # Only now are the forced files re-extracted; an exception above
            # leaves the set in place for the next run to carry over.
            clear_force_miss()
            return result
        extract_.__name__ = extract_.__qualname__ = "extract"
        extract_.__doc__ = original.__doc__
        return extract_

    _wrap(extract, "extract", _run)

    # `from .cache import load_cached` binds the name when graphify.extract is
    # imported. It is normally the wrapper already (graphify.cache is patched
    # first), but not when graphify.extract was imported before install().
    if not callable(getattr(extract, "load_cached", None)):
        raise DrupalSeamError(
            "graphify.extract.load_cached is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )
    _wrap(extract, "load_cached", _force_miss_wrapper)
    # Registered here rather than in install() because the registry is imported
    # from graphify.extract's own dependency graph; doing it at this point keeps
    # install() free of any import of core.
    _register_resolvers()


def _absolute(path, base: Path | None) -> Path:
    """`path` made absolute against `base` (else the working directory), resolved."""
    p = Path(path)
    if not p.is_absolute():
        p = (Path(base) if base is not None else Path.cwd()) / p
    return p.resolve()


def _registry_widening(paths: list, context: list[dict], anchor) -> list[Path]:
    """The `force_miss()` files an incremental batch must add (spec §5.5).

    Only files that exist, are not in the batch, and have nodes in the
    read-only context: those nodes are what the changed registry made stale.
    A file with none needs nothing -- core re-queues zero-node files itself.
    """
    from graphify.drupal.discovery import force_miss

    forced = force_miss()
    if not forced:
        return []
    known = {_absolute(f, anchor) for f in {str(n.get("source_file") or "") for n in context} if f}
    given = {_absolute(p, anchor) for p in paths}
    out: list[Path] = []
    for path in sorted(forced):
        candidate = _absolute(path, anchor)
        if candidate in known and candidate not in given and candidate.is_file():
            out.append(candidate)
    return out


def _strip_context(kwargs: dict, pulled: list, anchor) -> None:
    """Drop the pulled files' nodes and edges from the read-only context, in place.

    Context entries carry `source_file` as core wrote it -- relative to the
    anchor, or as given -- so both spellings are matched.
    """
    from graphify.drupal.merge import _relative

    base = Path(anchor) if anchor is not None else None
    names = {_relative(str(p), base) for p in pulled} | {str(p) for p in pulled}
    for key in ("resolution_context_nodes", "resolution_context_edges"):
        entries = kwargs.get(key)
        if entries:
            kwargs[key] = [e for e in entries if str(e.get("source_file")) not in names] or None


def _force_miss_wrapper(original):
    """`load_cached` that misses for this run's `force_miss()` files (spec §5.5).

    The registry is deliberately not folded into `_EXTRACTOR_VERSION`: core
    deletes every other version namespace, so any manager change would then
    re-parse the whole project instead of the files it affects.
    """
    from graphify.drupal.discovery import force_miss

    seen: frozenset[str] | None = None
    resolved: frozenset[str] = frozenset()

    def forced_paths() -> frozenset[str]:
        # Resolved once per set: the wrapper runs for every file in a scan.
        nonlocal seen, resolved
        forced = force_miss()
        if forced is not seen:
            seen, resolved = forced, frozenset(Path(p).resolve().as_posix() for p in forced)
        return resolved

    def load_cached(path, root=Path("."), kind="ast", *args, **kwargs):
        if kind == "ast":
            forced = forced_paths()
            # Core reads a relative `path` against the working directory
            # (cache.file_hash), not against `root`, so this does too.
            if forced and _absolute(path, None).as_posix() in forced:
                return None
        return original(path, root, kind, *args, **kwargs)
    load_cached.__name__ = load_cached.__qualname__ = "load_cached"
    load_cached.__doc__ = original.__doc__
    return load_cached


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
            # ".php" so a manager-only incremental run (its `defines_plugin_type`
            # / `plugin_manager_for` edges, no .yml in the batch) still resolves.
            suffixes=frozenset({".yml", ".php"}),
            resolve=resolve_missing_targets,
        )
    )


def _patch_cache(cache: ModuleType) -> None:
    """Namespace core's AST cache by this package's source as well as graphify's version.

    Core keys an AST entry by graphify's version, a schema number and the file's
    content. A changed Drupal extractor moves none of them, so an unchanged
    `*.services.yml` would keep the nodes an older extractor made. The value is
    read when the cache directory is resolved, so rewriting it here is enough.

    `load_cached` is wrapped as well, so the files a changed plugin registry
    affects miss the cache this run (see `_force_miss_wrapper`).
    """
    from graphify.drupal.fingerprint import drupal_fingerprint

    if not callable(getattr(cache, "load_cached", None)):
        raise DrupalSeamError(
            "graphify.cache.load_cached is missing — the AST cache changed shape "
            "upstream; graphify/drupal/register.py must be updated"
        )
    _wrap(cache, "load_cached", _force_miss_wrapper)

    version = getattr(cache, "_EXTRACTOR_VERSION", None)
    if not isinstance(version, str):
        raise DrupalSeamError(
            "graphify.cache._EXTRACTOR_VERSION is missing or not a string — the AST "
            "cache namespace changed upstream; graphify/drupal/register.py must be updated"
        )
    if "+drupal." in version:
        return
    cache._EXTRACTOR_VERSION = f"{version}+drupal.{drupal_fingerprint()}"


def _patch_report(report: ModuleType) -> None:
    """Append the "Drupal coverage" section to `report.generate`'s output."""
    if not callable(getattr(report, "generate", None)):
        raise DrupalSeamError(
            "graphify.report.generate is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )

    def _generate(original):
        def generate_(*args, **kwargs):
            text = original(*args, **kwargs)
            from graphify.drupal.discovery import out_dir
            from graphify.drupal.inventory import current_inventory, load_inventory, render_section

            root = kwargs.get("root")
            if root is None and len(args) > 8:
                root = args[8]
            inventory = current_inventory()
            if inventory is None and root is not None:
                inventory = load_inventory(out_dir(Path(root)))
            if inventory is None:
                return text
            return text + "\n" + render_section(inventory)
        generate_.__name__ = generate_.__qualname__ = "generate"
        generate_.__doc__ = original.__doc__
        return generate_

    _wrap(report, "generate", _generate)


_PATCHERS = {
    "graphify.cache": _patch_cache,
    "graphify.detect": _patch_detect,
    "graphify.extract": _patch_extract,
    "graphify.report": _patch_report,
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
