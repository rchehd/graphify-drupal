"""Plugins and entity types from PHP attributes and annotations (P4 Task 2,
spec §5): `php_semantics.extract_php_semantics`, its composition onto core's
PHP handler, the resolver's binding of pending class edges, and the
`unknown_plugin_type` candidates in the inventory."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal import boundary, discovery
from graphify.drupal.discovery import prepare_run, type_id
from graphify.drupal.php_classes import read_php_attributes
from graphify.drupal.yaml_common import permission_id, plugin_id
from graphify.drupal.yaml_extract import extension_id
from graphify.ids import make_id
from tests.test_drupal_discovery import _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

FOO = "web/modules/custom/foo"

BLOCK_MANAGER = r"""<?php
namespace Drupal\Core\Block;

use Drupal\Core\Plugin\DefaultPluginManager;
use Drupal\Core\Block\Attribute\Block;

class BlockManager extends DefaultPluginManager {

  public function __construct(\Traversable $namespaces, $cache, $module_handler) {
    parent::__construct(
      'Plugin/Block',
      $namespaces,
      $module_handler,
      BlockPluginInterface::class,
      Block::class,
      'Drupal\Core\Block\Annotation\Block'
    );
    $this->alterInfo('block');
  }
}
"""

CORE_SERVICES = (
    "services:\n"
    "  plugin.manager.block:\n"
    "    class: Drupal\\Core\\Block\\BlockManager\n"
    "    parent: default_plugin_manager\n"
)

FOO_BLOCK = r"""<?php
namespace Drupal\foo\Plugin\Block;

use Drupal\Core\Block\Attribute\Block;
use Drupal\Core\StringTranslation\TranslatableMarkup;
use Drupal\foo\Plugin\Derivative\FooDeriver;

#[Block(id: "foo_block", admin_label: new TranslatableMarkup("Foo"), deriver: FooDeriver::class)]
class FooBlock extends BlockBase {
}
"""

BAR_BLOCK = r"""<?php
namespace Drupal\foo\Plugin\Block;

/**
 * A bar.
 *
 * @Block(
 *   id = "bar_block",
 *   admin_label = @Translation("Bar"),
 * )
 */
class BarBlock extends BlockBase {
}
"""

FOO_DERIVER = r"""<?php
namespace Drupal\foo\Plugin\Derivative;

class FooDeriver {
}
"""

FOO_ENTITY = r"""<?php
namespace Drupal\foo\Entity;

use Drupal\Core\Entity\Attribute\ContentEntityType;
use Drupal\Core\Entity\EntityListBuilder;
use Drupal\Core\StringTranslation\TranslatableMarkup;
use Drupal\foo\FooStorage;
use Drupal\foo\Form\FooForm;

#[ContentEntityType(
  id: "foo",
  label: new TranslatableMarkup("Foo"),
  handlers: [
    "storage" => FooStorage::class,
    "list_builder" => EntityListBuilder::class,
    "form" => [
      "default" => FooForm::class,
    ],
  ],
  base_table: "foo",
  admin_permission: "administer foo",
)]
class Foo extends ContentEntityBase {
}
"""

FOO_CONFIG_ENTITY = r"""<?php
namespace Drupal\foo\Entity;

/**
 * A config entity.
 *
 * @ConfigEntityType(
 *   id = "foo_type",
 *   label = @Translation("Foo type"),
 *   handlers = {
 *     "list_builder" = "Drupal\foo\FooTypeListBuilder",
 *     "form" = {
 *       "edit" = "Drupal\foo\Form\FooForm",
 *       "add" = "Drupal\foo\Form\FooForm",
 *     },
 *   },
 *   admin_permission = "administer foo",
 *   config_prefix = "type",
 * )
 */
class FooType extends ConfigEntityBase {
}
"""

UNKNOWN_PLUGIN = r"""<?php
namespace Drupal\foo\Plugin\X;

use Drupal\foo\Attribute\Thing;

