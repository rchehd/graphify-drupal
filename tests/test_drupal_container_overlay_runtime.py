"""The container overlay: hook implementations, plugins and event subscribers
laid over the assembled graph (P3 spec S7.4)."""
from __future__ import annotations

import copy

import networkx as nx
import pytest

from graphify.build import build_from_json
from graphify.drupal import boundary
from graphify.drupal.container import Artifact
from graphify.drupal.container_overlay import ORIGIN, apply, undo
from graphify.drupal.discovery import type_id
from graphify.drupal.hooks import hook_id, hook_impl_id
from graphify.drupal.yaml_common import plugin_id
from graphify.drupal.yaml_extract import extension_id
from graphify.ids import make_id
from tests.test_drupal_container_overlay import (
    FOO,
    _artifact_data,
    _dump,
    _e,
    _edge,
    _extraction,
    _n,
    _write_graph_json,
)
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

MODULE_FILE = f"{FOO}/foo.module"
HOOKS_FILE = f"{FOO}/src/Hook/FooHooks.php"
BLOCK_FILE = f"{FOO}/src/Plugin/Block/FooBlock.php"
DERIVER_FILE = f"{FOO}/src/Plugin/Derivative/FooDeriver.php"
SUBSCRIBER_FILE = f"{FOO}/src/EventSubscriber/FooSubscriber.php"
SYSTEM = "web/core/modules/system"

FORM_ALTER_FN = "foo_foo_form_alter"
HOOKS_CLASS = "foohooks_foohooks"
TOOLBAR = "foohooks_foohooks_toolbar"
BLOCK_CLASS = "fooblock_fooblock"
DERIVER_CLASS = "fooderiver_fooderiver"
SUBSCRIBER_CLASS = "foosubscriber_foosubscriber"

FORM_ALTER_IMPL = hook_impl_id("foo", "form_alter")
TOOLBAR_IMPL = hook_impl_id("foo", "toolbar")
DERIVATIVE = plugin_id("block", "foo_block:bar")
REQUEST_EVENT = make_id("drupal", "event", "kernel.request")


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state):  # noqa: F811
    boundary.clear_caches()
    yield
    boundary.clear_caches()


def _runtime_extraction() -> dict:
    """Task 4's module `foo`, plus what P2b and core's PHP extractor emit for
    its procedural `foo_form_alter`, and the class nodes of a hook class, a
    block plugin, its deriver and an event subscriber."""
    nodes = [
        _n("foo_module", "foo.module", MODULE_FILE),
        _n(FORM_ALTER_FN, "foo_form_alter()", MODULE_FILE),
        _n(FORM_ALTER_IMPL, "foo:form_alter", MODULE_FILE, type="drupal_hook_impl", layer="hook",
           realm="custom", module="foo", hook_name="form_alter", function="foo_form_alter",
           via="procedural", _origin="static_yaml"),
        {"id": hook_id("form_alter"), "label": "form_alter", "file_type": "concept",
         "type": "drupal_hook", "layer": "hook", "realm": "core", "boundary": True,
         "external": True, "hook_name": "form_alter", "_origin": "static_yaml",
         "source_file": MODULE_FILE, "source_location": "L1"},
        _n("foohooks", "FooHooks.php", HOOKS_FILE),
        _n(HOOKS_CLASS, "FooHooks", HOOKS_FILE),
        _n(TOOLBAR, ".toolbar()", HOOKS_FILE, source_location="L7"),
        _n("fooblock", "FooBlock.php", BLOCK_FILE),
        _n(BLOCK_CLASS, "FooBlock", BLOCK_FILE),
        _n("fooderiver", "FooDeriver.php", DERIVER_FILE),
        _n(DERIVER_CLASS, "FooDeriver", DERIVER_FILE),
        _n("foosubscriber", "FooSubscriber.php", SUBSCRIBER_FILE),
        _n(SUBSCRIBER_CLASS, "FooSubscriber", SUBSCRIBER_FILE),
    ]
    edges = [
        _e("foo_module", FORM_ALTER_FN, "contains", MODULE_FILE),
        _e(extension_id("foo"), hook_id("form_alter"), "implements_hook", MODULE_FILE,
           _origin="static_yaml", owner="foo", target_name="form_alter"),
        _e(FORM_ALTER_IMPL, FORM_ALTER_FN, "hook_implemented_by", MODULE_FILE, _origin="static_yaml"),
        _e("foohooks", HOOKS_CLASS, "contains", HOOKS_FILE),
        _e(HOOKS_CLASS, TOOLBAR, "method", HOOKS_FILE),
        _e("fooblock", BLOCK_CLASS, "contains", BLOCK_FILE),
        _e("fooderiver", DERIVER_CLASS, "contains", DERIVER_FILE),
        _e("foosubscriber", SUBSCRIBER_CLASS, "contains", SUBSCRIBER_FILE),
    ]
    return _extraction(extra_nodes=nodes, extra_edges=edges)


