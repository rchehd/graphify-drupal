"""The container overlay: undo, binding, services, aliases, routes and
extensions laid over the assembled graph (P3 spec S7)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import networkx as nx
import pytest

from graphify.build import build_from_json
from graphify.drupal import boundary
from graphify.drupal.container import Artifact
from graphify.drupal.container_overlay import ORIGIN, _Binder, apply, undo
from graphify.drupal.yaml_common import parameter_id, permission_id, route_id, service_id
from graphify.drupal.yaml_extract import extension_id
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

FOO = "web/modules/custom/foo"
CONTROLLER_FILE = f"{FOO}/src/Controller/FooController.php"
FOOBAR_FILE = f"{FOO}/src/FooBar.php"
OTHER_FILE = f"{FOO}/src/OtherBar.php"
CORE_ETM_FILE = "web/core/lib/Drupal/Core/Entity/EntityTypeManager.php"
CORE_USER_FILE = "web/core/lib/Drupal/Core/Session/AccountProxy.php"
BAR = "web/modules/contrib/bar"

CONTROLLER = "foocontroller_foocontroller"
PAGE = "foocontroller_foocontroller_page"
FOOBAR = "foobar_foobar"
OTHERBAR = "otherbar_otherbar"


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state):  # noqa: F811
    boundary.clear_caches()
    yield
    boundary.clear_caches()


def _n(nid, label, source_file, **extra):
    return {"id": nid, "label": label, "file_type": "code", "source_file": source_file,
            "source_location": "L1", "_origin": "ast", **extra}


def _e(source, target, relation, source_file, **extra):
    return {"source": source, "target": target, "relation": relation, "confidence": "EXTRACTED",
            "source_file": source_file, "source_location": "L1", "_origin": "ast", **extra}


def _extraction(*, abs_root: Path | None = None, extra_nodes=(), extra_edges=()) -> dict:
    """What the Drupal producers and core's PHP extractor would emit for a
    tiny custom module `foo` (and a contrib module `bar`)."""
    def sf(rel):
        return str(abs_root / rel) if abs_root is not None else rel

    nodes = [
        _n(extension_id("foo"), "foo", sf(f"{FOO}/foo.info.yml"),
           type="drupal_extension", layer="extension", realm="custom"),
        _n(service_id("foo.bar"), "foo.bar", sf(f"{FOO}/foo.services.yml"),
           type="drupal_service", layer="di", realm="custom", class_name="Drupal\\foo\\FooBar"),
        _n(service_id("foo.gone"), "foo.gone", sf(f"{FOO}/foo.services.yml"),
           type="drupal_service", layer="di", realm="custom", class_name="Drupal\\foo\\Gone"),
        {"id": service_id("entity_type.manager"), "label": "entity_type.manager",
         "file_type": "concept", "type": "drupal_service", "layer": "di", "realm": "core",
         "external": True, "boundary": True, "_origin": "static_yaml",
         "source_file": sf(f"{FOO}/foo.services.yml"), "source_location": "L1"},
        _n(route_id("foo.page"), "foo.page", sf(f"{FOO}/foo.routing.yml"),
           type="drupal_route", layer="routing", realm="custom", route_path="/foo",
           controller="\\Drupal\\foo\\Controller\\FooController::page"),
        _n("foocontroller", "FooController.php", sf(CONTROLLER_FILE)),
        _n(CONTROLLER, "FooController", sf(CONTROLLER_FILE)),
        _n(PAGE, ".page()", sf(CONTROLLER_FILE)),
        _n("foobar", "FooBar.php", sf(FOOBAR_FILE)),
        _n(FOOBAR, "FooBar", sf(FOOBAR_FILE)),
        _n("otherbar", "OtherBar.php", sf(OTHER_FILE)),
        _n(OTHERBAR, "OtherBar", sf(OTHER_FILE)),
        _n(extension_id("bar"), "bar", sf(f"{BAR}/bar.info.yml"),
           type="drupal_extension", layer="extension", realm="contrib"),
        _n(service_id("bar.svc"), "bar.svc", sf(f"{BAR}/bar.services.yml"),
           type="drupal_service", layer="di", realm="contrib"),
        *extra_nodes,
    ]
    edges = [
        _e(extension_id("foo"), service_id("foo.bar"), "declares_service", sf(f"{FOO}/foo.services.yml")),
        _e(extension_id("foo"), service_id("foo.gone"), "declares_service", sf(f"{FOO}/foo.services.yml")),
        _e(service_id("foo.bar"), service_id("entity_type.manager"), "injects_service",
           sf(f"{FOO}/foo.services.yml"), target_name="entity_type.manager"),
        _e(extension_id("foo"), route_id("foo.page"), "declares_route", sf(f"{FOO}/foo.routing.yml")),
        _e("foocontroller", CONTROLLER, "contains", sf(CONTROLLER_FILE)),
        _e(CONTROLLER, PAGE, "method", sf(CONTROLLER_FILE)),
        _e("foobar", FOOBAR, "contains", sf(FOOBAR_FILE)),
        _e("otherbar", OTHERBAR, "contains", sf(OTHER_FILE)),
        _e(extension_id("bar"), service_id("bar.svc"), "declares_service", sf(f"{BAR}/bar.services.yml")),
        *extra_edges,
    ]
    return {"nodes": nodes, "edges": edges}


def _svc(sid, cls, file, provider=None, arguments=(), tags=(), decorates=None):
    return {"id": sid, "class": cls, "file": file, "arguments": list(arguments),
            "tags": list(tags), "decorates": decorates, "provider": provider}


def _artifact_data() -> dict:
    return {
        "schema_version": 1,
        "services": [
            _svc("foo.bar", "Drupal\\foo\\FooBar", FOOBAR_FILE, "foo",
                 arguments=["entity_type.manager", "current_user", "%foo.param%"]),
            _svc("entity_type.manager", "Drupal\\Core\\Entity\\EntityTypeManager", CORE_ETM_FILE),
            _svc("current_user", "Drupal\\Core\\Session\\AccountProxy", CORE_USER_FILE),
            _svc("cache.unused", "Drupal\\Core\\Cache\\CacheBackendInterface",
                 "web/core/lib/Drupal/Core/Cache/CacheBackendInterface.php"),
            _svc("bar.svc", "Drupal\\bar\\BarSvc", f"{BAR}/src/BarSvc.php", "bar",
                 arguments=["bar.helper"]),
            _svc("bar.helper", "Drupal\\bar\\Helper", f"{BAR}/src/Helper.php", "bar"),
        ],
        "aliases": {"foo.alias": "foo.bar", "Drupal\\foo\\FooBarInterface": "foo.bar"},
        "routes": [
            {"name": "foo.page", "path": "/foo",
             "defaults": {"_controller": "\\Drupal\\foo\\Controller\\FooController::page"},
             "requirements": {"_permission": "access content+administer foo"},
             "provider": "foo"},
        ],
        "extensions": [
            {"name": "foo", "type": "module", "path": FOO, "status": 1, "weight": 0,
             "dependencies": ["node"]},
            {"name": "bar", "type": "module", "path": BAR, "status": 1, "weight": 2,
             "dependencies": []},
            {"name": "node", "type": "module", "path": "web/core/modules/node", "status": 1,
             "weight": 0, "dependencies": []},
        ],
        "hooks": {}, "plugins": {}, "subscribers": {}, "stamp": {}, "errors": [],
    }


def _artifact(root: Path, data: dict | None = None) -> Artifact:
    return Artifact(data=data if data is not None else _artifact_data(),
                    path=root / "drupal-container.json")


def _graph(tmp_path, **kw) -> nx.Graph:
    return build_from_json(_extraction(**kw))


def _edge(G, u, v):
    assert G.has_edge(u, v), f"no edge {u} -> {v}"
    return G.edges[u, v]


def _dump(G) -> str:
    data = nx.node_link_data(G, edges="links")
    data["nodes"] = sorted(data["nodes"], key=lambda n: n["id"])
    data["links"] = sorted(data["links"], key=lambda e: (e["source"], e["target"]))
    return json.dumps(data, sort_keys=True, default=str)


# -- services -------------------------------------------------------------------


def test_service_implemented_by_is_added_with_origin_container(tmp_path):
    G = _graph(tmp_path)
    result = apply(G, _artifact(tmp_path), tmp_path)
    assert result.status == "fresh"
    data = _edge(G, service_id("foo.bar"), FOOBAR)
    assert data["relation"] == "service_implemented_by"
    assert data["origin"] == ORIGIN
    assert data["_origin"] == "ast"
    assert data["confidence"] == "EXTRACTED"
    assert data["confidence_score"] == 1.0
    assert data["source_file"] == "drupal-container.json"
    assert data["source_location"] == "L1"
    # Direction kept the way core keeps it on an undirected graph.
    assert data["_src"] == service_id("foo.bar")
    assert data["_tgt"] == FOOBAR


def test_an_existing_static_injection_is_confirmed_not_duplicated(tmp_path):
    G = _graph(tmp_path)
    result = apply(G, _artifact(tmp_path), tmp_path)
    data = _edge(G, service_id("foo.bar"), service_id("entity_type.manager"))
    assert data["relation"] == "injects_service"
    assert data["confirmed_by"] == ORIGIN
    assert "origin" not in data
    assert "confirmed_by" in data["_overlay_attrs"]
    assert result.edges["confirmed"] >= 1


def test_a_container_only_injection_to_a_core_service_is_added_with_a_stub(tmp_path):
    G = _graph(tmp_path)
    result = apply(G, _artifact(tmp_path), tmp_path)
    target = service_id("current_user")
    data = _edge(G, service_id("foo.bar"), target)
    assert data["relation"] == "injects_service"
    assert data["origin"] == ORIGIN
    stub = G.nodes[target]
    assert stub["boundary"] is True
    assert stub["external"] is True
    assert stub["file_type"] == "concept"
    assert stub["realm"] == "core"
    assert stub["layer"] == "di"
    assert stub["type"] == "drupal_service"
    assert stub["_overlay"] is True
    assert stub["origin"] == ORIGIN
    # The boundary fact about current_user reaches the stub the custom fact made.
    assert stub["class_name"] == "Drupal\\Core\\Session\\AccountProxy"
    param = _edge(G, service_id("foo.bar"), parameter_id("foo.param"))
    assert param["relation"] == "injects_parameter"
    assert result.edges["container_only"] >= 3


def test_a_core_stub_gains_class_name_only_when_absent(tmp_path):
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path), tmp_path)
    stub = G.nodes[service_id("entity_type.manager")]
    assert stub["class_name"] == "Drupal\\Core\\Entity\\EntityTypeManager"
    assert "class_name" in stub["_overlay_attrs"]

    extraction = _extraction()
    for node in extraction["nodes"]:
        if node["id"] == service_id("entity_type.manager"):
            node["class_name"] = "Drupal\\Core\\Entity\\Static"
    G2 = build_from_json(extraction)
    result = apply(G2, _artifact(tmp_path), tmp_path)
    stub2 = G2.nodes[service_id("entity_type.manager")]
    assert stub2["class_name"] == "Drupal\\Core\\Entity\\Static"
    assert "class_name" not in stub2.get("_overlay_attrs", [])
    assert {"relation": "attribute:class_name", "source": service_id("entity_type.manager"),
            "static_target": "Drupal\\Core\\Entity\\Static",
            "container_target": "Drupal\\Core\\Entity\\EntityTypeManager"} in result.conflicts


def test_a_core_service_no_custom_code_references_creates_no_node(tmp_path):
    G = _graph(tmp_path)
    result = apply(G, _artifact(tmp_path), tmp_path)
    assert service_id("cache.unused") not in G
    assert result.counts["services"]["total"] == 6
    assert result.counts["services"]["custom"] == 1


def test_declares_service_is_confirmed_and_aliases_land_on_the_target(tmp_path):
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path), tmp_path)
    assert _edge(G, extension_id("foo"), service_id("foo.bar"))["confirmed_by"] == ORIGIN
    assert G.nodes[service_id("foo.bar")]["aliases"] == ["Drupal\\foo\\FooBarInterface", "foo.alias"]


def test_a_conflicting_service_implemented_by_is_recorded_and_both_edges_stay(tmp_path):
    static = _e(service_id("foo.bar"), OTHERBAR, "service_implemented_by", f"{FOO}/foo.services.yml")
    G = _graph(tmp_path, extra_edges=[static])
    result = apply(G, _artifact(tmp_path), tmp_path)
    assert _edge(G, service_id("foo.bar"), OTHERBAR)["relation"] == "service_implemented_by"
    assert _edge(G, service_id("foo.bar"), FOOBAR)["origin"] == ORIGIN
    assert {"relation": "service_implemented_by", "source": service_id("foo.bar"),
            "static_target": OTHERBAR, "container_target": FOOBAR} in result.conflicts
    assert result.edges["conflict"] == 1


def test_a_pair_already_carrying_another_relation_gets_no_second_edge(tmp_path):
    static = _e(service_id("foo.bar"), FOOBAR, "references", f"{FOO}/foo.services.yml")
    G = _graph(tmp_path, extra_edges=[static])
    result = apply(G, _artifact(tmp_path), tmp_path)
    data = _edge(G, service_id("foo.bar"), FOOBAR)
    assert data["relation"] == "references"
    assert "origin" not in data and "confirmed_by" not in data
    assert result.edges["pair_taken"] == 1


def test_tags_and_decorates_when_the_artifact_carries_them(tmp_path):
    data = _artifact_data()
    data["services"][0]["tags"] = [{"name": "event_subscriber", "attributes": {"priority": 5}}]
    data["services"][0]["decorates"] = "entity_type.manager"
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path, data), tmp_path)
    tag = _edge(G, service_id("foo.bar"), "drupal_tag_event_subscriber")
    assert tag["relation"] == "tagged_as"
    assert tag["priority"] == 5
    assert G.nodes["drupal_tag_event_subscriber"]["type"] == "drupal_service_tag"
    # The pair already carries injects_service: one relation per pair.
    assert _edge(G, service_id("foo.bar"), service_id("entity_type.manager"))["relation"] == "injects_service"


def test_decorates_is_added_for_a_free_pair(tmp_path):
    data = _artifact_data()
    data["services"][0]["decorates"] = "current_route_match"
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path, data), tmp_path)
    assert _edge(G, service_id("foo.bar"), service_id("current_route_match"))["relation"] == "decorates"


# -- routes ---------------------------------------------------------------------


def test_routes_to_targets_the_method_node(tmp_path):
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path), tmp_path)
    data = _edge(G, route_id("foo.page"), PAGE)
    assert data["relation"] == "routes_to"
    assert data["origin"] == ORIGIN
    assert _edge(G, extension_id("foo"), route_id("foo.page"))["confirmed_by"] == ORIGIN
    for perm in ("access content", "administer foo"):
        assert _edge(G, route_id("foo.page"), permission_id(perm))["relation"] == "requires_permission"


def test_a_route_path_without_its_leading_slash_is_no_conflict(tmp_path):
    """FormsRemote's `path: 'admin/system/settings'`: Symfony's `Route`
    prepends the slash, so the container's `/admin/system/settings` is the
    same path, not a divergence. The static value is kept."""
    G = _graph(tmp_path)
    G.nodes[route_id("foo.page")]["route_path"] = "foo"
    result = apply(G, _artifact(tmp_path), tmp_path)
    assert [c for c in result.conflicts if c["relation"] == "attribute:route_path"] == []
    assert G.nodes[route_id("foo.page")]["route_path"] == "foo"


def test_route_form_and_custom_access(tmp_path):
    data = _artifact_data()
    data["routes"].append({
        "name": "foo.form", "path": "/foo/form",
        "defaults": {"_form": "\\Drupal\\foo\\FooBar"},
        "requirements": {"_custom_access": "\\Drupal\\foo\\Controller\\FooController::page"},
        "provider": "foo"})
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path, data), tmp_path)
    rid = route_id("foo.form")
    node = G.nodes[rid]
    assert node["_overlay"] is True and node["route_path"] == "/foo/form"
    assert _edge(G, rid, FOOBAR)["relation"] == "routes_to_form"
    assert _edge(G, rid, PAGE)["relation"] == "access_checked_by"


def test_an_unbound_controller_becomes_an_attribute(tmp_path):
    data = _artifact_data()
    data["routes"].append({
        "name": "foo.lost", "path": "/lost",
        "defaults": {"_controller": "\\Drupal\\foo\\Controller\\Missing::go"},
        "requirements": {}, "provider": "foo"})
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path, data), tmp_path)
    assert G.nodes[route_id("foo.lost")]["controller"] == "\\Drupal\\foo\\Controller\\Missing::go"


# -- extensions -----------------------------------------------------------------


def test_extension_dependencies_and_attributes(tmp_path):
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path), tmp_path)
    foo = G.nodes[extension_id("foo")]
    assert foo["weight"] == 0 and foo["enabled"] is True
    assert _edge(G, extension_id("foo"), extension_id("node"))["relation"] == "depends_on_module"
    node = G.nodes[extension_id("node")]
    assert node["boundary"] is True and node["realm"] == "core"
    # A boundary extension already in G gets its attributes.
    assert G.nodes[extension_id("bar")]["weight"] == 2


# -- runtime --------------------------------------------------------------------


def test_runtime_present_and_absent(tmp_path):
    G = _graph(tmp_path)
    result = apply(G, _artifact(tmp_path), tmp_path)
    assert G.nodes[service_id("foo.bar")]["runtime"] == "present"
    assert G.nodes[service_id("foo.gone")]["runtime"] == "absent"
    assert G.nodes[route_id("foo.page")]["runtime"] == "present"
    assert G.nodes[extension_id("foo")]["runtime"] == "present"
    assert "runtime" in G.nodes[service_id("foo.gone")]["_overlay_attrs"]
    assert "runtime" not in G.nodes[FOOBAR]
    assert result.runtime_absent == {"drupal_service": 1}
    assert service_id("foo.bar") in result.seen["drupal_service"]


# -- idempotence, undo, no artifact ---------------------------------------------


def test_apply_twice_equals_apply_once(tmp_path):
    once = _graph(tmp_path)
    apply(once, _artifact(tmp_path), tmp_path)
    twice = _graph(tmp_path)
    apply(twice, _artifact(tmp_path), tmp_path)
    apply(twice, _artifact(tmp_path), tmp_path)
    assert _dump(once) == _dump(twice)


def test_apply_then_undo_equals_the_static_graph(tmp_path):
    static = _graph(tmp_path)
    G = copy.deepcopy(static)
    apply(G, _artifact(tmp_path), tmp_path)
    assert _dump(G) != _dump(static)
    undo(G)
    assert _dump(G) == _dump(static)


def test_undo_survives_a_graph_json_round_trip(tmp_path):
    static = _graph(tmp_path)
    G = copy.deepcopy(static)
    apply(G, _artifact(tmp_path), tmp_path)
    reloaded = build_from_json(nx.node_link_data(G, edges="links"))
    apply(reloaded, _artifact(tmp_path), tmp_path)
    expected = copy.deepcopy(static)
    apply(expected, _artifact(tmp_path), tmp_path)
    assert _dump(build_from_json(nx.node_link_data(expected, edges="links"))) == _dump(reloaded)


def test_no_artifact_marks_nothing(tmp_path):
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path), tmp_path)
    result = apply(G, None, tmp_path)
    assert result.status == "unavailable"
    for _nid, data in G.nodes(data=True):
        assert "runtime" not in data and "origin" not in data
    for _u, _v, data in G.edges(data=True):
        assert "origin" not in data and "confirmed_by" not in data
    assert _dump(G) == _dump(_graph(tmp_path))


def test_an_internal_error_becomes_a_status_and_leaves_the_static_graph(tmp_path):
    G = _graph(tmp_path)
    data = _artifact_data()
    data["services"] = [5]
    result = apply(G, _artifact(tmp_path, data), tmp_path)
    assert result.status == "error"
    assert result.reasons
    assert _dump(G) == _dump(_graph(tmp_path))


def test_stale_status_is_passed_through(tmp_path):
    G = _graph(tmp_path)
    result = apply(G, _artifact(tmp_path), tmp_path, status="stale", reasons=["composer.lock changed"])
    assert result.status == "stale" and result.reasons == ["composer.lock changed"]
    assert G.nodes[service_id("foo.bar")]["runtime"] == "present"


# -- the boundary ---------------------------------------------------------------


def test_a_contrib_fact_is_attributes_only_unless_included(tmp_path):
    G = _graph(tmp_path)
    result = apply(G, _artifact(tmp_path), tmp_path)
    assert G.nodes[service_id("bar.svc")]["class_name"] == "Drupal\\bar\\BarSvc"
    assert service_id("bar.helper") not in G
    assert result.counts["services"]["applied"] >= 1

    (tmp_path / ".graphifyrc").write_text("drupal.include = contrib\n", encoding="utf-8")
    boundary.clear_caches()
    G2 = _graph(tmp_path)
    result2 = apply(G2, _artifact(tmp_path), tmp_path)
    data = _edge(G2, service_id("bar.svc"), service_id("bar.helper"))
    assert data["relation"] == "injects_service" and data["origin"] == ORIGIN
    assert result2.counts["services"]["custom"] == 3


# -- binding --------------------------------------------------------------------


def test_binder_normalises_absolute_source_files(tmp_path):
    G = build_from_json(_extraction(abs_root=tmp_path))
    binder = _Binder(G, tmp_path)
    assert binder.php_node(CONTROLLER_FILE, "FooController") == CONTROLLER
    assert binder.php_node(str(tmp_path / CONTROLLER_FILE), "Drupal\\foo\\Controller\\FooController") == CONTROLLER
    assert binder.php_node(CONTROLLER_FILE, "FooController::page") == PAGE
    assert binder.php_node(CONTROLLER_FILE, "FooController::nope") is None
    assert binder.php_node(None, "FooController") is None
    assert binder.php_node(FOOBAR_FILE, "FooController") is None


def test_a_directed_graph_keeps_the_given_direction(tmp_path):
    G = build_from_json(_extraction(), directed=True)
    apply(G, _artifact(tmp_path), tmp_path)
    assert G.has_edge(service_id("foo.bar"), FOOBAR)
    assert not G.has_edge(FOOBAR, service_id("foo.bar"))


# -- fix round 1: static take-over through core's merge, and the composer root --


def _write_graph_json(G, root: Path) -> Path:
    from graphify.export import to_json

    path = root / "graphify-out" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    to_json(G, {}, str(path), force=True)
    return path


def _services_chunk_injecting_current_user() -> dict:
    """What re-extracting `foo.services.yml` gives once it injects `@current_user`
    statically: its own nodes and edges, plus the resolver's stub."""
    sf = f"{FOO}/foo.services.yml"
    ext = _extraction()
    nodes = [n for n in ext["nodes"] if n["source_file"] == sf]
    nodes.append({"id": service_id("current_user"), "label": "current_user",
                  "file_type": "concept", "type": "drupal_service", "layer": "di",
                  "realm": "unknown", "external": True, "boundary": True,
                  "_origin": "static_yaml", "source_file": sf, "source_location": "L7"})
    edges = [e for e in ext["edges"] if e["source_file"] == sf]
    edges.append(_e(service_id("foo.bar"), service_id("current_user"), "injects_service", sf,
                    _origin="static_yaml", target_name="current_user"))
    return {"nodes": nodes, "edges": edges}


