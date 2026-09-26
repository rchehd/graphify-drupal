"""The container artifact's shape, path resolution, and staleness (spec S6)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from graphify.drupal.container import (
    ENV_ARTIFACT,
    RC_ARTIFACT,
    SCHEMA_VERSION,
    SOURCES,
    Artifact,
    ArtifactError,
    artifact_path,
    compute_host_stamp,
    container_sources,
    load_artifact,
    staleness,
    validate,
)
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

_INSTALLER_PATHS = {
    "web/core": ["type:drupal-core"],
    "web/modules/contrib/{$name}": ["type:drupal-module"],
    "web/modules/custom/{$name}": ["type:drupal-custom-module"],
}

_PACKAGES = [
    {"name": "drupal/core", "type": "drupal-core"},
    {"name": "acme/foo", "type": "drupal-custom-module"},
    {"name": "drupal/bar", "type": "drupal-module"},
]


def _touch(root: Path, rel: str, text: str = "x: 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _composer_site(root: Path) -> Path:
    composer_json = {"extra": {"installer-paths": _INSTALLER_PATHS}}
    _touch(root, "composer.json", json.dumps(composer_json))
    _touch(root, "composer.lock", json.dumps({"packages": _PACKAGES, "packages-dev": []}))
    _touch(root, "web/core/lib/Drupal.php", "<?php\n")
    return root


def _foo_module(root: Path) -> Path:
    """A custom module with every kind of source file `container_sources` cares
    about, plus a `src/Util.php` that must NOT be picked up."""
    base = root / "web/modules/custom/foo"
    _touch(root, "web/modules/custom/foo/foo.info.yml", "name: Foo\ntype: module\n")
    _touch(root, "web/modules/custom/foo/foo.services.yml", "services: {}\n")
    _touch(root, "web/modules/custom/foo/foo.routing.yml", "foo.route: {}\n")
    _touch(root, "web/modules/custom/foo/foo.module", "<?php\n")
    _touch(root, "web/modules/custom/foo/src/FooServiceProvider.php", "<?php\nclass FooServiceProvider {}\n")
    _touch(root, "web/modules/custom/foo/src/EventSubscriber/FooSubscriber.php",
           "<?php\nclass FooSubscriber {}\n")
    _touch(root, "web/modules/custom/foo/src/Plugin/Block/FooBlock.php", "<?php\nclass FooBlock {}\n")
    _touch(root, "web/modules/custom/foo/src/Hook/FooHooks.php", "<?php\nclass FooHooks {}\n")
    _touch(root, "web/modules/custom/foo/src/Util.php", "<?php\nclass Util {}\n")
    return base


def _bar_contrib_module(root: Path) -> Path:
    base = root / "web/modules/contrib/bar"
    _touch(root, "web/modules/contrib/bar/bar.info.yml", "name: Bar\ntype: module\n")
    _touch(root, "web/modules/contrib/bar/bar.services.yml", "services: {}\n")
    return base


def _site(tmp_path: Path) -> Path:
    root = _composer_site(tmp_path)
    _foo_module(root)
    _bar_contrib_module(root)
    return root


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_git(root: Path) -> None:
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "test@example.com")
    _run_git(root, "config", "user.name", "Test")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "initial")


# -- container_sources --------------------------------------------------------


def test_container_sources_is_exactly_the_custom_files(tmp_path):
    root = _site(tmp_path)
    sources = container_sources(root)
    rels = sorted(p.relative_to(root).as_posix() for p in sources)
    assert rels == sorted([
        "web/modules/custom/foo/foo.info.yml",
        "web/modules/custom/foo/foo.services.yml",
        "web/modules/custom/foo/foo.routing.yml",
        "web/modules/custom/foo/foo.module",
        "web/modules/custom/foo/src/FooServiceProvider.php",
        "web/modules/custom/foo/src/EventSubscriber/FooSubscriber.php",
        "web/modules/custom/foo/src/Plugin/Block/FooBlock.php",
        "web/modules/custom/foo/src/Hook/FooHooks.php",
    ])
    assert sources == sorted(sources)


def test_container_sources_is_sorted(tmp_path):
    root = _site(tmp_path)
    sources = container_sources(root)
    assert sources == sorted(sources)


def test_container_sources_uses_the_registry_for_files_no_glob_would_find(
    tmp_path, _isolated_discovery_state,
):
    """A manager class and a plain service class sitting directly under
    `src/` (not under `src/Plugin`, `src/Hook`, `src/EventSubscriber`, and
    not a `*ServiceProvider.php`) match none of `container_sources`' glob
    patterns -- the registry is what names them. A contrib service's class
    file must still be excluded."""
    root = _site(tmp_path)
    foo_dir = root / "web/modules/custom/foo"
    bar_dir = root / "web/modules/contrib/bar"

    manager_file = _touch(root, "web/modules/custom/foo/src/FooManager.php",
                          "<?php\nclass FooManager {}\n")
    service_file = _touch(root, "web/modules/custom/foo/src/FooGenericService.php",
                          "<?php\nclass FooGenericService {}\n")
    contrib_service_file = _touch(root, "web/modules/contrib/bar/src/BarService.php",
                                  "<?php\nclass BarService {}\n")

    from graphify.drupal.discovery import PluginType, Registry, set_current

    registry = Registry(
        web_root=(root / "web").as_posix(),
        types={
            "foo_manager": PluginType(
                plugin_type="foo_manager",
                manager_class="Drupal\\foo\\FooManager",
                class_file=manager_file.as_posix(),
                line=1,
                owner="foo",
                registered=True,
            ),
        },
        extensions={"foo": foo_dir.as_posix(), "bar": bar_dir.as_posix()},
        services={
            "foo.generic": ("Drupal\\foo\\FooGenericService", "foo"),
            "bar.service": ("Drupal\\bar\\BarService", "bar"),
        },
    )
    set_current(registry)

    sources = container_sources(root)
    rels = {p.relative_to(root).as_posix() for p in sources}

    assert manager_file.relative_to(root).as_posix() in rels
    assert service_file.relative_to(root).as_posix() in rels
    assert contrib_service_file.relative_to(root).as_posix() not in rels


# -- artifact_path -------------------------------------------------------------


def test_artifact_path_defaults_to_root(tmp_path):
    assert artifact_path(tmp_path) == tmp_path / "drupal-container.json"


def test_artifact_path_rc_wins_over_default(tmp_path):
    _touch(tmp_path, ".graphifyrc", f"{RC_ARTIFACT} = out/container.json\n")
    assert artifact_path(tmp_path) == tmp_path / "out/container.json"


def test_artifact_path_env_wins_over_rc(tmp_path, monkeypatch):
    _touch(tmp_path, ".graphifyrc", f"{RC_ARTIFACT} = out/container.json\n")
    env_path = tmp_path / "elsewhere.json"
    monkeypatch.setenv(ENV_ARTIFACT, str(env_path))
    assert artifact_path(tmp_path) == env_path


# -- validate -------------------------------------------------------------------


def _valid_payload() -> dict:
    payload = {"schema_version": SCHEMA_VERSION, "stamp": {}, "errors": []}
    for key in SOURCES:
        payload[key] = [] if key in ("services", "routes", "extensions") else {}
    return payload


def test_validate_accepts_a_well_formed_payload():
    payload = _valid_payload()
    assert validate(payload) == payload


def test_validate_rejects_non_object():
    with pytest.raises(ArtifactError):
        validate([1, 2, 3])


def test_validate_rejects_missing_key():
    payload = _valid_payload()
    del payload["routes"]
    with pytest.raises(ArtifactError, match="routes"):
        validate(payload)


def test_validate_rejects_wrong_schema_version():
    payload = _valid_payload()
    payload["schema_version"] = 2
    with pytest.raises(ArtifactError, match="schema_version"):
        validate(payload)


def test_validate_rejects_services_not_a_list():
    payload = _valid_payload()
    payload["services"] = {}
    with pytest.raises(ArtifactError, match="services"):
        validate(payload)


# -- load_artifact --------------------------------------------------------------


def test_load_artifact_returns_none_when_absent(tmp_path):
    assert load_artifact(tmp_path) is None


def test_load_artifact_raises_on_bad_json(tmp_path):
    _touch(tmp_path, "drupal-container.json", "{not valid json")
    with pytest.raises(ArtifactError):
        load_artifact(tmp_path)


def test_load_artifact_raises_on_invalid_schema(tmp_path):
    _touch(tmp_path, "drupal-container.json", json.dumps({"schema_version": 2}))
    with pytest.raises(ArtifactError):
        load_artifact(tmp_path)


def test_load_artifact_reads_a_valid_file(tmp_path):
    payload = _valid_payload()
    _touch(tmp_path, "drupal-container.json", json.dumps(payload))
    artifact = load_artifact(tmp_path)
    assert artifact is not None
    assert artifact.data == payload
    assert artifact.stamp == {}
    assert artifact.path == tmp_path / "drupal-container.json"


# -- compute_host_stamp / staleness ---------------------------------------------


def test_stamp_carries_no_absolute_paths(tmp_path):
    root = _site(tmp_path)
    stamp = compute_host_stamp(root)

    def _walk(value):
        if isinstance(value, dict):
            for v in value.values():
                yield from _walk(v)
        elif isinstance(value, list):
            for v in value:
                yield from _walk(v)
        else:
            yield value

    for value in _walk(stamp):
        if isinstance(value, str):
            assert str(tmp_path) not in value


def test_staleness_is_fresh_right_after_compute_host_stamp(tmp_path):
    root = _site(tmp_path)
    stamp = compute_host_stamp(root)
    artifact = Artifact(data={**_valid_payload(), "stamp": stamp}, path=root / "drupal-container.json")
    status, reasons = staleness(artifact, root)
    assert status == "fresh"
    assert reasons == []


def test_staleness_flags_a_changed_source_file(tmp_path):
    root = _site(tmp_path)
    stamp = compute_host_stamp(root)
    artifact = Artifact(data={**_valid_payload(), "stamp": stamp}, path=root / "drupal-container.json")

    (root / "web/modules/custom/foo/foo.services.yml").write_text("services: {changed: true}\n")

    status, reasons = staleness(artifact, root)
    assert status == "stale"
    assert any("foo.services.yml" in r for r in reasons)


def test_staleness_flags_a_changed_composer_lock(tmp_path):
    root = _site(tmp_path)
    stamp = compute_host_stamp(root)
    artifact = Artifact(data={**_valid_payload(), "stamp": stamp}, path=root / "drupal-container.json")

    packages = _PACKAGES + [{"name": "drupal/baz", "type": "drupal-module"}]
    (root / "composer.lock").write_text(json.dumps({"packages": packages, "packages-dev": []}))

    status, reasons = staleness(artifact, root)
    assert status == "stale"
    assert "composer.lock changed" in reasons


def test_compute_host_stamp_reads_git(tmp_path):
    root = _site(tmp_path)
    _init_git(root)
    stamp = compute_host_stamp(root)
    assert stamp["git_commit"]
    assert stamp["git_dirty"] is False

    (root / "web/modules/custom/foo/foo.module").write_text("<?php\n// dirty\n")
    stamp2 = compute_host_stamp(root)
    assert stamp2["git_dirty"] is True


def test_compute_host_stamp_outside_git_has_none_commit(tmp_path):
    root = _site(tmp_path)
    stamp = compute_host_stamp(root)
    assert stamp["git_commit"] is None
    assert stamp["git_dirty"] is False


# -- final review: composer-root anchors, no git at build, the full changed list ----


def _stamped(root: Path) -> Artifact:
    return Artifact(data={**_valid_payload(), "stamp": compute_host_stamp(root)},
                    path=root / "drupal-container.json")


@pytest.mark.parametrize("with_registry", [False, True])
def test_a_stamp_taken_at_the_composer_root_is_fresh_for_a_scan_of_web(
        tmp_path, with_registry, _isolated_discovery_state):
    """Finding 7: `sources` keys are relative to the composer root at both
    collection and build time, whatever the scan root."""
    from graphify.drupal import discovery

    root = _site(tmp_path / "site")
    if with_registry:
        discovery.prepare_run(root, tmp_path / "collect")
    artifact = _stamped(root)
    assert all(k.startswith("web/") for k in artifact.stamp["sources"])
    if with_registry:
        discovery.prepare_run(root / "web", tmp_path / "build")

    status, reasons = staleness(artifact, root / "web")
    assert (status, reasons) == ("fresh", [])


def test_a_source_outside_the_composer_root_is_never_stored_absolute(tmp_path):
    from graphify.drupal.container import _sources_sha

    anchor = tmp_path / "site"
    anchor.mkdir()
    outside = _touch(tmp_path, "elsewhere/x.info.yml")
    _sha, hashes = _sources_sha(anchor, [outside])
    assert list(hashes) == ["../elsewhere/x.info.yml"]


def test_staleness_runs_no_git(tmp_path, monkeypatch):
    """Finding 8: a build's staleness check uses neither `git_commit` nor
    `git_dirty`, so it runs no git."""
    from graphify.drupal import container

    root = _site(tmp_path)
    artifact = _stamped(root)

    def _no_git(*_args, **_kwargs):
        raise AssertionError("git ran during staleness")

    monkeypatch.setattr(container, "_run_git", _no_git)
    monkeypatch.setattr(container.subprocess, "run", _no_git)
    assert staleness(artifact, root) == ("fresh", [])


@pytest.mark.parametrize("scan", [".", "web"])
def test_possibly_stale_holds_past_the_tenth_changed_file(tmp_path, scan):
    """Finding 9: the reason shows 10 paths, but every changed file marks
    its records `possibly_stale`."""
    import networkx as nx

    from graphify.drupal import divergence
    from graphify.drupal.container import check_staleness
    from graphify.drupal.container_overlay import OverlayResult

    root = _site(tmp_path)
    names = [f"m{i:02d}" for i in range(12)]
    for name in names:
        _touch(root, f"web/modules/custom/{name}/{name}.info.yml", f"name: {name}\ntype: module\n")
    artifact = _stamped(root)
    for name in names:
        _touch(root, f"web/modules/custom/{name}/{name}.info.yml", f"name: {name}\ntype: module\n#\n")

    status, reasons, changed = check_staleness(artifact, root / scan)
    assert status == "stale" and len(changed) == 12
    assert reasons == [f"12 container source files changed: {', '.join(sorted(changed)[:10])}"]

    scan_root = (root / scan).resolve()
    G = nx.Graph()
    for name in names:
        file = (root / f"web/modules/custom/{name}/{name}.info.yml").resolve()
        G.add_node(f"drupal_extension_{name}", type="drupal_extension", realm="custom",
                   runtime="absent", source_file=file.relative_to(scan_root).as_posix())
    result = OverlayResult(status=status, reasons=reasons, stale_files=changed)
    records = divergence.compute(G, result, artifact, root / scan)
    stale = [r for r in records if r["kind"] == "static_only"]
    assert len(stale) == 12
    assert all(r["possibly_stale"] for r in stale)
