"""A changed plugin registry re-extracts the files it affects (P2a Task 6, spec §5.5).

Core's AST cache is keyed by a file's content, so a family file whose own bytes
did not change keeps the nodes an older registry made -- unless the registry
change forces it to miss the cache, and (on an incremental run) pulls it into
the batch.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from graphify.drupal.discovery import affected_files, build_registry, type_id
from graphify.drupal.yaml_common import plugin_id
from tests.test_drupal_discovery import YAML_MANAGER, _module, _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401 (reused fixture)

BAR_SERVICES = "services:\n  plugin.manager.bar:\n    class: Drupal\\bar\\BarManager\n"
FOO_BAR = "one:\n  class: Drupal\\foo\\One\ntwo:\n  class: Drupal\\foo\\Two\n"
FOO_BAZ = "three:\n  class: Drupal\\foo\\Three\n"

MANAGER = "web/modules/custom/bar/src/BarManager.php"
BAR_SERVICES_FILE = "web/modules/custom/bar/bar.services.yml"
FOO_BAR_FILE = "web/modules/custom/foo/foo.bar.yml"
FOO_BAZ_FILE = "web/modules/custom/foo/foo.baz.yml"


def _bar_site(root: Path) -> Path:
    return _site(root, {
        **_module("bar", BAR_SERVICES, {"src/BarManager.php": YAML_MANAGER}),
        **_module("foo", "services: {}\n"),
        FOO_BAR_FILE: FOO_BAR,
        FOO_BAZ_FILE: FOO_BAZ,
    })


def _rename_family(root: Path) -> None:
    manager = root / MANAGER
    manager.write_text(
        manager.read_text(encoding="utf-8").replace("'bar'", "'baz'"), encoding="utf-8")


def _run_cli(root: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only"],
        capture_output=True, text=True, cwd=root,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))


def _nodes(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


def _plugins_from(graph: dict, rel: str) -> dict[str, dict]:
    return {
        n["id"]: n for n in graph["nodes"]
        if n.get("type") == "drupal_plugin" and str(n.get("source_file", "")).endswith(rel)
    }


def _edge_set(graph: dict) -> set[tuple]:
    links = graph.get("links", graph.get("edges", []))
    return {json.dumps(e, sort_keys=True) for e in links}


def _node_set(graph: dict) -> set[str]:
    return {json.dumps(n, sort_keys=True) for n in graph["nodes"]}


# -- affected_files ------------------------------------------------------------


def test_a_renamed_yaml_name_affects_both_families_and_the_manager(tmp_path):
    root = _bar_site(tmp_path)
    before = build_registry(root)
    _rename_family(root)
    after = build_registry(root)

    assert affected_files(before, after) == {
        (root / FOO_BAR_FILE).as_posix(),
        (root / FOO_BAZ_FILE).as_posix(),
        (root / MANAGER).as_posix(),
    }


def test_identical_registries_affect_nothing(tmp_path):
    root = _bar_site(tmp_path)
    assert affected_files(build_registry(root), build_registry(root)) == set()


def test_no_previous_registry_affects_nothing(tmp_path):
    root = _bar_site(tmp_path)
    assert affected_files(None, build_registry(root)) == set()


def test_a_changed_manager_service_affects_the_class_file(tmp_path):
    """The class file is untouched, but the type it carries is renamed."""
    root = _bar_site(tmp_path)
    before = build_registry(root)
    (root / BAR_SERVICES_FILE).write_text(
        BAR_SERVICES.replace("plugin.manager.bar", "plugin.manager.bar2"), encoding="utf-8")
    after = build_registry(root)

    assert (root / MANAGER).as_posix() in affected_files(before, after)
    assert (root / FOO_BAR_FILE).as_posix() in affected_files(before, after)


# -- the seam --------------------------------------------------------------------


def test_prepare_run_stores_the_force_miss_set(tmp_path, _isolated_discovery_state):
    import os

    from graphify.drupal import discovery

    root = _bar_site(tmp_path)
    discovery.prepare_run(root)
    assert discovery.force_miss() == frozenset()

    _rename_family(root)
    discovery.prepare_run(root)
    assert (root / FOO_BAR_FILE).as_posix() in discovery.force_miss()

    # A spawned worker has no in-process state: it reads the set from the file.
    discovery.set_current(None)
    assert (root / FOO_BAR_FILE).as_posix() in discovery.force_miss()

    # Undo what `prepare_run` set, as `_isolated_discovery_state` will at teardown.
    os.environ.pop(discovery.ENV_VAR, None)
    assert discovery.force_miss() == frozenset()


def test_load_cached_misses_a_forced_file(tmp_path, _isolated_discovery_state):
    """Core's extract module binds `load_cached` at import, so the wrapper must
    be what `graphify.extract` reads, not only `graphify.cache`'s attribute."""
    from graphify import cache
    from graphify.drupal import discovery, register

    register.install()
    import graphify.extract as extract

    root = _bar_site(tmp_path)
    discovery.prepare_run(root)
    target = root / FOO_BAR_FILE
    other = root / "web/modules/custom/foo/foo.info.yml"
    cache.save_cached(target, {"nodes": [{"id": "x"}], "edges": []}, root, cache_root=tmp_path)
    cache.save_cached(other, {"nodes": [{"id": "y"}], "edges": []}, root, cache_root=tmp_path)
    assert extract.load_cached(target, root, cache_root=tmp_path) is not None

    _rename_family(root)
    discovery.prepare_run(root)
    assert extract.load_cached(target, root, cache_root=tmp_path) is None
    assert cache.load_cached(target, root, cache_root=tmp_path) is None
    # A relative path is resolved against root before it is compared.
    assert extract.load_cached(Path(FOO_BAR_FILE), root, cache_root=tmp_path) is None
    # A file the change does not affect still hits.
    assert extract.load_cached(other, root, cache_root=tmp_path) is not None