#[Thing(id: "thing")]
class Thing1 {
}
"""


def _cls(name: str, namespace: str = "Drupal\\foo") -> str:
    return f"<?php\nnamespace {namespace};\n\nclass {name} {{\n}}\n"


def _plugin_site(root: Path, extra: dict[str, str] | None = None) -> Path:
    return _site(root, {
        "web/core/core.services.yml": CORE_SERVICES,
        "web/core/lib/Drupal/Core/Block/BlockManager.php": BLOCK_MANAGER,
        f"{FOO}/foo.info.yml": "name: Foo\ntype: module\n",
        f"{FOO}/src/Plugin/Block/FooBlock.php": FOO_BLOCK,
        f"{FOO}/src/Plugin/Block/BarBlock.php": BAR_BLOCK,
        f"{FOO}/src/Plugin/Derivative/FooDeriver.php": FOO_DERIVER,
        f"{FOO}/src/Entity/Foo.php": FOO_ENTITY,
        f"{FOO}/src/Entity/FooType.php": FOO_CONFIG_ENTITY,
        f"{FOO}/src/FooStorage.php": _cls("FooStorage"),
        f"{FOO}/src/FooTypeListBuilder.php": _cls("FooTypeListBuilder"),
        f"{FOO}/src/Form/FooForm.php": _cls("FooForm", "Drupal\\foo\\Form"),
        f"{FOO}/src/Plugin/X/Thing1.php": UNKNOWN_PLUGIN,
        **(extra or {}),
    })


def _core_php(path: Path) -> dict:
    import graphify.extract as core

    return core._DISPATCH[".php"](path)


def _class_node(result: dict, label: str) -> str:
    found = [n["id"] for n in result["nodes"] if n.get("label") == label]
    assert len(found) == 1, (label, found)
    return found[0]


def _rel(edges: list[dict]) -> set[tuple[str, str, str]]:
    return {(e["source"], e["relation"], e["target"]) for e in edges}


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state, monkeypatch):  # noqa: F811
    from graphify.drupal.container import ENV_ARTIFACT

    monkeypatch.delenv(ENV_ARTIFACT, raising=False)
    boundary.clear_caches()
    yield
    boundary.clear_caches()


# -- the reader ------------------------------------------------------------------


def test_attribute_arguments_carry_structured_literal_values(tmp_path):
    path = _plugin_site(tmp_path) / FOO / "src/Entity/Foo.php"
    (cls,) = read_php_attributes(path)
    args = {a.name: a for a in cls.attributes[0].args}
    assert args["id"].value == "foo"
    assert args["handlers"].value == {
        "storage": {"class": "Drupal\\foo\\FooStorage"},
        "list_builder": {"class": "Drupal\\Core\\Entity\\EntityListBuilder"},
        "form": {"default": {"class": "Drupal\\foo\\Form\\FooForm"}},
    }
    # A non-literal value is dropped, never evaluated.
    assert args["label"].value is None


def test_an_annotation_s_unnamed_first_value_is_its_id(tmp_path):
    from graphify.drupal.php_classes import read_class_annotations

    path = tmp_path / "Sub.php"
    path.write_text("<?php\nnamespace Drupal\\foo\\Element;\n\n/**\n * @RenderElement(\"foo_sub\")\n"
                    " */\nclass Sub {\n}\n", encoding="utf-8")
    ((fqcn, annotation),) = read_class_annotations(path)
    assert (fqcn, annotation.short, annotation.values) == (
        "Drupal\\foo\\Element\\Sub", "RenderElement", {"id": "foo_sub"})


# -- the per-file extractor --------------------------------------------------------


def test_attribute_and_annotation_plugins_have_the_same_shape(tmp_path):
    from graphify.drupal.php_semantics import PENDING, extract_php_semantics

    root = _plugin_site(tmp_path)
    prepare_run(root)
    foo_path = root / FOO / "src/Plugin/Block/FooBlock.php"
    bar_path = root / FOO / "src/Plugin/Block/BarBlock.php"
    foo_core, bar_core = _core_php(foo_path), _core_php(bar_path)
    foo = extract_php_semantics(foo_path, foo_core)
    bar = extract_php_semantics(bar_path, bar_core)

    (foo_node,) = foo["nodes"]
    (bar_node,) = bar["nodes"]
    shape = {"type", "layer", "label", "plugin_id", "plugin_type", "class_name", "provider"}
    assert {k: foo_node[k] for k in shape} == {
        "type": "drupal_plugin", "layer": "plugin", "label": "foo_block", "plugin_id": "foo_block",
        "plugin_type": "block", "class_name": "Drupal\\foo\\Plugin\\Block\\FooBlock",
        "provider": "foo"}
    assert {k: bar_node[k] for k in shape} == {
        "type": "drupal_plugin", "layer": "plugin", "label": "bar_block", "plugin_id": "bar_block",
        "plugin_type": "block", "class_name": "Drupal\\foo\\Plugin\\Block\\BarBlock",
        "provider": "foo"}
    assert foo_node["id"] == plugin_id("block", "foo_block")
    assert foo_node["deriver"] == "Drupal\\foo\\Plugin\\Derivative\\FooDeriver"
    assert "deriver" not in bar_node

    for result, node, core, label in ((foo, foo_node, foo_core, "FooBlock"),
                                      (bar, bar_node, bar_core, "BarBlock")):
        rel = _rel(result["edges"])
        assert (extension_id("foo"), "provides_plugin", node["id"]) in rel
        assert (node["id"], "plugin_of_type", type_id("block")) in rel
        assert (node["id"], "plugin_implemented_by", _class_node(core, label)) in rel

    (derives,) = [e for e in foo["edges"] if e["relation"] == "derives_plugins"]
    assert derives[PENDING] and derives["target_name"] == "Drupal\\foo\\Plugin\\Derivative\\FooDeriver"
    assert not [e for e in bar["edges"] if e["relation"] == "derives_plugins"]
    assert foo["php_candidates"] == [] and bar["php_candidates"] == []


def test_entity_types_from_an_attribute_and_an_annotation(tmp_path):
    from graphify.drupal.php_semantics import PENDING, extract_php_semantics

    root = _plugin_site(tmp_path)
    prepare_run(root)
    content = extract_php_semantics(root / FOO / "src/Entity/Foo.php",
                                    _core_php(root / FOO / "src/Entity/Foo.php"))
    config = extract_php_semantics(root / FOO / "src/Entity/FooType.php",
                                   _core_php(root / FOO / "src/Entity/FooType.php"))

    (foo,) = content["nodes"]
    assert (foo["id"], foo["type"], foo["layer"], foo["label"]) == (
        make_id("drupal", "entity_type", "foo"), "drupal_entity_type", "model", "foo")
    assert (foo["entity_kind"], foo["class_name"], foo["base_table"], foo["admin_permission"]) == (
        "content", "Drupal\\foo\\Entity\\Foo", "foo", "administer foo")
    assert foo["handlers"] == {
        "storage": "Drupal\\foo\\FooStorage",
        "list_builder": "Drupal\\Core\\Entity\\EntityListBuilder",
        "form.default": "Drupal\\foo\\Form\\FooForm",
    }
    rel = _rel(content["edges"])
    assert (extension_id("foo"), "defines_entity_type", foo["id"]) in rel
    assert (foo["id"], "requires_permission", permission_id("administer foo")) in rel
    handlers = {e["handler"]: e for e in content["edges"] if e["relation"] == "entity_handler"}
    assert set(handlers) == {"storage", "list_builder", "form.default"}
    assert all(e[PENDING] for e in handlers.values())
    assert handlers["storage"]["target_name"] == "Drupal\\foo\\FooStorage"

    (foo_type,) = config["nodes"]
    assert (foo_type["id"], foo_type["type"], foo_type["entity_kind"], foo_type["class_name"]) == (
        make_id("drupal", "entity_type", "foo_type"), "drupal_entity_type", "config",
        "Drupal\\foo\\Entity\\FooType")
    assert foo_type["handlers"] == {"list_builder": "Drupal\\foo\\FooTypeListBuilder",
                                    "form.edit": "Drupal\\foo\\Form\\FooForm",
                                    "form.add": "Drupal\\foo\\Form\\FooForm"}
    # One edge per handler class: the names it serves, comma-separated.
    assert sorted(e["handler"] for e in config["edges"] if e["relation"] == "entity_handler") == [
        "form.edit,form.add", "list_builder"]
    assert "base_table" not in foo_type
    rel = _rel(config["edges"])
    assert (extension_id("foo"), "defines_entity_type", foo_type["id"]) in rel
    assert (foo_type["id"], "requires_permission", permission_id("administer foo")) in rel
    # An entity type is not a plugin node.
    assert not [n for n in content["nodes"] + config["nodes"] if n["type"] == "drupal_plugin"]


def test_an_unknown_plugin_attribute_is_a_candidate(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics, find_php_candidates

    root = _plugin_site(tmp_path)
    registry = prepare_run(root)
    path = root / FOO / "src/Plugin/X/Thing1.php"
    result = extract_php_semantics(path, _core_php(path))
    expected = [{"kind": "unknown_plugin_type", "module": "foo",
                 "class": "Drupal\\foo\\Plugin\\X\\Thing1",
                 "attribute": "Drupal\\foo\\Attribute\\Thing", "file": str(path), "line": 6}]
    assert result["nodes"] == [] and result["edges"] == []
    assert result["php_candidates"] == expected
    assert find_php_candidates(path, registry) == expected


def test_recognition_needs_the_type_subdir_and_the_right_annotation_class(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics

    misplaced = FOO_BLOCK.replace("Plugin\\Block;", "Other;").replace("foo_block", "misplaced")
    imported = BAR_BLOCK.replace(
        "namespace Drupal\\foo\\Plugin\\Block;\n",
        "namespace Drupal\\foo\\Plugin\\Block;\n\nuse Drupal\\foo\\Annotation\\Block;\n")
    root = _plugin_site(tmp_path, {f"{FOO}/src/Other/Misplaced.php": misplaced,
                                   f"{FOO}/src/Plugin/Block/Imported.php": imported,
                                   f"{FOO}/lib/Plugin/Block/Outside.php": FOO_BLOCK})
    prepare_run(root)
    for rel in ("src/Other/Misplaced.php", "src/Plugin/Block/Imported.php",
                "lib/Plugin/Block/Outside.php"):
        path = root / FOO / rel
        result = extract_php_semantics(path, _core_php(path))
        assert result["nodes"] == [], rel


def test_no_registry_yields_nothing(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics

    root = _plugin_site(tmp_path)
    path = root / FOO / "src/Plugin/Block/FooBlock.php"
    assert extract_php_semantics(path, _core_php(path)) == {
        "nodes": [], "edges": [], "php_candidates": []}
    assert extract_php_semantics(tmp_path / "missing.php", {}) == {
        "nodes": [], "edges": [], "php_candidates": []}


# -- composition and the resolver -----------------------------------------------------


def _extract(root: Path) -> dict:
    from graphify.drupal.register import install

    install()
    import graphify.extract as core

    prepare_run(root)
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in (".php", ".yml"))
    return core.extract(paths, root=root, parallel=False)


def test_the_resolver_binds_pending_class_edges_or_drops_them(tmp_path):
    root = _plugin_site(tmp_path)
    result = _extract(root)

    nodes = {n["id"]: n for n in result["nodes"]}
    assert not [e for e in result["edges"] if "pending" in e]

    def class_id(label: str) -> str:
        found = [n["id"] for n in result["nodes"]
                 if n.get("label") == label and not str(n.get("type", "")).startswith("drupal_")
                 and str(n.get("source_file", "")).endswith(".php")]
        assert len(found) == 1, (label, found)
        return found[0]

    rel = _rel(result["edges"])
    foo_block = plugin_id("block", "foo_block")
    assert (foo_block, "derives_plugins", class_id("FooDeriver")) in rel
    assert (foo_block, "plugin_implemented_by", class_id("FooBlock")) in rel

    foo = make_id("drupal", "entity_type", "foo")
    foo_type = make_id("drupal", "entity_type", "foo_type")
    handlers = {(e["source"], e["handler"]): e["target"]
                for e in result["edges"] if e["relation"] == "entity_handler"}
    assert handlers == {
        (foo, "storage"): class_id("FooStorage"),
        (foo, "form.default"): class_id("FooForm"),
        (foo_type, "list_builder"): class_id("FooTypeListBuilder"),
        (foo_type, "form.edit,form.add"): class_id("FooForm"),
    }
    # The core list builder is outside the graph: no edge, it stays on the node.
    assert nodes[foo]["handlers"] == {"list_builder": "Drupal\\Core\\Entity\\EntityListBuilder"}
    assert "handlers" not in nodes[foo_type]
    # A permission no *.permissions.yml declares is a boundary stub.
    assert nodes[permission_id("administer foo")]["type"] == "drupal_permission"


def test_an_unbound_deriver_is_dropped(tmp_path):
    root = _plugin_site(tmp_path)
    (root / FOO / "src/Plugin/Derivative/FooDeriver.php").unlink()
    result = _extract(root)
    assert not [e for e in result["edges"] if "pending" in e]
    assert not [e for e in result["edges"] if e["relation"] == "derives_plugins"]
    assert {n["id"]: n for n in result["nodes"]}[plugin_id("block", "foo_block")]["deriver"] == \
        "Drupal\\foo\\Plugin\\Derivative\\FooDeriver"


def test_a_hook_class_file_is_composed_too(tmp_path):
    from graphify.drupal.register import install

    install()
    import graphify.extract as core

    hooks = r"""<?php
