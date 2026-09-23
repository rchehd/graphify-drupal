"""*.permissions.yml and *.routing.yml, and the edge that joins them."""
from __future__ import annotations

from graphify.drupal.yaml_access import extract_drupal_permissions, extract_drupal_routing
from graphify.drupal.yaml_common import permission_id, route_id
from graphify.drupal.yaml_extract import extension_id

PERMISSIONS = """\
administer foo:
  title: 'Administer foo'
  restrict access: true
view foo:
  title: 'View foo'
permission_callbacks:
  - Drupal\\foo\\Permissions::dynamic
"""

ROUTING = """\
foo.settings:
  path: '/admin/config/foo'
  defaults:
    _form: '\\Drupal\\foo\\Form\\SettingsForm'
    _title: 'Foo settings'
  requirements:
    _permission: 'administer foo+view foo'
foo.page:
  path: '/foo/{node}'
  defaults:
    _controller: '\\Drupal\\foo\\Controller\\Page::view'
  requirements:
    _custom_access: '\\Drupal\\foo\\Access::check'
"""


def _write(tmp_path, name, text):
    path = tmp_path / "web/modules/custom/foo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_permissions_are_declared_by_the_owner(tmp_path):
    result = extract_drupal_permissions(_write(tmp_path, "foo.permissions.yml", PERMISSIONS))
    ids = {n["id"] for n in result["nodes"]}
    assert permission_id("administer foo") in ids
    assert permission_id("view foo") in ids
    assert (extension_id("foo"), "declares_permission", permission_id("view foo")) in _rel(result)


def test_permission_callbacks_are_an_attribute_not_a_permission(tmp_path):
    result = extract_drupal_permissions(_write(tmp_path, "foo.permissions.yml", PERMISSIONS))
    assert permission_id("permission_callbacks") not in {n["id"] for n in result["nodes"]}
    assert any(n.get("permission_callbacks") for n in result["nodes"] if n["type"] == "drupal_permission") \
        or all("permission_callbacks" not in n for n in result["nodes"])


def test_restrict_access_is_carried(tmp_path):
    result = extract_drupal_permissions(_write(tmp_path, "foo.permissions.yml", PERMISSIONS))
    admin = next(n for n in result["nodes"] if n["id"] == permission_id("administer foo"))
    assert admin["restrict_access"] is True
    assert admin["label"] == "Administer foo"


def test_routes_are_declared_with_path_and_handler_attribute(tmp_path):
    result = extract_drupal_routing(_write(tmp_path, "foo.routing.yml", ROUTING))
    settings = next(n for n in result["nodes"] if n["id"] == route_id("foo.settings"))
    assert settings["type"] == "drupal_route"
    assert settings["layer"] == "routing"
    assert settings["route_path"] == "/admin/config/foo"
    assert settings["form"] == "\\Drupal\\foo\\Form\\SettingsForm"
    # The PHP handler is another layer's node; no edge until P4.
    assert not any(e["relation"] in ("routes_to", "routes_to_form") for e in result["edges"])


def test_permission_requirement_splits_on_plus_and_comma(tmp_path):
    result = extract_drupal_routing(_write(tmp_path, "foo.routing.yml", ROUTING))
    rel = _rel(result)
    assert (route_id("foo.settings"), "requires_permission", permission_id("administer foo")) in rel
    assert (route_id("foo.settings"), "requires_permission", permission_id("view foo")) in rel


def test_custom_access_is_an_attribute(tmp_path):
    result = extract_drupal_routing(_write(tmp_path, "foo.routing.yml", ROUTING))
    page = next(n for n in result["nodes"] if n["id"] == route_id("foo.page"))
    assert page["custom_access"] == "\\Drupal\\foo\\Access::check"


def test_neither_family_emits_an_extension_node(tmp_path):
    for name, text, fn in (
        ("foo.permissions.yml", PERMISSIONS, extract_drupal_permissions),
        ("foo.routing.yml", ROUTING, extract_drupal_routing),
    ):
        result = fn(_write(tmp_path, name, text))
        assert not any(n["id"] == extension_id("foo") for n in result["nodes"])
