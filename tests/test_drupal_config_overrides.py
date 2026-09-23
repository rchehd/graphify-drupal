"""overrides_config: the value in the file is not the value in production (spec §5.3)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id, config_patch_id, config_translation_id
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


def test_a_split_patch_emits_one_node_and_overrides_keys(tmp_path):
    root = _site(tmp_path)
    patch = _touch(root, "config/splits/prod/config_split.patch.domain.record.forms_public.yml",
                   "adding:\n  hostname: live.example\n  name: Live\n"
                   "removing:\n  hostname: dev.example\n")
    result = extract_drupal_config(patch)

    [cfg] = result["nodes"]
    assert cfg["id"] == config_patch_id("prod", "domain.record.forms_public")
    assert cfg["type"] == "drupal_config_patch"
    assert cfg["layer"] == "config"
    assert cfg["config_name"] == "config_split.patch.domain.record.forms_public"
    assert cfg["store"] == "split"
    assert cfg["split"] == "prod"
    assert cfg["target_name"] == "domain.record.forms_public"
    assert cfg["realm"] == "custom"
    assert "_rank" not in cfg

    [override, contains] = [e for e in result["edges"] if e["relation"] == "overrides_config"], \
        [e for e in result["edges"] if e["relation"] == "contains"]
    [override] = override
    [contains] = contains
    assert override["source"] == config_id("config_split.config_split.prod")
    assert override["target"] == config_id("domain.record.forms_public")
    assert override["override_source"] == "split"
    assert override["keys"] == ["hostname", "name"]
    assert "live.example" not in repr(result) and "dev.example" not in repr(result)

    assert contains["source"] == config_id("config_split.config_split.prod")
    assert contains["target"] == cfg["id"]


def test_two_splits_patching_the_same_target_produce_distinct_node_ids(tmp_path):
    root = _site(tmp_path)
    _touch(root, "config/sync/config_split.config_split.test.yml",
           "id: test\nfolder: ../config/splits/test\nstatus: false\n")
    prod = _touch(root, "config/splits/prod/config_split.patch.domain.record.forms_staff.yml",
                  "adding:\n  hostname: prod.example\n")
    test = _touch(root, "config/splits/test/config_split.patch.domain.record.forms_staff.yml",
                  "adding:\n  hostname: test.example\n")
    prod_result = extract_drupal_config(prod)
    test_result = extract_drupal_config(test)
    assert prod_result["nodes"][0]["id"] != test_result["nodes"][0]["id"]


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


def test_a_language_override_emits_one_node(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/language/fr/system.site.yml", "name: Site\nslogan: S\n"))

    [cfg] = result["nodes"]
    assert cfg["id"] == config_translation_id("fr", "sync", "", "system.site")
    assert cfg["type"] == "drupal_config_translation"
    assert cfg["layer"] == "config"
    assert cfg["config_name"] == "system.site"
    assert cfg["language"] == "fr"
    assert cfg["store"] == "sync"
    assert cfg["realm"] == "custom"
    assert "_rank" not in cfg

    [override] = [e for e in result["edges"] if e["relation"] == "overrides_config"]
    [contains] = [e for e in result["edges"] if e["relation"] == "contains"]
    assert override["source"] == config_id("language.entity.fr")
    assert override["target"] == config_id("system.site")
    assert (override["override_source"], override["keys"]) == ("language", ["name", "slogan"])

    assert contains["source"] == config_id("language.entity.fr")
    assert contains["target"] == cfg["id"]


def test_nested_keys_are_dotted_paths(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/splits/prod/config_split.patch.system.mail.yml",
        "adding:\n  interface:\n    default: smtp\n"))
    assert _overrides(result)[0]["keys"] == ["interface.default"]
