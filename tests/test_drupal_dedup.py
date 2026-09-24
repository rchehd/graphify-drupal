"""Core's entity dedup must not merge distinct Drupal nodes by label.

A Drupal id is the entity's identity (`make_id` of its kind and name), like a
code symbol's. Boundary stubs are `file_type: concept`, the one type core's
dedup unifies across files by label: on the reference corpus the core module
`toolbar`'s extension stub was merged into the hook stub `toolbar` (so
`installs_extension` pointed at a hook), and route, menu-link and local-task
stubs sharing a label were folded into one another.
"""
from __future__ import annotations

import pytest

from graphify.drupal.register import DrupalSeamError, install


def _stub(node_id: str, label: str, node_type: str, source_file: str) -> dict:
    return {"id": node_id, "label": label, "type": node_type, "file_type": "concept",
            "external": True, "boundary": True, "source_file": source_file,
            "source_location": "L1"}


def _edge(source: str, target: str, relation: str, source_file: str) -> dict:
    return {"source": source, "target": target, "relation": relation,
            "confidence": "EXTRACTED", "source_file": source_file, "source_location": "L1"}


def _build(nodes: list[dict], edges: list[dict]):
    install()
    from graphify.build import build

    return build([{"nodes": nodes, "edges": edges}], directed=True)


def test_an_extension_stub_and_a_hook_stub_with_one_label_stay_two_nodes():
    graph = _build(
        [
            {"id": "drupal_config_core_extension", "label": "core.extension",
             "type": "drupal_config", "file_type": "code",
             "source_file": "config/sync/core.extension.yml", "source_location": "L1"},
            {"id": "drupal_extension_deployment_status", "label": "deployment_status",
             "type": "drupal_module", "file_type": "code",
             "source_file": "web/modules/custom/ds/ds.info.yml", "source_location": "L1"},
            _stub("drupal_extension_toolbar", "toolbar", "drupal_extension",
                  "config/sync/core.extension.yml"),
            _stub("drupal_hook_toolbar", "toolbar", "drupal_hook",
                  "web/modules/custom/ds/src/Hook/ToolbarHooks.php"),
        ],
        [
            _edge("drupal_config_core_extension", "drupal_extension_toolbar",
                  "installs_extension", "config/sync/core.extension.yml"),
            _edge("drupal_extension_deployment_status", "drupal_hook_toolbar",
                  "implements_hook", "web/modules/custom/ds/src/Hook/ToolbarHooks.php"),
        ],
    )
    assert {"drupal_extension_toolbar", "drupal_hook_toolbar"} <= set(graph.nodes)
    assert graph.has_edge("drupal_config_core_extension", "drupal_extension_toolbar")
    assert graph.has_edge("drupal_extension_deployment_status", "drupal_hook_toolbar")
    assert "hook_name" not in graph.nodes["drupal_extension_toolbar"]


def test_near_identical_route_and_link_stubs_are_not_fuzzy_merged():
    labels = ("entity.webform_integrations_log.canonical",
              "entity.webform_integrations_log.collection",
              "entity.webform_integrations_log.edit_form",
              "entity.webform_integrations_log.delete_form")
    nodes = [_stub(f"drupal_route_{label.replace('.', '_')}", label, "drupal_route",
                   f"web/modules/custom/m/m.links.{i}.yml") for i, label in enumerate(labels)]
    nodes.append(_stub("drupal_menu_link_domain_admin", "domain.admin", "drupal_menu_link",
                       "web/modules/custom/m/m.links.menu.yml"))
    nodes.append(_stub("drupal_local_task_domain_admin", "domain.admin", "drupal_local_task",
                       "web/modules/custom/m/m.links.task.yml"))
    graph = _build(nodes, [])
    assert {n["id"] for n in nodes} <= set(graph.nodes)


def test_non_drupal_concepts_are_still_deduplicated_by_core():
    graph = _build(
        [
            {"id": "concept_a", "label": "Payment Gateway Integration", "file_type": "concept",
             "source_file": "docs/a.md", "source_location": "L1"},
            {"id": "concept_b", "label": "Payment Gateway Integration", "file_type": "concept",
             "source_file": "docs/b.md", "source_location": "L1"},
        ],
        [],
    )
    assert len({"concept_a", "concept_b"} & set(graph.nodes)) == 1


def test_seam_fails_loudly_when_dedup_moves(monkeypatch):
    import graphify.dedup as dedup
    from graphify.drupal.register import _patch_dedup

    monkeypatch.delattr(dedup, "_is_code", raising=True)
    with pytest.raises(DrupalSeamError, match="_is_code"):
        _patch_dedup(dedup)


def _merge(tmp_path, existing: list[dict], new_nodes: list[dict], new_edges: list[dict]):
    """`build_merge` of one new chunk onto a graph.json holding `existing`:
    the incremental path, where graph.json's copy of an id meets a fresh one."""
    import json

    install()
    from graphify.build import build_merge

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps({"directed": True, "multigraph": False, "graph": {},
                                      "nodes": existing, "links": []}), encoding="utf-8")
    return build_merge([{"nodes": new_nodes, "edges": new_edges}], graph_path,
                       directed=True, root=tmp_path)


def test_build_merge_keeps_same_label_stubs_apart(tmp_path):
    """The dedup-by-id fix holds on the incremental path too (final review minor 13)."""
    existing = [
        _stub("drupal_extension_toolbar", "toolbar", "drupal_extension",
              "config/sync/core.extension.yml"),
        _stub("drupal_route_a_b", "entity.log.canonical", "drupal_route", "m/m.routing.yml"),
    ]
    new = [
        _stub("drupal_hook_toolbar", "toolbar", "drupal_hook",
              "web/modules/custom/ds/src/Hook/ToolbarHooks.php"),
        _stub("drupal_route_a_c", "entity.log.collection", "drupal_route", "m/m.links.task.yml"),
    ]
    graph = _merge(tmp_path, existing, new, [])
    assert {n["id"] for n in existing + new} <= set(graph.nodes)
    assert "hook_name" not in graph.nodes["drupal_extension_toolbar"]


def test_a_declared_node_beats_the_stub_graph_json_carries(tmp_path):
    """A hook first only invoked (a stub, source_file the invoking file), then
    declared by a re-extracted in-graph `*.api.php`: the declared node survives
    the merge with its own attributes, not the stale stub's."""
    stub = {**_stub("drupal_hook_new_hook", "new_hook", "drupal_hook",
                    "web/modules/custom/a/src/Thing.php"),
            "hook_name": "new_hook", "missing": True, "realm": "unknown"}
    declared = {"id": "drupal_hook_new_hook", "label": "new_hook", "type": "drupal_hook",
                "file_type": "code", "hook_name": "new_hook", "provider": "a",
                "realm": "custom", "source_file": "web/modules/custom/a/a.api.php",
                "source_location": "L6"}
    graph = _merge(tmp_path, [stub], [declared], [])
    node = graph.nodes["drupal_hook_new_hook"]
    assert (node["source_file"], node.get("provider"), node.get("realm")) == (
        "web/modules/custom/a/a.api.php", "a", "custom")
    assert "missing" not in node and "boundary" not in node


def test_seam_fails_loudly_when_defines_id_moves(monkeypatch):
    import graphify.dedup as dedup
    from graphify.drupal.register import _patch_dedup

    monkeypatch.delattr(dedup, "_defines_id", raising=True)
    with pytest.raises(DrupalSeamError, match="_defines_id"):
        _patch_dedup(dedup)
