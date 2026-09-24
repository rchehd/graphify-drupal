"""Hook implementations: `#[Hook]` attributes and procedural `<ext>_<hook>()`
functions (P2b Task 4, spec §5.2-§5.5)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graphify.drupal.discovery import affected_files, build_registry, prepare_run
from graphify.drupal.hooks import (
    extract_hook_implementations,
    hook_id,
    hook_impl_id,
    is_hook_class_file,
    is_procedural_file,
)
from graphify.drupal.inventory import build_inventory, render_section
from graphify.drupal.register import DrupalSeamError, _patch_detect, _patch_extract, _patch_watch, install
from graphify.drupal.yaml_extract import extension_id
from tests.test_drupal_discovery import _module, _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

CORE_API_PHP = """<?php

function hook_cron() {
}

function hook_form_FORM_ID_alter(&$form, $form_state, $form_id) {
}

function hook_entity_insert($entity) {
}
"""

FOO_MODULE = """<?php

use Drupal\\Core\\Form\\FormStateInterface;

/**
 * Implements hook_cron().
 */
function foo_cron() {
}

/**
 * Implements hook_form_FORM_ID_alter().
 */
function foo_form_user_login_form_alter(&$form, FormStateInterface $form_state, $form_id) {
}

function foo_helper() {
}

function _foo_private() {
}
"""

FOO_VIEWS_INC = """<?php

/**
 * Implements hook_views_data().
 */
function foo_views_data() {
  return [];
}
"""

FOO_HOOKS_PHP = """<?php

namespace Drupal\\foo\\Hook;

use Drupal\\Core\\Hook\\Attribute\\Hook;
use Drupal\\Core\\Hook\\Order\\Order;

#[Hook('cron', method: 'run')]
class FooHooks {

  #[Hook('entity_insert', order: Order::First)]
  public function entityInsert($entity) {
  }

  public function run() {
  }

  #[Hook('node_insert', module: 'bar')]
  public function nodeInsert($node) {
  }

}

#[\\Drupal\\Core\\Hook\\Attribute\\Hook('cron')]
class FooCronInvoke {

  public function __invoke() {
  }

}
"""

NOT_A_HOOK_PHP = """<?php

namespace Drupal\\foo;

use Some\\Other\\Hook;

class NotHooks {

