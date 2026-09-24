"""Realm derived from composer, with P0's path rules as fallback (spec S4.1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphify.drupal.boundary import boundary_dir, clear_caches, install_map, realm_of
from graphify.drupal.yaml_common import node


def _touch(root: Path, rel: str, text: str = "x: 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


_INSTALLER_PATHS = {
    "web/core": ["type:drupal-core"],
    "web/modules/contrib/{$name}": ["type:drupal-module"],
    "web/modules/custom/{$name}": ["type:drupal-custom-module"],
    "recipes/{$name}": ["type:drupal-recipe"],
    "web/libraries/ace": ["npm-asset/ace-builds"],
    "web/libraries/{$name}": ["type:drupal-library"],
}

_PACKAGES = [
    {"name": "drupal/core", "type": "drupal-core"},
    {"name": "drupal/token", "type": "drupal-module"},
    {"name": "acme/mymod", "type": "drupal-custom-module"},
    {"name": "drupal/standard_recipe", "type": "drupal-recipe"},
    {"name": "symfony/yaml", "type": "library"},
]


def _composer_project(root: Path, installer_paths=None, vendor_dir=None, packages=None) -> Path:
    composer_json = {
        "extra": {"installer-paths": _INSTALLER_PATHS if installer_paths is None else installer_paths},
    }
    if vendor_dir is not None:
        composer_json["config"] = {"vendor-dir": vendor_dir}
    _touch(root, "composer.json", json.dumps(composer_json))
    composer_lock = {"packages": _PACKAGES if packages is None else packages, "packages-dev": []}
    _touch(root, "composer.lock", json.dumps(composer_lock))
    _touch(root, "web/core/lib/Drupal.php", "<?php\n")
    return root


@pytest.fixture(autouse=True)
def _clear():
    clear_caches()
    yield
    clear_caches()


@pytest.mark.parametrize("rel, expected", [
    ("web/core/lib/Drupal.php", "core"),
    ("web/modules/contrib/token/token.info.yml", "contrib"),
    ("web/modules/custom/mymod/x.php", "custom"),
    ("recipes/standard_recipe/recipe.yml", "contrib"),
    ("recipes/my_own/recipe.yml", "custom"),
    ("vendor/symfony/yaml/x.php", "vendor"),
    ("web/sites/default/settings.php", "custom"),
    ("config/sync/system.site.yml", "custom"),
])
def test_realm_of_from_composer(tmp_path, rel, expected):
    root = _composer_project(tmp_path)
    assert realm_of(root / rel) == expected


def test_boundary_dir_for_composer_paths(tmp_path):
    root = _composer_project(tmp_path)
    assert boundary_dir(root / "web/modules/contrib/token") == ("contrib", "composer")
    assert boundary_dir(root / "vendor") == ("vendor", "vendor_dir")
    assert boundary_dir(root / "web/modules/custom/mymod") is None


def test_custom_vendor_dir(tmp_path):
    root = _composer_project(tmp_path, vendor_dir="lib/vendor")
    assert realm_of(root / "lib/vendor/symfony/yaml/x.php") == "vendor"
    assert boundary_dir(root / "lib/vendor") == ("vendor", "vendor_dir")


def test_package_name_selector(tmp_path):
    packages = _PACKAGES + [{"name": "npm-asset/ace-builds", "type": "npm-asset"}]
    root = _composer_project(tmp_path, packages=packages)
    assert realm_of(root / "web/libraries/ace/x.js") == "contrib"


def test_drupal_include_excludes_a_realm_from_boundary_dir(tmp_path):
    root = _composer_project(tmp_path)
    _touch(root, ".graphifyrc", "drupal.include = contrib\n")
    assert boundary_dir(root / "web/modules/contrib/token") is None
    # Realm resolution is unaffected -- only boundary_dir (detect pruning) reads it.
    assert realm_of(root / "web/modules/contrib/token/token.info.yml") == "contrib"


def test_broken_lock_falls_back_to_path_rules(tmp_path):
    root = _composer_project(tmp_path)
    _touch(root, "composer.lock", "{not valid json")
    imap = install_map(root / "web/modules/contrib/token/token.info.yml")
    assert imap.error
    assert realm_of(root / "web/modules/contrib/token/token.info.yml") == "contrib"
    assert realm_of(root / "web/modules/custom/mymod/x.php") == "custom"


def test_no_composer_json_uses_path_rules(tmp_path):
    _touch(tmp_path, "web/core/lib/Drupal.php", "<?php\n")
    _touch(tmp_path, "web/modules/contrib/token/token.info.yml")
    assert install_map(tmp_path / "web/modules/contrib/token/token.info.yml") is None
    assert realm_of(tmp_path / "web/core/lib/Drupal.php") == "core"
    assert realm_of(tmp_path / "web/modules/contrib/token/token.info.yml") == "contrib"
    # Falls back to a vendor dir sibling of the web root.
    _touch(tmp_path, "vendor/symfony/yaml/x.php")
    assert realm_of(tmp_path / "vendor/symfony/yaml/x.php") == "vendor"
    assert realm_of(tmp_path / "web/sites/default/settings.php") == "custom"


def test_realm_of_never_unknown(tmp_path):
    root = _composer_project(tmp_path)
    for rel in (
        "web/core/lib/Drupal.php",
        "web/modules/contrib/token/token.info.yml",
        "web/modules/custom/mymod/x.php",
        "recipes/standard_recipe/recipe.yml",
        "recipes/my_own/recipe.yml",
        "vendor/symfony/yaml/x.php",
        "web/sites/default/settings.php",
        "config/sync/system.site.yml",
        "somewhere/odd/thing.txt",
    ):
        assert realm_of(root / rel) != "unknown"


def test_node_carries_realm_from_composer(tmp_path):
    root = _composer_project(tmp_path)
    path = root / "web/modules/contrib/token/token.info.yml"
    payload = node("x", "Token", type="drupal_extension", layer="config", path=path)
    assert payload["realm"] == "contrib"


DRUPAL_CORPUS = Path("/home/user/Projects/FormsRemote")


@pytest.mark.skipif(not DRUPAL_CORPUS.is_dir(), reason="reference corpus not available")
def test_measure_on_formsremote():
    # The project's own composer.json/lock, read from a path with no nested
    # composer.json of its own (drupal/core ships web/core/composer.json,
    # which has no composer.lock beside it and is the *nearer* match).
    imap = install_map(DRUPAL_CORPUS / "recipes" / "x.yml")
    assert imap is not None
    assert not imap.error
    assert len(imap.paths) > 0
    # web/core/lib/Drupal.php's nearest composer.json is drupal/core's own
    # (no composer.lock beside it) -- falls back to path rules, still "core".
    assert realm_of(DRUPAL_CORPUS / "web/core/lib/Drupal.php") == "core"
    assert realm_of(DRUPAL_CORPUS / "web/modules/contrib/token/token.info.yml") == "contrib"
    assert realm_of(DRUPAL_CORPUS / "vendor/symfony/yaml/x.php") == "vendor"