def _runtime_data() -> dict:
    data = _artifact_data()
    data["extensions"].append({"name": "system", "type": "module", "path": SYSTEM, "status": 1,
                               "weight": 0, "dependencies": []})
    data["hooks"] = {
        "form_alter": [
            {"module": "system", "callable": "system_form_alter", "file": f"{SYSTEM}/system.module"},
            {"module": "foo", "callable": "foo_form_alter", "file": MODULE_FILE},
        ],
        "toolbar": [
            {"module": "foo", "callable": "Drupal\\foo\\Hook\\FooHooks::toolbar", "file": HOOKS_FILE},
        ],
    }
    data["plugins"] = {
        "block": [
            {"id": "foo_block:bar", "class": "Drupal\\foo\\Plugin\\Block\\FooBlock", "file": BLOCK_FILE,
             "provider": "foo", "deriver": "Drupal\\foo\\Plugin\\Derivative\\FooDeriver",
             "base_plugin_id": "foo_block"},
            {"id": "system_branding_block", "class": "Drupal\\system\\Plugin\\Block\\SystemBrandingBlock",
             "file": f"{SYSTEM}/src/Plugin/Block/SystemBrandingBlock.php", "provider": "system",
             "deriver": None, "base_plugin_id": None},
        ],
    }
    data["subscribers"] = {
        "kernel.request": [
            {"callable": "Drupal\\Core\\EventSubscriber\\CoreSubscriber::onRequest",
             "file": "web/core/lib/Drupal/Core/EventSubscriber/CoreSubscriber.php", "priority": 300},
            {"callable": "Drupal\\foo\\EventSubscriber\\FooSubscriber::onRequest",
             "file": SUBSCRIBER_FILE, "priority": 30},
        ],
    }
    return data


def _graph() -> nx.Graph:
    return build_from_json(_runtime_extraction())


def _artifact(root, data=None) -> Artifact:
    return Artifact(data=data if data is not None else _runtime_data(), path=root / "drupal-container.json")


# -- hooks ----------------------------------------------------------------------


def test_a_static_impl_gets_its_runtime_order_and_its_implementation_is_confirmed(tmp_path):
    G = _graph()
    result = apply(G, _artifact(tmp_path), tmp_path)
    impl = G.nodes[FORM_ALTER_IMPL]
    assert impl["runtime_order"] == 1
    assert "runtime_order" in impl["_overlay_attrs"]
    assert "order" not in impl
    assert "_overlay" not in impl
    assert impl["runtime"] == "present"
    assert _edge(G, FORM_ALTER_IMPL, FORM_ALTER_FN)["confirmed_by"] == ORIGIN
    assert _edge(G, extension_id("foo"), hook_id("form_alter"))["confirmed_by"] == ORIGIN
    # The core implementation annotates nothing it would have to create.
    assert hook_impl_id("system", "form_alter") not in G
    assert result.counts["hooks"]["total"] == 3
    assert result.counts["hooks"]["custom"] == 2


