"""overrides_config: the value in the file is not the value in production (spec §5.3)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id
from graphify.drupal.yaml_config import extract_drupal_config


def _touch(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _site(root: Path) -> Path:
    _touch(root, "web/core/lib/Drupal.php", "<?php\n")
    _touch(root, "config/sync/core.extension.yml", "module: {}\ntheme: {}\n")
    _touch(root, "config/sync/config_split.config_split.prod.yml",
           "id: prod\nfolder: ../config/splits/prod\nstatus: false\n")
    return root


def _overrides(result):
    return [e for e in result["edges"] if e["relation"] == "overrides_config"]


def test_a_split_patch_overrides_keys_and_emits_no_node(tmp_path):
    root = _site(tmp_path)
    patch = _touch(root, "config/splits/prod/config_split.patch.domain.record.forms_public.yml",
                   "adding:\n  hostname: live.example\n  name: Live\n"
                   "removing:\n  hostname: dev.example\n")
    result = extract_drupal_config(patch)
    assert result["nodes"] == []
    [edge] = _overrides(result)
    assert edge["source"] == config_id("config_split.config_split.prod")
    assert edge["target"] == config_id("domain.record.forms_public")
    assert edge["override_source"] == "split"
    assert edge["keys"] == ["hostname", "name"]
    assert "live.example" not in repr(result) and "dev.example" not in repr(result)


def test_a_split_copy_is_a_config_node_and_a_whole_override(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(root, "config/splits/prod/devel.settings.yml", "a: 1\n"))
    cfg = result["nodes"][0]
    assert (cfg["id"], cfg["store"], cfg["active"]) == (config_id("devel.settings"), "split", False)
    [edge] = _overrides(result)
    assert edge["source"] == config_id("config_split.config_split.prod")
    assert edge["whole"] is True


def test_a_domain_config_file_overrides_the_named_config(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/domain.config.forms_public.system.site.yml", "name: Public\n"))
    [edge] = _overrides(result)
    assert edge["source"] == config_id("domain.record.forms_public")
    assert edge["target"] == config_id("system.site")
    assert edge["override_source"] == "domain"
    assert edge["keys"] == ["name"]
    assert "alt_target" not in edge
    # The file is itself a synced config object.
    assert result["nodes"][0]["id"] == config_id("domain.config.forms_public.system.site")


def test_a_domain_file_with_a_language_segment_offers_both_readings(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/domain.config.forms_public.fr.system.site.yml", "name: Public\n"))
    [edge] = _overrides(result)
    assert edge["target"] == config_id("fr.system.site")
    assert edge["alt_target"] == config_id("system.site")
    assert edge["alt_target_name"] == "system.site"


def test_a_language_override_emits_no_node(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/language/fr/system.site.yml", "name: Site\nslogan: S\n"))
    assert result["nodes"] == []
    [edge] = _overrides(result)
    assert edge["source"] == config_id("language.entity.fr")
    assert edge["target"] == config_id("system.site")
    assert (edge["override_source"], edge["keys"]) == ("language", ["name", "slogan"])


def test_nested_keys_are_dotted_paths(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/splits/prod/config_split.patch.system.mail.yml",
        "adding:\n  interface:\n    default: smtp\n"))
    assert _overrides(result)[0]["keys"] == ["interface.default"]
