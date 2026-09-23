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
