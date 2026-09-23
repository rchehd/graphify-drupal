"""A changed Drupal extractor must not be served from core's AST cache.

Core keys AST entries by graphify's version, a schema number and the file's
content hash. None of those moves when this package's extractors change, so an
unchanged `*.services.yml` would keep the nodes an older extractor produced.
"""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.fingerprint import drupal_fingerprint
from graphify.drupal.register import install


def test_fingerprint_follows_the_package_source(tmp_path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")
    before = drupal_fingerprint(tmp_path)
    assert drupal_fingerprint(tmp_path) == before
    (tmp_path / "b.py").write_text("# changed\n", encoding="utf-8")
    assert drupal_fingerprint(tmp_path) != before


def test_fingerprint_ignores_non_source_files(tmp_path):
    (tmp_path / "a.py").write_text("# a\n", encoding="utf-8")
    before = drupal_fingerprint(tmp_path)
    (tmp_path / "notes.txt").write_text("irrelevant\n", encoding="utf-8")
    assert drupal_fingerprint(tmp_path) == before


def test_the_ast_cache_is_namespaced_by_the_drupal_fingerprint(tmp_path):
    install()
    import graphify.cache as cache

    suffix = f"+drupal.{drupal_fingerprint()}"
    assert cache._EXTRACTOR_VERSION.endswith(suffix)
    assert cache._EXTRACTOR_VERSION.count("+drupal.") == 1
    assert suffix in cache.cache_dir(tmp_path).name


def test_patching_twice_does_not_stack_the_suffix():
    from graphify.drupal.register import _patch_cache
    import graphify.cache as cache

    install()
    _patch_cache(cache)
    assert cache._EXTRACTOR_VERSION.count("+drupal.") == 1


def test_the_real_package_is_fingerprinted():
    package = Path(__file__).resolve().parents[1] / "graphify" / "drupal"
    assert drupal_fingerprint() == drupal_fingerprint(package)
