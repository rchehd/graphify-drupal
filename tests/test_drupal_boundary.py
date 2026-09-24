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
    # The project root is the nearest ancestor with BOTH composer.json and
    # composer.lock. drupal/core ships its own web/core/composer.json (no
    # lock beside it), and most contrib modules and vendor packages ship
    # their own too -- none of those are project roots, so a path under any
    # of them still resolves against the real project root's install map.
    imap = install_map(DRUPAL_CORPUS / "web/core/lib/Drupal.php")
    assert imap is not None
    assert imap.project_root == DRUPAL_CORPUS.as_posix()
    assert not imap.error
    assert len(imap.paths) > 0
    assert realm_of(DRUPAL_CORPUS / "web/core/lib/Drupal.php") == "core"
    assert realm_of(DRUPAL_CORPUS / "web/modules/contrib/token/token.info.yml") == "contrib"
    assert realm_of(DRUPAL_CORPUS / "vendor/symfony/yaml/x.php") == "vendor"
    # The critical fix: web/libraries/nouislider ships its own lock-less
    # composer.json, and its actual install dir (nouislider) differs from
    # its package's {$name} (nouislider_js) via extra.installer-name -- both
    # must be seen past to get the composer.lock's drupal-library -> contrib.
    assert (DRUPAL_CORPUS / "web/libraries/nouislider").is_dir()
    assert realm_of(DRUPAL_CORPUS / "web/libraries/nouislider/x.js") == "contrib"


@pytest.mark.skipif(not DRUPAL_CORPUS.is_dir(), reason="reference corpus not available")
def test_every_installed_contrib_type_package_gets_its_realm():
    """Every composer.lock package of a contrib-shaped type whose install dir
    actually exists on disk resolves to the realm its type implies."""
    lock = json.loads((DRUPAL_CORPUS / "composer.lock").read_text(encoding="utf-8"))
    contrib_types = {
        "drupal-module", "drupal-theme", "drupal-profile", "drupal-recipe",
        "drupal-drush", "drupal-library", "npm-asset", "bower-asset",
    }
    from graphify.drupal.boundary import _install_dir_for

    composer_json = json.loads((DRUPAL_CORPUS / "composer.json").read_text(encoding="utf-8"))
    checked = 0
    for package in lock["packages"] + lock.get("packages-dev", []):
        if package.get("type") not in contrib_types:
            continue
        # Recompute the install dir the same way boundary.py does, then check
        # it against realm_of when the directory exists on disk.
        installer_paths = composer_json.get("extra", {}).get("installer-paths", {})
        installer_name = (package.get("extra") or {}).get("installer-name")
        install_dir = _install_dir_for(
            package["name"], package["type"], installer_paths, DRUPAL_CORPUS, installer_name,
        )
        if install_dir is None or not Path(install_dir).is_dir():
            continue
        checked += 1
        assert realm_of(Path(install_dir)) == "contrib", (package["name"], install_dir)
    assert checked > 50, "expected the corpus to actually exercise this assertion"


def test_lockless_composer_json_is_not_a_project_root(tmp_path):
    """A contrib module (or a web/libraries package) shipping its own
    composer.json with no composer.lock beside it must not shadow the real
    project root -- its files still resolve through the project's own
    composer.lock."""
    packages = _PACKAGES + [{"name": "drupal/mylib", "type": "drupal-library"}]
    root = _composer_project(tmp_path, packages=packages)
    _touch(root, "web/modules/contrib/token/composer.json", json.dumps({"name": "drupal/token"}))
    _touch(root, "web/libraries/mylib/composer.json", json.dumps({"name": "drupal/mylib"}))
    assert realm_of(root / "web/modules/contrib/token/token.info.yml") == "contrib"
    assert realm_of(root / "web/libraries/mylib/mylib.js") == "contrib"
    imap = install_map(root / "web/modules/contrib/token/token.info.yml")
    assert imap.project_root == root.absolute().as_posix()
    assert not imap.error


def test_standalone_checkout_with_no_lock_anywhere_uses_path_rules(tmp_path):
    """A module checked out on its own -- composer.json present, but no
    composer.lock anywhere above it -- has no project root at all."""
    _touch(tmp_path, "composer.json", json.dumps({"name": "acme/mymod"}))
    _touch(tmp_path, "mymod.info.yml", "name: My module\n")
    assert install_map(tmp_path / "mymod.info.yml") is None
    assert realm_of(tmp_path / "mymod.info.yml") == "custom"


def test_installer_name_overrides_name_substitution(tmp_path):
    packages = _PACKAGES + [{
        "name": "drupal/nouislider_js", "type": "drupal-library",
        "extra": {"installer-name": "nouislider"},
    }]
    root = _composer_project(tmp_path, packages=packages)
    assert realm_of(root / "web/libraries/nouislider/x.js") == "contrib"
    assert boundary_dir(root / "web/libraries/nouislider") == ("contrib", "composer")


def test_caches_are_cleared(tmp_path):
    root = _composer_project(tmp_path)
    assert realm_of(root / "web/modules/contrib/token/token.info.yml") == "contrib"
    clear_caches()
    # A composer.lock edit after clear_caches() must be seen on the next call.
    _touch(root, "composer.lock", json.dumps({"packages": [], "packages-dev": []}))
    assert realm_of(root / "web/modules/contrib/token/token.info.yml") == "custom"
