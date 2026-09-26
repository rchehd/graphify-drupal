"""The single point where graphify.drupal touches graphify core.

Nothing in core is edited. `install()` places a finder on `sys.meta_path` that
wraps the loader for `graphify.build`, `graphify.cache`, `graphify.cli`, `graphify.dedup`, `graphify.detect`,
`graphify.extract`, `graphify.report` and `graphify.watch`, applying the patch
immediately after each module finishes executing — and patches any of them
directly if it is already in `sys.modules`.

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


#: [(run, env value), artifact path] for `_is_container_artifact`.
_artifact_for_run: list = []


def _is_container_artifact(path) -> bool:
    """`path` is the container artifact (P3 spec S6.1) of the current Drupal
    run's root. False while no run is current: outside a Drupal build the
    artifact's location is unknown (and nothing about it matters)."""
    import os

    from graphify.drupal.container import ENV_ARTIFACT, artifact_path
    from graphify.drupal.discovery import current_run

    run = current_run()
    if run is None:
        return False
    try:
        # Resolved once per run (`prepare_run` makes a new tuple) and env value:
        # `artifact_path` reads `.graphifyrc`.
        key = (run, os.environ.get(ENV_ARTIFACT))
        if _artifact_for_run and _artifact_for_run[0][0] is run and _artifact_for_run[0][1] == key[1]:
            artifact = _artifact_for_run[1]
        else:
            artifact = artifact_path(run[0])
            _artifact_for_run[:] = [key, artifact]
        path = Path(path)
        # The name first: this runs for every file `detect()` classifies.
        if path.name != artifact.name:
            return False
        return path.absolute() == artifact.absolute() or path.resolve() == artifact.resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _patch_detect(detect: ModuleType) -> None:
    from graphify.drupal.boundary import boundary_dir
    from graphify.drupal.discovery import current_registry, walking_registry
    from graphify.drupal.families import is_drupal_file
    from graphify.drupal.hooks import PROCEDURAL_SUFFIXES

    # `ignored_predicate` is not wrapped but called by the registry walk
    # (discovery._scan_predicate); `GRAPHIFY_OUT` is what `out_dir` resolves.
    # `_MANIFEST_PATH` is how `detect_incremental_` tells core's default
    # manifest (no out dir to infer) from an `--out` one.
    for attr in ("classify_file", "FileType", "_is_graphable_source", "detect",
                 "detect_incremental", "ignored_predicate", "_is_noise_dir", "_MANIFEST_PATH"):
        if not hasattr(detect, attr):
            raise DrupalSeamError(
                f"graphify.detect.{attr} is missing — graphify core changed shape; "
                "graphify/drupal/register.py must be updated"
            )
    import graphify.paths as core_paths

    if not isinstance(getattr(core_paths, "GRAPHIFY_OUT", None), str):
        raise DrupalSeamError(
            "graphify.paths.GRAPHIFY_OUT is missing or not a string — graphify core "
            "changed shape; graphify/drupal/register.py must be updated"
        )

    # Procedural PHP (P2b spec §5.2): the suffixes join core's own set, in
    # place, so everything reading it -- `watch._CODE_EXTENSIONS` is this very
    # object -- sees them. Classification stays gated below.
    code_extensions = getattr(detect, "CODE_EXTENSIONS", None)
    if not isinstance(code_extensions, set):
        raise DrupalSeamError(
            "graphify.detect.CODE_EXTENSIONS is missing or no longer a set — graphify core "
            "changed shape; graphify/drupal/register.py must be updated"
        )
    new_to_core = frozenset(s for s in PROCEDURAL_SUFFIXES if s not in code_extensions)
    code_extensions.update(PROCEDURAL_SUFFIXES)

    def _classify(original):
        def classify_file(path: Path):
            if is_drupal_file(path):
                return detect.FileType.CODE
            # The container artifact is `.json`, which core extracts as code
            # (`json_config`): above 1 MiB that fails and is re-extracted on
            # every build, below it an artifact-only change is a re-extracted
            # file. It is the overlay's input, never graph content. The wrapped
            # `detect()` runs `prepare_run` (which sets `current_run()`)
            # before core classifies a single file.
            if _is_container_artifact(path):
                return None
            # A `.module` that is no extension's procedural file (a library's)
            # stays what it was before the suffix joined CODE_EXTENSIONS:
            # unclassified. `.inc` was core's already (a Pascal include), so a
            # non-procedural one keeps core's own answer.
            if path.suffix.lower() in new_to_core:
                return None
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

    def _noise(original):
        # Core calls `_is_noise_dir` by bare name from detect()'s prune loop and
        # from `ignored_predicate`, both resolved through this module global at
        # call time, so wrapping it prunes a boundary tree (core, contrib,
        # vendor install dir) whether it is committed or gitignored (spec §4.2).
        #
        # Only in a Drupal run (a registry is current): P0's path rules would
        # otherwise prune `*/core/lib/*` or `*/modules/contrib/*` in any
        # repository. Never while the registry walks: it must read the boundary.
        # `graphify watch` builds its ignore predicate this way too, so boundary
        # events are ignored -- once its first rebuild has made a registry current.
        def _is_noise_dir(part, parent=None):
            if original(part, parent):
                return True
            if parent is None or walking_registry() or current_registry() is None:
                return False
            return boundary_dir(Path(parent) / part) is not None
        return _is_noise_dir

    def _detect(original):
        # The registry must exist before core scans a single file — extraction
        # workers spawned later in the same run read it through
        # `current_registry()`, and `prepare_run` is what makes that possible.
        def detect_(root, *, follow_symlinks=None, google_workspace=None,
                    extra_excludes=None, cache_root=None, gitignore=True):
            if cache_root is None:
                # Called by core's `detect_incremental`, which passes none: the
                # location its manifest implies, as a full `--out` run gets it.
                cache_root = incremental_cache_root[-1] if incremental_cache_root else None
            from graphify.drupal.boundary import clear_caches as clear_boundary_caches
            from graphify.drupal.discovery import out_dir, prepare_run
            from graphify.drupal.inventory import (
                build_inventory, remove_inventory, set_current_inventory, write_inventory,
            )

            # composer.json/lock and `.graphifyrc` may have changed since the
            # last run in this process (`graphify watch`).
            clear_boundary_caches()
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
            # as prepare_run's own "not a Drupal tree" outcome -- and any
            # inventory a PRIOR Drupal run left on disk in this same out dir
            # must go too, or report.generate's file fallback would keep
            # reporting that site's coverage for a tree that is no longer
            # Drupal (or no longer scanned here) at all.
            #
            # Neither step may fail detect() (spec §5.8): an unwritable out dir
            # (read-only checkout) keeps the inventory in process only.
            registry = current_registry()
            if registry is not None:
                detected: set[str] = set()
                for paths in (result.get("files") or {}).values():
                    detected.update(paths)
                detected.update(result.get("unclassified") or [])
                try:
                    inventory = build_inventory(registry, detected, root_path,
                                                out_dir(root_path, cache_root))
                except Exception:
                    inventory = None
                set_current_inventory(inventory)
                if inventory is not None:
                    try:
                        write_inventory(inventory, out_dir(root_path, cache_root))
                    except OSError:
                        pass
            else:
                set_current_inventory(None)
                remove_inventory(out_dir(root_path, cache_root))
            return result
        detect_.__name__ = detect_.__qualname__ = "detect"
        detect_.__doc__ = original.__doc__
        return detect_

    # The `cache_root` core's nested `detect()` should have had, for the
    # duration of a wrapped `detect_incremental` (a stack: re-entrant).
    incremental_cache_root: list[Path | None] = []

    def _cache_root_for(out: Path) -> Path | None:
        """The `cache_root` whose `<cache_root>/<GRAPHIFY_OUT>` is `out` --
        what a full `extract --out` run passes to `detect()`, so its word-count
        cache (`cache/stat-index.json`) and office sidecars land in the out dir
        rather than in the scanned tree. None when `GRAPHIFY_OUT` is absolute
        (core already ignores `cache_root` for it) or `out` does not end in it."""
        graphify_out = Path(core_paths.GRAPHIFY_OUT)
        if graphify_out.is_absolute():
            return None
        parts = graphify_out.parts
        if not parts or out.parts[-len(parts):] != parts:
            return None
        return Path(*out.parts[:-len(parts)])

    def _incremental(original):
        # Core's detect_incremental calls detect() without a cache_root, so on
        # `extract --out` the nested prepare_run would look for the previous
        # registry (and write both files) under the scanned tree. Its
        # `manifest_path` is `<out>/manifest.json` there: that directory is
        # the out dir. The default manifest path carries no such information
        # (it is relative to the working directory), so it changes nothing.
        def detect_incremental_(root, *args, **kwargs):
            from graphify.drupal.discovery import using_out_dir

            manifest = args[0] if args else kwargs.get("manifest_path")
            default = detect._MANIFEST_PATH
            if manifest is None or str(manifest) == str(default):
                return _promote_forced(original(root, *args, **kwargs), root)
            out = Path(manifest).resolve().parent
            incremental_cache_root.append(_cache_root_for(out))
            try:
                with using_out_dir(out):
                    return _promote_forced(original(root, *args, **kwargs), root)
            finally:
                incremental_cache_root.pop()
        detect_incremental_.__name__ = detect_incremental_.__qualname__ = "detect_incremental"
        detect_incremental_.__doc__ = original.__doc__
        return detect_incremental_

    def _promote_forced(result, root):
        # The widening in the `extract` wrapper only runs when core extracts
        # something: a run whose only change is the registry or the boundary
        # (`drupal.include` toggled, final review I3) has no changed file, so
        # core never calls `extract()` and the forced files stay cached. An
        # unchanged code file this run must re-extract is reported as new.
        from graphify.drupal.discovery import force_miss

        forced = force_miss()
        if not forced or not isinstance(result, dict):
            return result
        unchanged = (result.get("unchanged_files") or {}).get("code")
        new = (result.get("new_files") or {}).get("code")
        if not isinstance(unchanged, list) or not isinstance(new, list):
            return result
        wanted = {Path(p).resolve().as_posix() for p in forced}
        base = Path(root)
        promoted = [f for f in unchanged
                    if (Path(f) if Path(f).is_absolute() else base / f).resolve().as_posix() in wanted]
        if promoted:
            moved = set(promoted)
            result["unchanged_files"]["code"] = [f for f in unchanged if f not in moved]
            new.extend(promoted)
            if isinstance(result.get("new_total"), int):
                result["new_total"] += len(promoted)
        return result

    _wrap(detect, "classify_file", _classify)
    _wrap(detect, "_is_graphable_source", _graphable)
    _wrap(detect, "_is_noise_dir", _noise)
    _wrap(detect, "detect", _detect)
    _wrap(detect, "detect_incremental", _incremental)


