"""The tolerant loader, the line map, and the shared node/edge shapes."""
from __future__ import annotations

from graphify.drupal.yaml_common import (
    edge,
    key_lines,
    load_drupal_yaml,
    node,
    service_id,
)

TAGGED_ITERATOR = """\
services:
  cache_contexts_manager:
    class: Drupal\\Core\\Cache\\CacheContextsManager
    arguments: ['@service_container', !tagged_iterator cache.context]
"""

SERVICE_CLOSURE = """\
services:
  modeler_api.owner:
    class: Drupal\\modeler_api\\Owner
    arguments: [!service_closure '@entity_type.manager']
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_tagged_iterator_parses(tmp_path):
    """core.services.yml uses this; safe_load raises ConstructorError on it."""
    data, error = load_drupal_yaml(_write(tmp_path, "core.services.yml", TAGGED_ITERATOR))
    assert error is None
    assert "cache_contexts_manager" in data["services"]
    assert data["services"]["cache_contexts_manager"]["arguments"][1] == "cache.context"


def test_service_closure_parses(tmp_path):
    data, error = load_drupal_yaml(_write(tmp_path, "m.services.yml", SERVICE_CLOSURE))
    assert error is None
    assert data["services"]["modeler_api.owner"]["arguments"] == ["@entity_type.manager"]


def test_structural_error_is_reported_not_raised(tmp_path):
    data, error = load_drupal_yaml(_write(tmp_path, "x.libraries.yml", "a:\n b: 1\n c\n"))
    assert data is None
    assert "parse error" in error


def test_non_mapping_document_is_not_an_error(tmp_path):
    data, error = load_drupal_yaml(_write(tmp_path, "x.libraries.yml", "- one\n- two\n"))
    assert data is None and error is None


def test_oversized_file_is_refused(tmp_path):
    path = _write(tmp_path, "big.services.yml", "a: 1\n")
    path.write_text("#" * (3 * 1024 * 1024), encoding="utf-8")
    data, error = load_drupal_yaml(path)
    assert data is None and "too large" in error


def test_key_lines_separates_the_two_indents():
    text = "services:\n  alpha:\n    class: A\n  beta:\n    class: B\n"
    lines = key_lines(text)
    assert lines[0] == {"services": 1}
    assert lines[2] == {"alpha": 2, "beta": 4}


def test_key_lines_ignores_list_items_and_comments():
    text = "# note\nroutes:\n  - not_a_key\nfoo.bar:\n  path: /x\n"
    lines = key_lines(text)
    assert lines[0] == {"routes": 2, "foo.bar": 4}
    assert "not_a_key" not in lines[2]


def test_key_lines_keeps_keys_containing_spaces():
    """A Drupal permission is `administer foo`; only the colon delimits a key."""
    assert key_lines("administer foo:\n  title: X\n")[0] == {"administer foo": 1}


def test_key_lines_does_not_let_a_property_shadow_an_entity():
    """A route named `path` must not take the line of some route's `path:`."""
    text = "path:\n  path: /a\nother:\n  path: /b\n"
    assert key_lines(text)[0] == {"path": 1, "other": 3}


def test_node_carries_the_universal_attributes(tmp_path):
    path = tmp_path / "web/modules/custom/foo/foo.services.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("services: {}\n", encoding="utf-8")
    n = node(service_id("foo.bar"), "foo.bar", type="drupal_service",
             layer="di", path=path, line=7, class_name="Drupal\\foo\\Bar")
    assert n["file_type"] == "code"
    assert n["type"] == "drupal_service"
    assert n["layer"] == "di"
    assert n["realm"] == "custom"
    assert n["_origin"] == "static_yaml"
    assert n["source_file"] == str(path)
    assert n["source_location"] == "L7"
    assert n["class_name"] == "Drupal\\foo\\Bar"


def test_edge_carries_the_universal_attributes(tmp_path):
    path = tmp_path / "foo.services.yml"
    path.write_text("services: {}\n", encoding="utf-8")
    e = edge(service_id("a"), service_id("b"), "injects_service", path=path, line=3)
    assert e["confidence"] == "EXTRACTED"
    assert e["_origin"] == "static_yaml"
    assert e["source_location"] == "L3"
