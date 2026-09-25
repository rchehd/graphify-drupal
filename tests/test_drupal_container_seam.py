"""The container overlay on every build: the `build_from_json` seam, the
boundary facts it re-applies, the divergence log and the report block
(P3 spec S7.1, S7.7, S7.8, S8, S9, S11.2)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal import boundary, discovery
from graphify.drupal.container import ENV_ARTIFACT, compute_host_stamp
from graphify.drupal.divergence import DIVERGENCE_FILENAME
from graphify.drupal.register import DrupalSeamError, _patch_build, install
from graphify.drupal.yaml_common import service_id
from graphify.drupal.yaml_extract import extension_id
from tests.test_drupal_discovery import _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

FOO = "web/modules/custom/foo"

CORE_SERVICES = (
    "services:\n"
    "  entity_type.manager:\n"
    "    class: Drupal\\Core\\Entity\\EntityTypeManager\n"
)

FOO_SERVICES = (
    "services:\n"
    "  foo.bar:\n"
    "    class: Drupal\\foo\\FooBar\n"
    "    arguments: ['@entity_type.manager']\n"
    "  foo.other:\n"
    "    class: Drupal\\foo\\OtherBar\n"
    "  foo.decorator:\n"
    "    class: Drupal\\foo\\Decorator\n"
    "    decorates: foo.bar\n"
    "  foo.gone:\n"
    "    class: Drupal\\foo\\Gone\n"
)


def _php(cls: str) -> str:
    return f"<?php\n\nnamespace Drupal\\foo;\n\nclass {cls} {{\n  public function run() {{}}\n}}\n"


def _container_site(root: Path) -> Path:
    """A custom module `foo` with four services, a core service it injects,
    and a sync store whose `core.extension.yml` enables `node` too."""
    return _site(root, {
        "web/core/core.services.yml": CORE_SERVICES,
        f"{FOO}/foo.info.yml": "name: Foo\ntype: module\n",
        f"{FOO}/foo.services.yml": FOO_SERVICES,
        f"{FOO}/src/FooBar.php": _php("FooBar"),
        f"{FOO}/src/OtherBar.php": _php("OtherBar"),
        f"{FOO}/src/Decorator.php": _php("Decorator"),
        "config/sync/core.extension.yml": "module:\n  foo: 0\n  node: 0\n  system: 0\ntheme: {}\n",
    })


def _svc(sid, cls, file, provider=None, arguments=(), decorates=None):
    return {"id": sid, "class": cls, "file": file, "arguments": list(arguments),
            "tags": [], "decorates": decorates, "provider": provider}


def _enabled_sha(extensions: list[dict]) -> str:
    keys = sorted(f"{e['type']}:{e['name']}" for e in extensions if e.get("status") == 1)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _artifact_data(root: Path, stamp_scratch: Path, *, extra_services=()) -> dict:
    """What the collector would print for `_container_site`, with a stamp
    matching the tree (so the build finds it `fresh`)."""
    extensions = [
        {"name": "foo", "type": "module", "path": FOO, "status": 1, "weight": 0,
         "dependencies": []},
        {"name": "system", "type": "module", "path": "web/core/modules/system", "status": 1,
         "weight": 0, "dependencies": []},
        # Enabled in the container only: custom (logged), core and not in the graph (not).
        {"name": "foo_extra", "type": "module", "path": "web/modules/custom/foo_extra",
         "status": 1, "weight": 0, "dependencies": []},
        {"name": "big_pipe", "type": "module", "path": "web/core/modules/big_pipe",
         "status": 1, "weight": 0, "dependencies": []},
    ]
    data = {
        "schema_version": 1,
        "services": [
            _svc("foo.bar", "Drupal\\foo\\FooBar", f"{FOO}/src/FooBar.php", "foo",
                 arguments=["entity_type.manager"]),
            _svc("foo.other", "Drupal\\foo\\OtherBar", f"{FOO}/src/OtherBar.php", "foo"),
            # Static says it decorates foo.bar: a one-target conflict.
            _svc("foo.decorator", "Drupal\\foo\\Decorator", f"{FOO}/src/Decorator.php", "foo",
                 decorates="foo.other"),
            # Declared by no *.services.yml (a ServiceProvider's): container only.
            _svc("foo.dynamic", "Drupal\\foo\\FooBar", f"{FOO}/src/FooBar.php", "foo"),
            _svc("entity_type.manager", "Drupal\\Core\\Entity\\EntityTypeManager",
                 "web/core/lib/Drupal/Core/Entity/EntityTypeManager.php", "core"),
            *extra_services,
        ],
        "aliases": {}, "routes": [], "extensions": extensions,
        "hooks": {}, "plugins": {}, "subscribers": {}, "errors": [],
        "stamp": {"created_at": "2026-09-25T12:00:00Z", "runner": "ddev",
                  "drupal_version": "11.2.0", "enabled_extensions_sha": _enabled_sha(extensions)},
    }
    # The host half of the stamp as a build recomputes it: with the registry current.
    discovery.prepare_run(root, stamp_scratch)
    try:
        data["stamp"].update(compute_host_stamp(root))
    finally:
        discovery.set_current(None)
    return data


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True, indent=1), encoding="utf-8")
    return path


_SUMMARY = re.compile(r"incremental summary: .*?(\d+) re-extracted")


def _env(artifact: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in (discovery.ENV_VAR, ENV_ARTIFACT)}
    if artifact is not None:
        env[ENV_ARTIFACT] = str(artifact)
    return env


def _extract(root: Path, out: Path, artifact: Path | None) -> tuple[dict, int | None, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--out", str(out)],
        capture_output=True, text=True, cwd=root.parent, env=_env(artifact),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = (out / "graphify-out" / "graph.json").read_text(encoding="utf-8")
    found = _SUMMARY.search(proc.stdout)
    return json.loads(text), int(found.group(1)) if found else None, text


def _report(root: Path, out: Path) -> str:
    graph = out / "graphify-out" / "graph.json"
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "cluster-only", str(root), "--graph", str(graph),
         "--no-label", "--no-viz"],
        capture_output=True, text=True, cwd=root.parent, env=_env(None),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return (out / "graphify-out" / "GRAPH_REPORT.md").read_text(encoding="utf-8")


def _links(graph: dict) -> list[dict]:
    return graph.get("links") or graph.get("edges") or []


def _nodes(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


def _overlay_files(graph: dict) -> set[str]:
    items = [*graph["nodes"], *_links(graph)]
    return {str(i.get("source_file")) for i in items if i.get("origin") == "container"}


def _assert_overlay_paths_relative(graph: dict) -> None:
    files = _overlay_files(graph)
    assert files
    assert not [f for f in files if Path(f).is_absolute()], files


def _container_block(report: str) -> str:
    assert "### Container" in report, report
    return report.split("### Container", 1)[1]


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state, monkeypatch):  # noqa: F811
    monkeypatch.delenv(ENV_ARTIFACT, raising=False)
    boundary.clear_caches()
    yield
    boundary.clear_caches()


# -- the real CLI -----------------------------------------------------------------


def test_a_build_lays_the_artifact_writes_the_divergences_and_reports(tmp_path):
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))

    graph, _, _ = _extract(root, out, artifact)

    links = _links(graph)
    assert any(e.get("origin") == "container" for e in links)
    nodes = _nodes(graph)
    assert nodes[service_id("foo.bar")]["runtime"] == "present"
    assert nodes[service_id("foo.gone")]["runtime"] == "absent"
    assert nodes[service_id("foo.dynamic")]["runtime"] == "present"

    records = json.loads((out / "graphify-out" / DIVERGENCE_FILENAME).read_text(encoding="utf-8"))
    by_kind: dict[str, set[str]] = {}
    for r in records:
        by_kind.setdefault(r["kind"], set()).add(r["subject"])
    assert service_id("foo.gone") in by_kind["static_only"]
    assert service_id("foo.dynamic") in by_kind["container_only"]
    assert service_id("foo.decorator") in by_kind["conflict"]
    # node: enabled in core.extension.yml only, a boundary stub in the graph;
    # foo_extra: custom, enabled in the container only; big_pipe: core, not
    # in the graph, so not a logged subject (spec S8).
    assert by_kind["extension_state"] == {extension_id("node"), extension_id("foo_extra")}
    assert records == sorted(records, key=lambda r: (r["kind"], r["subject"]))
    assert not any(r["possibly_stale"] for r in records)
    _assert_overlay_paths_relative(graph)

    inventory = json.loads((out / "graphify-out" / "drupal-inventory.json").read_text(encoding="utf-8"))
    assert inventory["container"]["status"] == "fresh"
    assert inventory["container"]["divergence"]["conflict"] >= 1

    block = _container_block(_report(root, out))
    assert "status: fresh" in block
    assert "runner: ddev" in block
    assert "static_only" in block


def test_a_rerun_with_no_change_writes_an_identical_graph(tmp_path):
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))

    _, _, first = _extract(root, out, artifact)
    _, rerun, second = _extract(root, out, artifact)

    assert rerun == 0
    strip = re.compile(r'"built_at_(commit|time)": "[^"]*"')
    assert strip.sub("", first) == strip.sub("", second)


def test_changing_only_the_artifact_re_extracts_nothing_and_reaches_the_graph(tmp_path):
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"
    path = tmp_path / "artifact" / "drupal-container.json"
    _write(path, _artifact_data(root, tmp_path / "stamp"))
    _extract(root, out, path)

    later = _svc("foo.later", "Drupal\\foo\\OtherBar", f"{FOO}/src/OtherBar.php", "foo")
    _write(path, _artifact_data(root, tmp_path / "stamp", extra_services=[later]))
    graph, rerun, _ = _extract(root, out, path)

    assert rerun == 0
    assert service_id("foo.later") in _nodes(graph)
    _assert_overlay_paths_relative(graph)
    assert _overlay_files(graph) == {"../artifact/drupal-container.json"}


def test_update_lays_a_changed_artifact_over_the_graph(tmp_path):
    root = _container_site(tmp_path / "site")
    path = tmp_path / "artifact" / "drupal-container.json"
    _write(path, _artifact_data(root, tmp_path / "stamp"))

    def update() -> dict:
        proc = subprocess.run([sys.executable, "-m", "graphify", "update", str(root)],
                              capture_output=True, text=True, cwd=root, env=_env(path))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))

    proc = subprocess.run([sys.executable, "-m", "graphify", "extract", str(root), "--code-only"],
                          capture_output=True, text=True, cwd=root, env=_env(path))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    later = _svc("foo.later", "Drupal\\foo\\OtherBar", f"{FOO}/src/OtherBar.php", "foo")
    _write(path, _artifact_data(root, tmp_path / "stamp", extra_services=[later]))
    assert service_id("foo.later") in _nodes(update())


def test_without_an_artifact_nothing_is_marked_and_the_report_says_unavailable(tmp_path):
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"

    graph, _, _ = _extract(root, out, None)

    assert not any(e.get("origin") == "container" for e in _links(graph))
    assert not any("runtime" in n for n in graph["nodes"])
    assert not (out / "graphify-out" / DIVERGENCE_FILENAME).exists()
    block = _container_block(_report(root, out))
    assert "status: unavailable" in block
    assert "plugin derivatives" in block
    assert "routes added by `RouteSubscriber`" in block


def test_a_divergence_log_is_removed_once_the_artifact_is_gone(tmp_path):
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))
    _extract(root, out, artifact)
    assert (out / "graphify-out" / DIVERGENCE_FILENAME).exists()

    graph, _, _ = _extract(root, out, None)
    assert not (out / "graphify-out" / DIVERGENCE_FILENAME).exists()
    assert not any(e.get("origin") == "container" for e in _links(graph))
    assert not any("runtime" in n for n in graph["nodes"])


def test_a_broken_artifact_is_invalid_and_the_build_succeeds(tmp_path):
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"
    artifact = tmp_path / "artifact" / "drupal-container.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{not json", encoding="utf-8")

    graph, _, _ = _extract(root, out, artifact)

    assert not any(e.get("origin") == "container" for e in _links(graph))
    assert not (out / "graphify-out" / DIVERGENCE_FILENAME).exists()
    inventory = json.loads((out / "graphify-out" / "drupal-inventory.json").read_text(encoding="utf-8"))
    assert inventory["container"]["status"] == "invalid"
    assert "status: invalid (" in _container_block(_report(root, out))


def test_a_registry_only_change_reaches_the_boundary_stub(tmp_path):
    """P2b S9 / P3 S11.2: a boundary service's class changes in the registry,
    no custom file changes; the next build's stub follows."""
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"
    etm = service_id("entity_type.manager")

    first, _, _ = _extract(root, out, None)
    assert _nodes(first)[etm]["class_name"] == "Drupal\\Core\\Entity\\EntityTypeManager"
    assert _nodes(first)[etm]["boundary"] is True

    (root / "web/core/core.services.yml").write_text(
        CORE_SERVICES.replace("EntityTypeManager", "OtherEntityTypeManager"), encoding="utf-8")
    second, rerun, _ = _extract(root, out, None)

    assert rerun == 0
    assert _nodes(second)[etm]["class_name"] == "Drupal\\Core\\Entity\\OtherEntityTypeManager"


