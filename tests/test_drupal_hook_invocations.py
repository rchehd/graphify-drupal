"""Hook invocation sites: `invokeAll`/`invoke`/`alter`/... calls and the
plugin-type `alterInfo` edge (P2b Task 5, spec §5.3-§5.4)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from graphify.drupal.discovery import extract_plugin_types, prepare_run, type_id
from graphify.drupal.hooks import (
    extract_hook_invocations,
    has_hook_invocation_marker,
    hook_id,
    is_procedural_file,
)
from graphify.drupal.inventory import build_inventory
from graphify.drupal.register import install
from tests.test_drupal_discovery import D11_MANAGER, FOO_SERVICES, _module, _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

CORE_API_PHP = """<?php

function hook_cron() {
}
"""

INVOKER_PHP = """<?php

namespace Drupal\\foo;

class Invoker {

  protected $moduleHandler;

  public function run($name) {
    $this->moduleHandler->invokeAll('foo_info');
    $this->moduleHandler->alter('foo_info', $x);
    $this->moduleHandler->alter(['a', 'b'], $x);
    $this->moduleHandler->invoke('bar', 'cron');
    $this->moduleHandler->invokeAll($name);
  }

}
"""

FOO_MODULE = """<?php

/**
 * Implements hook_cron().
 */