  #[Hook('cron')]
  public function cron() {
  }

}
"""

FOO = "web/modules/custom/foo"


def _hooks_site(tmp_path: Path) -> Path:
    files = {
        "web/core/core.api.php": CORE_API_PHP,
        **_module("foo", "services: {}\n", {
            "foo.module": FOO_MODULE,
            "foo.views.inc": FOO_VIEWS_INC,
            "src/Hook/FooHooks.php": FOO_HOOKS_PHP,
            "src/NotHooks.php": NOT_A_HOOK_PHP,
            "includes/foo.admin.inc": "<?php\nfunction foo_admin_cron() {}\n",
        }),
        "web/libraries/lib/lib.inc": "<?php\nfunction lib_cron() {}\n",
        "web/libraries/lib/lib.module": "<?php\nfunction lib_cron() {}\n",
    }
    return _site(tmp_path, files)


def _core_php(path: Path) -> dict:
    import graphify.extract as core

    return core._DISPATCH[".php"](path)


def _rel(result: dict) -> set[tuple[str, str, str]]:
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def _node(result: dict, nid: str) -> dict:
    found = [n for n in result["nodes"] if n["id"] == nid]
    assert len(found) == 1, (nid, found)
    return found[0]


def _core_id(core_result: dict, label: str) -> str:
    found = [n["id"] for n in core_result["nodes"] if n.get("label") == label]
    assert len(found) == 1, (label, found)
    return found[0]


# -- which files are procedural ------------------------------------------------


def test_is_procedural_file(tmp_path):
    root = _hooks_site(tmp_path)
    foo = root / FOO
    assert is_procedural_file(foo / "foo.module")
    assert is_procedural_file(foo / "foo.views.inc")
    assert not is_procedural_file(foo / "includes/foo.admin.inc")  # not beside the info file
    assert not is_procedural_file(foo / "foo.info.yml")
    assert not is_procedural_file(foo / "src/Hook/FooHooks.php")
    assert not is_procedural_file(root / "web/libraries/lib/lib.inc")
    assert not is_procedural_file(root / "web/libraries/lib/lib.module")
    for suffix in (".install", ".theme", ".profile"):
        path = foo / f"foo{suffix}"
        path.write_text("<?php\n", encoding="utf-8")
        assert is_procedural_file(path)
    # A file named after another extension is not this directory's.
    (foo / "bar.module").write_text("<?php\n", encoding="utf-8")
    assert not is_procedural_file(foo / "bar.module")


def test_procedural_files_classify_as_code_only_beside_their_info_file(tmp_path):
    install()
    import graphify.detect as detect

    root = _hooks_site(tmp_path)
    foo = root / FOO
    assert detect.classify_file(foo / "foo.module") == detect.FileType.CODE
    assert detect.classify_file(foo / "foo.views.inc") == detect.FileType.CODE
    # `.module` means nothing to core: a library's stays unclassified.
    assert detect.classify_file(root / "web/libraries/lib/lib.module") is None
    # `.inc` was already core's (Pascal include): a non-extension one keeps
    # exactly core's own answer and core's own handler.
    lib_inc = root / "web/libraries/lib/lib.inc"
    assert detect.classify_file(lib_inc) == detect.classify_file.__wrapped__(lib_inc)
    import graphify.extract as core

    assert core._get_extractor(lib_inc) is core._DISPATCH[".inc"]
    assert core._get_extractor(foo / "includes/foo.admin.inc") is core._DISPATCH[".inc"]
    assert core._get_extractor(root / "web/libraries/lib/lib.module") is None


def test_code_extensions_are_mutated_in_place_and_watch_sees_them():
    install()
    import graphify.detect as detect
    import graphify.watch as watch

    for suffix in (".module", ".install", ".theme", ".profile", ".inc"):
        assert suffix in detect.CODE_EXTENSIONS
        assert suffix in watch._WATCHED_EXTENSIONS
    assert watch._CODE_EXTENSIONS is detect.CODE_EXTENSIONS


def test_the_seam_fails_loudly_when_what_it_extends_changed_shape(monkeypatch):
    install()
    import graphify.detect as detect
    import graphify.extract as core
    import graphify.watch as watch

    monkeypatch.setattr(detect, "CODE_EXTENSIONS", frozenset(detect.CODE_EXTENSIONS))
    with pytest.raises(DrupalSeamError, match=r"CODE_EXTENSIONS"):
        _patch_detect(detect)
    monkeypatch.undo()

    monkeypatch.setattr(watch, "_CODE_EXTENSIONS", set(detect.CODE_EXTENSIONS))
    with pytest.raises(DrupalSeamError, match=r"watch\._CODE_EXTENSIONS"):
        _patch_watch(watch)
    monkeypatch.undo()

    monkeypatch.setattr(core, "_DISPATCH", {k: v for k, v in core._DISPATCH.items() if k != ".php"})
    with pytest.raises(DrupalSeamError, match=r"'\.php' handler"):
        _patch_extract(core)


# -- procedural implementations --------------------------------------------------


def test_procedural_implementations(tmp_path, _isolated_discovery_state):
    install()
    root = _hooks_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "foo.module"
    core_result = _core_php(path)

    result = extract_hook_implementations(path, core_result)

    impl = hook_impl_id("foo", "cron")
    assert [n["id"] for n in result["nodes"]] == [impl]
    node = _node(result, impl)
    assert node["type"] == "drupal_hook_impl"
    assert node["layer"] == "hook"
    assert node["module"] == "foo"
    assert node["hook_name"] == "cron"
    assert node["via"] == "procedural"
    assert node["function"] == "foo_cron"
    assert "order" not in node and "class_name" not in node
    assert _rel(result) == {
        (extension_id("foo"), "implements_hook", hook_id("cron")),
        (impl, "hook_implemented_by", _core_id(core_result, "foo_cron()")),
    }
    # `form_user_login_form_alter` only matches a variable hook: no edge.
    assert result["hook_candidates"] == [{
        "kind": "variable", "module": "foo", "name": "form_user_login_form_alter",
        "pattern": "form_*_alter", "file": str(path), "line": 14,
    }]


def test_an_undeclared_hook_in_a_group_file_is_only_a_candidate(tmp_path, _isolated_discovery_state):
    install()
    root = _hooks_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "foo.views.inc"

    result = extract_hook_implementations(path, _core_php(path))

    assert result["nodes"] == [] and result["edges"] == []
    assert result["hook_candidates"] == [{
        "kind": "undeclared", "module": "foo", "name": "views_data",
        "file": str(path), "line": 6,
    }]


# -- attribute implementations -----------------------------------------------------


def test_attribute_implementations(tmp_path, _isolated_discovery_state):
    install()
    root = _hooks_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/Hook/FooHooks.php"
    core_result = _core_php(path)

    result = extract_hook_implementations(path, core_result)

    insert, cron = hook_impl_id("foo", "entity_insert"), hook_impl_id("foo", "cron")
    assert sorted(n["id"] for n in result["nodes"]) == sorted([insert, cron])
    node = _node(result, insert)
    assert (node["via"], node["class_name"], node["method"], node["order"]) == (
        "attribute", "FooHooks", "entityInsert", "Order::First")
    assert node["module"] == "foo" and node["hook_name"] == "entity_insert"
    assert "function" not in node
    # Two classes implement cron: one node (the first), one edge per method.
    node = _node(result, cron)
    assert (node["class_name"], node["method"]) == ("FooHooks", "run")
    assert "order" not in node
    assert _rel(result) == {
        (extension_id("foo"), "implements_hook", hook_id("entity_insert")),
        (extension_id("foo"), "implements_hook", hook_id("cron")),
        (insert, "hook_implemented_by", _core_id(core_result, ".entityInsert()")),
        (cron, "hook_implemented_by", _core_id(core_result, ".run()")),
        (cron, "hook_implemented_by", _core_id(core_result, ".__invoke()")),
    }
    assert result["hook_candidates"] == [{
        "kind": "undeclared", "module": "bar", "name": "node_insert",
        "file": str(path), "line": 18,
    }]


def test_the_module_argument_overrides_the_owner(tmp_path, _isolated_discovery_state):
    install()
    root = _hooks_site(tmp_path)
    path = root / FOO / "src/Hook/BarHooks.php"
    path.write_text(
        "<?php\nnamespace Drupal\\foo\\Hook;\nuse Drupal\\Core\\Hook\\Attribute\\Hook;\n"
        "class BarHooks {\n  #[Hook(hook: 'cron', module: 'bar', order: new OrderAfter(['x']))]\n"
        "  public function cron() {}\n}\n", encoding="utf-8")
    prepare_run(root)
    core_result = _core_php(path)

    result = extract_hook_implementations(path, core_result)

    impl = hook_impl_id("bar", "cron")
    node = _node(result, impl)
    assert (node["module"], node["order"]) == ("bar", "new OrderAfter(['x'])")
    assert _rel(result) == {
        (extension_id("bar"), "implements_hook", hook_id("cron")),
        (impl, "hook_implemented_by", _core_id(core_result, ".cron()")),
    }


def test_a_hook_attribute_of_another_namespace_is_ignored(tmp_path, _isolated_discovery_state):
    install()
    root = _hooks_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/NotHooks.php"

    result = extract_hook_implementations(path, _core_php(path))
    assert result == {"nodes": [], "edges": [], "hook_candidates": []}


def test_no_registry_and_bad_input_yield_nothing(tmp_path, _isolated_discovery_state):
    root = _hooks_site(tmp_path)
    path = root / FOO / "foo.module"
    empty = {"nodes": [], "edges": [], "hook_candidates": []}
    assert extract_hook_implementations(path, _core_php(path)) == empty

    prepare_run(root)
    garbage = root / FOO / "foo.install"
    garbage.write_bytes(b"\x00\xff<?php function (((")
    assert extract_hook_implementations(garbage, {}) == empty
    assert extract_hook_implementations(root / FOO / "missing.module", {"nodes": []}) == empty


def test_every_edge_target_is_computed_with_cores_own_ids(tmp_path, _isolated_discovery_state):
    """The target is emitted only when core emitted that very id for the file
    (spec §5.3): with core's result emptied, no `hook_implemented_by` survives."""
    install()
    root = _hooks_site(tmp_path)
    prepare_run(root)
    for rel in ("foo.module", "src/Hook/FooHooks.php"):
        path = root / FOO / rel
        core_result = _core_php(path)
        ids = {n["id"] for n in core_result["nodes"]}
        result = extract_hook_implementations(path, core_result)
        targets = [e["target"] for e in result["edges"] if e["relation"] == "hook_implemented_by"]
        assert targets and all(t in ids for t in targets)

        starved = extract_hook_implementations(path, {"nodes": [], "edges": []})
        assert not [e for e in starved["edges"] if e["relation"] == "hook_implemented_by"]
        assert [n["id"] for n in starved["nodes"]] == [n["id"] for n in result["nodes"]]