def test_a_container_fact_meets_the_current_registry_not_the_old_stub(tmp_path):
    """The registry's facts land before the container's: a stub whose class
    changed in the registry only, confirmed by the container, is no conflict."""
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"
    etm = service_id("entity_type.manager")
    _extract(root, out, None)

    new_class = "Drupal\\Core\\Entity\\OtherEntityTypeManager"
    (root / "web/core/core.services.yml").write_text(
        CORE_SERVICES.replace("EntityTypeManager", "OtherEntityTypeManager"), encoding="utf-8")
    data = _artifact_data(root, tmp_path / "stamp")
    for s in data["services"]:
        if s["id"] == "entity_type.manager":
            s["class"] = new_class
    artifact = _write(tmp_path / "artifact" / "drupal-container.json", data)
    graph, _, _ = _extract(root, out, artifact)

    assert _nodes(graph)[etm]["class_name"] == new_class
    records = json.loads((out / "graphify-out" / DIVERGENCE_FILENAME).read_text(encoding="utf-8"))
    assert not [r for r in records if r["kind"] == "conflict" and r["subject"] == etm]


def test_an_in_root_artifact_is_never_extracted_as_code(tmp_path):
    """The default location, `<root>/drupal-container.json`, is `.json`: core
    would extract it (and, above 1 MiB, fail and re-extract it every build)."""
    root = _container_site(tmp_path / "site")
    out = tmp_path / "out"
    data = _artifact_data(root, tmp_path / "stamp")
    data["_padding"] = "x" * (1024 * 1024 + 4096)
    _write(root / "drupal-container.json", data)
    assert (root / "drupal-container.json").stat().st_size > 1024 * 1024

    first, _, _ = _extract(root, out, None)
    second, rerun, _ = _extract(root, out, None)

    assert rerun == 0
    for graph in (first, second):
        assert any(e.get("origin") == "container" for e in _links(graph))
        from_json = [n["id"] for n in graph["nodes"]
                     if n.get("source_file") == "drupal-container.json"
                     and n.get("origin") != "container"]
        assert from_json == []
        assert _overlay_files(graph) == {"drupal-container.json"}


