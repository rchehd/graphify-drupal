"""The family table: one source for classification and dispatch."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.drupal.families import (
    FAMILY_EXTRACTORS,
    extension_owner,
    family_extractor,
    is_drupal_yaml,
)


def test_info_family_is_registered():
    from graphify.drupal.yaml_extract import extract_drupal_info

    assert FAMILY_EXTRACTORS[".info.yml"] is extract_drupal_info


def test_owner_is_the_name_before_the_family_suffix():
    assert extension_owner(Path("/p/views_ui.info.yml")) == "views_ui"
    assert extension_owner(Path("/p/token.services.yml")) == "token"


def test_an_unregistered_family_has_no_owner():
    """extension_owner answers only for files the table claims.

    `*.schema.yml` is configuration schema, which P1 does not read.
    """
    assert extension_owner(Path("/p/foo.schema.yml")) == ""


def test_the_longest_matching_suffix_wins(monkeypatch):
    """`*.links.menu.yml` must never be matched as some `*.menu.yml`.

    Asserted against a table holding both, so the property is pinned now rather
    than when the link families happen to be registered.
    """
    monkeypatch.setitem(FAMILY_EXTRACTORS, ".menu.yml", lambda path: {})
    monkeypatch.setitem(FAMILY_EXTRACTORS, ".links.menu.yml", lambda path: {})
    assert extension_owner(Path("/p/foo.links.menu.yml")) == "foo"
    assert extension_owner(Path("/p/foo.menu.yml")) == "foo"


@pytest.mark.parametrize("name", [
    "docker-compose.yml", "ci.yml", "token.yml", "credentials.yaml",
    "system.menu.main.yml", ".info.yml",
])
def test_non_family_yaml_is_not_drupal_yaml(name):
    assert is_drupal_yaml(Path("/p") / name) is False
    assert family_extractor(Path("/p") / name) is None


def test_classification_and_dispatch_cannot_disagree():
    """Every suffix is_drupal_yaml accepts must also have a handler."""
    for suffix in FAMILY_EXTRACTORS:
        probe = Path("/p/owner" + suffix)
        assert is_drupal_yaml(probe) is True
        assert family_extractor(probe) is not None
