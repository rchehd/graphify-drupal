"""settings*.php `$config[…]` lines (spec §5.4, plan decision 1)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id, settings_id
from graphify.drupal.yaml_settings import extract_drupal_settings, is_settings_php

SETTINGS = """\
<?php
// $config['system.site']['name'] = 'commented';
# $config['system.site']['slogan'] = 'commented';
$config['config_split.config_split.prod']['status'] = FALSE;
if (getenv('ENV') === 'prod') {
  $config['config_split.config_split.prod']['status'] = TRUE;
}
$config['smtp.settings']['smtp_host'] = 'mail.secret.example';
$config['smtp.settings']['smtp_password'] = 'hunter2';
$config["system.logging"]["error_level"] = 'hide';
if ($x == $config['system.site']['name']) {}
"""


def _write(root: Path, name: str = "settings.php", text: str = SETTINGS) -> Path:
    path = root / "web/sites/default" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_only_site_settings_files_are_recognised(tmp_path):
    assert is_settings_php(_write(tmp_path))
    assert is_settings_php(_write(tmp_path, "settings.local.php"))
    assert not is_settings_php(tmp_path / "web/modules/custom/foo/settings.php")
    assert not is_settings_php(_write(tmp_path, "services.php"))


def test_assignments_become_ambiguous_overrides_with_keys(tmp_path):
    result = extract_drupal_settings(_write(tmp_path))
    [settings] = result["nodes"]
    assert settings["id"] == settings_id("default/settings.php")
    assert (settings["type"], settings["layer"], settings["realm"]) == (
        "drupal_settings", "config", "custom")
    edges = {e["target"]: e for e in result["edges"]}
    assert set(edges) == {config_id("config_split.config_split.prod"),
                          config_id("smtp.settings"), config_id("system.logging")}
    prod = edges[config_id("config_split.config_split.prod")]
    assert (prod["relation"], prod["confidence"]) == ("overrides_config", "AMBIGUOUS")
    assert (prod["override_source"], prod["keys"]) == ("settings_php", ["status"])
    assert prod["source_location"] == "L4"
    assert edges[config_id("smtp.settings")]["keys"] == ["smtp_host", "smtp_password"]


def test_values_never_leave_the_file(tmp_path):
    text = repr(extract_drupal_settings(_write(tmp_path)))
    assert "mail.secret.example" not in text and "hunter2" not in text and "hide" not in text


def test_a_file_without_config_lines_yields_nothing(tmp_path):
    assert extract_drupal_settings(_write(tmp_path, text="<?php\n$settings['x'] = 1;\n")) == {
        "nodes": [], "edges": []}