def test_patch_cache_fails_loudly_when_load_cached_disappears(monkeypatch):
    import pytest

    from graphify import cache
    from graphify.drupal.register import DrupalSeamError, _patch_cache

    monkeypatch.delattr(cache, "load_cached", raising=True)
    with pytest.raises(DrupalSeamError, match=r"graphify\.cache\.load_cached"):
        _patch_cache(cache)


# -- through the real CLI --------------------------------------------------------


def test_renaming_a_family_moves_its_plugins(tmp_path):
    root = _bar_site(tmp_path)
    first = _run_cli(root)
    assert set(_plugins_from(first, FOO_BAR_FILE)) == {plugin_id("bar", "one"), plugin_id("bar", "two")}
    assert _plugins_from(first, FOO_BAZ_FILE) == {}

    _rename_family(root)
    second = _run_cli(root)
    assert _plugins_from(second, FOO_BAR_FILE) == {}
    assert set(_plugins_from(second, FOO_BAZ_FILE)) == {plugin_id("bar", "three")}
    assert _nodes(second)[type_id("bar")]["yaml_name"] == "baz"


def test_an_unchanged_rerun_is_identical(tmp_path):
    root = _bar_site(tmp_path)
    first = _run_cli(root)
    second = _run_cli(root)
    assert _node_set(second) == _node_set(first)
    assert _edge_set(second) == _edge_set(first)


def test_a_renamed_manager_service_renames_the_type(tmp_path):
    root = _bar_site(tmp_path)
    first = _run_cli(root)
    assert type_id("bar") in _nodes(first)

    (root / BAR_SERVICES_FILE).write_text(
        BAR_SERVICES.replace("plugin.manager.bar", "plugin.manager.bar2"), encoding="utf-8")
    second = _run_cli(root)
    nodes = _nodes(second)
    assert nodes[type_id("bar2")]["type"] == "drupal_plugin_type"
    assert type_id("bar") not in nodes
    assert set(_plugins_from(second, FOO_BAR_FILE)) == {plugin_id("bar2", "one"), plugin_id("bar2", "two")}


def test_only_files_already_in_the_graph_are_pulled_in(tmp_path, _isolated_discovery_state):
    """A forced file with no nodes in the context has nothing stale to replace --
    core re-queues a zero-node file itself, and a file the scan excluded must not
    enter the graph through the widening."""
    from graphify.drupal import discovery
    from graphify.drupal.register import _registry_widening

    root = _bar_site(tmp_path)
    discovery.prepare_run(root)
    _rename_family(root)
    discovery.prepare_run(root)

    context = [{"id": "a", "source_file": FOO_BAR_FILE}]
    assert _registry_widening([root / MANAGER], context, root) == [(root / FOO_BAR_FILE).resolve()]
    assert _registry_widening([root / FOO_BAR_FILE], context, root) == []
