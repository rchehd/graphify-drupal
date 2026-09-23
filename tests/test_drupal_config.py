"""Configuration objects and what they state about themselves (P1b spec §5)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id
from graphify.drupal.yaml_config import extract_drupal_config
from graphify.drupal.yaml_extract import extension_id


def _touch(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _site(root: Path) -> Path:
    _touch(root, "web/core/lib/Drupal.php", "<?php\n")
    _touch(root, "config/sync/core.extension.yml",
           "module:\n  node: 0\n  foo: 0\n  standard: 1000\ntheme:\n  olivero: 0\nprofile: standard\n")
    _touch(root, "web/modules/custom/foo/foo.info.yml", "name: Foo\ntype: module\n")
    return root


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


VIEW = """\
uuid: 1
status: true
dependencies:
  config:
    - node.type.page
  module:
    - node
    - views
  enforced:
    module:
      - foo
  content:
    - block_content:basic:abc
id: frontpage
label: Frontpage
"""


def test_a_synced_config_is_an_active_node(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(root, "config/sync/views.view.frontpage.yml", VIEW))
    cfg = next(n for n in result["nodes"] if n["id"] == config_id("views.view.frontpage"))
    assert cfg["type"] == "drupal_config"
    assert cfg["layer"] == "config"
    assert cfg["config_name"] == "views.view.frontpage"
    assert cfg["active"] is True
    assert cfg["status"] is True
    assert cfg["realm"] == "custom"
    assert cfg["content_dependencies"] == ["block_content:basic:abc"]


def test_dependencies_become_edges_and_enforced_wins(tmp_path):
    root = _site(tmp_path)
    text = VIEW.replace("  enforced:\n    module:\n      - foo\n",
                        "  enforced:\n    module:\n      - views\n")
    result = extract_drupal_config(_touch(root, "config/sync/views.view.frontpage.yml", text))
    rel = _rel(result)
    own = config_id("views.view.frontpage")
    assert (own, "config_depends_on", config_id("node.type.page")) in rel
    assert (own, "config_depends_on", extension_id("node")) in rel
    assert (own, "enforced_dependency", extension_id("views")) in rel
    assert (own, "config_depends_on", extension_id("views")) not in rel


def test_values_are_not_recorded(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/smtp.settings.yml", "smtp_host: mail.secret.example\nsmtp_port: 25\n"))
    assert "mail.secret.example" not in repr(result)


def test_shipped_config_is_defined_by_its_extension(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "web/modules/custom/foo/config/optional/foo.settings.yml", "a: 1\n"))
    cfg = result["nodes"][0]
    assert cfg["active"] is False
    assert cfg["install_mode"] == "optional"
    assert (extension_id("foo"), "defines_config", config_id("foo.settings")) in _rel(result)


def test_core_extension_installs_modules_and_themes(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(root / "config/sync/core.extension.yml")
    own = config_id("core.extension")
    installs = {e["target"]: e for e in result["edges"] if e["relation"] == "installs_extension"}
    assert set(installs) == {extension_id(n) for n in ("node", "foo", "standard", "olivero")}
    assert installs[extension_id("standard")]["weight"] == 1000
    assert all(e["source"] == own for e in installs.values())


def test_shipped_core_extension_installs_nothing(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "web/core/config/install/core.extension.yml", "module: {}\ntheme: {}\n"))
    assert not any(e["relation"] == "installs_extension" for e in result["edges"])


def test_split_entity_is_typed_and_lists_what_it_splits(tmp_path):
    root = _site(tmp_path)
    text = (
        "id: dev\nstatus: false\nfolder: ../config/splits/dev\n"
        "module:\n  devel: 0\ntheme: {}\n"
        "complete_list:\n  - devel.settings\n"
        "partial_list:\n  - system.site\n  - 'webform.*'\n"
    )
    result = extract_drupal_config(_touch(root, "config/sync/config_split.config_split.dev.yml", text))
    split = result["nodes"][0]
    own = config_id("config_split.config_split.dev")
    assert split["type"] == "drupal_config_split"
    assert split["folder"] == "../config/splits/dev"
    assert split["status"] is False
    assert split["split_patterns"] == ["webform.*"]
    kinds = {(e["relation"], e["target"], e.get("split_kind")) for e in result["edges"]}
    assert ("splits_extension", extension_id("devel"), None) in kinds
    assert ("splits_config", config_id("devel.settings"), "complete") in kinds
    assert ("splits_config", config_id("system.site"), "partial") in kinds
    assert all(e["source"] == own for e in result["edges"])


def test_domain_record_is_typed(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/domain.record.forms_public.yml", "id: forms_public\nhostname: x.example\n"))
    assert result["nodes"][0]["type"] == "drupal_domain"
    assert "x.example" not in repr(result)


def test_empty_config_is_still_a_config_object(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(root, "config/sync/empty.settings.yml", ""))
    assert [n["id"] for n in result["nodes"]] == [config_id("empty.settings")]


def test_malformed_config_reports_instead_of_raising(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(root, "config/sync/bad.yml", "a: [\n"))
    assert result["nodes"] == [] and "parse error" in result["error"]
