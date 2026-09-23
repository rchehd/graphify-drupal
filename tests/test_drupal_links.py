"""The four *.links.*.yml families: the UI surface over the routing table."""
from __future__ import annotations

from graphify.drupal.yaml_common import link_id, menu_id, route_id
from graphify.drupal.yaml_extract import extension_id
from graphify.drupal.yaml_links import (
    extract_drupal_contextual_links,
    extract_drupal_local_actions,
    extract_drupal_local_tasks,
    extract_drupal_menu_links,
)

MENU = """\
foo.admin:
  title: 'Foo'
  route_name: foo.settings
  menu_name: admin
  parent: system.admin_config
  weight: 10
"""

TASK = """\
foo.settings_tab:
  title: 'Settings'
  route_name: foo.settings
  base_route: foo.settings
foo.advanced_tab:
  title: 'Advanced'
  route_name: foo.advanced
  base_route: foo.settings
  parent_id: foo.settings_tab
"""

ACTION = """\
foo.add:
  title: 'Add foo'
  route_name: foo.add_form
  appears_on:
    - foo.collection
"""

CONTEXTUAL = """\
foo.edit:
  title: 'Edit'
  route_name: foo.edit_form
  group: foo
"""


def _write(tmp_path, name, text):
    path = tmp_path / "web/modules/custom/foo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_menu_link_reaches_its_route_and_menu(tmp_path):
    result = extract_drupal_menu_links(_write(tmp_path, "foo.links.menu.yml", MENU))
    lid = link_id("menu_link", "foo.admin")
    rel = _rel(result)
    assert (extension_id("foo"), "declares_menu_link", lid) in rel
    assert (lid, "links_to_route", route_id("foo.settings")) in rel
    assert (lid, "in_menu", menu_id("admin")) in rel
    menu = next(n for n in result["nodes"] if n["id"] == menu_id("admin"))
    assert menu["type"] == "drupal_menu"
    assert (lid, "parent_link", link_id("menu_link", "system.admin_config")) in rel


def test_local_task_carries_base_route(tmp_path):
    result = extract_drupal_local_tasks(_write(tmp_path, "foo.links.task.yml", TASK))
    lid = link_id("local_task", "foo.advanced_tab")
    rel = _rel(result)
    assert (lid, "links_to_route", route_id("foo.advanced")) in rel
    assert (lid, "base_route", route_id("foo.settings")) in rel


def test_default_tab_keeps_links_to_route_and_is_marked(tmp_path):
    """route_name == base_route is the tab shown by default: one edge, one flag."""
    result = extract_drupal_local_tasks(_write(tmp_path, "foo.links.task.yml", TASK))
    lid = link_id("local_task", "foo.settings_tab")
    rel = _rel(result)
    assert (lid, "links_to_route", route_id("foo.settings")) in rel
    assert (lid, "base_route", route_id("foo.settings")) not in rel
    tab = next(n for n in result["nodes"] if n["id"] == lid)
    assert tab["default_tab"] is True


def test_local_task_parent_is_parent_id(tmp_path):
    """Local tasks name their parent with `parent_id`, menu links with `parent`."""
    result = extract_drupal_local_tasks(_write(tmp_path, "foo.links.task.yml", TASK))
    assert (link_id("local_task", "foo.advanced_tab"), "parent_link",
            link_id("local_task", "foo.settings_tab")) in _rel(result)


def test_one_relation_per_ordered_pair_when_route_equals_base_route(tmp_path):
    """links_to_route and base_route share endpoints here; the reader keeps one."""
    result = extract_drupal_local_tasks(_write(tmp_path, "foo.links.task.yml", TASK))
    pairs = [(e["source"], e["target"]) for e in result["edges"]]
    assert len(pairs) == len(set(pairs))


def test_local_action_appears_on_routes(tmp_path):
    result = extract_drupal_local_actions(_write(tmp_path, "foo.links.action.yml", ACTION))
    lid = link_id("local_action", "foo.add")
    rel = _rel(result)
    assert (lid, "links_to_route", route_id("foo.add_form")) in rel
    assert (lid, "appears_on_route", route_id("foo.collection")) in rel


def test_contextual_link(tmp_path):
    result = extract_drupal_contextual_links(
        _write(tmp_path, "foo.links.contextual.yml", CONTEXTUAL))
    lid = link_id("contextual_link", "foo.edit")
    assert (lid, "links_to_route", route_id("foo.edit_form")) in _rel(result)


def test_no_family_emits_an_extension_or_route_node(tmp_path):
    """Routes belong to *.routing.yml; these families only reference them."""
    result = extract_drupal_menu_links(_write(tmp_path, "foo.links.menu.yml", MENU))
    ids = {n["id"] for n in result["nodes"]}
    assert extension_id("foo") not in ids
    assert route_id("foo.settings") not in ids


def test_a_menu_named_by_two_files_is_one_node(tmp_path):
    """Menus are global, like tags; the seam collapses the two declarations."""
    from graphify.extract import extract

    paths = [
        _write(tmp_path, "foo.links.menu.yml", MENU),
        _write(tmp_path.joinpath("b"), "bar.links.menu.yml",
               MENU.replace("foo.admin", "bar.admin")),
    ]
    result = extract(paths, root=tmp_path)
    menus = [n for n in result["nodes"] if n.get("type") == "drupal_menu"]
    assert [n["id"] for n in menus] == [menu_id("admin")]