def test_a_theme_hook_impl_gets_no_runtime_mark(tmp_path):
    """FormsRemote's `govuk_forms_theme()`: the collector reads module hook
    lists (`hook_data`), and a theme's implementations are not in them (the
    theme registry calls them). The container does not know either way, so
    the node gets no `runtime` and no `static_only` record."""
    from graphify.drupal.divergence import compute

    theme_file = "web/themes/custom/bartheme/bartheme.theme"
    impl = hook_impl_id("bartheme", "theme_suggestions_alter")
    extraction = _runtime_extraction()
    extraction["nodes"].append(_n(impl, "bartheme:theme_suggestions_alter", theme_file,
                                  type="drupal_hook_impl", layer="hook", realm="custom",
                                  module="bartheme", hook_name="theme_suggestions_alter",
                                  function="bartheme_theme_suggestions_alter", via="procedural",
                                  _origin="static_yaml"))
    data = _runtime_data()
    data["extensions"].append({"name": "bartheme", "type": "theme", "path": "web/themes/custom/bartheme",
                               "status": 1, "weight": 0, "dependencies": []})
    G = build_from_json(extraction)
    artifact = _artifact(tmp_path, data)
    result = apply(G, artifact, tmp_path)
    assert "runtime" not in G.nodes[impl]
    assert G.nodes[FORM_ALTER_IMPL]["runtime"] == "present"
    assert "drupal_hook_impl" not in result.runtime_absent
    assert [r for r in compute(G, result, artifact, tmp_path) if r["subject"] == impl] == []


def test_a_container_only_class_method_impl_is_created_with_both_edges(tmp_path):
    G = _graph()
    apply(G, _artifact(tmp_path), tmp_path)
    impl = G.nodes[TOOLBAR_IMPL]
    assert impl["_overlay"] is True
    assert impl["type"] == "drupal_hook_impl" and impl["layer"] == "hook"
    assert impl["label"] == "foo:toolbar"
    assert impl["module"] == "foo" and impl["hook_name"] == "toolbar"
    assert impl["class_name"] == "FooHooks" and impl["method"] == "toolbar"
    assert "function" not in impl
    assert impl["realm"] == "custom"
    assert impl["source_file"] == HOOKS_FILE
    assert impl["_origin"] == "ast" and impl["origin"] == ORIGIN
    assert impl["runtime_order"] == 0
    # Its line is the method's, not a made-up L1.
    assert impl["source_location"] == "L7"
    assert "boundary" not in impl
    by = _edge(G, TOOLBAR_IMPL, TOOLBAR)
    assert by["relation"] == "hook_implemented_by" and by["origin"] == ORIGIN
    implements = _edge(G, extension_id("foo"), hook_id("toolbar"))
    assert implements["relation"] == "implements_hook" and implements["origin"] == ORIGIN
    hook = G.nodes[hook_id("toolbar")]
    assert hook["type"] == "drupal_hook" and hook["layer"] == "hook"
    assert hook["boundary"] is True and hook["_overlay"] is True
    assert hook["hook_name"] == "toolbar"


def test_a_procedural_impl_without_a_file_binds_under_its_module(tmp_path):
    data = _runtime_data()
    data["hooks"]["form_alter"][1]["file"] = None
    G = _graph()
    apply(G, _artifact(tmp_path, data), tmp_path)
    assert _edge(G, FORM_ALTER_IMPL, FORM_ALTER_FN)["confirmed_by"] == ORIGIN


def test_a_procedural_call_identifier_is_the_function(tmp_path):
    data = _runtime_data()
    data["hooks"]["form_alter"][1]["callable"] = "Drupal\\Core\\Extension\\ProceduralCall::foo_form_alter"
    data["hooks"]["form_alter"][1]["file"] = "web/core/lib/Drupal/Core/Extension/ProceduralCall.php"
    data["hooks"]["form_alter"].append({
        "module": "foo", "callable": "Drupal\\Core\\Extension\\ProceduralCall::foo_menu_alter",
        "file": "web/core/lib/Drupal/Core/Extension/ProceduralCall.php"})
    data["hooks"]["menu_alter"] = [data["hooks"]["form_alter"].pop()]
    extraction = _runtime_extraction()
    extraction["nodes"].append(_n("foo_foo_menu_alter", "foo_menu_alter()", MODULE_FILE))
    extraction["edges"].append(_e("foo_module", "foo_foo_menu_alter", "contains", MODULE_FILE))
    G = build_from_json(extraction)
    apply(G, _artifact(tmp_path, data), tmp_path)
    assert _edge(G, FORM_ALTER_IMPL, FORM_ALTER_FN)["confirmed_by"] == ORIGIN
    created = G.nodes[hook_impl_id("foo", "menu_alter")]
    assert created["function"] == "foo_menu_alter"
    # Where the function is, not ProceduralCall.php.
    assert created["source_file"] == MODULE_FILE
    assert _edge(G, hook_impl_id("foo", "menu_alter"), "foo_foo_menu_alter")["relation"] == "hook_implemented_by"