def test_an_out_dir_inside_the_root_keeps_the_overlay_on_a_rerun(tmp_path):
    """With `--out` inside the scan root, core also reads a relative
    `source_file` from the out dir: `../artifact/...` would land back in the
    root and be pruned, so the overlay falls back to the absolute path."""
    root = _container_site(tmp_path / "site")
    out = root / "o"
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))

    _extract(root, out, artifact)
    graph, rerun, _ = _extract(root, out, artifact)

    assert rerun == 0
    assert _overlay_files(graph) == {artifact.absolute().as_posix()}


# -- in process -----------------------------------------------------------------


def _tiny_extraction() -> dict:
    return {"nodes": [
        {"id": service_id("foo.bar"), "label": "foo.bar", "file_type": "concept",
         "type": "drupal_service", "layer": "di", "realm": "custom",
         "source_file": f"{FOO}/foo.services.yml", "source_location": "L1", "_origin": "ast"},
    ], "edges": []}


def test_build_from_json_outside_a_run_adds_nothing(tmp_path, monkeypatch):
    install()
    import graphify.build as build
    from graphify.drupal import container_overlay

    root = _container_site(tmp_path / "site")
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))
    monkeypatch.setenv(ENV_ARTIFACT, str(artifact))
    discovery.prepare_run(root, tmp_path / "scratch")
    assert discovery.current_run() is not None
    discovery.set_current(None)
    assert discovery.current_run() is None

    G = build.build_from_json(_tiny_extraction())
    assert container_overlay.run_for_build(G) is None
    assert not any("runtime" in d for _, d in G.nodes(data=True))
    assert not (tmp_path / "scratch" / "graphify-out" / DIVERGENCE_FILENAME).exists()


