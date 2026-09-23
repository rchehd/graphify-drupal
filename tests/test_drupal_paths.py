"""Realm resolution and the *.info.yml predicate."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.drupal.paths import (
    DEFAULT_REALM_RULES,
    extension_machine_name,
    is_drupal_info_yaml,
    load_realm_rules,
    resolve_realm,
)


@pytest.mark.parametrize("name, expected", [
    ("foo.info.yml", True),
    ("mytheme.info.yml", True),
    (".info.yml", False),
    ("foo.services.yml", False),
    ("foo.yml", False),
    ("info.yml", False),    # no machine name -- not an extension file
    ("docker-compose.yml", False),
])
def test_info_yaml_predicate(name, expected):
    assert is_drupal_info_yaml(Path("/p") / name) is expected


def test_machine_name_is_the_stem_before_info():
    assert extension_machine_name(Path("/p/web/modules/custom/foo/foo.info.yml")) == "foo"


@pytest.mark.parametrize("path, expected", [
    ("/p/core/modules/node/node.info.yml", "core"),
    # The `core` pseudo-extension's own files, and the scaffold copies core ships.
    ("/p/web/core/core.services.yml", "core"),
    ("/p/web/core/core.libraries.yml", "core"),
    ("/p/web/core/assets/scaffold/files/default.services.yml", "core"),
    # Site-level files belong to no extension; unknown is the honest answer.
    ("/p/web/sites/default/default.services.yml", "unknown"),
    ("/p/web/core/modules/node/node.info.yml", "core"),
    ("/p/web/modules/contrib/token/token.info.yml", "contrib"),
    ("/p/docroot/themes/contrib/olivero/olivero.info.yml", "contrib"),
    ("/p/web/modules/custom/foo/foo.info.yml", "custom"),
    ("/p/modules/custom/foo/foo.info.yml", "custom"),
    ("/p/web/profiles/myprofile/modules/bar/bar.info.yml", "custom"),
    ("/p/web/modules/contrib/core_flag/core_flag.info.yml", "contrib"),
    # Core ships extension fixtures under core/tests; found on a real corpus,
    # where these 8 files were the only ones resolving to "unknown".
    ("/p/web/core/tests/fixtures/test_stable/test_stable.info.yml", "core"),
    ("/p/web/core/tests/Drupal/Tests/Core/Extension/modules/mh_test/mh_test.info.yml", "core"),
    ("/p/somewhere/odd/foo.info.yml", "unknown"),
])
def test_realm_resolution(path, expected):
    assert resolve_realm(Path(path)) == expected


def test_realm_rules_are_overridable_from_graphifyrc(tmp_path):
    (tmp_path / ".graphifyrc").write_text(
        "viz_node_limit=0\n"
        "drupal.realm.custom = */modules/acme/*\n",
        encoding="utf-8",
    )
    rules = load_realm_rules(tmp_path)
    assert rules["custom"] == ("*/modules/acme/*",)
    assert rules["core"] == DEFAULT_REALM_RULES["core"]
    assert resolve_realm(Path("/p/web/modules/acme/foo/foo.info.yml"), rules) == "custom"


def test_missing_graphifyrc_yields_defaults(tmp_path):
    assert load_realm_rules(tmp_path) == DEFAULT_REALM_RULES


def test_core_graphifyrc_reader_tolerates_our_keys(tmp_path):
    """Our keys must not break graphify's own .graphifyrc parser."""
    from graphify.hooks import _load_graphifyrc

    (tmp_path / ".graphifyrc").write_text(
        "drupal.realm.custom = */modules/acme/*\nviz_node_limit=0\n", encoding="utf-8"
    )
    assert _load_graphifyrc(tmp_path) == {"viz_node_limit": 0}