def test_a_declared_order_and_the_runtime_order_coexist(tmp_path):
    extraction = _runtime_extraction()
    for node in extraction["nodes"]:
        if node["id"] == FORM_ALTER_IMPL:
            node["order"] = "Order::Last"
    static = build_from_json(extraction)
    G = copy.deepcopy(static)
    result = apply(G, _artifact(tmp_path), tmp_path)
    impl = G.nodes[FORM_ALTER_IMPL]
    assert impl["order"] == "Order::Last"
    assert impl["runtime_order"] == 1
    assert "order" not in impl["_overlay_attrs"]
    assert not [c for c in result.conflicts if "order" in c["relation"]]
    undo(G)
    assert G.nodes[FORM_ALTER_IMPL]["order"] == "Order::Last"
    assert "runtime_order" not in G.nodes[FORM_ALTER_IMPL]
    assert _dump(G) == _dump(static)


def test_a_boundary_impl_only_annotates_an_existing_node(tmp_path):
    system_impl = hook_impl_id("system", "form_alter")
    extra = _n(system_impl, "system:form_alter", f"{SYSTEM}/system.module", type="drupal_hook_impl",
               layer="hook", realm="core", module="system", hook_name="form_alter",
               function="system_form_alter", _origin="static_yaml")
    extraction = _runtime_extraction()
    extraction["nodes"].append(extra)
    G = build_from_json(extraction)
    before = set(G.edges(system_impl))
    apply(G, _artifact(tmp_path), tmp_path)
    assert G.nodes[system_impl]["runtime_order"] == 0
    assert set(G.edges(system_impl)) == before


# -- plugins --------------------------------------------------------------------


def test_a_custom_derivative_plugin_is_created_with_its_edges(tmp_path):
    G = _graph()
    result = apply(G, _artifact(tmp_path), tmp_path)
    plugin = G.nodes[DERIVATIVE]
    assert plugin["_overlay"] is True
    assert plugin["type"] == "drupal_plugin" and plugin["layer"] == "plugin"
    assert plugin["plugin_type"] == "block"
    assert plugin["class_name"] == "Drupal\\foo\\Plugin\\Block\\FooBlock"
    assert plugin["provider"] == "foo"
    assert plugin["deriver"] == "Drupal\\foo\\Plugin\\Derivative\\FooDeriver"
    assert plugin["base_plugin_id"] == "foo_block"
    assert plugin["derivative"] is True
    assert plugin["realm"] == "custom"
    assert plugin["runtime"] == "present"
    assert _edge(G, DERIVATIVE, type_id("block"))["relation"] == "plugin_of_type"
    stub = G.nodes[type_id("block")]
    assert stub["type"] == "drupal_plugin_type" and stub["boundary"] is True and stub["_overlay"] is True
    assert _edge(G, extension_id("foo"), DERIVATIVE)["relation"] == "provides_plugin"
    assert _edge(G, DERIVATIVE, BLOCK_CLASS)["relation"] == "plugin_implemented_by"
    assert _edge(G, DERIVATIVE, DERIVER_CLASS)["relation"] == "derives_plugins"
    assert result.counts["plugins"] == {"total": 2, "custom": 1, "applied": 1}


