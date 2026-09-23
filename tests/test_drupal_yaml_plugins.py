"""YAML-discovered plugins of a learned plugin type (P2a Task 4, spec §4.1/§4.2/§5.2)."""
from __future__ import annotations

import pytest

from graphify.drupal.discovery import current_registry, prepare_run, set_current, type_id
from graphify.drupal.families import is_drupal_file
from graphify.drupal.resolvers import resolve_missing_targets
from graphify.drupal.yaml_common import link_id, plugin_id
from graphify.drupal.yaml_extract import extension_id
from graphify.drupal.yaml_links import extract_drupal_menu_links
from graphify.drupal.yaml_plugins import (
    extract_drupal_yaml_plugins,
    learned_family,
    plugin_type_for_family,
)
from tests.test_drupal_discovery import (
    CORE_SERVICES,
    MENU_LINK_INTERFACE,
    MENU_LINK_MANAGER,
    YAML_MANAGER,
    _module,
    _site,
)
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401 (reused fixture)

FOO_BAR = (
    "one:\n"
    "  class: Drupal\\foo\\One\n"
    "  label: One\n"
    "two:\n"
    "  deriver: Drupal\\foo\\D\n"
    "  weight: 3\n"
)

BAR_SERVICES = "services:\n  plugin.manager.bar:\n    class: Drupal\\bar\\BarManager\n"

DEFERRED_MANAGER = r"""<?php
namespace Drupal\baz;

use Drupal\Core\Plugin\Discovery\YamlDiscoveryDecorator;
use Drupal\Core\Plugin\Discovery\YamlDiscovery;

class BazManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  protected function getDiscovery() {
    if (!$this->discovery) {
      $discovery = new AnnotatedClassDiscovery($this->subdir, $this->namespaces);
      $discovery = new YamlDiscoveryDecorator($discovery, 'component', $this->moduleHandler->getModuleDirectories());
      $this->discovery = new ContainerDerivativeDiscoveryDecorator($discovery);
    }
    return $this->discovery;
  }
}
"""

_UNIVERSAL = {
    "id", "label", "file_type", "type", "layer", "realm",
    "_origin", "source_file", "source_location",
}


def _bar_and_foo_site(tmp_path):
    files = {
        **_module("bar", BAR_SERVICES, {"src/BarManager.php": YAML_MANAGER}),
        **_module("foo", "services: {}\n"),
        "web/modules/custom/foo/foo.bar.yml": FOO_BAR,
    }
    return _site(tmp_path, files)


def test_two_plugin_nodes_and_their_edges(tmp_path, _isolated_discovery_state):
    root = _bar_and_foo_site(tmp_path)
    prepare_run(root)
    path = root / "web/modules/custom/foo/foo.bar.yml"

    result = extract_drupal_yaml_plugins(path)
    by_pid = {n["plugin_id"]: n for n in result["nodes"]}
    assert set(by_pid) == {"one", "two"}

    one, two = by_pid["one"], by_pid["two"]
    assert one["label"] == "one"          # not the YAML `label: One`
    assert one["plugin_type"] == "bar"
    assert one["provider"] == "foo"
    assert one["class_name"] == "Drupal\\foo\\One"
    assert set(one) - _UNIVERSAL == {"plugin_id", "plugin_type", "provider", "class_name"}

    assert two["label"] == "two"
    assert two["deriver"] == "Drupal\\foo\\D"
    assert set(two) - _UNIVERSAL == {"plugin_id", "plugin_type", "provider", "deriver"}

    rel = {(e["source"], e["relation"], e["target"]) for e in result["edges"]}
    tid = type_id("bar")
    assert (extension_id("foo"), "provides_plugin", plugin_id("bar", "one")) in rel
    assert (extension_id("foo"), "provides_plugin", plugin_id("bar", "two")) in rel
    assert (plugin_id("bar", "one"), "plugin_of_type", tid) in rel
    assert (plugin_id("bar", "two"), "plugin_of_type", tid) in rel


def test_learned_family_none_outside_the_extension_root(tmp_path, _isolated_discovery_state):
    root = _bar_and_foo_site(tmp_path)
    prepare_run(root)

    outside = root / "elsewhere" / "foo.bar.yml"
    outside.parent.mkdir(parents=True)
    outside.write_text(FOO_BAR, encoding="utf-8")
    assert learned_family(outside) is None


def test_learned_family_none_for_a_p1_family_file(tmp_path, _isolated_discovery_state):
    root = _bar_and_foo_site(tmp_path)
    prepare_run(root)

    p1 = root / "web/modules/custom/foo/foo.links.menu.yml"
    p1.write_text("foo.admin:\n  title: Foo\n  route_name: foo.settings\n", encoding="utf-8")
    assert learned_family(p1) is None