# -- the pipeline ------------------------------------------------------------------


def test_pipeline_extracts_procedural_files_as_php_with_implementations(
    tmp_path, _isolated_discovery_state,
):
    install()
    import graphify.detect as detect
    from graphify.extract import extract

    root = _hooks_site(tmp_path)
    found = detect.detect(root)
    code = {Path(p).resolve() for p in found["files"]["code"]}
    foo = (root / FOO).resolve()
    assert foo / "foo.module" in code
    assert foo / "foo.views.inc" in code
    assert (root / "web/libraries/lib/lib.module").resolve() not in code

    paths = [root / FOO / "foo.module", root / FOO / "foo.views.inc",
             root / FOO / "src/Hook/FooHooks.php"]
    result = extract(paths, root=root)

    ids = {n["id"] for n in result["nodes"]}
    # core's PHP extractor ran on the `.module` file.
    assert any(n.get("label") == "foo_cron()" for n in result["nodes"])
    implemented_by = [e for e in result["edges"] if e["relation"] == "hook_implemented_by"]
    assert len(implemented_by) == 4
    assert all(e["target"] in ids and e["source"] in ids for e in implemented_by)
    # foo implements cron in two files: one node, both files named.
    cron = _node(result, hook_impl_id("foo", "cron"))
    assert cron["declared_in"] == [f"{FOO}/foo.module", f"{FOO}/src/Hook/FooHooks.php"]
    assert cron["via"] == "procedural"


