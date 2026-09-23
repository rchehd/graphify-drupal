"""Plugin-type nodes from their manager class files (P2a Task 5, spec §4.1/§4.2/§5.4)."""
from __future__ import annotations

from graphify.drupal.discovery import (
    current_registry,
    extract_plugin_types,
    is_manager_class_file,
    prepare_run,
    type_id,
)
from graphify.drupal.register import install
from graphify.drupal.resolvers import resolve_missing_targets
from graphify.drupal.yaml_common import service_id
from graphify.drupal.yaml_extract import extension_id
from graphify.extract import extract
from tests.test_drupal_discovery import (
    CORE_SERVICES,
    D11_MANAGER,
    FOO_SERVICES,
    MENU_LINK_INTERFACE,
    MENU_LINK_MANAGER,
    _module,
    _site,
)
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

_UNIVERSAL = {
    "id", "label", "file_type", "type", "layer", "realm",
    "_origin", "source_file", "source_location",
}

QUX_MANAGER = r"""<?php
namespace Drupal\qux\Plugin;
use Drupal\Core\Plugin\DefaultPluginManager;
class QuxManager extends DefaultPluginManager {
  public function __construct($namespaces, $module_handler) {
    parent::__construct('Plugin/Qux', $namespaces, $module_handler);
  }
}
"""

PLAIN_PHP = "<?php\nnamespace Drupal\\foo;\nclass NotAManager {}\n"


def _foo_site(tmp_path):
    return _site(tmp_path, _module("foo", FOO_SERVICES, {"src/FooManager.php": D11_MANAGER}))


def test_extract_plugin_types_direct_call(tmp_path, _isolated_discovery_state):
    root = _foo_site(tmp_path)
    prepare_run(root)
    manager_path = root / "web/modules/custom/foo/src/FooManager.php"
    tid = type_id("foo")

    assert is_manager_class_file(manager_path) is True

    result = extract_plugin_types(manager_path)
    assert len(result["nodes"]) == 1
    t = result["nodes"][0]
    assert t["id"] == tid
    assert t["label"] == "foo"
    assert t["type"] == "drupal_plugin_type"
    assert t["layer"] == "plugin"
    assert t["source_location"] == "L10"
    assert set(t) - _UNIVERSAL == {
        "plugin_type", "discovery", "manager_class", "registered",
        "manager_service", "subdir", "interface", "annotation_class",
        "attribute_class", "alter_hook",
    }
    assert t["plugin_type"] == "foo"
    assert t["manager_class"] == "Drupal\\foo\\FooManager"
    assert t["manager_service"] == "plugin.manager.foo"
    assert t["registered"] is True
    assert t["discovery"] == "mixed"
    assert t["subdir"] == "Plugin/Foo"
    assert t["interface"] == "Drupal\\foo\\FooInterface"
    assert t["annotation_class"] == "Drupal\\foo\\Annotation\\Foo"
    assert t["attribute_class"] == "Drupal\\foo\\Attribute\\Foo"
    assert t["alter_hook"] == "foo_info"

    rel = {(e["source"], e["relation"], e["target"]) for e in result["edges"]}
    assert (extension_id("foo"), "defines_plugin_type", tid) in rel
    assert (service_id("plugin.manager.foo"), "plugin_manager_for", tid) in rel
    defines_edge = next(e for e in result["edges"] if e["relation"] == "defines_plugin_type")
    assert defines_edge["owner"] == "foo"
    assert defines_edge["target_name"] == "foo"
    manager_edge = next(e for e in result["edges"] if e["relation"] == "plugin_manager_for")
    assert manager_edge["source_name"] == "plugin.manager.foo"
    assert manager_edge["target_name"] == "foo"


def test_a_non_manager_php_file_yields_no_type_node(tmp_path, _isolated_discovery_state):
    root = _foo_site(tmp_path)
    other = root / "web/modules/custom/foo/src/NotAManager.php"
    other.write_text(PLAIN_PHP, encoding="utf-8")
    prepare_run(root)

    assert is_manager_class_file(other) is False
    result = extract_plugin_types(other)
    assert result == {"nodes": [], "edges": []}


