"""Typed boundary nodes and the carried P2a fixes (P2b Task 6, spec §4.3, §6).

The resolver materialises a node for every endpoint no scanned file declares;
those nodes are the boundary. They say so (`boundary: true`) and carry what the
registry knows about them. Separately, core's zero-node heal must stop
re-queuing configuration copies whose nodes P1b's collapse gave to another copy.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal.discovery import prepare_run, type_id
from graphify.drupal.hooks import hook_id
from graphify.drupal.register import DrupalSeamError, _patch_cli, install
from graphify.drupal.yaml_common import link_id, service_id
from graphify.drupal.yaml_extract import extension_id
from tests.test_drupal_discovery import (
    MENU_LINK_INTERFACE,
    MENU_LINK_MANAGER,
    _site,
)
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

FOO = "web/modules/custom/foo"

CORE_SERVICES = (
    "services:\n"
    "  plugin.manager.menu.link:\n"
    "    class: Drupal\\Core\\Menu\\MenuLinkManager\n"
    "  entity_type.manager:\n"
    "    class: Drupal\\Core\\Entity\\EntityTypeManager\n"
)

CORE_API_PHP = "<?php\n\nfunction hook_cron() {\n}\n"

SYSTEM_LINKS = "system.admin_config:\n  title: Configuration\n  route_name: system.admin_config\n"


def _boundary_site(root: Path) -> Path:
    """A custom module that injects a core service, depends on a contrib
    module, implements a core hook and hangs a menu link under a core one."""
    return _site(root, {
        "web/core/core.services.yml": CORE_SERVICES,
        "web/core/core.api.php": CORE_API_PHP,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManagerInterface.php": MENU_LINK_INTERFACE,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManager.php": MENU_LINK_MANAGER,
        "web/core/modules/system/system.info.yml": "name: System\ntype: module\n",
        "web/core/modules/system/system.links.menu.yml": SYSTEM_LINKS,
        "web/modules/contrib/token/token.info.yml": "name: Token\ntype: module\n",
        f"{FOO}/foo.info.yml": "name: Foo\ntype: module\ndependencies:\n  - token:token\n",
        f"{FOO}/foo.services.yml":
            "services:\n  foo.bar:\n    class: Drupal\\foo\\Bar\n"
            "    arguments: ['@entity_type.manager']\n",
        f"{FOO}/foo.module": "<?php\n\nfunction foo_cron() {\n}\n",
        f"{FOO}/foo.links.menu.yml":
            "foo.admin:\n  title: Foo\n  route_name: foo.admin\n  parent: system.admin_config\n",
    })


def _custom_files(root: Path) -> list[Path]:
    return [root / FOO / name for name in
            ("foo.info.yml", "foo.services.yml", "foo.module", "foo.links.menu.yml")]


def _by_id(nodes: list[dict]) -> dict[str, dict]:
    return {n["id"]: n for n in nodes}


_MENU_TYPE_FACTS = {
    "plugin_type": "menu.link",
    "discovery": "yaml",
    "manager_class": "Drupal\\Core\\Menu\\MenuLinkManager",
    "registered": True,
    "manager_service": "plugin.manager.menu.link",
    "yaml_name": "links.menu",
}


def _expected(root: Path) -> dict[str, dict]:
    """The facts each boundary stub carries, by id (paths relative to `root`)."""
    return {
        extension_id("token"): {
            "type": "drupal_extension", "boundary": True, "extension_type": "module",
            "extension_path": "web/modules/contrib/token", "realm": "contrib"},
        service_id("entity_type.manager"): {
            "type": "drupal_service", "boundary": True,
            "class_name": "Drupal\\Core\\Entity\\EntityTypeManager", "provider": "core",
            "realm": "core"},
        hook_id("cron"): {
            "type": "drupal_hook", "boundary": True, "hook_name": "cron", "provider": "core",
            "declared_file": "web/core/core.api.php", "line": 3, "realm": "core"},
        link_id("menu_link", "system.admin_config"): {
            "type": "drupal_menu_link", "boundary": True, "realm": "unknown"},
        type_id("menu.link"): {
            "type": "drupal_plugin_type", "boundary": True, "realm": "core",
            **_MENU_TYPE_FACTS},
    }


def _facts(node: dict, keys) -> dict:
    return {k: node.get(k) for k in keys}


def test_boundary_stubs_carry_the_registry_facts(tmp_path, _isolated_discovery_state):
    install()
    from graphify.extract import extract

    root = _boundary_site(tmp_path)
    prepare_run(root)
    result = extract(_custom_files(root), root=root)
    nodes = _by_id(result["nodes"])

    for nid, facts in _expected(root).items():
        assert nid in nodes, nid
        assert _facts(nodes[nid], facts) == facts, nid
    # A fixed hook name has no pattern.
    assert "pattern" not in nodes[hook_id("cron")]
    # Every resolver-made node says it is the boundary; nothing declared in the batch does.
    for n in result["nodes"]:
        assert bool(n.get("boundary")) == (n.get("file_type") == "concept"), n["id"]

    relations = {(e["source"], e["relation"], e["target"]) for e in result["edges"]}
    stub = link_id("menu_link", "system.admin_config")
    assert (stub, "plugin_of_type", type_id("menu.link")) in relations
    [typed] = [e for e in result["edges"]
               if e["source"] == stub and e["relation"] == "plugin_of_type"]
    assert typed["target_name"] == "menu.link"


@pytest.mark.parametrize("kind, node_type, family", [
    ("local_task", "drupal_local_task", "links.task"),
    ("local_action", "drupal_local_action", "links.action"),
    ("contextual_link", "drupal_contextual_link", "links.contextual"),
])
def test_every_link_family_stub_gets_its_learned_type(
        kind, node_type, family, _isolated_discovery_state):
    from graphify.drupal.discovery import PluginType, Registry, set_current
    from graphify.drupal.resolvers import resolve_missing_targets

    name = f"{kind}.type"
    set_current(Registry(web_root=None, types={name: PluginType(
        plugin_type=name, manager_class="Drupal\\X", class_file="/x/X.php", line=1,
        owner="core", registered=False, discovery="yaml", yaml_name=family)}))
    child, parent = link_id(kind, "foo.child"), link_id(kind, "core.parent")
    nodes = [{"id": child, "label": "Child", "type": node_type}]
    parent_edge = {"source": child, "target": parent, "relation": "parent_link",
                   "target_name": "core.parent", "source_file": "x/foo.yml"}
    # Two children of one parent: still one typing edge.
    edges = [parent_edge, {**parent_edge, "source": "other"}]
    resolve_missing_targets([], nodes, edges)

    stubs = _by_id(nodes)
    assert stubs[parent]["type"] == node_type and stubs[parent]["boundary"] is True
    typed = [e for e in edges if e["relation"] == "plugin_of_type"]
    assert [(e["source"], e["target"], e["target_name"]) for e in typed] == [
        (parent, type_id(name), name)]
    assert stubs[type_id(name)]["boundary"] is True


def test_without_a_registry_a_stub_is_still_the_boundary(tmp_path, _isolated_discovery_state):
    from graphify.drupal.resolvers import resolve_missing_targets

    nodes: list[dict] = []
    edges = [{"source": "drupal_extension_foo", "target": extension_id("token"),
              "relation": "depends_on_module", "target_name": "token",
              "source_file": "x/foo.info.yml"}]
    resolve_missing_targets([], nodes, edges)
    [stub] = nodes
    assert (stub["boundary"], stub["realm"], stub["label"]) == (True, "unknown", "token")
    assert "extension_type" not in stub and "extension_path" not in stub


def test_a_path_outside_the_scan_root_stays_absolute(tmp_path, _isolated_discovery_state):
    """`extension_path` is relative to the scan root only when inside it (spec §4.3)."""
    install()
    from graphify.extract import extract

    root = _boundary_site(tmp_path)
    prepare_run(root)
    # Scanning the custom module alone: contrib and core sit outside the root.
    result = extract(_custom_files(root), root=root / FOO)
    nodes = _by_id(result["nodes"])
    assert nodes[extension_id("token")]["extension_path"] == (root / "web/modules/contrib/token").as_posix()
    assert nodes[hook_id("cron")]["declared_file"] == (root / "web/core/core.api.php").as_posix()


def test_a_watch_style_extract_keeps_boundary_paths_relative(tmp_path, _isolated_discovery_state):
    """`graphify watch` calls `extract(..., cache_root=<watch root>)` with no
    `root`: boundary paths must still be relative to that anchor (final review I1)."""
    install()
    from graphify.extract import extract

    root = _boundary_site(tmp_path)
    prepare_run(root)
    by_root = _by_id(extract(_custom_files(root), root=root)["nodes"])
    by_cache_root = _by_id(extract(_custom_files(root), cache_root=root)["nodes"])

    for nid, key in ((extension_id("token"), "extension_path"), (hook_id("cron"), "declared_file")):
        assert by_cache_root[nid][key] == by_root[nid][key], nid
    assert by_cache_root[extension_id("token")]["extension_path"] == "web/modules/contrib/token"
    assert by_cache_root[hook_id("cron")]["declared_file"] == "web/core/core.api.php"


# -- the real CLI ------------------------------------------------------------------


_SUMMARY = re.compile(r"incremental summary: .*?(\d+) re-extracted")


def _cli(root: Path, out: Path) -> tuple[dict, int | None]:
    """Run `graphify extract --code-only --out`; the graph and the rerun count."""
    env = {k: v for k, v in os.environ.items() if k != "GRAPHIFY_DRUPAL_DISCOVERY"}
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--out", str(out)],
        capture_output=True, text=True, cwd=root, env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    graph = json.loads(next(out.rglob("graph.json")).read_text(encoding="utf-8"))
    found = _SUMMARY.search(proc.stdout)
    return graph, int(found.group(1)) if found else None


def test_boundary_facts_survive_incremental_reruns(tmp_path):
    """Stubs are carried by graph.json between runs: an unchanged rerun, a
    change of an unrelated file and a change of the referencing file each keep
    every fact (the last one re-materialises the stub from the registry)."""
    root = _boundary_site(tmp_path / "site")
    out = tmp_path / "out"
    expected = _expected(root)

    def check(graph: dict) -> None:
        nodes = _by_id(graph["nodes"])
        for nid, facts in expected.items():
            assert _facts(nodes[nid], facts) == facts, nid
        links = graph.get("links") or graph.get("edges")
        assert any(e["relation"] == "plugin_of_type"
                   and {e["source"], e["target"]} == {link_id("menu_link", "system.admin_config"),
                                                      type_id("menu.link")} for e in links)

    first, _ = _cli(root, out)
    check(first)
    second, rerun = _cli(root, out)
    check(second)
    assert rerun == 0

    (root / "web/modules/custom/bar/bar.info.yml").parent.mkdir(parents=True)
    (root / "web/modules/custom/bar/bar.info.yml").write_text(
        "name: Bar\ntype: module\n", encoding="utf-8")
    check(_cli(root, out)[0])

    for name in ("foo.info.yml", "foo.services.yml", "foo.module", "foo.links.menu.yml"):
        path = root / FOO / name
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    check(_cli(root, out)[0])


def _shadowed_site(root: Path) -> Path:
    """`foo.settings` in the sync store and shipped by foo's `config/install`:
    P1b's collapse gives the node to the sync copy, so the shipped file owns none.
    (The dependency keeps an edge in the graph whichever copy is left: core's
    clustering fails on an edgeless graph.)"""
    return _site(root, {
        "config/sync/core.extension.yml": "module:\n  foo: 0\ntheme: {}\n",
        "config/sync/foo.settings.yml": "enabled: true\n",
        f"{FOO}/foo.info.yml": "name: Foo\ntype: module\ndependencies:\n  - token\n",
        f"{FOO}/config/install/foo.settings.yml": "enabled: false\n",
    })


def test_an_unchanged_rerun_does_not_re_extract_a_shadowed_copy(tmp_path):
    root = _shadowed_site(tmp_path / "site")
    out = tmp_path / "out"
    first, _ = _cli(root, out)
    [settings] = [n for n in first["nodes"] if n.get("config_name") == "foo.settings"]
    assert f"{FOO}/config/install/foo.settings.yml" in settings["declared_in"]

    second, rerun = _cli(root, out)
    assert rerun == 0
    assert {n["id"] for n in second["nodes"]} == {n["id"] for n in first["nodes"]}


def test_deleting_the_winning_copy_re_extracts_the_shadowed_one(tmp_path):
    """The old graph's `declared_in` names the install copy, but its only
    other declaring file is gone: core's heal must re-queue the copy."""
    root = _shadowed_site(tmp_path / "site")
    out = tmp_path / "out"
    _cli(root, out)
    (root / "config/sync/foo.settings.yml").unlink()
    second, _ = _cli(root, out)
    [settings] = [n for n in second["nodes"] if n.get("config_name") == "foo.settings"]
    assert settings["source_file"] == f"{FOO}/config/install/foo.settings.yml"


