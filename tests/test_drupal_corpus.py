"""P1's acceptance criteria, measured on a real Drupal tree.

Skipped when the corpus is absent, so CI stays hermetic. The numbers are the
ones in the P1 spec; a change in them is either a regression or a corpus that
moved, and both deserve a look.

Measured through `graphify.extract.extract`, not by calling each family's
extractor: collapsing duplicate ids and materialising undeclared endpoints both
happen in the pipeline, and a per-file loop would test neither.
"""
from __future__ import annotations

import collections
import os
from pathlib import Path

import pytest

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))

pytestmark = pytest.mark.skipif(
    not (CORPUS / "web" / "core").is_dir(),
    reason="reference Drupal corpus not present",
)


def _family_files() -> list[Path]:
    import graphify  # noqa: F401  (installs the seam)
    from graphify.drupal.families import is_drupal_yaml

    return sorted(
        p for p in CORPUS.glob("web/**/*.yml")
        if "node_modules" not in p.parts and is_drupal_yaml(p)
    )


def _is_drupal(node: dict) -> bool:
    return str(node.get("type", "")).startswith("drupal")


@pytest.fixture(scope="module")
def family_files() -> list[Path]:
    return _family_files()


@pytest.fixture(scope="module")
def corpus_extraction(family_files, tmp_path_factory):
    from graphify.extract import extract

    # A fresh cache: an entry written by an older extractor must not stand in
    # for what this one produces, and the corpus must not gain a graphify-out/.
    cache = tmp_path_factory.mktemp("corpus-cache")
    return extract(family_files, cache_root=cache, root=CORPUS)


def test_criterion_1_only_the_malformed_core_fixture_fails(family_files):
    from graphify.drupal.families import family_extractor

    errors = [p.name for p in family_files if family_extractor(p)(p).get("error")]
    assert errors == ["invalid_file.libraries.yml"]


def test_criterion_2_the_tolerant_loader_finds_every_service(corpus_extraction):
    services = [n for n in corpus_extraction["nodes"] if n["type"] == "drupal_service"]
    declared = [n for n in services if not n.get("external")]
    # 2,205 declarations; 14 are overrides of a service declared elsewhere.
    assert len(declared) == 2191, "1628 means safe_load crept back in"


def test_criterion_3_no_family_file_is_dropped_as_a_secret(family_files):
    from graphify.detect import _is_sensitive

    assert [p for p in family_files if _is_sensitive(p)] == []


def test_criterion_3b_real_secret_stores_are_still_caught():
    import graphify  # noqa: F401
    from graphify.detect import _is_sensitive

    for name in ("token.yml", "token.json", "credentials.yaml", "secrets.yml"):
        assert _is_sensitive(CORPUS / name), name


def test_criterion_4_only_info_yml_declares_extensions(corpus_extraction):
    declared = [
        n for n in corpus_extraction["nodes"]
        if n["type"] in ("drupal_module", "drupal_theme", "drupal_profile")
    ]
    # 1,140 files; three pairs are core's name-collision fixtures, one node each.
    assert len(declared) == 1137
    assert all(n["source_file"].endswith(".info.yml") for n in declared)
    # Any other extension node is one the resolver made for a name nothing declares.
    others = [n for n in corpus_extraction["nodes"]
              if n["id"].startswith("drupal_extension_") and n not in declared]
    assert all(n.get("external") for n in others)


def test_criterion_4b_no_drupal_id_is_salted(corpus_extraction):
    salted = [n["id"] for n in corpus_extraction["nodes"]
              if _is_drupal(n) and not n["id"].startswith("drupal_")]
    assert salted == []


def test_criterion_5_nothing_dangles_after_the_resolver(corpus_extraction):
    ids = {n["id"] for n in corpus_extraction["nodes"]}
    dangling = collections.Counter(
        e["relation"] for e in corpus_extraction["edges"]
        if e["source"] not in ids or e["target"] not in ids
    )
    assert dangling == {}


def test_criterion_6_every_entity_is_owned_once_per_declaring_file(corpus_extraction):
    declares = collections.Counter(
        e["target"] for e in corpus_extraction["edges"] if e["relation"].startswith("declares_")
    )
    declared_in = {n["id"]: len(n.get("declared_in") or [n]) for n in corpus_extraction["nodes"]}
    # More than one owner only where that many files declare the entity (Task 5b).
    mismatched = {t: n for t, n in declares.items() if n != declared_in.get(t)}
    assert mismatched == {}


def test_criterion_7_every_node_is_filterable(corpus_extraction):
    for node in corpus_extraction["nodes"]:
        if not _is_drupal(node):
            continue
        assert node.get("realm") in ("core", "contrib", "custom", "unknown"), node["id"]
        assert node.get("layer"), node["id"]
    # External nodes are named, not located, so `unknown` is their honest realm.
    unknown = [n["id"] for n in corpus_extraction["nodes"]
               if _is_drupal(n) and n["realm"] == "unknown" and not n.get("external")]
    assert unknown == []


def test_criterion_7b_the_custom_slice_renders_without_aggregation(corpus_extraction):
    custom = [n for n in corpus_extraction["nodes"] if n.get("realm") == "custom"]
    assert len(custom) < 5000, "the custom slice must stay visually readable"