function foo_cron() {
  \\Drupal::moduleHandler()->invokeAll('cron');
}
"""

FOO = "web/modules/custom/foo"


def _invocations_site(tmp_path: Path) -> Path:
    files = {
        "web/core/core.api.php": CORE_API_PHP,
        **_module("foo", "services: {}\n", {
            "foo.module": FOO_MODULE,
            "src/Invoker.php": INVOKER_PHP,
        }),
    }
    return _site(tmp_path, files)


def _core_php(path: Path) -> dict:
    import graphify.extract as core

    return core._DISPATCH[".php"](path)


def _rel(result: dict) -> set[tuple[str, str, str]]:
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def _edge(result: dict, target: str) -> dict:
    found = [e for e in result["edges"] if e["target"] == target]
    assert len(found) == 1, (target, found)
    return found[0]


def _core_id(core_result: dict, label: str) -> str:
    found = [n["id"] for n in core_result["nodes"] if n.get("label") == label]
    assert len(found) == 1, (label, found)
    return found[0]


# -- the text pre-check ------------------------------------------------------------


def test_has_hook_invocation_marker(tmp_path):
    invoke = tmp_path / "invoke.php"
    invoke.write_text("<?php\n$x->invokeAll('a');\n", encoding="utf-8")
    assert has_hook_invocation_marker(invoke)

    alter = tmp_path / "alter.php"
    alter.write_text("<?php\n$x->alter('a', $y);\n", encoding="utf-8")
    assert has_hook_invocation_marker(alter)

    has_impl = tmp_path / "has.php"
    has_impl.write_text("<?php\n$x->hasImplementations('a');\n", encoding="utf-8")
    assert has_hook_invocation_marker(has_impl)

    plain = tmp_path / "plain.php"
    plain.write_text("<?php\n$x->doSomethingElse();\n", encoding="utf-8")
    assert not has_hook_invocation_marker(plain)
    assert not has_hook_invocation_marker(tmp_path / "missing.php")


# -- a custom class's invocation sites ----------------------------------------------


def test_invocation_edges_and_non_literal_candidate(tmp_path, _isolated_discovery_state):
    install()
    root = _invocations_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/Invoker.php"
    core_result = _core_php(path)
    run = _core_id(core_result, ".run()")

    result = extract_hook_invocations(path, core_result)

    assert result["nodes"] == []
    assert sorted((e["target"], e.get("target_name"), e.get("undeclared")) for e in result["edges"]) == sorted([
        (hook_id("foo_info"), "foo_info", True),
        (hook_id("foo_info_alter"), "foo_info_alter", True),
        (hook_id("a_alter"), "a_alter", True),
        (hook_id("b_alter"), "b_alter", True),
        (hook_id("cron"), "cron", None),
    ])
    assert {e["source"] for e in result["edges"]} == {run}
    assert all(e["relation"] == "invokes_hook" for e in result["edges"])
    cron_edge = _edge(result, hook_id("cron"))
    assert "undeclared" not in cron_edge
    foo_info_edge = _edge(result, hook_id("foo_info"))
    assert foo_info_edge["undeclared"] is True

    assert result["hook_candidates"] == [
        {"kind": "non_literal", "module": "", "name": "$name", "file": str(path), "line": 14},
    ]


def test_a_procedural_function_invokes_a_hook(tmp_path, _isolated_discovery_state):
    install()
    root = _invocations_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "foo.module"
    core_result = _core_php(path)
    cron_fn = _core_id(core_result, "foo_cron()")

    result = extract_hook_invocations(path, core_result)

    assert result["hook_candidates"] == []
    assert _rel(result) == {(cron_fn, "invokes_hook", hook_id("cron"))}
    edge = _edge(result, hook_id("cron"))
    assert edge["target_name"] == "cron"
    assert "undeclared" not in edge


def test_no_invocation_marker_short_circuits(tmp_path, _isolated_discovery_state):
    install()
    root = _invocations_site(tmp_path)
    prepare_run(root)
    other = root / FOO / "src/Other.php"
    other.write_text("<?php\nnamespace Drupal\\foo;\nclass Other {\n  public function x() {}\n}\n",
                     encoding="utf-8")

    result = extract_hook_invocations(other, _core_php(other))
    assert result == {"nodes": [], "edges": [], "hook_candidates": []}


def test_no_registry_and_bad_input_yield_nothing(tmp_path, _isolated_discovery_state):
    root = _invocations_site(tmp_path)
    path = root / FOO / "src/Invoker.php"
    empty = {"nodes": [], "edges": [], "hook_candidates": []}
    assert extract_hook_invocations(path, _core_php(path)) == empty

    prepare_run(root)
    garbage = root / FOO / "garbage.php"
    garbage.write_bytes(b"\x00\xff<?php ->invoke( ((")
    assert extract_hook_invocations(garbage, {}) == empty
    assert extract_hook_invocations(root / FOO / "missing.php", {"nodes": []}) == empty


def test_edges_only_target_a_source_core_actually_emitted(tmp_path, _isolated_discovery_state):
    """Same fix as Task 4: an emptied `core_result` yields no `invokes_hook` edge."""
    install()
    root = _invocations_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/Invoker.php"

    starved = extract_hook_invocations(path, {"nodes": [], "edges": []})
    assert starved["edges"] == []
    # The non-literal candidate does not depend on the source resolving.
    assert starved["hook_candidates"] == [
        {"kind": "non_literal", "module": "", "name": "$name", "file": str(path), "line": 14},
    ]


# -- the inventory picks up invocation candidates too, detect-time -----------------


def test_the_inventory_lists_the_non_literal_invocation_candidate(tmp_path, _isolated_discovery_state):
    install()
    root = _invocations_site(tmp_path)
    registry = prepare_run(root)
    detected = {str(p) for p in root.rglob("*") if p.is_file()}

    inventory = build_inventory(registry, detected, root)
    assert inventory["hook_candidates"] == [
        {"kind": "non_literal", "module": "", "name": "$name",
         "file": f"{FOO}/src/Invoker.php", "line": 14},
    ]


# -- the pipeline: composed onto core's PHP handler ---------------------------------


def test_pipeline_composes_invocations_onto_an_arbitrary_php_file(tmp_path, _isolated_discovery_state):
    install()
    from graphify.extract import extract

    root = _invocations_site(tmp_path)
    prepare_run(root)

    result = extract([root / FOO / "src/Invoker.php", root / FOO / "foo.info.yml"], root=root)
    edges = [e for e in result["edges"] if e["relation"] == "invokes_hook"]
    assert edges
    ids = {n["id"] for n in result["nodes"]}
    assert all(e["source"] in ids for e in edges)


def test_pipeline_composes_invocations_onto_a_procedural_file(tmp_path, _isolated_discovery_state):
    install()
    from graphify.extract import extract

    root = _invocations_site(tmp_path)
    prepare_run(root)

    result = extract([root / FOO / "foo.module", root / FOO / "foo.info.yml"], root=root)
    edges = [e for e in result["edges"] if e["relation"] == "invokes_hook"]
    assert [e["target"] for e in edges] == [hook_id("cron")]


# -- a plugin type's own alterInfo() is an invocation too --------------------------


def test_a_plugin_type_invokes_its_alter_hook(tmp_path, _isolated_discovery_state):
    root = _site(tmp_path, _module("foo", FOO_SERVICES, {"src/FooManager.php": D11_MANAGER}))
    prepare_run(root)
    manager_path = root / "web/modules/custom/foo/src/FooManager.php"

    result = extract_plugin_types(manager_path)
    tid = type_id("foo")
    assert (tid, "invokes_hook", hook_id("foo_info_alter")) in _rel(result)
    edge = _edge(result, hook_id("foo_info_alter"))
    assert edge["target_name"] == "foo_info_alter"


def test_a_plugin_type_with_no_alter_hook_gets_no_invokes_hook_edge(tmp_path, _isolated_discovery_state):
    orphan = "<?php\nnamespace Drupal\\orphan;\nuse Drupal\\Core\\Plugin\\DefaultPluginManager;\n" \
             "class OrphanManager extends DefaultPluginManager {\n" \
             "  public function __construct($namespaces, $module_handler) {\n" \
             "    parent::__construct('Plugin/Orphan', $namespaces, $module_handler);\n  }\n}\n"
    root = _site(tmp_path, {"web/src/OrphanManager.php": orphan})
    prepare_run(root)

    result = extract_plugin_types(root / "web/src/OrphanManager.php")
    assert not any(e["relation"] == "invokes_hook" for e in result["edges"])


# -- the corpus (spec §8) -----------------------------------------------------------

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))


@pytest.mark.skipif(not (CORPUS / "web" / "core").is_dir(), reason="reference Drupal corpus not present")
def test_corpus_custom_invocations_and_type_alter_edges(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.discovery import current_registry

    registry = prepare_run(CORPUS, cache_root=tmp_path)
    assert registry is not None
    found = detect.detect(CORPUS, cache_root=tmp_path)
    code = [Path(p) for p in found["files"]["code"] if "/tests/" not in p]

    custom_invocations = 0
    for path in code:
        if path.suffix != ".php" and not is_procedural_file(path):
            continue
        if "modules/custom" not in path.as_posix() and "themes/custom" not in path.as_posix():
            continue
        result = extract_hook_invocations(path, _core_php(path))
        custom_invocations += len([e for e in result["edges"] if e["relation"] == "invokes_hook"])
    # Measured on FormsRemote: no custom code calls invoke/alter/hasImplementations directly.
    assert custom_invocations == 0

    type_alter_edges = 0
    for t in current_registry().types.values():
        if not t.alter_hook:
            continue
        result = extract_plugin_types(Path(t.class_file))
        type_alter_edges += len([e for e in result["edges"] if e["relation"] == "invokes_hook"])
    assert type_alter_edges == sum(1 for t in registry.types.values() if t.alter_hook)


def _cli(root: Path, out: Path) -> None:
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "GRAPHIFY_DRUPAL_DISCOVERY"}
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--out", str(out)],
        capture_output=True, text=True, cwd=root, env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# -- `invoke`/`alter` need a module- or theme-handler receiver (controller ruling) ---

RECEIVERS_PHP = """<?php

