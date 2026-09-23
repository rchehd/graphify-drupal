"""The plugin registry wired into the pipeline: built at the start of every
`detect()`, persisted next to the rest of graphify's output, and visible to a
spawned extraction worker through `current_registry()`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal.discovery import ENV_VAR
from graphify.drupal.register import DrupalSeamError, _patch_detect, install
from tests.test_drupal_discovery import D11_MANAGER, FOO_SERVICES, _module, _site


@pytest.fixture
def _isolated_discovery_state(monkeypatch):
    """Undo everything `prepare_run` touches directly (env var, in-process
    registry, the env-file cache) so later tests in the suite are unaffected.

    `prepare_run` writes `os.environ[ENV_VAR]` itself rather than through
    `monkeypatch`, so `monkeypatch.delenv`'s own teardown does not see it;
    the saved value is restored by hand.
    """
    had = ENV_VAR in os.environ
    saved = os.environ.get(ENV_VAR)
    monkeypatch.delenv(ENV_VAR, raising=False)
    yield
    from graphify.drupal import discovery

    discovery.set_current(None, None)
    discovery._env_cache_path = None
    discovery._env_cache_mtime = None
    discovery._env_cache_registry = None
    if had:
        os.environ[ENV_VAR] = saved
    else:
        os.environ.pop(ENV_VAR, None)


def _drupal_site(tmp_path: Path) -> Path:
    return _site(tmp_path, _module("foo", FOO_SERVICES, {"src/FooManager.php": D11_MANAGER}))


def test_detect_writes_the_registry_and_sets_the_env_var(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect

    root = _drupal_site(tmp_path)
    detect.detect(root)

    registry_path = root / "graphify-out" / "drupal-discovery.json"
    assert registry_path.is_file()
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "foo" in data["types"]
    assert os.environ[ENV_VAR] == str(registry_path.absolute())


def test_a_second_run_sees_the_first_as_previous(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.discovery import current_registry, previous_registry

    root = _drupal_site(tmp_path)
    detect.detect(root)
    first = current_registry()
    assert first is not None

    detect.detect(root)
    assert previous_registry() == first


def test_a_worker_process_reads_the_registry_from_the_env_var(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect

    root = _drupal_site(tmp_path)
    detect.detect(root)

    env = {**os.environ}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import graphify\n"
         "from graphify.drupal.discovery import current_registry\n"
         "print(sorted(current_registry().types))\n"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "['foo']"


def test_an_absolute_graphify_out_moves_the_file(tmp_path, monkeypatch, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    import graphify.paths as paths

    shared_out = tmp_path / "shared-out"
    monkeypatch.setattr(paths, "GRAPHIFY_OUT", str(shared_out))
    root = _drupal_site(tmp_path / "site")
    detect.detect(root)

    registry_path = shared_out / "drupal-discovery.json"
    assert registry_path.is_file()
    assert not (root / "graphify-out" / "drupal-discovery.json").exists()
    assert os.environ[ENV_VAR] == str(registry_path.absolute())


def test_patch_detect_fails_loudly_when_detect_disappears(monkeypatch):
    """An upstream rename of `detect()` must crash, not silently skip the
    registry build for the whole run."""
    install()
    import graphify.detect as detect

    monkeypatch.delattr(detect, "detect", raising=True)
    with pytest.raises(DrupalSeamError, match=r"graphify\.detect\.detect"):
        _patch_detect(detect)


def _walk_tracker(monkeypatch) -> list[str]:
    visited: list[str] = []
    real_walk = os.walk

    def tracking_walk(top, *args, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
            visited.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", tracking_walk)
    return visited


def test_a_graphifyignored_module_is_never_walked_and_defines_no_type(
        tmp_path, monkeypatch, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.discovery import current_registry

    root = _drupal_site(tmp_path)
    (root / "web/modules/custom/foo").rename(root / "web/modules/ignored_foo")
    (root / ".graphifyignore").write_text("web/modules/ignored_foo/\n", encoding="utf-8")
    visited = _walk_tracker(monkeypatch)
    detect.detect(root)

    assert "foo" not in current_registry().types
    assert "foo" not in current_registry().extensions
    assert not any("ignored_foo" in Path(v).parts for v in visited)


CORE_BLOCK_MANAGER = r"""<?php
namespace Drupal\Core\Block;
use Drupal\Core\Plugin\DefaultPluginManager;
class BlockManager extends DefaultPluginManager {}
"""


def test_a_gitignored_core_still_defines_its_types(tmp_path, _isolated_discovery_state):
    """A composer-managed site gitignores web/core and contrib, which define
    almost every plugin type. The registry is knowledge about the site, not
    graph content: it does not honour .gitignore (the graph still does)."""
    install()
    import graphify.detect as detect
    from graphify.drupal.discovery import current_registry

    root = _drupal_site(tmp_path)
    (root / "web/core/core.services.yml").write_text(
        "services:\n  plugin.manager.block:\n    class: Drupal\\Core\\Block\\BlockManager\n",
        encoding="utf-8")
    block = root / "web/core/lib/Drupal/Core/Block/BlockManager.php"
    block.parent.mkdir(parents=True)
    block.write_text(CORE_BLOCK_MANAGER, encoding="utf-8")
    (root / ".gitignore").write_text("/web/core\n/web/modules/custom\n", encoding="utf-8")

    result = detect.detect(root)

    assert {"block", "foo"} <= set(current_registry().types)
    # The graph's own scope is unchanged: detect still leaves the ignored trees out.
    scanned = [f for files in result["files"].values() for f in files]
    assert not any("/web/core/" in f or "/modules/custom/" in f for f in scanned)


def test_an_excluded_module_is_left_out_of_the_registry(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.discovery import current_registry

    root = _drupal_site(tmp_path)
    detect.detect(root)
    assert "foo" in current_registry().types

    detect.detect(root, extra_excludes=["web/modules/custom/foo"])
    assert "foo" not in current_registry().types
    assert not any("/modules/custom/foo/" in p for p in current_registry().root_yaml)


def test_a_non_drupal_tree_builds_no_registry_and_never_walks(
        tmp_path, monkeypatch, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal import discovery

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    def no_walk(*args, **kwargs):
        raise AssertionError("graphify.drupal walked a non-Drupal tree")

    monkeypatch.setattr(discovery, "_walk", no_walk)
    detect.detect(tmp_path)

    assert discovery.current_registry() is None
    assert not (tmp_path / "graphify-out" / "drupal-discovery.json").exists()
    assert ENV_VAR not in os.environ


def test_a_single_module_checkout_still_gets_its_type(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.discovery import current_registry
    from tests.test_drupal_discovery import D11_MANAGER, FOO_SERVICES

    (tmp_path / "src").mkdir()
    (tmp_path / "foo.info.yml").write_text("name: foo\ntype: module\n", encoding="utf-8")
    (tmp_path / "foo.services.yml").write_text(FOO_SERVICES, encoding="utf-8")
    (tmp_path / "src" / "FooManager.php").write_text(D11_MANAGER, encoding="utf-8")
    detect.detect(tmp_path)

    registry = current_registry()
    assert registry is not None
    assert "foo" in registry.types
    assert ("no_drupal_core", "") in {(u["reason"], u["class"]) for u in registry.unresolved}
