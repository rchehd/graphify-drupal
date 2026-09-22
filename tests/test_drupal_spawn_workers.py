"""The seam must exist inside spawned subprocesses, not just the parent.

A spawned worker re-imports modules from scratch. It unpickles
graphify.extract._extract_single_file, which imports graphify.extract, which
imports the parent package graphify first — installing the hook before extract
executes. This test is the proof of that chain.
"""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

MODULE_INFO = "name: Foo\ntype: module\ndependencies:\n  - drupal:node\n"


def _probe_in_worker(_ignored):
    """Runs in a spawned subprocess: report whether core is patched there."""
    import graphify.detect as detect
    import graphify.extract as extract

    return (
        getattr(extract._get_extractor, "_drupal_patched", False),
        getattr(detect.classify_file, "_drupal_patched", False),
        getattr(detect._is_graphable_source, "_drupal_patched", False),
    )


def test_seam_is_active_in_a_spawned_worker():
    ctx = mp.get_context("spawn")
    with ctx.Pool(1) as pool:
        extract_patched, classify_patched, graphable_patched = pool.map(_probe_in_worker, [None])[0]
    assert extract_patched is True
    assert classify_patched is True
    assert graphable_patched is True


def test_parallel_extraction_matches_sequential(tmp_path: Path):
    """Whatever start method the pool uses, the graph must not depend on it."""
    from graphify.extract import extract

    paths = []
    for i in range(12):
        path = tmp_path / f"web/modules/custom/m{i}/m{i}.info.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MODULE_INFO, encoding="utf-8")
        paths.append(path)

    parallel = extract(paths, root=tmp_path, parallel=True)
    sequential = extract(paths, root=tmp_path, parallel=False)

    assert {n["id"] for n in parallel["nodes"]} == {n["id"] for n in sequential["nodes"]}
    assert len(parallel["edges"]) == len(sequential["edges"])
    assert len(parallel["edges"]) == 12
