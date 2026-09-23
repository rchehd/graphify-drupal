"""The *.info.yml extractor: nodes, edges, and failure modes."""
from __future__ import annotations

from graphify.drupal.yaml_extract import extension_id, extract_drupal_info

MODULE_INFO = """\
name: Foo
type: module
core_version_requirement: ^10 || ^11
dependencies:
  - drupal:node
  - views:views_ui
  - token
"""

THEME_INFO = """\
name: My Theme
type: theme
base theme: olivero
"""


def _write(tmp_path, rel, text):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_module_node_carries_the_drupal_taxonomy(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml", MODULE_INFO)
    result = extract_drupal_info(path)
    own = next(n for n in result["nodes"] if n["id"] == extension_id("foo"))
    assert own["label"] == "Foo"
    assert own["file_type"] == "code"       # schema value, never "drupal_module"
    assert own["type"] == "drupal_module"   # taxonomy lives here
    assert own["layer"] == "extension"
    assert own["realm"] == "custom"
    assert own["_origin"] == "static_yaml"
    assert own["source_file"] == str(path)


def test_dependency_spellings_all_normalise_to_one_id(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml", MODULE_INFO)
    result = extract_drupal_info(path)
    deps = {e["target"] for e in result["edges"] if e["relation"] == "depends_on_module"}
    assert deps == {extension_id("node"), extension_id("views_ui"), extension_id("token")}


def test_extractor_emits_no_placeholder_for_dependency_targets(tmp_path):
    """Placeholders are the cross-file resolver's job, not this extractor's.

    A stub emitted here carries the REFERENCING file as its source_file. When
    the target is also declared by its own info.yml, two nodes end up sharing
    one id with different source_files, and extract()'s id-remap pass splits
    the extension by prefixing one with its file path.
    """
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml", MODULE_INFO)
    result = extract_drupal_info(path)
    assert [n["id"] for n in result["nodes"]] == [extension_id("foo")]
    assert {e["target_name"] for e in result["edges"]} == {"node", "views_ui", "token"}


def test_resolver_materialises_only_undeclared_targets():
    from graphify.drupal.resolvers import resolve_missing_extensions

    nodes = [{"id": extension_id("foo")}, {"id": extension_id("token")}]
    edges = [
        {"relation": "depends_on_module", "target": extension_id("token"),
         "target_name": "token", "source_file": "a/foo.info.yml"},
        {"relation": "depends_on_module", "target": extension_id("facets"),
         "target_name": "facets", "source_file": "a/foo.info.yml"},
        {"relation": "calls", "target": "some_other_language_node",
         "source_file": "a/x.py"},
    ]
    resolve_missing_extensions([], nodes, edges)

    added = [n for n in nodes if n.get("external")]
    assert [n["id"] for n in added] == [extension_id("facets")]
    assert added[0]["label"] == "facets"
    assert added[0]["file_type"] == "concept"
    # A dangling endpoint belonging to another language is not ours to invent.
    assert not any(n["id"] == "some_other_language_node" for n in nodes)


def test_theme_emits_base_theme_edge(tmp_path):
    path = _write(tmp_path, "web/themes/custom/mytheme/mytheme.info.yml", THEME_INFO)
    result = extract_drupal_info(path)
    own = next(n for n in result["nodes"] if n["id"] == extension_id("mytheme"))
    assert own["type"] == "drupal_theme"
    assert [(e["source"], e["relation"], e["target"]) for e in result["edges"]] == [
        (extension_id("mytheme"), "base_theme", extension_id("olivero")),
    ]


def test_one_relation_per_ordered_pair(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml",
                  "name: Foo\ntype: module\ndependencies:\n  - node\n  - drupal:node\n")
    result = extract_drupal_info(path)
    pairs = [(e["source"], e["target"]) for e in result["edges"]]
    assert len(pairs) == len(set(pairs)) == 1


def test_self_dependency_is_dropped(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml",
                  "name: Foo\ntype: module\ndependencies:\n  - foo\n")
    assert extract_drupal_info(path)["edges"] == []


def test_malformed_yaml_returns_an_error_not_an_exception(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml", "name: [unclosed\n")
    result = extract_drupal_info(path)
    assert result["nodes"] == [] and result["edges"] == []
    assert "parse error" in result["error"]


def test_node_ids_survive_builder_normalisation(tmp_path):
    """make_id output must be a fixed point of the builder's own normaliser."""
    from graphify.ids import normalize_id

    path = _write(tmp_path, "web/modules/custom/foo_bar/foo_bar.info.yml",
                  "name: Foo Bar\ntype: module\n")
    for node in extract_drupal_info(path)["nodes"]:
        assert normalize_id(node["id"]) == node["id"]


def test_resolver_materialises_missing_targets_of_every_p1_relation():
    from graphify.drupal.resolvers import resolve_missing_targets
    from graphify.drupal.yaml_common import library_id, permission_id, route_id, service_id

    nodes = [{"id": service_id("database")}]
    edges = [
        {"relation": "injects_service", "target": service_id("database"),
         "source_file": "a/foo.services.yml"},
        {"relation": "injects_service", "target": service_id("absent.service"),
         "target_name": "absent.service", "source_file": "a/foo.services.yml"},
        {"relation": "requires_permission", "target": permission_id("access content"),
         "target_name": "access content", "source_file": "a/foo.routing.yml"},
        {"relation": "library_depends_on", "target": library_id("core", "once"),
         "target_name": "core/once", "source_file": "a/foo.libraries.yml"},
        {"relation": "links_to_route", "target": route_id("user.login"),
         "target_name": "user.login", "source_file": "a/foo.links.menu.yml"},
        {"relation": "calls", "target": "another_language_node", "source_file": "a/x.py"},
    ]
    resolve_missing_targets([], nodes, edges)

    created = {n["id"]: n for n in nodes if n.get("external")}
    assert service_id("absent.service") in created
    assert permission_id("access content") in created
    assert library_id("core", "once") in created
    assert route_id("user.login") in created
    assert service_id("database") not in created      # already declared
    assert "another_language_node" not in created     # not ours to invent
    assert created[route_id("user.login")]["type"] == "drupal_route"
    assert created[permission_id("access content")]["label"] == "access content"


def test_a_missing_parent_takes_the_kind_of_its_child():
    """`parent_link` joins a menu link to a menu link, a local task to a local task."""
    from graphify.drupal.resolvers import resolve_missing_targets
    from graphify.drupal.yaml_common import link_id

    child = link_id("local_task", "foo.child")
    nodes = [{"id": child, "type": "drupal_local_task"}]
    edges = [{"relation": "parent_link", "source": child,
              "target": link_id("local_task", "gone.parent"),
              "target_name": "gone.parent", "source_file": "a/foo.links.task.yml"}]
    resolve_missing_targets([], nodes, edges)
    created = next(n for n in nodes if n.get("external"))
    assert created["type"] == "drupal_local_task"
    assert created["label"] == "gone.parent"


def test_an_owner_without_info_yml_is_materialised_from_its_file(tmp_path):
    """`core.services.yml` has no `core.info.yml`; `core` must still exist."""
    from graphify.drupal.resolvers import resolve_missing_targets
    from graphify.drupal.yaml_common import service_id

    source = tmp_path / "web/core/core.services.yml"
    nodes = [{"id": service_id("database"), "type": "drupal_service"}]
    edges = [{"relation": "declares_service", "source": extension_id("core"),
              "target": service_id("database"), "source_file": str(source)}]
    resolve_missing_targets([], nodes, edges)
    owner = next(n for n in nodes if n["id"] == extension_id("core"))
    assert owner["label"] == "core"
    assert owner["type"] == "drupal_extension"
    assert owner["realm"] == "core"
    assert owner["external"] is True
