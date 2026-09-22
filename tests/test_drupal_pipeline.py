"""P0's central bet: a runtime-registered extractor behaves like a built-in one.

Covers detect -> extract -> build, the incremental path through the real CLI, and
the requirement that non-Drupal YAML is completely unaffected.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal.yaml_extract import extension_id

MODULE_INFO = """\
name: Foo
type: module
dependencies:
  - drupal:node
  - token
"""

THEME_INFO = "name: My Theme\ntype: theme\nbase theme: olivero\n"
CONTRIB_INFO = "name: Token\ntype: module\n"


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    files = {
        "web/modules/custom/foo/foo.info.yml": MODULE_INFO,
        "web/modules/contrib/token/token.info.yml": CONTRIB_INFO,
        "web/themes/custom/mytheme/mytheme.info.yml": THEME_INFO,
        "docker-compose.yml": "services:\n  web:\n    image: php:8.3\n",
        ".github/workflows/ci.yml": "name: CI\non: [push]\n",
        "web/modules/custom/foo/src/Foo.php": "<?php\nclass Foo {}\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_detect_classifies_info_yaml_as_code_and_other_yaml_as_document(corpus):
    from graphify.detect import detect

    result = detect(corpus)
    code = {Path(p).name for p in result["files"].get("code", [])}
    docs = {Path(p).name for p in result["files"].get("document", [])}
    assert {"foo.info.yml", "token.info.yml", "mytheme.info.yml"} <= code
    assert {"docker-compose.yml", "ci.yml"} <= docs
    assert "docker-compose.yml" not in code


def test_extract_produces_drupal_nodes_and_edges(corpus):
    from graphify.extract import extract

    paths = list(corpus.rglob("*.info.yml"))
    result = extract(paths, root=corpus)
    ids = {n["id"] for n in result["nodes"]}
    assert extension_id("foo") in ids
    assert extension_id("mytheme") in ids
    relations = {(e["source"], e["relation"], e["target"]) for e in result["edges"]}
    assert (extension_id("foo"), "depends_on_module", extension_id("node")) in relations
    assert (extension_id("mytheme"), "base_theme", extension_id("olivero")) in relations


def test_collect_files_does_not_reach_info_yaml(corpus):
    """Documents the boundary: collect_files walks `_DISPATCH` suffixes.

    `.info.yml` has suffix `.yml`, which is not a dispatch key, so the library
    walker does not return it. Drupal files reach extraction through `detect()`,
    which the CLI drives — this is why the incremental tests below use the CLI.
    """
    from graphify.extract import collect_files

    found = {p.name for p in collect_files(corpus, root=corpus)}
    assert "foo.info.yml" not in found
    assert "Foo.php" in found


def test_build_preserves_the_drupal_taxonomy(corpus):
    from graphify.build import build
    from graphify.extract import extract

    paths = list(corpus.rglob("*.info.yml"))
    graph = build([extract(paths, root=corpus)])
    node = graph.nodes[extension_id("foo")]
    assert node["file_type"] == "code"       # not rewritten to "concept"
    assert node["type"] == "drupal_module"   # taxonomy survived
    assert node["realm"] == "custom"
    assert node["layer"] == "extension"


def test_no_dependency_edge_is_left_dangling(corpus):
    """Every named extension resolves to a node, declared or external.

    Asserted as an invariant rather than by exact id: when one machine name is
    declared in one file and referenced from another, graphify may disambiguate
    the two by prefixing each with its file stem. On a real 1,140-extension tree
    that happens 6 times, all of them core's own deliberate name-collision
    fixtures (`evil` ships as both a module and a theme), and no dependency edge
    points at one. What has to hold everywhere is that no edge dangles.
    """
    from graphify.build import build
    from graphify.extract import extract

    paths = list(corpus.rglob("*.info.yml"))
    result = extract(paths, root=corpus)
    ids = {n["id"] for n in result["nodes"]}
    dangling = [
        e for e in result["edges"]
        if e.get("relation") in ("depends_on_module", "base_theme") and e["target"] not in ids
    ]
    assert dangling == []

    graph = build([result])
    assert {n for n in ids} <= set(graph.nodes)


def test_undeclared_target_becomes_an_external_node(corpus):
    """`olivero` is named as a base theme but declared by nothing here."""
    from graphify.extract import extract

    result = extract(list(corpus.rglob("*.info.yml")), root=corpus)
    olivero = next(n for n in result["nodes"] if n["id"] == extension_id("olivero"))
    assert olivero["external"] is True
    assert olivero["file_type"] == "concept"
    assert olivero["label"] == "olivero"


def test_declared_extension_keeps_its_realm_and_type(corpus):
    """A contrib extension declared in the corpus is not downgraded to external."""
    from graphify.extract import extract

    result = extract(list(corpus.rglob("*.info.yml")), root=corpus)
    declared = [
        n for n in result["nodes"]
        if n.get("source_file", "").endswith("token.info.yml") and not n.get("external")
    ]
    assert len(declared) == 1
    assert declared[0]["realm"] == "contrib"
    assert declared[0]["type"] == "drupal_module"
    assert declared[0]["file_type"] == "code"


def _run_cli(corpus: Path) -> dict:
    """Run the real CLI and return the produced graph.json.

    `--code-only` keeps the run deterministic and offline by skipping the LLM
    document pass. Going through the CLI rather than calling a cache function
    directly is deliberate: it tests the incremental path the user actually
    gets, whichever cache layer implements it.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(corpus), "--code-only"],
        capture_output=True, text=True, cwd=corpus,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((corpus / "graphify-out" / "graph.json").read_text(encoding="utf-8"))


def _relations(graph: dict) -> set[tuple[str, str, str]]:
    links = graph.get("links", graph.get("edges", []))
    return {(e["source"], e["relation"], e["target"]) for e in links if "relation" in e}


def test_cli_produces_drupal_edges(corpus):
    graph = _run_cli(corpus)
    assert (extension_id("foo"), "depends_on_module", extension_id("node")) in _relations(graph)


def test_rerunning_with_no_changes_reproduces_the_same_graph(corpus):
    first = _run_cli(corpus)
    second = _run_cli(corpus)
    assert {n["id"] for n in second["nodes"]} == {n["id"] for n in first["nodes"]}
    assert _relations(second) == _relations(first)


def test_changing_one_dependency_changes_exactly_one_edge(corpus):
    before = _relations(_run_cli(corpus))
    (corpus / "web/modules/custom/foo/foo.info.yml").write_text(
        MODULE_INFO.replace("  - token\n", "  - path_alias\n"), encoding="utf-8"
    )
    after = _relations(_run_cli(corpus))

    assert before - after == {
        (extension_id("foo"), "depends_on_module", extension_id("token"))
    }
    assert after - before == {
        (extension_id("foo"), "depends_on_module", extension_id("path_alias"))
    }
