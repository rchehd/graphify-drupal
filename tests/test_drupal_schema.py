"""config/schema/*.schema.yml: one node per schema type (spec §5.1)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import schema_id
from graphify.drupal.yaml_config import extract_drupal_config
from graphify.drupal.yaml_extract import extension_id

SCHEMA = """\
foo.settings:
  type: config_object
  mapping:
    enabled:
      type: boolean
field.field.*.*.*.third_party.foo:
  type: mapping
"""


def _write(root: Path) -> Path:
    (root / "web/modules/custom/foo").mkdir(parents=True)
    (root / "web/modules/custom/foo/foo.info.yml").write_text("name: Foo\n", encoding="utf-8")
    path = root / "web/modules/custom/foo/config/schema/foo.schema.yml"
    path.parent.mkdir(parents=True)
    path.write_text(SCHEMA, encoding="utf-8")
    return path


def test_each_top_level_key_is_a_schema_type(tmp_path):
    result = extract_drupal_config(_write(tmp_path))
    types = {n["schema_type"]: n for n in result["nodes"]}
    assert set(types) == {"foo.settings", "field.field.*.*.*.third_party.foo"}
    assert types["foo.settings"]["pattern"] is False
    assert types["field.field.*.*.*.third_party.foo"]["pattern"] is True
    assert types["foo.settings"]["type"] == "drupal_config_schema"
    assert types["foo.settings"]["source_location"] == "L1"


def test_the_extension_defines_its_schemas(tmp_path):
    result = extract_drupal_config(_write(tmp_path))
    assert (extension_id("foo"), "defines_schema", schema_id("foo.settings")) in {
        (e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_a_wildcard_type_and_a_literal_type_get_distinct_ids():
    assert schema_id("views.area.*") != schema_id("views_area")
    assert schema_id("entity_reference_selection.default:*") != schema_id(
        "entity_reference_selection.default")


def test_a_plain_type_keeps_its_current_id():
    assert schema_id("foo.settings") == "drupal_config_schema_foo_settings"


def test_distinct_types_never_share_an_id():
    """The corpus pairs that used to collide, plus every character class that can."""
    pairs = [
        ("views.field.user", "views_field_user"),
        ("views.field.bulk_form", "views_field_bulk_form"),
        ("Foo.bar", "foo.bar"),
        ("views.area.*", "views.area"),
        ("views.area", "views_area"),
        ("views.area.*", "views_area"),
    ]
    for a, b in pairs:
        assert schema_id(a) != schema_id(b), (a, b)
