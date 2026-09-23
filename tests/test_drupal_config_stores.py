"""Configuration is found by where it lives (P1b spec §3)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.config_stores import config_name, config_store, is_config_yaml


def _touch(root: Path, rel: str, text: str = "x: 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _site(tmp_path: Path) -> Path:
    """A project laid out like the reference corpus."""
    _touch(tmp_path, "web/core/lib/Drupal.php", "<?php\n")
    _touch(tmp_path, "config/sync/core.extension.yml", "module:\n  node: 0\ntheme: {}\n")
    _touch(tmp_path, "config/sync/config_split.config_split.dev.yml",
           "id: dev\nfolder: ../config/splits/dev\nstatus: false\n")
    _touch(tmp_path, "web/modules/custom/foo/foo.info.yml", "name: Foo\ntype: module\n")
    return tmp_path


def test_a_directory_with_core_extension_is_a_sync_store(tmp_path):
    root = _site(tmp_path)
    store = config_store(_touch(root, "config/sync/system.site.yml"))
    assert store.kind == "sync"
    assert store.directory == root / "config/sync"


def test_split_folder_is_resolved_from_the_web_root(tmp_path):
    root = _site(tmp_path)
    store = config_store(_touch(root, "config/splits/dev/config_split.patch.user.settings.yml"))
    assert store.kind == "split"
    assert store.split == "dev"


def test_shipped_config_is_owned_by_the_extension_above_it(tmp_path):
    root = _site(tmp_path)
    install = config_store(_touch(root, "web/modules/custom/foo/config/install/foo.settings.yml"))
    optional = config_store(_touch(root, "web/modules/custom/foo/config/optional/views.view.foo.yml"))
    assert (install.kind, install.owner) == ("install", "foo")
    assert (optional.kind, optional.owner) == ("optional", "foo")


def test_core_default_config_is_not_a_sync_store(tmp_path):
    """core/config/install ships its own core.extension.yml."""
    root = _site(tmp_path)
    path = _touch(root, "web/core/config/install/core.extension.yml")
    store = config_store(path)
    assert (store.kind, store.owner) == ("install", "core")


def test_schema_files_are_a_store_of_their_own(tmp_path):
    root = _site(tmp_path)
    store = config_store(_touch(root, "web/modules/custom/foo/config/schema/foo.schema.yml"))
    assert (store.kind, store.owner) == ("schema", "foo")


def test_recipes_live_under_a_recipes_directory(tmp_path):
    root = _site(tmp_path)
    recipe = config_store(_touch(root, "web/core/recipes/standard/recipe.yml"))
    shipped = config_store(_touch(root, "web/core/recipes/standard/config/node.type.page.yml"))
    stray = config_store(_touch(root, "tools/recipe.yml"))
    assert (recipe.kind, recipe.owner) == ("recipe", "standard")
    assert (shipped.kind, shipped.owner) == ("recipe", "standard")
    assert stray is None


def test_language_directories_inherit_their_store(tmp_path):
    root = _site(tmp_path)
    store = config_store(_touch(root, "config/sync/language/fr/system.site.yml"))
    assert (store.kind, store.language) == ("sync", "fr")


def test_test_modules_are_excluded_but_core_recipe_fixtures_are_not(tmp_path):
    root = _site(tmp_path)
    fixture_module = _touch(
        root, "web/core/modules/system/tests/modules/t/config/install/t.settings.yml")
    _touch(root, "web/core/modules/system/tests/modules/t/t.info.yml", "name: T\n")
    fixture_recipe = _touch(root, "web/core/tests/fixtures/recipes/input_test/recipe.yml")
    assert config_store(fixture_module) is None
    assert config_store(fixture_recipe).kind == "recipe"


def test_config_dirs_without_an_extension_are_not_drupal(tmp_path):
    """A non-Drupal repository with config/install/*.yml is left alone."""
    assert config_store(_touch(tmp_path, "app/config/install/settings.yml")) is None


def test_graphifyrc_names_a_sync_store_without_the_marker(tmp_path):
    _touch(tmp_path, ".graphifyrc", "drupal.config.sync = exported\n")
    assert config_store(_touch(tmp_path, "exported/system.site.yml")).kind == "sync"


def test_non_yaml_and_family_files_outside_stores(tmp_path):
    root = _site(tmp_path)
    assert not is_config_yaml(_touch(root, "web/modules/custom/foo/foo.services.yml"))
    assert not is_config_yaml(_touch(root, "config/sync/readme.txt"))


def test_config_name_is_the_file_name_without_yml():
    assert config_name(Path("/s/field.field.node.page.body.yml")) == "field.field.node.page.body"


def test_config_directories_are_recognised_even_when_excluded(tmp_path):
    from graphify.drupal.config_stores import in_config_directory

    path = _touch(tmp_path, "web/core/modules/system/tests/modules/m/config/install/m.links.action.yml")
    assert config_store(path) is None
    assert in_config_directory(path)
    assert not in_config_directory(_touch(tmp_path, "web/modules/custom/foo/foo.links.action.yml"))


def test_a_new_marker_is_seen_once_the_caches_are_cleared(tmp_path):
    """P1b final review, Important 3: watch/MCP call extract() many times per process."""
    from graphify.drupal.config_stores import clear_caches

    path = _touch(tmp_path, "config/sync/system.site.yml")
    assert config_store(path) is None
    _touch(tmp_path, "config/sync/core.extension.yml", "module: {}\n")
    clear_caches()
    assert config_store(path).kind == "sync"


def test_every_extract_run_starts_with_fresh_store_caches(tmp_path):
    from graphify.drupal.yaml_common import config_id
    from graphify.extract import extract

    path = _touch(tmp_path, "config/sync/system.site.yml")
    first = extract([path], cache_root=tmp_path / ".c1", root=tmp_path)
    assert config_id("system.site") not in {n["id"] for n in first["nodes"]}
    _touch(tmp_path, "config/sync/core.extension.yml", "module: {}\n")
    second = extract([path], cache_root=tmp_path / ".c2", root=tmp_path)
    assert config_id("system.site") in {n["id"] for n in second["nodes"]}


def test_a_checkout_under_a_tests_directory_keeps_its_configuration(tmp_path):
    """P1b final review minor: only a `tests` segment inside the Drupal tree excludes."""
    root = _site(tmp_path / "home/ci/tests/checkout")
    assert config_store(_touch(root, "config/sync/system.site.yml")).kind == "sync"
    shipped = _touch(root, "web/modules/custom/foo/config/install/foo.settings.yml")
    assert config_store(shipped).kind == "install"
    fixture = _touch(root, "web/modules/custom/foo/tests/modules/foo_test/foo_test.info.yml",
                     "name: T\ntype: module\n")
    assert config_store(_touch(fixture.parent, "config/install/foo_test.settings.yml")) is None
    assert config_store(_touch(root, "web/core/tests/fixtures/config/sync/core.extension.yml",
                               "module: {}\n")) is None


def test_an_unreadable_graphifyrc_is_no_override(tmp_path):
    from graphify.drupal.config_stores import clear_caches

    (tmp_path / ".graphifyrc").write_bytes(b"drupal.config.sync = \xff\xfe\n")
    clear_caches()
    assert config_store(_touch(tmp_path, "exported/system.site.yml")) is None
