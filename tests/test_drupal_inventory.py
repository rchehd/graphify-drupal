"""The coverage inventory and the "Drupal coverage" report section (P2a Task 7,
spec §5.7)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from graphify.drupal.discovery import prepare_run
from graphify.drupal.inventory import (
    DEFERRED_FAMILIES,
    build_inventory,
    render_section,
)
from graphify.drupal.register import install
from tests.test_drupal_discovery import YAML_MANAGER, _module, _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401 (reused fixture)

BAR_SERVICES = "services:\n  plugin.manager.bar:\n    class: Drupal\\bar\\BarManager\n"

FOO_BAR_PLUGINS = (
    "one:\n"
    "  class: Drupal\\foo\\One\n"
    "  label: One\n"
    "two:\n"
    "  deriver: Drupal\\foo\\D\n"
    "  weight: 3\n"
)

SECOND_MANAGER = r"""<?php
namespace Drupal\second\Plugin;
use Drupal\Core\Plugin\DefaultPluginManager;
class SecondManager extends DefaultPluginManager {
  public function __construct($namespaces, $module_handler) {
    parent::__construct('Plugin/Second', $namespaces, $module_handler);
  }
}
"""


def _coverage_site(tmp_path: Path) -> Path:
    files = {
        **_module("foo", "services: {}\n"),
        "web/modules/custom/foo/foo.bar.yml": FOO_BAR_PLUGINS,
        "web/modules/custom/foo/foo.qux.yml": "some_plugin: {}\n",
        "web/modules/custom/foo/components/x/foo.component.yml": "props: {}\n",
        **_module("bar", BAR_SERVICES, {"src/BarManager.php": YAML_MANAGER}),
        **_module("second", "services: {}\n", {"src/Plugin/SecondManager.php": SECOND_MANAGER}),
        ".gitlab-ci.yml": "stages: []\n",
    }
    return _site(tmp_path, files)


def _detected_yaml(root: Path) -> set[str]:
    """Stand-in for what `detect()`'s "files"/"unclassified" categories would
    hold: every `.yml` under the site, as absolute paths."""
    return {str(p) for p in root.rglob("*.yml")}


def test_build_inventory_partitions_the_site(tmp_path, _isolated_discovery_state):
    install()
    root = _coverage_site(tmp_path)
    registry = prepare_run(root)
    assert registry is not None

    inventory = build_inventory(registry, _detected_yaml(root), root)

    assert inventory["unrecognised_yaml"] == [{
        "family": "qux",
        "files": 1,
        "owners": ["foo"],
        "examples": ["web/modules/custom/foo/foo.qux.yml"],
    }]
    assert inventory["deferred"] == [{
        "family": "component", "phase": DEFERRED_FAMILIES["component"], "files": 1,
    }]
    assert inventory["managers_unresolved"] == [{
        "class": "Drupal\\second\\Plugin\\SecondManager",
        "file": (root / "web/modules/custom/second/src/Plugin/SecondManager.php").as_posix(),
        "reason": "not_a_service",
    }]
    assert inventory["summary"] == {
        "types": 2,
        "registered_types": 1,
        "yaml_plugins": 2,
        "deferred_files": 1,
        "unrecognised_families": 1,
        "unrecognised_files": 1,
        "filtered": 1,
        # No composer project: web/core as a whole is the one boundary dir.
        "boundary": {"core": 1, "contrib": 0, "vendor": 0, "files": 0},
        "boundary_reasons": {"path_rule": 1},
    }


def test_unrecognised_families_are_sorted_by_count_then_name(tmp_path, _isolated_discovery_state):
    install()
    files = {
        **_module("foo", "services: {}\n"),
        "web/modules/custom/foo/foo.alpha.yml": "a: {}\n",
        "web/modules/custom/foo/foo.zulu.yml": "z: {}\n",
        "web/modules/custom/foo/foo.zulu2.yml": "z2: {}\n",
    }
    # foo.zulu2.yml also reads as family "zulu2" (different owner-prefixed
    # name), so give "zulu" two files by adding it under a second extension.
    files.update(_module("bar", "services: {}\n"))
    files["web/modules/custom/bar/bar.zulu.yml"] = "z: {}\n"
    root = _site(tmp_path, files)
    registry = prepare_run(root)

    inventory = build_inventory(registry, _detected_yaml(root), root)
    families = [(e["family"], e["files"]) for e in inventory["unrecognised_yaml"]]
    # "zulu" (2 files) sorts before "alpha"/"zulu2" (1 file each, alphabetical).
    assert families == [("zulu", 2), ("alpha", 1), ("zulu2", 1)]


def test_render_section_of_an_empty_inventory_still_renders_the_heading_and_zeros():
    text = render_section({})
    assert text.startswith("## Drupal coverage")
    assert "| plugin types | 0 |" in text
    assert "| filtered | 0 |" in text
    assert "- none" in text


def test_render_section_includes_the_built_inventory(tmp_path, _isolated_discovery_state):
    install()
    root = _coverage_site(tmp_path)
    registry = prepare_run(root)
    inventory = build_inventory(registry, _detected_yaml(root), root)

    text = render_section(inventory)
    assert "## Drupal coverage" in text
    assert "qux" in text
    assert "component" in text
    assert "not_a_service" in text


def _run_cli(corpus: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(corpus), "--code-only"],
        capture_output=True, text=True, cwd=corpus,
    )


def test_a_stale_inventory_is_removed_once_the_tree_stops_being_drupal(
        tmp_path, _isolated_discovery_state):
    """A prior run's `drupal-inventory.json` must not survive a later run on
    the same out dir whose tree has no Drupal marker -- otherwise
    `report.generate`'s file fallback keeps reporting that old site's
    coverage for a tree that isn't Drupal (or isn't scanned here) any more."""
    install()
    import networkx as nx
    import graphify.detect as detect
    import graphify.report as report
    from graphify.drupal.discovery import current_registry, out_dir
    from graphify.drupal.inventory import current_inventory, load_inventory

    root = _coverage_site(tmp_path)
    detect.detect(root)
    assert current_registry() is not None
    inventory_path = out_dir(root) / "drupal-inventory.json"
    assert inventory_path.is_file()
    assert current_inventory() is not None

    detection_result = {"warning": "test corpus"}
    before = report.generate(nx.Graph(), {}, {}, {}, [], [], detection_result, {}, str(root))
    assert "## Drupal coverage" in before

    (root / "web/core/lib/Drupal.php").unlink()
    detect.detect(root)

    assert current_registry() is None
    assert not inventory_path.exists()
    assert current_inventory() is None
    assert load_inventory(out_dir(root)) is None

    after = report.generate(nx.Graph(), {}, {}, {}, [], [], detection_result, {}, str(root))
    assert "## Drupal coverage" not in after


def test_cli_writes_the_inventory_and_the_report_section(tmp_path):
    root = _coverage_site(tmp_path)
    proc = _run_cli(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    inventory_path = root / "graphify-out" / "drupal-inventory.json"
    assert inventory_path.is_file()
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert inventory["unrecognised_yaml"][0]["family"] == "qux"

    # `extract` stops at graph.json; GRAPH_REPORT.md comes from `cluster-only`,
    # which re-reads the inventory from disk (process state is gone by now).
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "cluster-only", str(root), "--no-label", "--no-viz"],
        capture_output=True, text=True, cwd=root,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    report = (root / "graphify-out" / "GRAPH_REPORT.md").read_text(encoding="utf-8")
    assert "## Drupal coverage" in report
    assert "qux" in report


def test_cluster_only_with_a_graph_in_another_out_dir_reports_coverage(tmp_path):
    root = _coverage_site(tmp_path / "site")
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--out", str(out)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    graph = out / "graphify-out" / "graph.json"

    # `cluster-only <site> --graph <out>/graphify-out/graph.json` writes the
    # report beside that graph; the inventory is there too, not under <site>.
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "cluster-only", str(root), "--graph", str(graph),
         "--no-label", "--no-viz"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    report = (out / "graphify-out" / "GRAPH_REPORT.md").read_text(encoding="utf-8")
    assert "## Drupal coverage" in report
    assert "qux" in report


def test_yaml_plugins_counts_only_the_entries_extraction_emits(tmp_path, _isolated_discovery_state):
    install()
    root = _site(tmp_path, {
        **_module("foo", "services: {}\n"),
        **_module("bar", BAR_SERVICES, {"src/BarManager.php": YAML_MANAGER}),
        # A mapping and a null are plugins; a scalar and a list are skipped.
        "web/modules/custom/foo/foo.bar.yml": "one: {}\ntwo: ~\nthree: 5\nfour: [a]\n",
    })
    registry = prepare_run(root)

    inventory = build_inventory(registry, _detected_yaml(root), root)
    assert inventory["summary"]["yaml_plugins"] == 2
