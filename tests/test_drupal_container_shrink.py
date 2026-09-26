"""The overlay's items never count as a lost node (P3 final review, finding 1),
and the raw `--no-cluster` write gets the overlay too (finding 2).

`undo` drops what the previous overlay made; when the new artifact does not
make it again (the artifact removed, a service gone from it) the graph is
smaller than graph.json. Core's shrink guards (`watch._check_shrink` for
`update`/`watch`, `build_merge`'s #479 guard for `extract --no-dedup`) must
not read that as an unexplained loss: the graph is written and the stale
item is gone."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal import boundary, discovery
from graphify.drupal.container import ENV_ARTIFACT
from graphify.drupal.yaml_common import service_id
from tests.test_drupal_container_seam import (
    _artifact_data,
    _container_site,
    _env,
    _links,
    _nodes,
    _write,
)
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

DYNAMIC = service_id("foo.dynamic")


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state, monkeypatch):  # noqa: F811
    monkeypatch.delenv(ENV_ARTIFACT, raising=False)
    boundary.clear_caches()
    yield
    boundary.clear_caches()


def _run(args: list[str], cwd: Path, artifact: Path | None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "graphify", *args],
                          capture_output=True, text=True, cwd=cwd, env=_env(artifact))


def _graph(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_overlay_gone(graph: dict) -> None:
    assert DYNAMIC not in _nodes(graph)
    assert not any(e.get("origin") == "container" for e in _links(graph))
    assert not any(n.get("origin") == "container" for n in graph["nodes"])


def _assert_one_service_fewer(graph: dict) -> None:
    assert DYNAMIC not in _nodes(graph)
    # The rest of the overlay is still laid.
    assert any(e.get("origin") == "container" for e in _links(graph))
    assert _nodes(graph)[service_id("foo.bar")]["runtime"] == "present"


def _without_dynamic(root: Path, scratch: Path) -> dict:
    data = _artifact_data(root, scratch)
    data["services"] = [s for s in data["services"] if s["id"] != "foo.dynamic"]
    return data


def _first_build(root: Path, tmp_path: Path) -> Path:
    """`extract` (default out dir) with the full artifact: foo.dynamic is in graph.json."""
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))
    proc = _run(["extract", str(root), "--code-only"], root, artifact)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert DYNAMIC in _nodes(_graph(root / "graphify-out" / "graph.json"))
    return artifact


# -- update ---------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["removed", "one_fewer"])
def test_update_writes_the_graph_when_the_artifact_shrinks(tmp_path, change):
    root = _container_site(tmp_path / "site")
    artifact = _first_build(root, tmp_path)
    if change == "removed":
        artifact.unlink()
    else:
        _write(artifact, _without_dynamic(root, tmp_path / "stamp"))

    proc = _run(["update", str(root)], root, artifact)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Refusing to overwrite" not in proc.stderr

    graph = _graph(root / "graphify-out" / "graph.json")
    if change == "removed":
        _assert_overlay_gone(graph)
    else:
        _assert_one_service_fewer(graph)


# -- watch (its loop calls `_rebuild_code(path)`, a full rebuild) -----------------------


@pytest.mark.parametrize("change", ["removed", "one_fewer"])
def test_watch_rebuild_writes_the_graph_when_the_artifact_shrinks(tmp_path, monkeypatch, change):
    from graphify.drupal.register import install

    root = _container_site(tmp_path / "site")
    artifact = _first_build(root, tmp_path)
    if change == "removed":
        artifact.unlink()
    else:
        _write(artifact, _without_dynamic(root, tmp_path / "stamp"))

    install()
    import graphify.watch as watch

    monkeypatch.chdir(root)
    monkeypatch.setenv(ENV_ARTIFACT, str(artifact))
    try:
        assert watch._rebuild_code(root) is True
    finally:
        discovery.set_current(None)

    graph = _graph(root / "graphify-out" / "graph.json")
    if change == "removed":
        _assert_overlay_gone(graph)
    else:
        _assert_one_service_fewer(graph)


# -- extract --no-dedup (build_merge's #479 guard) ---------------------------------------


@pytest.mark.parametrize("change", ["removed", "one_fewer"])
def test_extract_no_dedup_writes_the_graph_when_the_artifact_shrinks(tmp_path, change):
    root = _container_site(tmp_path / "site")
    artifact = _first_build(root, tmp_path)
    if change == "removed":
        artifact.unlink()
    else:
        _write(artifact, _without_dynamic(root, tmp_path / "stamp"))

    proc = _run(["extract", str(root), "--code-only", "--no-dedup"], root, artifact)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "would drop" not in proc.stderr

    graph = _graph(root / "graphify-out" / "graph.json")
    if change == "removed":
        _assert_overlay_gone(graph)
    else:
        _assert_one_service_fewer(graph)


# -- the seam ---------------------------------------------------------------------------


@pytest.mark.parametrize("module, attr, patcher", [
    ("graphify.build", "_load_existing_graph", "_patch_build"),
    ("graphify.watch", "_check_shrink", "_patch_watch"),
    ("graphify.build", "dedupe_nodes", "_patch_build"),
    ("graphify.build", "dedupe_edges", "_patch_build"),
    ("graphify.build", "build_merge", "_patch_build"),
])
def test_the_seam_fails_loudly_without_a_shrink_symbol(monkeypatch, module, attr, patcher):
    import importlib

    from graphify.drupal import register

    target = importlib.import_module(module)
    monkeypatch.delattr(target, attr, raising=True)
    with pytest.raises(register.DrupalSeamError, match=attr):
        getattr(register, patcher)(target)


# -- --no-cluster (finding 2): the raw write path gets the overlay too --------------------


def _raw_extract(root: Path, artifact: Path | None) -> dict:
    proc = _run(["extract", str(root), "--code-only", "--no-cluster"], root, artifact)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return _graph(root / "graphify-out" / "graph.json")


def test_extract_no_cluster_lays_the_artifact(tmp_path):
    root = _container_site(tmp_path / "site")
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))

    graph = _raw_extract(root, artifact)

    nodes = _nodes(graph)
    assert nodes[service_id("foo.bar")]["runtime"] == "present"
    assert nodes[service_id("foo.gone")]["runtime"] == "absent"
    assert DYNAMIC in nodes and nodes[DYNAMIC]["origin"] == "container"
    assert any(e.get("origin") == "container" for e in _links(graph))
    assert (root / "graphify-out" / "drupal-divergence.json").is_file()


def test_extract_no_cluster_rerun_is_identical_and_follows_a_shrunk_artifact(tmp_path):
    root = _container_site(tmp_path / "site")
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))
    first = _raw_extract(root, artifact)
    # A code change so the incremental raw path writes (it exits early otherwise).
    php = root / "web/modules/custom/foo/src/OtherBar.php"
    php.write_text(php.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    second = _raw_extract(root, artifact)
    key = lambda g: (sorted(json.dumps(n, sort_keys=True) for n in g["nodes"]),  # noqa: E731
                     sorted(json.dumps(e, sort_keys=True) for e in _links(g)))
    assert key(first) == key(second)

    _write(artifact, _without_dynamic(root, tmp_path / "stamp"))
    php.write_text(php.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _assert_one_service_fewer(_raw_extract(root, artifact))

    artifact.unlink()
    php.write_text(php.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    graph = _raw_extract(root, artifact)
    _assert_overlay_gone(graph)
    assert not any("runtime" in n or "_overlay_attrs" in n for n in graph["nodes"])


@pytest.mark.parametrize("change", ["removed", "one_fewer"])
def test_update_no_cluster_follows_a_shrunk_artifact(tmp_path, change):
    root = _container_site(tmp_path / "site")
    artifact = _first_build(root, tmp_path)
    if change == "removed":
        artifact.unlink()
    else:
        _write(artifact, _without_dynamic(root, tmp_path / "stamp"))

    proc = _run(["update", str(root), "--no-cluster"], root, artifact)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    graph = _graph(root / "graphify-out" / "graph.json")
    if change == "removed":
        _assert_overlay_gone(graph)
        assert not any("runtime" in n or "_overlay_attrs" in n for n in graph["nodes"])
    else:
        _assert_one_service_fewer(graph)


# -- finding 5: the divergence log describes the graph core writes ----------------------


def test_the_divergence_log_follows_core_s_prune_of_a_deleted_file(tmp_path):
    """`build_merge` prunes a deleted file's nodes after `build_from_json`
    (where the overlay runs): the log and the report block must not keep
    records for what the prune removed."""
    root = _container_site(tmp_path / "site")
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))
    out = root / "graphify-out"
    proc = _run(["extract", str(root), "--code-only"], root, artifact)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    gone = service_id("foo.gone")
    records = json.loads((out / "drupal-divergence.json").read_text(encoding="utf-8"))
    assert gone in {r["subject"] for r in records}

    (root / "web/modules/custom/foo/foo.services.yml").unlink()
    proc = _run(["extract", str(root), "--code-only"], root, artifact)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    graph = _graph(out / "graph.json")
    records = json.loads((out / "drupal-divergence.json").read_text(encoding="utf-8"))
    assert gone not in _nodes(graph)
    subjects = {r["subject"].split("->", 1)[0] for r in records}
    assert gone not in subjects
    assert subjects <= set(_nodes(graph)) | {r["subject"] for r in records
                                            if r["kind"] == "extension_state"}
    inventory = json.loads((out / "drupal-inventory.json").read_text(encoding="utf-8"))
    counted = inventory["container"]["divergence"]
    by_kind: dict[str, int] = {}
    for r in records:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    assert counted == by_kind
