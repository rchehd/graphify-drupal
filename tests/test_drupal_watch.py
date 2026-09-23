"""`graphify watch` rebuilds (not just flags) when Drupal YAML changes."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.drupal.register import DrupalSeamError, _patch_watch, install


def test_batch_triggers_rebuild_for_existing_drupal_yaml(tmp_path):
    install()
    import graphify.watch as w

    path = tmp_path / "foo.services.yml"
    path.write_text("services: {}\n", encoding="utf-8")
    assert w._batch_triggers_rebuild([path]) is True


def test_batch_triggers_rebuild_stays_false_for_non_drupal_yaml(tmp_path):
    install()
    import graphify.watch as w

    path = tmp_path / "docker-compose.yml"
    path.write_text("services: {}\n", encoding="utf-8")
    assert w._batch_triggers_rebuild([path]) is False


def test_batch_needs_llm_flag_is_false_for_drupal_yaml(tmp_path):
    install()
    import graphify.watch as w

    path = tmp_path / "foo.services.yml"
    path.write_text("services: {}\n", encoding="utf-8")
    assert w._batch_needs_llm_flag([path]) is False


def test_batch_needs_llm_flag_stays_true_for_a_readme(tmp_path):
    install()
    import graphify.watch as w

    path = tmp_path / "README.md"
    path.write_text("# hi\n", encoding="utf-8")
    assert w._batch_needs_llm_flag([path]) is True


def test_seam_fails_loudly_when_watch_loses_has_non_code(monkeypatch):
    import graphify.watch as w

    monkeypatch.delattr(w, "_has_non_code", raising=True)
    with pytest.raises(DrupalSeamError, match="_has_non_code"):
        _patch_watch(w)