def test_learned_family_none_for_a_deferred_family(tmp_path, _isolated_discovery_state):
    files = {
        **_module("baz", "services:\n  plugin.manager.baz:\n    class: Drupal\\baz\\BazManager\n",
                  {"src/BazManager.php": DEFERRED_MANAGER}),
        **_module("foo", "services: {}\n"),
    }
    root = _site(tmp_path, files)
    registry = prepare_run(root)
    assert registry.types["baz"].deferred_to == "P5"

    component = root / "web/modules/custom/foo/foo.component.yml"
    component.write_text("one:\n  class: Drupal\\foo\\One\n", encoding="utf-8")
    assert learned_family(component) is None


def test_learned_family_none_without_a_registry(tmp_path, _isolated_discovery_state):
    root = _bar_and_foo_site(tmp_path)
    # Registry never built/set for this process.
    path = root / "web/modules/custom/foo/foo.bar.yml"
    assert current_registry() is None
    assert learned_family(path) is None


def test_is_drupal_file_true_only_while_the_registry_is_set(tmp_path, _isolated_discovery_state):
    import os

    from graphify.drupal.discovery import ENV_VAR

    root = _bar_and_foo_site(tmp_path)
    path = root / "web/modules/custom/foo/foo.bar.yml"

    assert is_drupal_file(path) is False
    prepare_run(root)
    assert is_drupal_file(path) is True

    # Undo everything `prepare_run` set, the way `_isolated_discovery_state`
    # itself will at teardown, so the mid-test check reflects "no registry".
    set_current(None, None)
    os.environ.pop(ENV_VAR, None)
    assert is_drupal_file(path) is False


def _menu_site(tmp_path):
    return _site(tmp_path, {
        "web/core/core.services.yml": CORE_SERVICES,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManagerInterface.php": MENU_LINK_INTERFACE,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManager.php": MENU_LINK_MANAGER,
    })


def test_p1_menu_links_get_plugin_of_type_only_with_a_registry(tmp_path, _isolated_discovery_state):
    menu_path = tmp_path / "web/modules/custom/foo/foo.links.menu.yml"
    menu_path.parent.mkdir(parents=True)
    menu_path.write_text("foo.admin:\n  title: Foo\n  route_name: foo.settings\n", encoding="utf-8")

    without_registry = extract_drupal_menu_links(menu_path)
    assert not any(e["relation"] == "plugin_of_type" for e in without_registry["edges"])

    root = _menu_site(tmp_path)
    prepare_run(root)
    assert plugin_type_for_family("links.menu") == type_id("menu.link")

    with_registry = extract_drupal_menu_links(menu_path)
    lid = link_id("menu_link", "foo.admin")
    rel = {(e["source"], e["relation"], e["target"]) for e in with_registry["edges"]}
    assert (lid, "plugin_of_type", type_id("menu.link")) in rel


def test_resolver_materialises_a_missing_plugin_type():
    source = plugin_id("bar", "one")
    tid = type_id("bar")
    nodes = [{"id": source, "type": "drupal_plugin", "label": "one"}]
    edges = [{"source": source, "relation": "plugin_of_type", "target": tid, "target_name": "bar",
              "source_file": "web/modules/custom/foo/foo.bar.yml", "confidence": "EXTRACTED"}]
    resolve_missing_targets([], nodes, edges)
    created = next(n for n in nodes if n["id"] == tid)
    assert created["type"] == "drupal_plugin_type"
    assert created["layer"] == "plugin"
    assert created["missing"] is True
    assert created["external"] is True


def test_pipeline_has_no_dangling_endpoints_for_the_new_relations(tmp_path, _isolated_discovery_state):
    from graphify.drupal.register import install
    from graphify.extract import extract

    install()
    files = {
        **_module("bar", BAR_SERVICES, {"src/BarManager.php": YAML_MANAGER}),
        "web/core/core.services.yml": CORE_SERVICES,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManagerInterface.php": MENU_LINK_INTERFACE,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManager.php": MENU_LINK_MANAGER,
        "web/modules/custom/foo/foo.info.yml": "name: Foo\ntype: module\n",
        "web/modules/custom/foo/foo.bar.yml": FOO_BAR,
        "web/modules/custom/foo/foo.links.menu.yml":
            "foo.admin:\n  title: Foo\n  route_name: foo.settings\n",
    }
    root = _site(tmp_path, files)
    prepare_run(root)

    paths = [
        root / "web/modules/custom/foo/foo.info.yml",
        root / "web/modules/custom/foo/foo.bar.yml",
        root / "web/modules/custom/foo/foo.links.menu.yml",
    ]
    result = extract(paths, root=root)
    ids = {n["id"] for n in result["nodes"]}
    new_relations = {"provides_plugin", "plugin_of_type"}
    dangling = [
        e for e in result["edges"]
        if e.get("relation") in new_relations
        and (e.get("source") not in ids or e.get("target") not in ids)
    ]
    assert dangling == []
    # And the pipeline really produced both kinds of new edges to check.
    seen = {e["relation"] for e in result["edges"]}
    assert new_relations <= seen