def test_a_static_edge_merged_over_an_overlay_edge_survives_undo(tmp_path):
    from graphify.build import build_merge

    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path), tmp_path)
    graph_json = _write_graph_json(G, tmp_path)

    M = build_merge([_services_chunk_injecting_current_user()], graph_path=graph_json, root=tmp_path)
    undo(M)
    u, v = service_id("foo.bar"), service_id("current_user")
    assert v in M and M.has_edge(u, v)
    data = M.edges[u, v]
    assert data["relation"] == "injects_service"
    assert "origin" not in data and "_overlay_file" not in data and "confidence_score" not in data
    node = M.nodes[v]
    assert "_overlay" not in node and "origin" not in node
    assert node["source_file"] == f"{FOO}/foo.services.yml"

    # Laid again, the now-static injection is confirmed, not container-only.
    result = apply(M, _artifact(tmp_path), tmp_path)
    assert M.edges[u, v]["confirmed_by"] == ORIGIN
    assert "origin" not in M.edges[u, v]
    assert result.edges["container_only"] >= 1
    # And with no artifact the static facts stay.
    apply(M, None, tmp_path)
    assert v in M and M.has_edge(u, v)


def test_an_overlay_route_later_declared_statically_survives_undo(tmp_path):
    from graphify.build import build_merge

    data = _artifact_data()
    data["routes"].append({"name": "foo.dyn", "path": "/dyn", "defaults": {},
                           "requirements": {}, "provider": "foo"})
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path, data), tmp_path)
    assert G.nodes[route_id("foo.dyn")]["_overlay"] is True
    graph_json = _write_graph_json(G, tmp_path)

    rf = f"{FOO}/foo.routing.yml"
    ext = _extraction()
    nodes = [n for n in ext["nodes"] if n["source_file"] == rf]
    nodes.append(_n(route_id("foo.dyn"), "foo.dyn", rf, type="drupal_route", layer="routing",
                    realm="custom", route_path="/dyn", _origin="static_yaml"))
    edges = [e for e in ext["edges"] if e["source_file"] == rf]
    edges.append(_e(extension_id("foo"), route_id("foo.dyn"), "declares_route", rf,
                    _origin="static_yaml"))
    M = build_merge([{"nodes": nodes, "edges": edges}], graph_path=graph_json, root=tmp_path)
    apply(M, None, tmp_path)
    assert route_id("foo.dyn") in M
    assert "_overlay" not in M.nodes[route_id("foo.dyn")]
    assert M.has_edge(extension_id("foo"), route_id("foo.dyn"))


