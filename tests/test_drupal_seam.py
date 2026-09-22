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
