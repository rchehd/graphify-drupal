"""The P1b cross-file decisions (spec §6.3)."""
from __future__ import annotations

from graphify.drupal.resolvers import resolve_missing_targets
from graphify.drupal.yaml_common import config_id, recipe_id, schema_id
from graphify.drupal.yaml_extract import extension_id


def _cfg(name):
    return {"id": config_id(name), "type": "drupal_config", "config_name": name, "label": name}


def _schema(type_):
    return {"id": schema_id(type_), "type": "drupal_config_schema", "schema_type": type_,
            "pattern": "*" in type_, "label": type_}


def _edge(source, relation, target, **extra):
    return {"source": source, "relation": relation, "target": target,
            "source_file": "config/sync/x.yml", "confidence": "EXTRACTED", **extra}


def test_schema_for_prefers_the_exact_type_then_the_most_specific_pattern():
    nodes = [_cfg("system.site"), _cfg("field.field.node.page.body"), _cfg("views.view.x"),
             _schema("system.site"), _schema("field.field.*.*.*"),
             _schema("field.field.node.*.*"), _schema("views.view.*.*")]
    edges: list[dict] = []
    resolve_missing_targets([], nodes, edges)
    schema_for = {(e["source"], e["target"]): e["confidence"]
                  for e in edges if e["relation"] == "schema_for"}
    assert schema_for == {
        (schema_id("system.site"), config_id("system.site")): "EXTRACTED",
        (schema_id("field.field.node.*.*"), config_id("field.field.node.page.body")): "INFERRED",
    }


def test_a_domain_override_keeps_the_declared_reading():
    nodes = [_cfg("system.site"), _cfg("domain.record.d")]
    edges = [_edge(config_id("domain.record.d"), "overrides_config", config_id("fr.system.site"),
                   target_name="fr.system.site", alt_target=config_id("system.site"),
                   alt_target_name="system.site")]
    resolve_missing_targets([], nodes, edges)
    [edge] = edges
    assert edge["target"] == config_id("system.site")
    assert "alt_target" not in edge and "alt_target_name" not in edge
    assert not any(n.get("external") for n in nodes)


def test_undeclared_p1b_targets_get_the_type_their_id_names():
    nodes = [_cfg("views.view.x"), {"id": recipe_id("blog"), "type": "drupal_recipe"}]
    edges = [
        _edge(config_id("views.view.x"), "config_depends_on", config_id("node.type.page"),
              target_name="node.type.page"),
        _edge(recipe_id("blog"), "applies_recipe", recipe_id("gone"), target_name="gone"),
        _edge(recipe_id("blog"), "imports_config", extension_id("claro"), target_name="claro"),
    ]
    resolve_missing_targets([], nodes, edges)
    created = {n["id"]: n for n in nodes if n.get("external")}
    assert created[config_id("node.type.page")]["type"] == "drupal_config"
    assert created[config_id("node.type.page")]["layer"] == "config"
    assert created[recipe_id("gone")]["type"] == "drupal_recipe"
    assert created[extension_id("claro")]["type"] == "drupal_extension"


def test_installed_and_missing_follow_core_extension():
    core_ext = config_id("core.extension")
    nodes = [_cfg("core.extension"),
             {"id": extension_id("node"), "type": "drupal_module"},
             {"id": extension_id("devel"), "type": "drupal_module"}]
    edges = [_edge(core_ext, "installs_extension", extension_id("node"), target_name="node"),
             _edge(core_ext, "installs_extension", extension_id("gone"), target_name="gone")]
    resolve_missing_targets([], nodes, edges)
    by_id = {n["id"]: n for n in nodes}
    assert by_id[extension_id("node")]["installed"] is True
    assert by_id[extension_id("devel")]["installed"] is False
    assert by_id[extension_id("gone")]["installed"] is True
    assert by_id[extension_id("gone")]["missing"] is True


def test_without_core_extension_nothing_carries_installed():
    nodes = [{"id": extension_id("node"), "type": "drupal_module"}]
    resolve_missing_targets([], nodes, [])
    assert "installed" not in nodes[0]


def test_a_shipped_configs_missing_owner_is_named_from_its_store(tmp_path):
    info_less = tmp_path / "web/core/config/install/core.menu.static_menu_link_overrides.yml"
    info_less.parent.mkdir(parents=True)
    info_less.write_text("definitions: {}\n", encoding="utf-8")
    nodes = [_cfg("core.menu.static_menu_link_overrides")]
    edges = [_edge(extension_id("core"), "defines_config",
                   config_id("core.menu.static_menu_link_overrides"),
                   source_file=str(info_less))]
    resolve_missing_targets([], nodes, edges)
    owner = next(n for n in nodes if n["id"] == extension_id("core"))
    assert owner["label"] == "core"