def _composer_site(tmp_path: Path) -> Path:
    (tmp_path / "composer.json").write_text(json.dumps({"extra": {"installer-paths": {
        "web/core": ["type:drupal-core"],
        "web/modules/contrib/{$name}": ["type:drupal-module"],
    }}}), encoding="utf-8")
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [
        {"name": "drupal/core", "type": "drupal-core"},
        {"name": "drupal/bar", "type": "drupal-module"},
    ]}), encoding="utf-8")
    web = tmp_path / "web"
    web.mkdir()
    boundary.clear_caches()
    return web


def test_a_scan_of_web_below_the_composer_root_still_binds(tmp_path):
    web = _composer_site(tmp_path)
    extraction = _extraction()
    for item in (*extraction["nodes"], *extraction["edges"]):
        item["source_file"] = item["source_file"].removeprefix("web/")
    G = build_from_json(extraction)
    result = apply(G, Artifact(data=_artifact_data(), path=web / "drupal-container.json"), web)
    assert result.status == "fresh"
    assert G.edges[service_id("foo.bar"), FOOBAR]["relation"] == "service_implemented_by"
    assert G.edges[route_id("foo.page"), PAGE]["relation"] == "routes_to"
    assert G.nodes[service_id("current_user")]["realm"] == "core"
    assert result.counts["services"]["custom"] == 1
    assert G.edges[service_id("foo.bar"), FOOBAR]["source_file"] == "drupal-container.json"