def test_no_registry_yields_no_type_node(tmp_path, _isolated_discovery_state):
    root = _foo_site(tmp_path)
    manager_path = root / "web/modules/custom/foo/src/FooManager.php"
    # Registry never built/set for this process.
    assert current_registry() is None
    assert is_manager_class_file(manager_path) is False
    assert extract_plugin_types(manager_path) == {"nodes": [], "edges": []}


def test_second_net_manager_is_unregistered_and_has_no_plugin_manager_for(tmp_path, _isolated_discovery_state):
    root = _site(tmp_path, _module("qux", "services: {}\n", {"src/Plugin/QuxManager.php": QUX_MANAGER}))
    prepare_run(root)
    manager_path = root / "web/modules/custom/qux/src/Plugin/QuxManager.php"

    result = extract_plugin_types(manager_path)
    assert len(result["nodes"]) == 1
    t = result["nodes"][0]
    assert t["registered"] is False
    assert "manager_service" not in t

    rel = {e["relation"] for e in result["edges"]}
    assert rel == {"defines_plugin_type"}


def test_pipeline_keeps_core_php_nodes_and_adds_the_type(tmp_path, _isolated_discovery_state):
    install()
    root = _foo_site(tmp_path)
    prepare_run(root)
    manager_path = root / "web/modules/custom/foo/src/FooManager.php"

    result = extract([manager_path], root=root)
    assert any(n.get("label") == "FooManager" for n in result["nodes"])

    tid = type_id("foo")
    type_nodes = [n for n in result["nodes"] if n["id"] == tid]
    assert len(type_nodes) == 1
    assert type_nodes[0]["type"] == "drupal_plugin_type"

    rel = {(e["source"], e["relation"], e["target"]) for e in result["edges"]}
    assert (extension_id("foo"), "defines_plugin_type", tid) in rel
    assert (service_id("plugin.manager.foo"), "plugin_manager_for", tid) in rel


def test_a_core_lib_managers_owner_resolves_to_core(tmp_path, _isolated_discovery_state):
    """A manager-only incremental run (only `.php` in the batch): the resolver
    must still run so `defines_plugin_type`'s source (`extension_id("core")`,
    declared by nothing since `core` has no `*.info.yml`) is materialised, and
    the `plugin_manager_for` source (the P1 service node, never extracted in
    this batch) is materialised as a missing `drupal_service`."""
    install()
    root = _site(tmp_path, {
        "web/core/core.services.yml": CORE_SERVICES,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManagerInterface.php": MENU_LINK_INTERFACE,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManager.php": MENU_LINK_MANAGER,
    })
    prepare_run(root)
    manager_path = root / "web/core/lib/Drupal/Core/Menu/MenuLinkManager.php"

    result = extract([manager_path], root=root)
    by_id = {n["id"]: n for n in result["nodes"]}

    core_ext_id = extension_id("core")
    assert core_ext_id in by_id
    assert by_id[core_ext_id]["type"] == "drupal_extension"

    svc_id = service_id("plugin.manager.menu.link")
    assert svc_id in by_id
    assert by_id[svc_id]["type"] == "drupal_service"
    assert by_id[svc_id]["missing"] is True


def test_resolver_runs_without_php_suffix_regression_guard(tmp_path, _isolated_discovery_state):
    """Direct unit check on the resolver itself (independent of the pipeline
    suffix wiring): given the edges a manager-only extraction would produce,
    `resolve_missing_targets` materialises both missing endpoints."""
    root = _foo_site(tmp_path)
    prepare_run(root)
    manager_path = root / "web/modules/custom/foo/src/FooManager.php"

    result = extract_plugin_types(manager_path)
    nodes = list(result["nodes"])
    edges = list(result["edges"])
    resolve_missing_targets([], nodes, edges)

    by_id = {n["id"]: n for n in nodes}
    assert extension_id("foo") in by_id
    assert by_id[extension_id("foo")]["label"] == "foo"
    assert service_id("plugin.manager.foo") in by_id
    assert by_id[service_id("plugin.manager.foo")]["missing"] is True