namespace Drupal\\foo;

class Receivers {

  public function run($object, $d) {
    $method = new \\ReflectionMethod($object, 'build');
    $method->invoke($object, 'reflected');
    $this->alter(['tiny', 'small']);
    $this->moduleHandler->alter('p', $d);
    \\Drupal::moduleHandler()->invoke('m', 'cron');
    $module_handler->alter('q', $d);
    \\Drupal::service('module_handler')->invoke('m', 'r');
    \\Drupal::service('theme.manager')->alter('s', $d);
    \\Drupal::theme()->alter('t', $d);
    $this->themeManager->alter('u', $d);
    $handler->invokeAll('kept');
    $handler->hasImplementations('also_kept');
  }

}
"""


def _receivers_site(tmp_path: Path) -> Path:
    files = {
        "web/core/core.api.php": CORE_API_PHP,
        **_module("foo", "services: {}\n", {"src/Receivers.php": RECEIVERS_PHP}),
    }
    return _site(tmp_path, files)


def test_invoke_and_alter_count_only_on_a_handler_receiver(tmp_path, _isolated_discovery_state):
    install()
    root = _receivers_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/Receivers.php"

    result = extract_hook_invocations(path, _core_php(path))

    assert sorted(e["target_name"] for e in result["edges"]) == [
        "also_kept", "cron", "kept", "p_alter", "q_alter", "r", "s_alter", "t_alter", "u_alter"]
    assert result["hook_candidates"] == [
        {"kind": "unknown_receiver", "module": "foo", "name": "'reflected'", "method": "invoke",
         "file": str(path), "line": 9},
        {"kind": "unknown_receiver", "module": "foo", "name": "['tiny', 'small']", "method": "alter",
         "file": str(path), "line": 10},
    ]


def test_the_inventory_lists_unknown_receiver_candidates(tmp_path, _isolated_discovery_state):
    install()
    root = _receivers_site(tmp_path)
    registry = prepare_run(root)
    detected = {str(p) for p in root.rglob("*") if p.is_file()}

    inventory = build_inventory(registry, detected, root)
    assert [(c["kind"], c["method"], c["line"]) for c in inventory["hook_candidates"]] == [
        ("unknown_receiver", "invoke", 9), ("unknown_receiver", "alter", 10)]
