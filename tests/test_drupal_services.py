"""*.services.yml: the container as declared."""
from __future__ import annotations

from graphify.drupal.yaml_common import parameter_id, service_id, tag_id
from graphify.drupal.yaml_extract import extension_id
from graphify.drupal.yaml_services import extract_drupal_services

SERVICES = """\
parameters:
  foo.setting: true
services:
  foo.locator:
    class: Drupal\\foo\\Locator
    arguments: ['@database', '@?optional.thing', '%foo.setting%']
    tags:
      - {name: event_subscriber}
      - {name: access_check, applies_to: _foo_access}
  foo.decorated:
    class: Drupal\\foo\\Decorated
    decorates: foo.locator
  foo.child:
    parent: foo.base
    abstract: true
"""


def _write(tmp_path, text=SERVICES):
    path = tmp_path / "web/modules/custom/foo/foo.services.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_service_node_carries_class_as_an_attribute_not_an_edge(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    svc = next(n for n in result["nodes"] if n["id"] == service_id("foo.locator"))
    assert svc["type"] == "drupal_service"
    assert svc["layer"] == "di"
    assert svc["realm"] == "custom"
    assert svc["class_name"] == "Drupal\\foo\\Locator"
    # The PHP class is another layer's node; no edge until P4.
    assert not any(e["relation"] == "service_implemented_by" for e in result["edges"])


def test_owner_declares_every_service(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    declared = {e["target"] for e in result["edges"] if e["relation"] == "declares_service"}
    assert declared == {
        service_id("foo.locator"), service_id("foo.decorated"), service_id("foo.child")
    }
    assert all(
        e["source"] == extension_id("foo")
        for e in result["edges"] if e["relation"] == "declares_service"
    )


def test_argument_injection(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    rel = _rel(result)
    assert (service_id("foo.locator"), "injects_service", service_id("database")) in rel
    assert (service_id("foo.locator"), "injects_parameter", parameter_id("foo.setting")) in rel


def test_optional_injection_is_inferred_not_extracted(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    optional = next(
        e for e in result["edges"]
        if e["relation"] == "injects_service" and e["target"] == service_id("optional.thing")
    )
    assert optional["confidence"] == "INFERRED"


def test_tags_decoration_and_parent(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    rel = _rel(result)
    assert (service_id("foo.locator"), "tagged_as", tag_id("event_subscriber")) in rel
    assert (service_id("foo.locator"), "tagged_as", tag_id("access_check")) in rel
    assert (service_id("foo.decorated"), "decorates", service_id("foo.locator")) in rel
    assert (service_id("foo.child"), "parent_service", service_id("foo.base")) in rel


def test_parameters_are_declared_by_the_owner(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    assert any(
        e["relation"] == "declares_parameter" and e["target"] == parameter_id("foo.setting")
        for e in result["edges"]
    )


def test_no_extension_node_is_emitted(tmp_path):
    """*.info.yml is the only family that may declare one."""
    result = extract_drupal_services(_write(tmp_path))
    assert not any(n["id"] == extension_id("foo") for n in result["nodes"])


def test_line_numbers_point_at_the_service(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    svc = next(n for n in result["nodes"] if n["id"] == service_id("foo.locator"))
    assert svc["source_location"] == "L4"


def test_symfony_tagged_argument_does_not_become_a_service(tmp_path):
    text = (
        "services:\n"
        "  foo.manager:\n"
        "    class: Drupal\\foo\\Manager\n"
        "    arguments: [!tagged_iterator foo.plugin]\n"
    )
    result = extract_drupal_services(_write(tmp_path, text))
    assert not any(e["relation"] == "injects_service" for e in result["edges"])


def test_malformed_file_reports_instead_of_raising(tmp_path):
    result = extract_drupal_services(_write(tmp_path, "services:\n  foo:\n - a\n  b: [\n"))
    assert result["nodes"] == [] and result["edges"] == []
    assert "parse error" in result["error"]