def test_build_from_json_inside_a_run_lays_the_overlay(tmp_path, monkeypatch):
    install()
    import graphify.build as build

    root = _container_site(tmp_path / "site")
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))
    monkeypatch.setenv(ENV_ARTIFACT, str(artifact))
    discovery.prepare_run(root, tmp_path / "scratch")

    G = build.build_from_json(_tiny_extraction())
    assert G.nodes[service_id("foo.bar")]["runtime"] == "present"
    assert (tmp_path / "scratch" / "graphify-out" / DIVERGENCE_FILENAME).is_file()


def test_the_seam_fails_loudly_without_build_from_json(monkeypatch):
    import graphify.build as build

    monkeypatch.delattr(build, "build_from_json", raising=True)
    with pytest.raises(DrupalSeamError, match="build_from_json"):
        _patch_build(build)


def test_a_changed_artifact_triggers_a_watch_rebuild(tmp_path, monkeypatch):
    install()
    import graphify.watch as w

    root = _container_site(tmp_path / "site")
    # A suffix core does not treat as code: only the seam can know it.
    artifact = tmp_path / "artifact" / "site.container"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ENV_ARTIFACT, str(artifact))
    assert w._batch_triggers_rebuild([artifact]) is False  # no run yet: nothing known

    discovery.prepare_run(root, tmp_path / "scratch")
    assert w._batch_triggers_rebuild([artifact]) is True
    assert w._has_non_code([artifact]) is False