# -- the inventory -------------------------------------------------------------------


def test_the_inventory_lists_every_candidate_of_the_detected_files(tmp_path, _isolated_discovery_state):
    install()
    root = _hooks_site(tmp_path)
    registry = prepare_run(root)
    detected = {str(p) for p in root.rglob("*") if p.is_file()}

    inventory = build_inventory(registry, detected, root)

    assert inventory["hook_candidates"] == [
        {"kind": "undeclared", "module": "bar", "name": "node_insert",
         "file": f"{FOO}/src/Hook/FooHooks.php", "line": 18},
        {"kind": "undeclared", "module": "foo", "name": "views_data",
         "file": f"{FOO}/foo.views.inc", "line": 6},
        {"kind": "variable", "module": "foo", "name": "form_user_login_form_alter",
         "pattern": "form_*_alter", "file": f"{FOO}/foo.module", "line": 14},
    ]
    assert inventory["summary"]["hook_candidates"] == 3
    text = render_section(inventory)
    assert "### Hook candidates\n- undeclared: 2\n- variable: 1" in text


def test_the_inventory_counts_only_detected_files(tmp_path, _isolated_discovery_state):
    install()
    root = _hooks_site(tmp_path)
    registry = prepare_run(root)
    detected = {str(root / FOO / "foo.module")}

    inventory = build_inventory(registry, detected, root)
    assert [c["name"] for c in inventory["hook_candidates"]] == ["form_user_login_form_alter"]


# -- affected_files ----------------------------------------------------------------