namespace Drupal\foo\Hook;

use Drupal\Core\Entity\Attribute\ContentEntityType;

#[ContentEntityType(id: "odd")]
class OddHooks {
}
"""
    root = _plugin_site(tmp_path, {f"{FOO}/src/Hook/OddHooks.php": hooks})
    prepare_run(root)
    path = root / FOO / "src/Hook/OddHooks.php"
    ids = {n["id"] for n in core._get_extractor(path)(path)["nodes"]}
    assert make_id("drupal", "entity_type", "odd") in ids


# -- the real CLI ---------------------------------------------------------------------


def _env(artifact: Path | None = None) -> dict:
    from graphify.drupal.container import ENV_ARTIFACT

    env = {k: v for k, v in os.environ.items() if k not in (discovery.ENV_VAR, ENV_ARTIFACT)}
    if artifact is not None:
        env[ENV_ARTIFACT] = str(artifact)
    return env


def _cli(root: Path, out: Path, artifact: Path | None = None, command: str = "extract") -> dict:
    args = [sys.executable, "-m", "graphify", command, str(root)]
    if command == "extract":
        args += ["--code-only", "--out", str(out)]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=root.parent,
                          env=_env(artifact))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    graph_dir = out / "graphify-out" if command == "extract" else root / "graphify-out"
    return json.loads((graph_dir / "graph.json").read_text(encoding="utf-8"))


def _links(graph: dict) -> list[dict]:
    return graph.get("links") or graph.get("edges") or []


def test_a_cli_run_writes_no_pending_key_and_lists_candidates(tmp_path):
    root = _plugin_site(tmp_path / "site")
    out = tmp_path / "out"
    graph = _cli(root, out)
    links = _links(graph)
    assert links
    assert not [e for e in links if "pending" in e]
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert plugin_id("block", "foo_block") in nodes
    assert make_id("drupal", "entity_type", "foo") in nodes
    assert any(e["relation"] == "derives_plugins" for e in links)
    assert any(e["relation"] == "entity_handler" for e in links)

    inventory = json.loads((out / "graphify-out" / "drupal-inventory.json").read_text(encoding="utf-8"))
    assert [(c["kind"], c["class"]) for c in inventory["php_candidates"]] == [
        ("unknown_plugin_type", "Drupal\\foo\\Plugin\\X\\Thing1")]
    assert inventory["summary"]["php_candidates"] == 1
    from graphify.drupal.inventory import render_section

    section = render_section(inventory)
    assert "| PHP candidates | 1 |" in section
    assert "### PHP candidates\n- unknown_plugin_type: 1" in section

    # A second run (all cached, incremental) keeps every bound edge.
    again = _cli(root, out)
    assert not [e for e in _links(again) if "pending" in e]
    assert _rel(_links(again)) == _rel(links)


def test_an_incremental_run_binds_to_an_unchanged_class(tmp_path):
    root = _plugin_site(tmp_path / "site")
    out = tmp_path / "out"
    _cli(root, out)
    block = root / FOO / "src/Plugin/Block/FooBlock.php"
    block.write_text(block.read_text(encoding="utf-8") + "\n// touched\n", encoding="utf-8")
    graph = _cli(root, out)
    links = _links(graph)
    assert not [e for e in links if "pending" in e]
    derives = [e for e in links if e["relation"] == "derives_plugins"]
    assert len(derives) == 1
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes[derives[0]["target"]]["label"] == "FooDeriver"


def _enabled_sha(extensions: list[dict]) -> str:
    keys = sorted(f"{e['type']}:{e['name']}" for e in extensions if e.get("status") == 1)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _artifact(root: Path, scratch: Path, target: Path) -> Path:
    from graphify.drupal.container import compute_host_stamp

    extensions = [{"name": "foo", "type": "module", "path": FOO, "status": 1, "weight": 0,
                   "dependencies": []}]
    block = {"base_plugin_id": None, "class": "Drupal\\foo\\Plugin\\Block\\FooBlock",
             "deriver": "Drupal\\foo\\Plugin\\Derivative\\FooDeriver",
             "file": f"{FOO}/src/Plugin/Block/FooBlock.php", "id": "foo_block", "provider": "foo"}
    data = {
        "schema_version": 1, "services": [], "aliases": {}, "routes": [],
        "extensions": extensions, "hooks": {}, "plugins": {"block": [block]},
        "subscribers": {}, "errors": [],
        "stamp": {"created_at": "2026-09-26T12:00:00Z", "runner": "ddev",
                  "drupal_version": "11.2.0", "enabled_extensions_sha": _enabled_sha(extensions)},
    }
    discovery.prepare_run(root, scratch)
    try:
        data["stamp"].update(compute_host_stamp(root))
    finally:
        discovery.set_current(None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, sort_keys=True, indent=1), encoding="utf-8")
    return target


def test_the_container_confirms_static_plugin_edges(tmp_path):
    root = _plugin_site(tmp_path / "site")
    out = tmp_path / "out"
    artifact = _artifact(root, tmp_path / "stamp", tmp_path / "artifact" / "drupal-container.json")
    graph = _cli(root, out, artifact)

    nodes = {n["id"]: n for n in graph["nodes"]}
    pid = plugin_id("block", "foo_block")
    assert nodes[pid].get("origin") != "container"
    assert nodes[pid]["runtime"] == "present"
    mine = {e["relation"]: e for e in _links(graph)
            if pid in (e["source"], e["target"])}
    for relation in ("provides_plugin", "plugin_of_type", "plugin_implemented_by",
                     "derives_plugins"):
        assert mine[relation].get("confirmed_by") == "container", relation
        assert mine[relation].get("origin") != "container", relation
    assert not [e for e in _links(graph) if "pending" in e]

    # A rerun keeps the static node the owner (the overlay only confirms).
    again = _cli(root, out, artifact)
    nodes = {n["id"]: n for n in again["nodes"]}
    assert nodes[pid].get("origin") != "container"
    assert sum(1 for n in again["nodes"] if n["id"] == pid) == 1


# -- the reference corpus -------------------------------------------------------------

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))


@pytest.mark.skipif(not (CORPUS / "web").is_dir(), reason="reference corpus not present")
def test_corpus_plugins_and_entity_types(tmp_path):
    from collections import Counter

    from graphify.drupal.php_semantics import extract_php_semantics, is_semantics_file

    registry = prepare_run(CORPUS, cache_root=tmp_path)
    files = sorted({Path(f["file"]) for f in registry.class_facts.values()})
    plugins: Counter = Counter()
    entity_types: set[str] = set()
    candidates: list[dict] = []
    for path in files:
        if not is_semantics_file(path):
            continue
        result = extract_php_semantics(path, _core_php(path))
        for n in result["nodes"]:
            if n["type"] == "drupal_plugin":
                plugins[n["plugin_type"]] += 1
            elif n["type"] == "drupal_entity_type":
                entity_types.add(n["label"])
        candidates += result["php_candidates"]
    assert entity_types == {
        "system", "task", "task_workflow", "webform_integration", "webform_integration_lim",
        "webform_integration_result", "webform_integrations_log", "webform_integrations_token"}
    assert plugins == {
        "action": 4, "eca.action": 4, "rest": 3, "advancedqueue_job_type": 2, "system_type": 2,
        "webform_integration_type": 6, "field.formatter": 2, "eca.event": 1, "eca.condition": 1,
        "mail": 1, "class:Drupal\\mailsystem\\MailsystemManager": 1,
        "webform.element": 6, "webform.handler": 3, "element_info": 3,
    }
    assert sorted(c["attribute"] for c in candidates) == ["@ViewsField", "@ViewsField"]