def test_a_plugin_that_is_its_own_base_is_no_derivative(tmp_path):
    data = _runtime_data()
    data["plugins"]["block"][0].update(id="foo_block", base_plugin_id=None, deriver=None)
    G = _graph()
    apply(G, _artifact(tmp_path, data), tmp_path)
    plugin = G.nodes[plugin_id("block", "foo_block")]
    assert "derivative" not in plugin and "deriver" not in plugin
    assert not G.has_edge(plugin_id("block", "foo_block"), DERIVER_CLASS)


def test_a_core_block_plugin_creates_nothing(tmp_path):
    G = _graph()
    apply(G, _artifact(tmp_path), tmp_path)
    assert plugin_id("block", "system_branding_block") not in G
    assert extension_id("system") not in G


def test_a_custom_file_plugin_of_a_core_provider_makes_no_provider_stub(tmp_path):
    data = _runtime_data()
    data["plugins"]["block"][0]["provider"] = "system"
    G = _graph()
    apply(G, _artifact(tmp_path, data), tmp_path)
    assert DERIVATIVE in G
    assert extension_id("system") not in G
    assert _edge(G, DERIVATIVE, BLOCK_CLASS)["relation"] == "plugin_implemented_by"


def test_a_static_plugin_node_is_confirmed_not_recreated(tmp_path):
    yml = f"{FOO}/foo.block.yml"
    extraction = _runtime_extraction()
    extraction["nodes"].append(_n(DERIVATIVE, "foo_block:bar", yml, type="drupal_plugin", layer="plugin",
                                  realm="custom", plugin_type="block", provider="foo",
                                  _origin="static_yaml"))
    extraction["edges"].append(_e(extension_id("foo"), DERIVATIVE, "provides_plugin", yml,
                                  _origin="static_yaml"))
    G = build_from_json(extraction)
    apply(G, _artifact(tmp_path), tmp_path)
    plugin = G.nodes[DERIVATIVE]
    assert "_overlay" not in plugin and plugin["source_file"] == yml
    assert plugin["derivative"] is True and "derivative" in plugin["_overlay_attrs"]
    assert _edge(G, extension_id("foo"), DERIVATIVE)["confirmed_by"] == ORIGIN


# -- subscribers ----------------------------------------------------------------


def test_a_custom_subscriber_creates_the_event_and_the_edge_with_priority(tmp_path):
    G = _graph()
    result = apply(G, _artifact(tmp_path), tmp_path)
    event = G.nodes[REQUEST_EVENT]
    assert event["type"] == "drupal_event" and event["layer"] == "di"
    assert event["label"] == "kernel.request"
    assert event["_overlay"] is True and event["realm"] == "custom"
    assert "boundary" not in event and "external" not in event
    data = _edge(G, SUBSCRIBER_CLASS, REQUEST_EVENT)
    assert data["relation"] == "subscribes_to_event"
    assert data["priority"] == 30
    assert data["origin"] == ORIGIN
    assert data["_src"] == SUBSCRIBER_CLASS
    assert result.counts["subscribers"] == {"total": 2, "custom": 1, "applied": 1}


def test_a_subscriber_with_an_inherited_listener_binds_by_its_own_class(tmp_path):
    """FormsRemote's live artifact: a custom `RouteSubscriberBase` subclass is
    listed with the file that declares `onAlterRoutes` (core's base class).
    The edge's subject is the class, so its own file decides realm and node."""
    data = _runtime_data()
    data["subscribers"]["kernel.request"][1]["file"] = \
        "web/core/lib/Drupal/Core/Routing/RouteSubscriberBase.php"
    G = _graph()
    result = apply(G, _artifact(tmp_path, data), tmp_path)
    assert _edge(G, SUBSCRIBER_CLASS, REQUEST_EVENT)["relation"] == "subscribes_to_event"
    assert result.counts["subscribers"] == {"total": 2, "custom": 1, "applied": 1}


def test_an_event_only_core_subscribes_to_creates_nothing(tmp_path):
    data = _runtime_data()
    del data["subscribers"]["kernel.request"][1]
    G = _graph()
    apply(G, _artifact(tmp_path, data), tmp_path)
    assert REQUEST_EVENT not in G