def test_a_changed_hook_set_forces_every_implementing_file(tmp_path):
    root = _hooks_site(tmp_path)
    previous = build_registry(root)
    (root / "web/core/core.api.php").write_text(
        CORE_API_PHP + "\nfunction hook_views_data() {\n}\n", encoding="utf-8")
    current = build_registry(root)

    forced = affected_files(previous, current)
    foo = (root / FOO).as_posix()
    assert {f"{foo}/foo.module", f"{foo}/foo.views.inc", f"{foo}/src/Hook/FooHooks.php",
            (root / "web/core/core.api.php").as_posix()} <= forced
    assert f"{foo}/src/NotHooks.php" not in forced
    assert f"{foo}/includes/foo.admin.inc" not in forced


def test_an_unchanged_hook_set_forces_no_implementing_file(tmp_path):
    root = _hooks_site(tmp_path)
    assert affected_files(build_registry(root), build_registry(root)) == set()


# -- the corpus (spec §8 item 3) --------------------------------------------------

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))


@pytest.mark.skipif(not (CORPUS / "web" / "core").is_dir(), reason="reference Drupal corpus not present")
def test_corpus_custom_implementations_are_edges_or_candidates(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect

    registry = prepare_run(CORPUS, cache_root=tmp_path)
    assert registry is not None
    found = detect.detect(CORPUS, cache_root=tmp_path)
    code = [Path(p) for p in found["files"]["code"] if "/tests/" not in p]

    impls: dict[str, int] = {"attribute": 0, "procedural": 0}
    candidates: dict[str, int] = {}
    for path in code:
        if path.suffix != ".php" and not is_procedural_file(path):
            continue
        result = extract_hook_implementations(path, _core_php(path))
        edges = [e for e in result["edges"] if e["relation"] == "hook_implemented_by"]
        via = {n["id"]: n["via"] for n in result["nodes"]}
        for e in edges:
            impls[via[e["source"]]] += 1
        for c in result["hook_candidates"]:
            candidates[c["kind"]] = candidates.get(c["kind"], 0) + 1
    # Measured on FormsRemote (task-4-report.md): spec §2 counts 19 attribute
    # hooks naming a declared hook literally and 23 procedural ones.
    assert impls == {"attribute": 19, "procedural": 23}
    # 12 attribute candidates (3 undeclared, 9 variable) + 15 procedural
    # variable ones (`update_N` / `post_update_*` are not hooks), none under tests/.
    assert candidates == {"undeclared": 3, "variable": 24}


# -- fix round 1 ---------------------------------------------------------------------


def test_a_boundary_hook_target_becomes_a_missing_hook_stub(tmp_path, _isolated_discovery_state):
    """`cron` is declared only in (boundary) core: the `implements_hook` edge
    survives into the graph, pointing at a materialised `drupal_hook` stub."""
    install()
    from graphify.build import build_from_json
    from graphify.extract import extract

    root = _hooks_site(tmp_path)
    prepare_run(root)
    result = extract([root / FOO / "foo.module", root / FOO / "foo.info.yml"], root=root)

    stub = _node(result, hook_id("cron"))
    assert (stub["type"], stub["layer"], stub["label"], stub["missing"]) == (
        "drupal_hook", "hook", "cron", True)
    assert (extension_id("foo"), "implements_hook", hook_id("cron")) in _rel(result)
    graph = build_from_json(result)
    assert graph.has_edge(extension_id("foo"), hook_id("cron")) or graph.has_edge(
        hook_id("cron"), extension_id("foo"))


def _cli(root: Path, out: Path) -> None:
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "GRAPHIFY_DRUPAL_DISCOVERY"}
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--out", str(out)],
        capture_output=True, text=True, cwd=root, env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_an_incremental_run_with_out_writes_nothing_into_the_site(tmp_path):
    """Core's `detect_incremental` calls `detect()` without a `cache_root`, so
    its word-count cache would land in `<site>/graphify-out/`; the seam hands
    the `--out` location down, as a full run gets it."""
    root = _hooks_site(tmp_path / "site")
    out = tmp_path / "out"
    _cli(root, out)
    # A new procedural file: a word-count entry the index does not hold yet.
    (root / FOO / "foo.install").write_text("<?php\nfunction foo_install() {}\n", encoding="utf-8")
    _cli(root, out)

    assert not (root / "graphify-out").exists()
    graph = json.loads(next(out.rglob("graph.json")).read_text(encoding="utf-8"))
    edges = graph.get("links") or graph.get("edges")
    assert any(e.get("relation") == "implements_hook" for e in edges)