# -- the cli wrapper ---------------------------------------------------------------


def _graph(path: Path, nodes: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")
    return path


def _fake_cli(result: list[str]):
    from types import ModuleType

    module = ModuleType("fake_cli")
    module._zero_node_stamped_code_sources = lambda graph_path, scan_root, unchanged: list(result)
    _patch_cli(module)
    return module._zero_node_stamped_code_sources


def test_the_wrapper_drops_only_declared_in_paths(tmp_path):
    root = tmp_path / "site"
    shadowed = (root / "a/config/install/x.yml").as_posix()
    other = (root / "b/b.info.yml").as_posix()
    graph = _graph(tmp_path / "out/graph.json", [
        {"id": "n", "declared_in": ["a/config/install/x.yml", "config/sync/x.yml"]}])
    heal = _fake_cli([shadowed, other])
    # The winning copy is gone: the shadowed one is left to core's heal.
    assert heal(graph, root, [shadowed, other]) == [shadowed, other]
    winner = root / "config/sync/x.yml"
    winner.parent.mkdir(parents=True)
    winner.write_text("a: 1\n", encoding="utf-8")
    assert heal(graph, root, [shadowed, other]) == [other]


def test_the_wrapper_keeps_the_result_when_the_graph_is_unreadable(tmp_path):
    root = tmp_path / "site"
    path = (root / "a.yml").as_posix()
    heal = _fake_cli([path])
    assert heal(tmp_path / "missing.json", root, [path]) == [path]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert heal(bad, root, [path]) == [path]


def test_a_missing_heal_function_is_a_seam_error():
    from types import ModuleType

    with pytest.raises(DrupalSeamError, match="_zero_node_stamped_code_sources"):
        _patch_cli(ModuleType("fake_cli"))


def test_the_real_cli_function_is_wrapped():
    install()
    import graphify.cli as cli

    assert getattr(cli._zero_node_stamped_code_sources, "_drupal_patched", False)