def test_an_internal_error_undoes_the_overlay_and_reports_error(tmp_path, monkeypatch):
    install()
    import graphify.build as build
    import graphify.detect as detect
    from graphify.drupal import container_overlay
    from graphify.drupal.inventory import current_inventory, render_section

    root = _container_site(tmp_path / "site")
    artifact = _write(tmp_path / "artifact" / "drupal-container.json",
                      _artifact_data(root, tmp_path / "stamp"))
    monkeypatch.setenv(ENV_ARTIFACT, str(artifact))
    scratch = tmp_path / "scratch"
    detect.detect(root, cache_root=scratch)
    out = discovery.current_run()[1]
    (out / DIVERGENCE_FILENAME).write_text("[]", encoding="utf-8")

    def boom(self):
        # Half-way through: a node and an edge laid, then a failure.
        half = self.ensure("half_done", type="drupal_service", layer="di", label="half",
                           realm="custom")
        self.edge(service_id("foo.bar"), half, "injects_service")
        raise RuntimeError("boom")

    monkeypatch.setattr(container_overlay._Overlay, "run", boom)
    G = build.build_from_json(_tiny_extraction())

    assert not any(d.get("origin") == "container" for *_, d in G.edges(data=True))
    assert "half_done" not in G
    assert not any("runtime" in d for _, d in G.nodes(data=True))
    assert not (out / DIVERGENCE_FILENAME).exists()
    container = current_inventory()["container"]
    assert container["status"] == "error"
    assert "status: error (RuntimeError: boom)" in render_section(current_inventory())
    on_disk = json.loads((out / "drupal-inventory.json").read_text(encoding="utf-8"))
    assert on_disk["container"]["status"] == "error"


def test_undo_keeps_a_key_a_static_producer_wrote_since(tmp_path):
    """A boundary fact replaced a value (kept in `_overlay_prev`); a fresh
    static stub then wrote the key again (core's dedup merges graph.json's
    copy, markers included, into it). Undo keeps the static value and drops
    the stale previous one."""
    import networkx as nx

    from graphify.drupal.container_overlay import _replace, undo

    G = nx.Graph()
    G.add_node("stub", type="drupal_service", boundary=True, class_name="Old")
    _replace(G.nodes["stub"], "class_name", "New")
    assert G.nodes["stub"]["_overlay_prev"] == {"class_name": "Old"}

    undo(G)
    assert G.nodes["stub"]["class_name"] == "Old"
    assert not any(k.startswith("_overlay") for k in G.nodes["stub"])

    _replace(G.nodes["stub"], "class_name", "New")
    G.nodes["stub"]["class_name"] = "Fresh"          # a static producer's write
    undo(G)
    assert G.nodes["stub"]["class_name"] == "Fresh"
    assert not any(k.startswith("_overlay") for k in G.nodes["stub"])
