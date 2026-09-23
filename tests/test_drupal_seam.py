"""The one place graphify.drupal touches core, and its guard rails."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from graphify.drupal.register import DrupalSeamError, _patch_detect, _patch_extract, install


def test_install_is_idempotent():
    install()
    install()
    from graphify.drupal.register import _DrupalFinder

    assert sum(isinstance(f, _DrupalFinder) for f in sys.meta_path) == 1


def test_classify_file_promotes_info_yaml_to_code():
    install()
    import graphify.detect as detect

    assert detect.classify_file(
        Path("/p/web/modules/custom/foo/foo.info.yml")
    ) is detect.FileType.CODE


def test_classify_file_leaves_other_yaml_alone():
    install()
    import graphify.detect as detect

    assert detect.classify_file(Path("/p/docker-compose.yml")) is detect.FileType.DOCUMENT
    assert detect.classify_file(Path("/p/.github/workflows/ci.yml")) is detect.FileType.DOCUMENT


def test_get_extractor_routes_info_yaml_and_nothing_else():
    install()
    import graphify.extract as extract
    from graphify.drupal.yaml_extract import extract_drupal_info

    assert extract._get_extractor(Path("/p/foo.info.yml")) is extract_drupal_info
    assert extract._get_extractor(Path("/p/docker-compose.yml")) is None
    assert extract._get_extractor(Path("/p/a.py")) is extract.extract_python


def test_patching_twice_does_not_stack_wrappers():
    install()
    import graphify.detect as detect

    first = detect.classify_file
    _patch_detect(detect)
    assert detect.classify_file is first


def test_seam_fails_loudly_when_core_moves(monkeypatch):
    """An upstream rename must crash, not silently drop every Drupal edge."""
    import graphify.detect as detect

    monkeypatch.delattr(detect, "classify_file", raising=True)
    with pytest.raises(DrupalSeamError, match="classify_file"):
        _patch_detect(detect)


def test_seam_fails_loudly_when_dispatch_is_replaced(monkeypatch):
    import graphify.extract as extract

    monkeypatch.setattr(extract, "_DISPATCH", None)
    monkeypatch.setattr(extract._get_extractor, "_drupal_patched", False, raising=False)
    with pytest.raises(DrupalSeamError, match="_DISPATCH"):
        _patch_extract(extract)


def test_drupal_info_yaml_is_not_treated_as_a_secret_store():
    """`token.info.yml` is an extension declaration, not a credential dump.

    Core's Stage 3 keyword screen drops it because the stem `token.info` is two
    words containing `token`. Promotion to CODE alone does not save it: the
    exemption runs through `_is_graphable_source`, which excludes every data
    format including `.yml`.
    """
    install()
    import graphify.detect as detect

    assert detect._is_sensitive(Path("/p/web/modules/contrib/token/token.info.yml")) is False
    assert detect._is_sensitive(Path("/p/web/modules/custom/foo/foo.info.yml")) is False
    assert detect._is_sensitive(Path("/p/web/modules/contrib/token/token.services.yml")) is False
    assert detect._is_sensitive(Path("/p/web/modules/contrib/token/token.routing.yml")) is False
    assert detect._is_sensitive(Path("/p/web/modules/contrib/token/token.libraries.yml")) is False


def test_real_secret_stores_are_still_caught():
    """The exemption must be exactly as wide as the family table and no wider."""
    install()
    import graphify.detect as detect

    for name in ("token.yml", "token.json", "credentials.yaml", "secrets.yml",
                 "api_token.txt"):
        assert detect._is_sensitive(Path("/p") / name) is True, name


def test_drupal_duplicates_are_collapsed_before_core_splits_them():
    install()
    import graphify.extract as extract

    wrapped = extract._disambiguate_colliding_node_ids
    assert getattr(wrapped, "_drupal_patched", False)