# -- idempotence and undo -------------------------------------------------------


def test_apply_twice_equals_apply_once(tmp_path):
    once = _graph()
    apply(once, _artifact(tmp_path), tmp_path)
    twice = _graph()
    apply(twice, _artifact(tmp_path), tmp_path)
    apply(twice, _artifact(tmp_path), tmp_path)
    assert _dump(once) == _dump(twice)


def test_apply_then_undo_equals_the_static_graph(tmp_path):
    static = _graph()
    G = copy.deepcopy(static)
    apply(G, _artifact(tmp_path), tmp_path)
    assert TOOLBAR_IMPL in G and DERIVATIVE in G and REQUEST_EVENT in G
    undo(G)
    assert _dump(G) == _dump(static)


def _round_trip(G: nx.Graph) -> nx.Graph:
    """G through graph.json: links written in their `_src`/`_tgt` direction,
    as `export.to_json` writes them. An overlay node created after the class
    it points to would otherwise come back as the link's target."""
    data = nx.node_link_data(G, edges="links")
    for link in data["links"]:
        if "_src" in link and "_tgt" in link:
            link["source"], link["target"] = link["_src"], link["_tgt"]
    return build_from_json(data)


def test_undo_survives_a_graph_json_round_trip(tmp_path):
    static = _graph()
    G = copy.deepcopy(static)
    apply(G, _artifact(tmp_path), tmp_path)
    reloaded = _round_trip(G)
    apply(reloaded, _artifact(tmp_path), tmp_path)
    expected = copy.deepcopy(static)
    apply(expected, _artifact(tmp_path), tmp_path)
    assert _dump(_round_trip(expected)) == _dump(reloaded)


def test_no_artifact_after_one_leaves_the_static_graph(tmp_path):
    G = _graph()
    apply(G, _artifact(tmp_path), tmp_path)
    apply(G, None, tmp_path)
    assert _dump(G) == _dump(_graph())


def test_an_overlay_impl_later_extracted_statically_survives_undo(tmp_path):
    from graphify.build import build_merge
    from graphify.drupal.register import install

    install()
    G = _graph()
    apply(G, _artifact(tmp_path), tmp_path)
    graph_json = _write_graph_json(G, tmp_path)

    # P2b now finds the `#[Hook('toolbar')]` it missed.
    ext = _runtime_extraction()
    nodes = [n for n in ext["nodes"] if n["source_file"] == HOOKS_FILE]
    nodes.append(_n(TOOLBAR_IMPL, "foo:toolbar", HOOKS_FILE, type="drupal_hook_impl", layer="hook",
                    realm="custom", module="foo", hook_name="toolbar", class_name="FooHooks",
                    method="toolbar", via="attribute", _origin="static_yaml", source_location="L12"))
    edges = [e for e in ext["edges"] if e["source_file"] == HOOKS_FILE]
    edges.append(_e(TOOLBAR_IMPL, TOOLBAR, "hook_implemented_by", HOOKS_FILE, _origin="static_yaml"))
    M = build_merge([{"nodes": nodes, "edges": edges}], graph_path=graph_json, root=tmp_path)
    apply(M, None, tmp_path)
    assert TOOLBAR_IMPL in M
    impl = M.nodes[TOOLBAR_IMPL]
    assert "_overlay" not in impl and "origin" not in impl and "runtime_order" not in impl
    assert impl["via"] == "attribute"
    assert M.has_edge(TOOLBAR_IMPL, TOOLBAR)
    assert "origin" not in M.edges[TOOLBAR_IMPL, TOOLBAR]


# -- with a current registry ----------------------------------------------------


