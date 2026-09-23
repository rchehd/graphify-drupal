"""P1b through the real pipeline: detection, dispatch, secret screen."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id, settings_id


def _touch(root: Path, rel: str, text: str = "a: 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _site(root: Path) -> list[Path]:
    return [
        _touch(root, "web/core/lib/Drupal.php", "<?php\n"),
        _touch(root, "config/sync/core.extension.yml", "module:\n  node: 0\ntheme: {}\n"),
        _touch(root, "config/sync/system.site.yml", "name: Site\n"),
        _touch(root, "config/sync/simple_oauth.oauth2_token.settings.yml", "expiration: 1\n"),
        _touch(root, "config/sync/key.key.api_token.yml", "key_provider: config\n"),
        _touch(root, "web/modules/custom/menu_test/menu_test.info.yml", "name: M\ntype: module\n"),
        _touch(root, "web/modules/custom/menu_test/config/install/menu_test.links.action.yml",
               "langcode: en\ntitle: Original\n"),
        _touch(root, "web/sites/default/settings.php",
               "<?php\n$config['system.site']['name'] = 'Secret Name';\n"),
    ]


def test_config_files_are_code_and_escape_the_secret_screen(tmp_path):
    import graphify  # noqa: F401  (installs the seam)
    from graphify.detect import FileType, _is_sensitive, classify_file

    _site(tmp_path)
    for rel in ("config/sync/system.site.yml",
                "config/sync/simple_oauth.oauth2_token.settings.yml",
                "config/sync/key.key.api_token.yml"):
        assert classify_file(tmp_path / rel) == FileType.CODE, rel
        assert _is_sensitive(tmp_path / rel) is False, rel


def test_a_config_file_with_a_family_suffix_is_configuration(tmp_path):
    from graphify.extract import extract

    paths = _site(tmp_path)
    result = extract([p for p in paths if p.suffix == ".yml"],
                     cache_root=tmp_path / ".cache", root=tmp_path)
    types = {n["id"]: n.get("type") for n in result["nodes"]}
    assert types[config_id("menu_test.links.action")] == "drupal_config"
    assert not any(t == "drupal_local_action" for t in types.values())


def test_a_test_modules_config_is_neither_config_nor_a_family(tmp_path):
    from graphify.drupal.families import is_drupal_file

    _site(tmp_path)
    path = _touch(tmp_path, "web/core/modules/system/tests/modules/menu_test/config/install/"
                            "menu_test.links.action.yml", "langcode: en\n")
    assert not is_drupal_file(path)


def test_settings_php_keeps_its_php_nodes_and_gains_overrides(tmp_path):
    from graphify.extract import extract

    paths = _site(tmp_path)
    result = extract(paths, cache_root=tmp_path / ".cache", root=tmp_path)
    ids = {n["id"] for n in result["nodes"]}
    assert settings_id("default/settings.php") in ids
    assert any(n.get("source_file", "").endswith("settings.php") and
               n["id"] != settings_id("default/settings.php") for n in result["nodes"])
    assert (settings_id("default/settings.php"), "overrides_config", config_id("system.site")) in {
        (e["source"], e["relation"], e["target"]) for e in result["edges"]}
    assert "Secret Name" not in repr(result)


def test_synced_and_shipped_copies_are_one_active_node(tmp_path):
    from graphify.extract import extract

    paths = [
        _touch(tmp_path, "config/sync/core.extension.yml", "module:\n  system: 0\n"),
        _touch(tmp_path, "config/sync/system.site.yml", "name: Site\n"),
        _touch(tmp_path, "web/core/modules/system/system.info.yml", "name: System\ntype: module\n"),
        _touch(tmp_path, "web/core/modules/system/config/install/system.site.yml", "name: ''\n"),
    ]
    result = extract(paths, cache_root=tmp_path / ".cache", root=tmp_path)
    [site] = [n for n in result["nodes"] if n["id"] == config_id("system.site")]
    assert site["active"] is True
    assert site["install_mode"] == "install"
    assert site["declared_in"] == ["config/sync/system.site.yml",
                                   "web/core/modules/system/config/install/system.site.yml"]


def test_nothing_dangles_in_a_small_site(tmp_path):
    from graphify.extract import extract

    paths = [
        _touch(tmp_path, "config/sync/core.extension.yml", "module:\n  node: 0\n  gone: 0\n"),
        _touch(tmp_path, "config/sync/views.view.x.yml",
               "dependencies:\n  config:\n    - node.type.absent\n  module:\n    - views\n"),
        _touch(tmp_path, "web/core/modules/node/node.info.yml", "name: Node\ntype: module\n"),
    ]
    result = extract(paths, cache_root=tmp_path / ".cache", root=tmp_path)
    ids = {n["id"] for n in result["nodes"]}
    assert [e for e in result["edges"] if e["source"] not in ids or e["target"] not in ids] == []