# -- minors round ------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


_HOOK_USE = "<?php\nnamespace Drupal\\foo\\Hook;\nuse Drupal\\Core\\Hook\\Attribute\\Hook;\n"


def test_is_hook_class_file_is_relative_to_the_owning_extension(tmp_path, _isolated_discovery_state):
    root = _hooks_site(tmp_path)
    hooks = root / FOO / "src/Hook/FooHooks.php"
    nested = _write(root / FOO / "lib/src/Hook/Nested.php", "<?php\n")
    stray = _write(root / "web/libraries/x/src/Hook/X.php", "<?php\n")
    assert not is_hook_class_file(hooks)  # no registry
    prepare_run(root)
    assert is_hook_class_file(hooks)
    assert not is_hook_class_file(nested)
    assert not is_hook_class_file(stray)
    assert not is_hook_class_file(root / FOO / "src/NotHooks.php")


def test_a_hook_attribute_outside_src_hook_is_misplaced(tmp_path, _isolated_discovery_state):
    install()
    import graphify.extract as core

    root = _hooks_site(tmp_path)
    path = _write(root / FOO / "src/Other/Misplaced.php",
                  "<?php\nnamespace Drupal\\foo\\Other;\nuse Drupal\\Core\\Hook\\Attribute\\Hook;\n"
                  "class Misplaced {\n  #[Hook('cron')]\n  public function cron() {}\n}\n")
    prepare_run(root)

    result = extract_hook_implementations(path, _core_php(path))
    assert result["nodes"] == [] and result["edges"] == []
    assert result["hook_candidates"] == [{
        "kind": "misplaced", "module": "foo", "name": "cron", "file": str(path), "line": 5}]
    # No implementation extractor is composed: the handler (core's own plus
    # the invocation extractor, final review minor 6) yields core's nodes only.
    composed = core._get_extractor(path)(path)
    assert [n["id"] for n in composed["nodes"]] == [n["id"] for n in _core_php(path)["nodes"]]
    assert not any(e["relation"] in ("implements_hook", "hook_implemented_by")
                   for e in composed["edges"])


def test_non_literal_module_or_method_is_a_candidate(tmp_path, _isolated_discovery_state):
    root = _hooks_site(tmp_path)
    path = _write(root / FOO / "src/Hook/Dynamic.php", _HOOK_USE + (
        "#[Hook('cron', method: self::M)]\n"
        "class Dynamic {\n"
        "  #[Hook('entity_insert', module: self::MODULE)]\n"
        "  public function insert() {}\n"
        "}\n"))
    prepare_run(root)

    result = extract_hook_implementations(path, _core_php(path))
    assert result["nodes"] == [] and result["edges"] == []
    assert result["hook_candidates"] == [
        {"kind": "non_literal", "module": "foo", "name": "cron", "file": str(path), "line": 4},
        {"kind": "non_literal", "module": "self::MODULE", "name": "entity_insert",
         "file": str(path), "line": 6},
    ]


def test_an_extension_implementing_its_own_hook_has_no_implements_edge(tmp_path, _isolated_discovery_state):
    root = _hooks_site(tmp_path)
    _write(root / FOO / "foo.api.php", "<?php\nfunction hook_foo_info() {\n}\n")
    path = root / FOO / "foo.module"
    path.write_text(FOO_MODULE + "\nfunction foo_foo_info() {\n}\n", encoding="utf-8")
    prepare_run(root)
    core_result = _core_php(path)

    result = extract_hook_implementations(path, core_result)
    impl = hook_impl_id("foo", "foo_info")
    _node(result, impl)
    rel = _rel(result)
    # `declares_hook` already joins foo -> foo_info: one relation per pair.
    assert (extension_id("foo"), "implements_hook", hook_id("foo_info")) not in rel
    assert (impl, "hook_implemented_by", _core_id(core_result, "foo_foo_info()")) in rel
    assert (extension_id("foo"), "implements_hook", hook_id("cron")) in rel