def test_registry_realms_for_stubs_and_a_self_declared_hook(tmp_path):
    from graphify.drupal import discovery
    from graphify.drupal.hooks import HookDecl

    core = tmp_path / "web/core"
    registry = discovery.Registry(web_root=str(tmp_path / "web"))
    registry.extensions = {"foo": str(tmp_path / FOO), "system": str(tmp_path / SYSTEM)}
    registry.hooks = {
        "toolbar": HookDecl("toolbar", "system", str(core / "modules/system/system.api.php"), 10),
        # foo declares the hook it implements: P2b's one relation per pair.
        "foo_info": HookDecl("foo_info", "foo", str(tmp_path / FOO / "foo.api.php"), 3),
    }
    registry.types = {"block": discovery.PluginType(
        plugin_type="block", manager_class="Drupal\\Core\\Block\\BlockManager",
        class_file=str(core / "lib/Drupal/Core/Block/BlockManager.php"), line=1, owner="core",
        registered=True, manager_service="plugin.manager.block")}
    discovery.set_current(registry, None)

    data = _runtime_data()
    data["hooks"]["foo_info"] = [{"module": "foo", "callable": "Drupal\\foo\\Hook\\FooHooks::toolbar",
                                  "file": HOOKS_FILE}]
    G = _graph()
    apply(G, _artifact(tmp_path, data), tmp_path)

    hook = G.nodes[hook_id("toolbar")]
    assert hook["realm"] == "core" and hook["boundary"] is True
    stub = G.nodes[type_id("block")]
    assert stub["realm"] == "core" and stub["boundary"] is True
    assert hook_impl_id("foo", "foo_info") in G
    assert hook_id("foo_info") not in G
    assert not G.has_edge(extension_id("foo"), hook_id("foo_info"))
    assert _edge(G, hook_impl_id("foo", "foo_info"), TOOLBAR)["relation"] == "hook_implemented_by"


def test_a_links_yaml_plugin_binds_to_p1s_link_node(tmp_path):
    """FormsRemote's 82 `menu.*` plugins: a plugin type read from a P1 links
    family (`yaml_name: links.action`) is P1's `drupal_local_action` node, not
    a second `drupal_plugin` node beside it. A derivative only the container
    knows is made with P1's type and id."""
    from graphify.drupal import discovery
    from graphify.drupal.yaml_common import link_id

    core = tmp_path / "web/core"
    registry = discovery.Registry(web_root=str(tmp_path / "web"))
    registry.extensions = {"foo": str(tmp_path / FOO)}
    registry.types = {"menu.local_action": discovery.PluginType(
        plugin_type="menu.local_action", manager_class="Drupal\\Core\\Menu\\LocalActionManager",
        class_file=str(core / "lib/Drupal/Core/Menu/LocalActionManager.php"), line=1, owner="core",
        registered=True, manager_service="plugin.manager.menu.local_action", discovery="yaml",
        yaml_name="links.action")}
    discovery.set_current(registry, None)

    actions_file = f"{FOO}/foo.links.action.yml"
    static = link_id("local_action", "foo.add")
    extraction = _runtime_extraction()
    extraction["nodes"].append(_n(static, "Add foo", actions_file, type="drupal_local_action",
                                  layer="routing", realm="custom", _origin="static_yaml"))
    data = _runtime_data()
    data["plugins"]["menu.local_action"] = [
        {"id": "foo.add", "class": "Drupal\\Core\\Menu\\LocalActionDefault", "file": None,
         "provider": "foo", "deriver": None, "base_plugin_id": None},
        {"id": "foo.derived:x", "class": "Drupal\\Core\\Menu\\LocalActionDefault", "file": None,
         "provider": "foo", "deriver": "Drupal\\foo\\Plugin\\Derivative\\FooDeriver",
         "base_plugin_id": "foo.derived"},
    ]
    G = build_from_json(extraction)
    apply(G, _artifact(tmp_path, data), tmp_path)

    assert plugin_id("menu.local_action", "foo.add") not in G
    assert plugin_id("menu.local_action", "foo.derived:x") not in G
    assert G.nodes[static]["type"] == "drupal_local_action"
    assert "_overlay" not in G.nodes[static]
    derived = G.nodes[link_id("local_action", "foo.derived:x")]
    assert derived["type"] == "drupal_local_action" and derived["_overlay"] is True
    assert derived["derivative"] is True
    assert _edge(G, link_id("local_action", "foo.derived:x"), DERIVER_CLASS)["relation"] \
        == "derives_plugins"