# -- fix round 2: the declared node wins core's dedup; pre-marker graphs clean up --


def test_a_statically_declared_route_beats_the_overlay_record_in_dedup(tmp_path):
    from graphify.build import build_merge
    from graphify.drupal.register import install

    install()
    data = _artifact_data()
    data["routes"].append({"name": "foo.dyn", "path": "/dyn", "defaults": {},
                           "requirements": {}, "provider": "foo"})
    G = _graph(tmp_path)
    apply(G, _artifact(tmp_path, data), tmp_path)
    graph_json = _write_graph_json(G, tmp_path)

    rf = f"{FOO}/foo.routing.yml"
    ext = _extraction()
    nodes = [n for n in ext["nodes"] if n["source_file"] == rf]
    nodes.append(_n(route_id("foo.dyn"), "foo.dyn", rf, type="drupal_route", layer="routing",
                    realm="custom", route_path="/dyn-static", title="Dyn",
                    _origin="static_yaml", source_location="L9"))
    edges = [e for e in ext["edges"] if e["source_file"] == rf]
    edges.append(_e(extension_id("foo"), route_id("foo.dyn"), "declares_route", rf,
                    _origin="static_yaml"))
    M = build_merge([{"nodes": nodes, "edges": edges}], graph_path=graph_json, root=tmp_path)

    node = M.nodes[route_id("foo.dyn")]
    assert node["source_file"] == rf and node["source_location"] == "L9"
    assert node["route_path"] == "/dyn-static" and node["title"] == "Dyn"
    assert "_overlay" not in node and "origin" not in node

    result = apply(M, _artifact(tmp_path, data), tmp_path)
    node = M.nodes[route_id("foo.dyn")]
    assert node["route_path"] == "/dyn-static"          # the static value is kept
    assert node["runtime"] == "present"
    assert {"relation": "attribute:route_path", "source": route_id("foo.dyn"),
            "static_target": "/dyn-static", "container_target": "/dyn"} in result.conflicts
    assert M.edges[extension_id("foo"), route_id("foo.dyn")]["confirmed_by"] == ORIGIN


def test_a_graph_overlaid_before_the_ownership_marker_still_undoes(tmp_path):
    static = _graph(tmp_path)
    G = copy.deepcopy(static)
    apply(G, _artifact(tmp_path), tmp_path)
    for _nid, data in G.nodes(data=True):
        data.pop("_overlay_file", None)
    for _u, _v, data in G.edges(data=True):
        data.pop("_overlay_file", None)
    undo(G)
    assert _dump(G) == _dump(static)