def _patch_extract(extract: ModuleType) -> None:
    from graphify.drupal.boundary import clear_caches as clear_boundary_caches
    from graphify.drupal.config_stores import clear_caches
    from graphify.drupal.discovery import clear_force_miss
    from graphify.drupal.discovery import extract_plugin_types, is_manager_class_file
    from graphify.drupal.families import drupal_extractor, is_api_php
    from graphify.drupal.hooks import extract_hook_declarations, extract_hook_invocations
    from graphify.drupal.merge import collapse_drupal_duplicates, collision_group, compose_handlers
    from graphify.drupal.php_semantics import extract_php_semantics, is_semantics_file
    from graphify.drupal.resolvers import drop_pending, scanning
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
    # Procedural files (P2b spec §5.2) dispatch to core's PHP handler, through
    # `families.drupal_extractor` -> `hooks.extract_php_with_hooks`.
    if not callable(extract._DISPATCH.get(".php")):
        raise DrupalSeamError(
            "graphify.extract._DISPATCH has no '.php' handler — graphify core changed "
            "shape; graphify/drupal/register.py must be updated"
        )

    # `hooks.py` computes `hook_implemented_by` targets and `invokes_hook`
    # sources with core's own PHP id helpers and emits an edge only when the
    # id is one core emitted: a renamed helper would drop every such edge
    # silently (inside the extractors' never-raise guard), so assert them here.
    import graphify.extractors.base as extractor_base

    for attr in ("_file_stem", "_make_id"):
        if not callable(getattr(extractor_base, attr, None)):
            raise DrupalSeamError(
                f"graphify.extractors.base.{attr} is missing — graphify core changed "
                "shape; graphify/drupal/register.py must be updated"
            )

    def _path_only(extra):
        return lambda p, _core_result: extra(p)

    settings_extra = _path_only(extract_drupal_settings)
    plugin_types_extra = _path_only(extract_plugin_types)
    hook_declarations_extra = _path_only(extract_hook_declarations)

    def _dispatch(original):
        def _get_extractor(path: Path):
            handler = drupal_extractor(path)
            if handler is not None:
                return handler
            base = original(path)
            extras = []
            if is_settings_php(path):
                # Keep core's PHP nodes; add the `$config[…]` overrides.
                extras.append(settings_extra)
            if path.suffix == ".php" and is_manager_class_file(path):
                # Keep core's PHP nodes; add the type's node and edges (spec §5.4).
                extras.append(plugin_types_extra)
            if is_api_php(path):
                # Keep core's PHP nodes; add the hook nodes and edges (spec §5.3).
                # Only an in-graph file reaches this dispatch at all -- a
                # boundary api.php never gets scanned (Task 2 prunes it).
                extras.append(hook_declarations_extra)
            if path.suffix == ".php":
                # Any other in-graph PHP file (a service, a controller, a
                # manager, `settings.php`, `*.api.php`) gets its invocation
                # sites too (spec §5.3); the extractor's own text pre-check
                # skips a file with none, so dispatch reads nothing. Procedural
                # files and `src/Hook/**/*.php` already carry this through
                # `hooks.extract_php_with_hooks` (`drupal_extractor`, above).
                extras.append(extract_hook_invocations)
            if is_semantics_file(path):
                # A class under an extension's `src/`: its plugins, entity
                # types (P4 spec §5). `src/Hook/**` gets this through
                # `hooks.extract_php_with_hooks` (`drupal_extractor`, above).
                extras.append(extract_php_semantics)
            # A `#[Hook]` outside `<extension>/src/Hook/` is no implementation
            # (Drupal never collects it): the inventory lists it as `misplaced`.
            return compose_handlers(base, extras) if extras else base
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
            clear_boundary_caches()
            # What core anchors relative `source_file`s to: `root`, else
            # `cache_root` -- `graphify watch` passes only the latter.
            root = kwargs.get("root")
            anchor = root if root is not None else cache_root
            context = kwargs.get("resolution_context_nodes")
            if context:
                # An incremental run. Pull in the unchanged files a changed
                # plugin registry affects (spec §5.5), then each collision group
                # (see merge.collision_group), and keep every pulled file out of
                # the read-only context, as core does for every file it extracts.
                given = list(paths)
                widened = collision_group(
                    given + _registry_widening(given, context, anchor), context, anchor)
                if len(widened) > len(given):
                    _strip_context(kwargs, widened[len(given):], anchor)
                    paths = widened
            # Boundary stubs name their paths relative to the scan root; the
            # resolver runs inside this call but is not handed the root.
            with scanning(anchor):
                result = original(paths, cache_root, **kwargs)
            # What the resolver could not bind never reaches the graph.
            drop_pending(result)
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
    from graphify.drupal.hooks import PROCEDURAL_SUFFIXES
    from graphify.drupal.resolvers import resolve_missing_targets

    if any(r.name == "drupal" for r in resolver_registry.registered_resolvers()):
        return
    resolver_registry.register(
        resolver_registry.LanguageResolver(
            name="drupal",
            # ".php" so a manager-only incremental run (its `defines_plugin_type`
            # / `plugin_manager_for` edges, no .yml in the batch) still resolves;
            # the procedural suffixes for a batch of only `.module` files (its
            # `implements_hook` edges, P2b §5.3).
            suffixes=frozenset({".yml", ".php", *PROCEDURAL_SUFFIXES}),
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
    for attr in ("generate", "load_learning_for_report"):
        if not callable(getattr(report, attr, None)):
            raise DrupalSeamError(
                f"graphify.report.{attr} is missing — graphify core changed shape; "
                "graphify/drupal/register.py must be updated"
            )

    # The directory of the graph a caller is about to report on. `generate`
    # is given only the scanned root, but every caller builds its `learning=`
    # argument with `load_learning_for_report(<out>/graph.json)` just before,
    # which names the out dir: `cluster-only <site> --graph <out>/.../graph.json`
    # after `extract --out` keeps the inventory there, not under <site>.
    noted: list[Path] = []

    def _learning(original):
        def load_learning_for_report(graph_path, *args, **kwargs):
            try:
                noted[:] = [Path(graph_path).parent]
            except TypeError:
                noted[:] = []
            return original(graph_path, *args, **kwargs)
        load_learning_for_report.__name__ = load_learning_for_report.__qualname__ = (
            "load_learning_for_report")
        load_learning_for_report.__doc__ = original.__doc__
        return load_learning_for_report

    def _generate(original):
        def generate_(*args, **kwargs):
            graph_out = noted[0] if noted else None
            noted[:] = []
            text = original(*args, **kwargs)
            from graphify.drupal.discovery import current_run, out_dir
            from graphify.drupal.inventory import (
                current_inventory, graph_counts, load_inventory, render_section, write_inventory,
            )

            root = kwargs.get("root")
            if root is None and len(args) > 8:
                root = args[8]
            inventory, home = current_inventory(), None
            if inventory is None and graph_out is not None:
                inventory, home = load_inventory(graph_out), graph_out
            if inventory is None and root is not None:
                # No `cache_root` here: `out_dir(root)` is the default out dir
                # under `root` itself.
                home = out_dir(Path(root))
                inventory = load_inventory(home)
            if inventory is None:
                return text
            # What P4 put in the graph being reported (spec §10), kept beside
            # the inventory for the next reader too; a write failure keeps it
            # in the report only.
            graph = args[0] if args else kwargs.get("G")
            inventory = {**inventory, "graph": graph_counts(graph)}
            if home is None and graph_out is not None:
                home = graph_out
            if home is None:
                run = current_run()
                home = run[1] if run is not None else None
            if home is not None:
                try:
                    write_inventory(inventory, home)
                except OSError:
                    pass
            return text + "\n" + render_section(inventory)
        generate_.__name__ = generate_.__qualname__ = "generate"
        generate_.__doc__ = original.__doc__
        return generate_

    _wrap(report, "load_learning_for_report", _learning)
    _wrap(report, "generate", _generate)


def _patch_watch(watch: ModuleType) -> None:
    """Make `graphify watch` rebuild (not just flag) when Drupal YAML changes.

    `watch._WATCHED_EXTENSIONS` already includes `.yml`, so a Drupal YAML edit
    is observed and debounced; the gap is that `.yml` is not in core's
    `_CODE_EXTENSIONS`, so `_batch_triggers_rebuild` sends it to the
    LLM-needed `needs_update` flag instead of the no-LLM AST rebuild. A
    Drupal family file's extractor is a fixed-schema parser, not an LLM, so
    it belongs on the rebuild side like any other code change.
    """
    from graphify.drupal.families import is_drupal_file

    # `is_drupal_file` knows a learned family (`foo.bar.yml` read by a plugin
    # manager) only once a detect() in this process has built the registry:
    # until watch's first rebuild runs one, such a file is not recognised here.
    for attr in ("_batch_triggers_rebuild", "_has_non_code"):
        if not hasattr(watch, attr):
            raise DrupalSeamError(
                f"graphify.watch.{attr} is missing — graphify core changed shape; "
                "graphify/drupal/register.py must be updated"
            )
    # Procedural PHP (P2b spec §5.2) reaches watch through `CODE_EXTENSIONS`,
    # which `_patch_detect` extended in place: watch's alias must be that
    # object, not a copy. Its watched set is a union built at import, so it
    # gains the suffixes explicitly (a no-op when detect was patched first).
    import graphify.detect as detect
    from graphify.drupal.hooks import PROCEDURAL_SUFFIXES

    if getattr(watch, "_CODE_EXTENSIONS", None) is not detect.CODE_EXTENSIONS:
        raise DrupalSeamError(
            "graphify.watch._CODE_EXTENSIONS is no longer graphify.detect.CODE_EXTENSIONS "
            "— graphify core changed shape; graphify/drupal/register.py must be updated"
        )
    watched = getattr(watch, "_WATCHED_EXTENSIONS", None)
    if not isinstance(watched, set):
        raise DrupalSeamError(
            "graphify.watch._WATCHED_EXTENSIONS is missing or no longer a set — graphify "
            "core changed shape; graphify/drupal/register.py must be updated"
        )
    watched.update(PROCEDURAL_SUFFIXES)

    def _triggers(original):
        def _batch_triggers_rebuild(batch):
            return original(batch) or any(
                (p.exists() and is_drupal_file(p)) or _is_container_artifact(p) for p in batch
            )
        return _batch_triggers_rebuild

    def _non_code(original):
        def _has_non_code(changed_paths):
            # Drupal-recognised files (and the container artifact) are excluded
            # before core's check: a batch of only Drupal YAML must not still
            # raise the LLM flag.
            return original([p for p in changed_paths
                             if not is_drupal_file(p) and not _is_container_artifact(p)])
        return _has_non_code

    _wrap(watch, "_batch_triggers_rebuild", _triggers)
    _wrap(watch, "_has_non_code", _non_code)

    # `update` and `watch` (`_rebuild_code`, clustered and `--no-cluster`)
    # refuse a smaller graph unless every lost node belongs to a rebuilt
    # source; an overlay-made node's `source_file` is the artifact (or a hook
    # implementation's PHP file), which is none. The overlay's own nodes are
    # left out of both counts in a Drupal run (P3 final review, finding 1):
    # the overlay is re-laid on every build, so they are never a silent loss.
    if not callable(getattr(watch, "_check_shrink", None)):
        raise DrupalSeamError(
            "graphify.watch._check_shrink is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )

    def _shrink(original):
        def _check_shrink(force, existing_data, new_data, *args, **kwargs):
            from graphify.drupal.discovery import current_run

            if current_run() is not None:
                existing_data = _without_overlay_nodes(existing_data)
                new_data = _without_overlay_nodes(new_data)
            return original(force, existing_data, new_data, *args, **kwargs)
        return _check_shrink

    _wrap(watch, "_check_shrink", _shrink)


def _without_overlay_nodes(data):
    """`data` (a graph.json dict) with the nodes the container overlay made left out."""
    from graphify.drupal.container_overlay import overlay_made

    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, list) or not any(overlay_made(n) for n in nodes):
        return data
    return {**data, "nodes": [n for n in nodes if not overlay_made(n)]}


def _co_declarers(graph_path: Path, root: Path) -> dict[str, set[str]]:
    """Each path in some node's `declared_in` in `graph_path` -> the other paths
    in those same lists, all as root-relative POSIX paths (P1b stores them so;
    an absolute one, from a root-less extract, is made relative when inside
    `root`). Raises on an unreadable or malformed file."""
    import json

    data = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in data.get("nodes", []):
        declared = node.get("declared_in") if isinstance(node, dict) else None
        if not isinstance(declared, list):
            continue
        paths = {_root_relative(e, root) for e in declared if isinstance(e, str) and e}
        for path in paths:
            found.setdefault(path, set()).update(paths - {path})
    return found


def _root_relative(path: str, root: Path) -> str:
    """`path` (absolute, or relative to `root`) as a root-relative POSIX path,
    NFC-normalised as core compares `source_file`; unchanged when outside."""
    from graphify.paths import nfc

    p = Path(path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(root)
        except (ValueError, OSError, RuntimeError):
            return nfc(p.as_posix())
    return nfc(p.as_posix())


def _patch_cli(cli: ModuleType) -> None:
    """Stop core's zero-node heal re-queuing shadowed configuration copies (P2b §6.1),
    and route `graphify drupal …` to the Drupal commands (P3 §10).

    `_zero_node_stamped_code_sources` re-queues every stamped code file that
    owns no node in graph.json. A configuration copy P1b's collapse gave to
    another copy (a module's `config/install` default shadowed by `config/sync`)
    owns none by design, yet its path is in the survivor's `declared_in`: it
    was extracted, so the stamp is honest -- as long as another copy in that
    list still exists. `watch` never calls the heal.
    """
    for attr in ("_zero_node_stamped_code_sources", "dispatch_command"):
        if not callable(getattr(cli, attr, None)):
            raise DrupalSeamError(
                f"graphify.cli.{attr} is missing — graphify core "
                "changed shape; graphify/drupal/register.py must be updated"
            )

    def _heal(original):
        # `cli` calls it by bare name, so the module attribute is what it gets.
        def _zero_node_stamped_code_sources(graph_path, scan_root, unchanged_code):
            healed = original(graph_path, scan_root, unchanged_code)
            if not healed:
                return healed
            try:
                root = Path(scan_root).resolve()
                co_declarers = _co_declarers(graph_path, root)
            except Exception:
                return healed

            def shadowed(f) -> bool:
                # Only while another declaring copy is still on disk: when the
                # winner was deleted, core prunes its node and this copy must
                # be re-extracted to take it over -- core's heal does that.
                others = co_declarers.get(_root_relative(str(f), root), ())
                return any((root / other).exists() for other in others)

            return [f for f in healed if not shadowed(f)]
        return _zero_node_stamped_code_sources

    _wrap(cli, "_zero_node_stamped_code_sources", _heal)
    # `__main__` binds `dispatch_command` by `from graphify.cli import`, which
    # runs after this patch (the loader patches the module as it finishes
    # executing), so the name it binds is the wrapper.
    _wrap(cli, "dispatch_command", _dispatch_wrapper)


def _dispatch_wrapper(original):
    """`graphify drupal container …` goes to `container.main`; `graphify
    drupal <anything else>` is a usage error (exit 2); every other command
    goes to core unchanged (P3 spec S10)."""
    def dispatch_command(cmd):
        if cmd != "drupal":
            return original(cmd)
        if len(sys.argv) > 2 and sys.argv[2] == "container":
            from graphify.drupal import container

            sys.exit(container.main(sys.argv[2:]))
        from graphify.drupal.container import _USAGE

        print(_USAGE, file=sys.stderr)
        sys.exit(2)
    return dispatch_command


def _patch_dedup(dedup: ModuleType) -> None:
    """Key Drupal nodes by id in core's entity dedup, as core keys code symbols.

    `deduplicate_entities` merges `concept` nodes across files by label, exactly
    and fuzzily; `_is_code` is what exempts a node, and core asks it by bare
    name. A Drupal id is the entity itself (`make_id` of its kind and name) and
    the same id from several files is already one node, so a label match is
    never evidence of sameness: boundary stubs are `concept` (vocabulary §1.3),
    and without this the core module `toolbar` merged into the hook `toolbar`,
    and route, menu-link and local-task stubs sharing a label into each other.

    `_defines_id` picks the survivor when one id reaches the build twice
    (graph.json's copy and a re-extracted one). A Drupal id encodes no file,
    so core's answer is False for every Drupal node and the tie falls to label
    and path: a boundary stub carried over from graph.json could beat the node
    a now-declaring file emits (`hook_new_hook` added to an in-graph
    `*.api.php` kept the stale `missing` stub). The declared node defines the
    id; a stub (`boundary: true`) only references it.

    A node the container overlay created (`_overlay: true`, or its
    `source_file` still the `_overlay_file` it was created with) is not a
    declaration either (P3 spec S7.2): carried over from graph.json, it
    would otherwise beat a `*.routing.yml` or `*.services.yml` that now
    declares the same id on the basename tie-break (`drupal-container.json`
    sorts first), and the static node's own attributes (`route_path`,
    `source_file`) would be lost. The next overlay puts its facts back.
    """
    for attr in ("_is_code", "_defines_id"):
        if not callable(getattr(dedup, attr, None)):
            raise DrupalSeamError(
                f"graphify.dedup.{attr} is missing — graphify core changed shape; "
                "graphify/drupal/register.py must be updated"
            )

    def _is_drupal(node) -> bool:
        return str(node.get("type", "")).startswith("drupal_")

    def _overlay_made(node) -> bool:
        from graphify.drupal.container_overlay import same_source

        own = node.get("_overlay_file")
        return bool(node.get("_overlay")) or (bool(own) and same_source(node.get("source_file"), own))

    def _key_by_id(original):
        def _is_code(node):
            return original(node) or _is_drupal(node)
        return _is_code

    def _declared_defines(original):
        def _defines_id(node):
            if _is_drupal(node):
                return (bool(node.get("source_file")) and not node.get("boundary")
                        and not _overlay_made(node))
            return original(node)
        return _defines_id

    _wrap(dedup, "_is_code", _key_by_id)
    _wrap(dedup, "_defines_id", _declared_defines)


def _patch_build(build: ModuleType) -> None:
    """Lay the container artifact over every graph core builds (P3 spec S7.1).

    `build_from_json` is where every build ends -- `build`, `build_merge`
    (`extract`, incremental `extract`) and `watch`/`update`'s rebuild -- and
    each calls it by bare name, so the module attribute is what they get.
    The overlay runs only while a Drupal run is current
    (`discovery.current_run()`, set by the `detect()` a build starts with):
    `query`, `path` and the other readers load graph.json through the same
    function in a process that ran no `detect()`, and are untouched. A
    re-entry guard keeps a build inside the overlay from overlaying twice.
    """
    if not callable(getattr(build, "build_from_json", None)):
        raise DrupalSeamError(
            "graphify.build.build_from_json is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )

    def _overlaid(original):
        def build_from_json(*args, **kwargs):
            G = original(*args, **kwargs)
            if _overlaying:
                return G
            from graphify.drupal.container_overlay import run_for_build

            _overlaying.append(True)
            try:
                run_for_build(G)
            finally:
                _overlaying.pop()
            return G
        build_from_json.__name__ = build_from_json.__qualname__ = "build_from_json"
        build_from_json.__doc__ = original.__doc__
        return build_from_json

    _wrap(build, "build_from_json", _overlaid)

    # The baseline of an incremental merge (P3 final review, finding 1):
    # `build_merge` (and the raw `merge_raw_extraction`) load graph.json
    # through `_load_existing_graph`, by bare name. What the last overlay
    # made is undone there, in a Drupal run, so the #479 guard of
    # `extract --no-dedup` never reads an item the new artifact does not
    # make again as a node "neither re-extracted nor pruned". The build that
    # follows lays the current artifact (`build_from_json`, above).
    if not callable(getattr(build, "_load_existing_graph", None)):
        raise DrupalSeamError(
            "graphify.build._load_existing_graph is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )

    def _baseline(original):
        def _load_existing_graph(graph_path):
            loaded = original(graph_path)
            from graphify.drupal.discovery import current_run

            if loaded is None or current_run() is None:
                return loaded
            from graphify.drupal.container_overlay import undo_records

            nodes, edges, *rest = loaded
            undo_records(nodes, edges)
            return (nodes, edges, *rest)
        _load_existing_graph.__name__ = _load_existing_graph.__qualname__ = "_load_existing_graph"
        _load_existing_graph.__doc__ = original.__doc__
        return _load_existing_graph

    _wrap(build, "_load_existing_graph", _baseline)

    # The raw write (P3 final review, finding 2): `extract --no-cluster`
    # (cli) and `update`/`watch --no-cluster` (`watch._rebuild_code`) write
    # the merged extraction without `build_from_json`. Both pass it through
    # `dedupe_nodes` and then `dedupe_edges` just before, and nothing else in
    # core calls either: the node list the first returns is remembered, and
    # the second lays the overlay on both (`lay_on_records`).
    for attr in ("dedupe_nodes", "dedupe_edges"):
        if not callable(getattr(build, attr, None)):
            raise DrupalSeamError(
                f"graphify.build.{attr} is missing — graphify core changed shape; "
                "graphify/drupal/register.py must be updated"
            )

    def _raw_nodes(original):
        def dedupe_nodes(nodes):
            result = original(nodes)
            from graphify.drupal.discovery import current_run

            _raw_pending[:] = [result] if current_run() is not None and not _overlaying else []
            return result
        dedupe_nodes.__name__ = dedupe_nodes.__qualname__ = "dedupe_nodes"
        dedupe_nodes.__doc__ = original.__doc__
        return dedupe_nodes

    def _raw_edges(original):
        def dedupe_edges(edges):
            result = original(edges)
            if not _raw_pending:
                return result
            nodes = _raw_pending.pop()
            from graphify.drupal.discovery import current_run

            if current_run() is None or _overlaying:
                return result
            from graphify.drupal.container_overlay import lay_on_records

            try:
                return lay_on_records(nodes, result, build.build_from_json)
            except Exception:  # noqa: BLE001 -- the overlay never breaks a build
                return result
        dedupe_edges.__name__ = dedupe_edges.__qualname__ = "dedupe_edges"
        dedupe_edges.__doc__ = original.__doc__
        return dedupe_edges

    _wrap(build, "dedupe_nodes", _raw_nodes)
    _wrap(build, "dedupe_edges", _raw_edges)

    # `build_merge` prunes deleted and excluded files' nodes after the build
    # (`build_from_json`, where the overlay ran): the divergence log and the
    # report block are recomputed for the graph it returns (P3 final review,
    # finding 5), so neither names a node the written graph does not have.
    if not callable(getattr(build, "build_merge", None)):
        raise DrupalSeamError(
            "graphify.build.build_merge is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )

    def _merged(original):
        def build_merge(*args, **kwargs):
            from graphify.drupal.container_overlay import _last_build, refresh_after_prune

            _last_build.clear()
            G = original(*args, **kwargs)
            refresh_after_prune(G)
            return G
        build_merge.__name__ = build_merge.__qualname__ = "build_merge"
        build_merge.__doc__ = original.__doc__
        return build_merge

    _wrap(build, "build_merge", _merged)


#: Non-empty while `run_for_build` runs (the build seam's re-entry guard).
_overlaying: list[bool] = []

#: The node list the last `dedupe_nodes` returned in a Drupal run, until the
#: `dedupe_edges` that follows it on a raw write takes it.
_raw_pending: list[list] = []


_PATCHERS = {
    "graphify.build": _patch_build,
    "graphify.cache": _patch_cache,
    "graphify.cli": _patch_cli,
    "graphify.dedup": _patch_dedup,
    "graphify.detect": _patch_detect,
    "graphify.extract": _patch_extract,
    "graphify.report": _patch_report,
    "graphify.watch": _patch_watch,
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