def test_update_functions_are_neither_implementations_nor_candidates(tmp_path, _isolated_discovery_state):
    root = _hooks_site(tmp_path)
    (root / "web/core/core.api.php").write_text(
        CORE_API_PHP + "\nfunction hook_update_N() {\n}\nfunction hook_post_update_NAME() {\n}\n"
        "function hook_update_last_removed() {\n}\n", encoding="utf-8")
    path = _write(root / FOO / "foo.install", (
        "<?php\nfunction foo_update_10001() {}\nfunction foo_post_update_fix() {}\n"
        "function foo_update_last_removed() {}\n"))
    prepare_run(root)

    result = extract_hook_implementations(path, _core_php(path))
    assert [n["hook_name"] for n in result["nodes"]] == ["update_last_removed"]
    assert result["hook_candidates"] == []


def test_several_hooks_on_one_method_and_a_duplicate_target(tmp_path, _isolated_discovery_state):
    root = _hooks_site(tmp_path)
    path = _write(root / FOO / "src/Hook/Multi.php", _HOOK_USE + (
        "#[Hook('cron', method: 'run')]\n"
        "class Multi {\n"
        "  #[Hook('cron')]\n"
        "  #[Hook('entity_insert'), Hook('form_alter')]\n"
        "  public function run() {}\n"
        "}\n"))
    prepare_run(root)
    core_result = _core_php(path)
    run = _core_id(core_result, ".run()")

    result = extract_hook_implementations(path, core_result)
    cron, insert = hook_impl_id("foo", "cron"), hook_impl_id("foo", "entity_insert")
    assert sorted(n["id"] for n in result["nodes"]) == sorted([cron, insert])
    # Class-level and method-level `cron` name the same method: one edge.
    assert sorted((e["source"], e["relation"], e["target"]) for e in result["edges"]) == sorted([
        (extension_id("foo"), "implements_hook", hook_id("cron")),
        (cron, "hook_implemented_by", run),
        (extension_id("foo"), "implements_hook", hook_id("entity_insert")),
        (insert, "hook_implemented_by", run),
    ])
    assert [c["name"] for c in result["hook_candidates"]] == ["form_alter"]


@pytest.mark.parametrize("attr", ["_file_stem", "_make_id"])
def test_the_seam_fails_loudly_when_core_id_helpers_move(monkeypatch, attr):
    """Final review I4: hooks.py computes `hook_implemented_by` targets and
    `invokes_hook` sources with core's id helpers; a rename upstream must fail
    here, not silently drop every such edge."""
    install()
    import graphify.extract as core
    import graphify.extractors.base as base

    monkeypatch.delattr(base, attr, raising=True)
    with pytest.raises(DrupalSeamError, match=rf"graphify\.extractors\.base\.{attr}"):
        _patch_extract(core)


def test_a_symlinked_checkout_matches_detects_resolved_paths(tmp_path, _isolated_discovery_state):
    """Final review minor 9: scanned through a symlink, `detect()` hands out
    resolved paths; the registry must name the same ones, or `src/Hook`
    files and owners stop matching."""
    install()
    import graphify.detect as detect
    from graphify.drupal.discovery import current_registry, registry_owner_of
    from graphify.drupal.hooks import is_hook_class_file

    real = _hooks_site(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    found = detect.detect(link, cache_root=tmp_path / "out")
    detected = [Path(p) for p in found["files"]["code"]]
    hook_file = next(p for p in detected if p.name == "FooHooks.php")
    module = next(p for p in detected if p.name == "foo.module")

    registry = current_registry()
    assert registry is not None
    assert is_hook_class_file(hook_file)
    assert registry_owner_of(registry, hook_file) == "foo"
    assert registry_owner_of(registry, module) == "foo"
    impls = extract_hook_implementations(module, _core_php(module))
    assert any(n["type"] == "drupal_hook_impl" for n in impls["nodes"])
