"""Variable hooks bound and events read (P4 Task 5, spec §6.2, §6.3, §8):
`form_FORM_ID_alter` implementations bound to the form they alter,
`hook_ENTITY_TYPE_*` ones to their entity type, and `subscribes_to_event`
from `getSubscribedEvents()`; the P3 overlay confirming all three."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graphify.build import build_from_json
from graphify.drupal import boundary
from graphify.drupal.discovery import prepare_run
from graphify.drupal.hooks import extract_hook_implementations, find_hook_candidates, hook_id, hook_impl_id
from graphify.drupal.inventory import build_inventory
from graphify.drupal.yaml_extract import extension_id
from graphify.ids import make_id
from tests.test_drupal_discovery import _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401
from tests.test_drupal_php_plugins import _core_php

FOO = "web/modules/custom/foo"
FOO_BAR = "web/modules/custom/foo_bar"
BAR = "web/modules/contrib/bar"
NODE = "web/core/modules/node"

CORE_API = """<?php

function hook_form_FORM_ID_alter(&$form, $form_state, $form_id) {
}

function hook_form_BASE_FORM_ID_alter(&$form, $form_state, $form_id) {
}

function hook_ENTITY_TYPE_insert($entity) {
}

function hook_ENTITY_TYPE_presave($entity) {
}

function hook_ENTITY_TYPE_translation_insert($translation) {
}

function hook_cron() {
}
"""


def _entity(namespace: str, cls: str, entity_type: str, forms: dict[str, str] | None = None,
            extra: str = "", kind: str = "ContentEntityType") -> str:
    handlers = ""
    if forms:
        inner = ", ".join(f'"{op}" => "{fqcn}"' for op, fqcn in forms.items())
        handlers = f',\n  handlers: ["form" => [{inner}]]'
    return (f"<?php\nnamespace {namespace};\n\nuse Drupal\\Core\\Entity\\Attribute\\{kind};\n\n"
            f'#[{kind}(\n  id: "{entity_type}"{handlers}{extra},\n)]\n'
            f"class {cls} {{\n}}\n")


def _form(namespace: str, cls: str, form_id: str, base: str | None = None) -> str:
    base_method = (f"  public function getBaseFormId() {{\n    return '{base}';\n  }}\n" if base else "")
    return (f"<?php\nnamespace {namespace};\n\nclass {cls} {{\n\n"
            f"  public function getFormId() {{\n    return '{form_id}';\n  }}\n{base_method}}}\n")


FOO_HOOKS = r"""<?php

namespace Drupal\foo\Hook;

use Drupal\Core\Hook\Attribute\Hook;

class FooHooks {

  #[Hook('form_foo_plain_alter')]
  public function plainAlter(&$form) {
  }

  #[Hook('form_foo_base_alter')]
  public function baseAlter(&$form) {
  }

  #[Hook('form_bar_settings_alter')]
  public function barAlter(&$form) {
  }

  #[Hook('form_nobody_knows_alter')]
  public function unknownAlter(&$form) {
  }

  #[Hook('form_foo_edit_form_alter')]
  public function entityFormAlter(&$form) {
  }

  #[Hook('form_node_form_alter')]
  public function nodeFormAlter(&$form) {
  }

  #[Hook('node_presave')]
  public function nodePresave($node) {
  }

  #[Hook('foo_insert')]
  public function fooInsert($foo) {
  }

  #[Hook('widget_insert')]
  public function widgetInsert($widget) {
  }

  #[Hook('form_foo_form_alter')]
  public function fooBaseFormAlter(&$form) {
  }

  #[Hook('form_node_article_edit_form_alter')]
  public function articleEditAlter(&$form) {
  }

  #[Hook('form_node_blog_form_alter')]
  public function blogAlter(&$form) {
  }

  #[Hook('form_bar_base_alter')]
  public function barBaseAlter(&$form) {
  }

  #[Hook('form_bar_computed_base_alter')]
  public function barComputedAlter(&$form) {
  }

}
"""

FOO_BAR_MODULE = """<?php

/**
 * Implements hook_ENTITY_TYPE_insert().
 */
function foo_bar_item_insert($item) {
}

/**
 * Implements hook_ENTITY_TYPE_insert().
 */
function foo_bar_thing_insert($thing) {
}

/**
 * Implements hook_form_FORM_ID_alter().
 */
function foo_bar_form_foo_plain_alter(&$form) {
}
"""

FOO_SUBSCRIBER = r"""<?php

namespace Drupal\foo\EventSubscriber;

use Drupal\bar\BarEvents;
use Drupal\nope\NopeEvents;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;

class FooSubscriber implements EventSubscriberInterface {

  public static function getSubscribedEvents(): array {
    return [
      'foo.literal' => 'onLiteral',
      BarEvents::SAVE => ['onSave', 50],
      NopeEvents::GONE => 'onGone',
      'foo.many' => [['second', -5], ['first', 10]],
      'foo.bad' => $this->callback,
    ];
  }

}
"""

BAZ_SUBSCRIBER = r"""<?php

namespace Drupal\foo\EventSubscriber;

use Drupal\bar\BarEvents;

class BazSubscriber {

  public static function getSubscribedEvents() {
    $events[BarEvents::SAVE][] = ['onBarSave', -20];
    $events['foo.literal'] = 'onIt';
    return $events;
  }

}
"""

BAR_EVENTS = r"""<?php

namespace Drupal\bar;

final class BarEvents {

  const SAVE = 'bar.save';

}
"""


def _binding_site(root: Path) -> Path:
    return _site(root, {
        "web/core/core.api.php": CORE_API,
        f"{NODE}/node.info.yml": "name: Node\ntype: module\n",
        f"{NODE}/src/Entity/Node.php": _entity(
            "Drupal\\node\\Entity", "Node", "node",
            {"default": "Drupal\\node\\NodeForm", "edit": "Drupal\\node\\NodeForm"},
            ',\n  entity_keys: ["id" => "nid", "bundle" => "type"],\n  bundle_entity_type: "node_type"'),
        f"{NODE}/src/Entity/NodeType.php": _entity(
            "Drupal\\node\\Entity", "NodeType", "node_type", None, ',\n  config_prefix: "type"',
            kind="ConfigEntityType"),
        # Bundles are proven by config file names only: a sync store outside
        # the web root and a module's `config/optional`.
        "config/sync/core.extension.yml": "module: {}\n",
        "config/sync/node.type.article.yml": "",
        f"{BAR}/config/optional/node.type.page.yml": "",
        f"{BAR}/src/Form/BarOneForm.php": _form("Drupal\\bar\\Form", "BarOneForm", "bar_one",
                                                "bar_base"),
        f"{BAR}/src/Form/BarTwoForm.php": _form("Drupal\\bar\\Form", "BarTwoForm", "bar_two",
                                                "bar_base"),
        f"{BAR}/src/Form/BarComputedForm.php": (
            "<?php\nnamespace Drupal\\bar\\Form;\n\nclass BarComputedForm {\n"
            "  public function getFormId() {\n    return 'bar_' . $this->x;\n  }\n"
            "  public function getBaseFormId() {\n    return 'bar_computed_base';\n  }\n}\n"),
        f"{BAR}/bar.info.yml": "name: Bar\ntype: module\n",
        f"{BAR}/src/Form/BarSettingsForm.php": _form("Drupal\\bar\\Form", "BarSettingsForm",
                                                     "bar_settings"),
        f"{BAR}/src/BarEvents.php": BAR_EVENTS,
        f"{FOO}/foo.info.yml": "name: Foo\ntype: module\n",
        f"{FOO}/src/Form/PlainForm.php": _form("Drupal\\foo\\Form", "PlainForm", "foo_plain"),
        f"{FOO}/src/Form/BasedForm.php": _form("Drupal\\foo\\Form", "BasedForm", "foo_based",
                                               "foo_base"),
        f"{FOO}/src/Form/OtherBasedForm.php": _form("Drupal\\foo\\Form", "OtherBasedForm",
                                                    "foo_other", "foo_base"),
        f"{FOO}/src/Entity/Foo.php": _entity(
            "Drupal\\foo\\Entity", "Foo", "foo",
            {"default": "Drupal\\foo\\Form\\FooEntityForm", "edit": "Drupal\\foo\\Form\\FooEntityForm"}),
        f"{FOO}/src/Form/FooEntityForm.php": "<?php\nnamespace Drupal\\foo\\Form;\n\nclass FooEntityForm {\n}\n",
        f"{FOO}/src/Hook/FooHooks.php": FOO_HOOKS,
        f"{FOO}/src/EventSubscriber/FooSubscriber.php": FOO_SUBSCRIBER,
        f"{FOO}/src/EventSubscriber/BazSubscriber.php": BAZ_SUBSCRIBER,
        f"{FOO_BAR}/foo_bar.info.yml": "name: Foo bar\ntype: module\n",
        f"{FOO_BAR}/foo_bar.module": FOO_BAR_MODULE,
        # The brief's case: `foo_bar_insert()` in foo.module reads as module
        # `foo` + entity type `bar` (which module foo_bar makes), or module
        # `foo_bar` + hook `insert` (no entity type): one split.
        f"{FOO}/foo.module": "<?php\n\nfunction foo_bar_insert($bar) {\n}\n",
        f"{FOO_BAR}/src/Entity/Bar.php": _entity("Drupal\\foo_bar\\Entity", "Bar", "bar"),
        f"{FOO_BAR}/src/Entity/Item.php": _entity("Drupal\\foo_bar\\Entity", "Item", "item"),
        f"{FOO_BAR}/src/Entity/Thing.php": _entity("Drupal\\foo_bar\\Entity", "Thing", "thing"),
        # `foo_bar_thing_insert()` also reads as module `foo`, entity type `bar_thing`.
        f"{FOO_BAR}/src/Entity/BarThing.php": _entity("Drupal\\foo_bar\\Entity", "BarThing",
                                                      "bar_thing"),
    })


def form_id(name: str) -> str:
    return make_id("drupal", "form", name)


def entity_form_id(entity_type: str, op: str) -> str:
    return make_id("drupal", "form", "entity", entity_type, op)


def entity_type_id(name: str) -> str:
    return make_id("drupal", "entity_type", name)


def event_id(name: str) -> str:
    return make_id("drupal", "event", name)


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state, monkeypatch):  # noqa: F811
    from graphify.drupal.container import ENV_ARTIFACT

    monkeypatch.delenv(ENV_ARTIFACT, raising=False)
    boundary.clear_caches()
    yield
    boundary.clear_caches()


def _rel(edges: list[dict]) -> set[tuple[str, str, str]]:
    return {(e["source"], e["relation"], e["target"]) for e in edges}


def _method(core: dict, cls: str, method: str) -> str:
    from graphify.extractors.base import _make_id

    found = [n["id"] for n in core["nodes"] if n.get("label") == cls]
    assert len(found) == 1, (cls, found)
    mid = _make_id(found[0], method)
    assert mid in {n["id"] for n in core["nodes"]}, mid
    return mid


def _hooks_result(root: Path) -> tuple[dict, dict]:
    path = root / FOO / "src/Hook/FooHooks.php"
    core = _core_php(path)
    return extract_hook_implementations(path, core), core


# -- form_FORM_ID_alter (spec §6.2) -------------------------------------------------


def test_a_custom_form_alter_is_bound_to_the_form(tmp_path):
    root = _binding_site(tmp_path)
    prepare_run(root)
    result, core = _hooks_result(root)
    impl = hook_impl_id("foo", "form_foo_plain_alter")
    node = next(n for n in result["nodes"] if n["id"] == impl)
    assert {k: node[k] for k in ("type", "layer", "module", "hook_name", "via", "class_name",
                                 "method", "declared_hook", "form_id")} == {
        "type": "drupal_hook_impl", "layer": "hook", "module": "foo",
        "hook_name": "form_foo_plain_alter", "via": "attribute", "class_name": "FooHooks",
        "method": "plainAlter", "declared_hook": "form_FORM_ID_alter", "form_id": "foo_plain"}
    rel = _rel(result["edges"])
    assert (extension_id("foo"), "implements_hook", hook_id("form_FORM_ID_alter")) in rel
    assert (impl, "hook_implemented_by", _method(core, "FooHooks", "plainAlter")) in rel
    assert (impl, "alters_form", form_id("foo_plain")) in rel
    # The concrete name is never a hook node of its own.
    assert not [e for e in result["edges"] if e["target"] == hook_id("form_foo_plain_alter")]
    alter = next(e for e in result["edges"] if e["relation"] == "alters_form" and e["source"] == impl)
    assert (alter["confidence"], alter["target_name"]) == ("EXTRACTED", "foo_plain")


def _alters(result: dict, impl: str) -> dict[str, dict]:
    return {e["target"]: e for e in result["edges"]
            if e["relation"] == "alters_form" and e["source"] == impl}


def test_a_base_form_alter_alters_every_form_of_that_base(tmp_path):
    root = _binding_site(tmp_path)
    prepare_run(root)
    result, _core = _hooks_result(root)
    impl = hook_impl_id("foo", "form_foo_base_alter")
    assert set(_alters(result, impl)) == {form_id("foo_based"), form_id("foo_other")}
    # Drupal invokes it as `hook_form_BASE_FORM_ID_alter`.
    node = next(n for n in result["nodes"] if n["id"] == impl)
    assert node["declared_hook"] == "form_BASE_FORM_ID_alter"
    assert (extension_id("foo"), "implements_hook", hook_id("form_BASE_FORM_ID_alter")) in \
        _rel(result["edges"])


def test_a_boundary_form_alter_targets_the_boundary_form(tmp_path):
    root = _binding_site(tmp_path)
    prepare_run(root)
    result, _core = _hooks_result(root)
    impl = hook_impl_id("foo", "form_bar_settings_alter")
    assert set(_alters(result, impl)) == {form_id("bar_settings")}
    # A boundary base id alters every boundary form of that base, never a
    # form node of the base id itself.
    base = hook_impl_id("foo", "form_bar_base_alter")
    assert set(_alters(result, base)) == {form_id("bar_one"), form_id("bar_two")}
    assert not [e for e in result["edges"] if e["target"] == form_id("bar_base")]
    assert next(n for n in result["nodes"] if n["id"] == base)["declared_hook"] == \
        "form_BASE_FORM_ID_alter"


def test_entity_form_alters_bind_only_to_forms_drupal_builds(tmp_path):
    root = _binding_site(tmp_path)
    registry = prepare_run(root)
    result, _core = _hooks_result(root)
    assert registry.entity_bundles == {"node": ["article", "page"]}
    assert registry.entity_forms["node"] == ["default", "edit"]
    # `foo` has no bundle key: `foo_edit_form` is exactly its `edit` form.
    edit = _alters(result, hook_impl_id("foo", "form_foo_edit_form_alter"))
    assert {k: (e["target_name"], e["confidence"], e.get("bundle")) for k, e in edit.items()} == {
        entity_form_id("foo", "edit"): ("foo_*_edit_form", "EXTRACTED", None)}
    # `<t>_form` is `EntityForm::getBaseFormId()`: every entity form of `t`.
    for impl, t in ((hook_impl_id("foo", "form_foo_form_alter"), "foo"),
                    (hook_impl_id("foo", "form_node_form_alter"), "node")):
        alters = _alters(result, impl)
        assert set(alters) == {entity_form_id(t, "default"), entity_form_id(t, "edit")}, t
        assert {e["confidence"] for e in alters.values()} == {"EXTRACTED"}
        assert next(n for n in result["nodes"] if n["id"] == impl)["declared_hook"] == \
            "form_BASE_FORM_ID_alter"
    # `node` has a bundle key: `article` is proven by `node.type.article.yml`.
    article = _alters(result, hook_impl_id("foo", "form_node_article_edit_form_alter"))
    assert {k: (e["confidence"], e["bundle"]) for k, e in article.items()} == {
        entity_form_id("node", "edit"): ("EXTRACTED", "article")}
    assert next(n for n in result["nodes"]
                if n["id"] == hook_impl_id("foo", "form_node_article_edit_form_alter")
                )["declared_hook"] == "form_FORM_ID_alter"
    # No `node.type.blog.yml`: the bundle is not guessed.
    assert hook_impl_id("foo", "form_node_blog_form_alter") not in {n["id"] for n in result["nodes"]}


def test_an_entity_base_form_that_is_also_another_type_s_exact_form_alters_both(tmp_path):
    """Final review m3: `foo_edit_form` is `EntityForm::getBaseFormId()` of
    entity type `foo_edit` and exactly `foo`'s `edit` form; Drupal runs the
    alter for both, so both are its targets."""
    root = _binding_site(tmp_path)
    (root / FOO / "src/Entity/FooEdit.php").write_text(_entity(
        "Drupal\\foo\\Entity", "FooEdit", "foo_edit",
        {"default": "Drupal\\foo\\Form\\FooEntityForm"}), encoding="utf-8")
    prepare_run(root)
    result, _core = _hooks_result(root)
    impl = hook_impl_id("foo", "form_foo_edit_form_alter")
    alters = _alters(result, impl)
    assert set(alters) == {entity_form_id("foo_edit", "default"), entity_form_id("foo", "edit")}
    assert {e["confidence"] for e in alters.values()} == {"EXTRACTED"}


@pytest.mark.parametrize("ignored", [
    "config/sync/node.type.article.yml\nweb/modules/contrib/bar/config/optional/node.type.page.yml\n",
    "config/sync/\nweb/modules/contrib/bar/config/\n",
])
def test_an_excluded_config_file_proves_no_bundle(tmp_path, ignored):
    """Final review m4: the sync store and `config/optional` names obey the
    user's excludes, as the rest of the registry walk does."""
    root = _binding_site(tmp_path)
    (root / ".graphifyignore").write_text(ignored, encoding="utf-8")
    registry = prepare_run(root)
    assert not registry.entity_bundles.get("node")


def test_an_unknown_form_is_an_unbound_form_candidate(tmp_path):
    root = _binding_site(tmp_path)
    registry = prepare_run(root)
    result, _core = _hooks_result(root)
    path = root / FOO / "src/Hook/FooHooks.php"
    assert hook_impl_id("foo", "form_nobody_knows_alter") not in {n["id"] for n in result["nodes"]}
    unbound = [c for c in result["hook_candidates"] if c["kind"] == "unbound_form"]
    assert unbound == [
        {"kind": "unbound_form", "module": "foo", "name": name, "form_id": form, "file": str(path),
         "line": line}
        for name, form, line in (("form_nobody_knows_alter", "nobody_knows", 21),
                                 ("form_node_blog_form_alter", "node_blog_form", 53),
                                 # A base id no class with a literal form id carries.
                                 ("form_bar_computed_base_alter", "bar_computed_base", 61))]
    # The inventory reads the same decision.
    assert [c for c in find_hook_candidates(path, registry)
            if c["name"].startswith("form_")] == unbound


# -- hook_ENTITY_TYPE_* (spec §6.3) --------------------------------------------------


def test_entity_type_hooks_are_bound_to_the_entity_type(tmp_path):
    root = _binding_site(tmp_path)
    prepare_run(root)
    result, core = _hooks_result(root)
    rel = _rel(result["edges"])
    presave = hook_impl_id("foo", "node_presave")
    node = next(n for n in result["nodes"] if n["id"] == presave)
    assert (node["declared_hook"], node["entity_type"], node["operation"]) == (
        "ENTITY_TYPE_presave", "node", "presave")
    assert (extension_id("foo"), "implements_hook", hook_id("ENTITY_TYPE_presave")) in rel
    assert (presave, "hooks_entity_type", entity_type_id("node")) in rel
    assert (presave, "hook_implemented_by", _method(core, "FooHooks", "nodePresave")) in rel
    insert = hook_impl_id("foo", "foo_insert")
    assert (insert, "hooks_entity_type", entity_type_id("foo")) in rel
    assert (extension_id("foo"), "implements_hook", hook_id("ENTITY_TYPE_insert")) in rel
    # No entity type `widget`: it stays P2b's candidate.
    assert hook_impl_id("foo", "widget_insert") not in {n["id"] for n in result["nodes"]}
    assert [c for c in result["hook_candidates"] if c["name"] == "widget_insert"] == [{
        "kind": "variable", "module": "foo", "name": "widget_insert", "pattern": "*_insert",
        "file": str(root / FOO / "src/Hook/FooHooks.php"), "line": 41}]
    # Bound variable hooks left the candidates.
    assert sorted(c["name"] for c in result["hook_candidates"]) == [
        "form_bar_computed_base_alter", "form_nobody_knows_alter", "form_node_blog_form_alter",
        "widget_insert"]


def test_a_procedural_hook_splits_only_when_modules_and_entity_types_agree(tmp_path):
    root = _binding_site(tmp_path)
    registry = prepare_run(root)
    path = root / FOO_BAR / "foo_bar.module"
    core = _core_php(path)
    result = extract_hook_implementations(path, core)
    rel = _rel(result["edges"])
    from graphify.extractors.base import _file_stem, _make_id

    # `foo_bar_item_insert`: module `foo_bar`, entity type `item` -- module
    # `foo` would need an entity type `bar_item`, which nothing defines.
    item = hook_impl_id("foo_bar", "item_insert")
    assert (item, "hooks_entity_type", entity_type_id("item")) in rel
    assert (item, "hook_implemented_by", _make_id(_file_stem(path), "foo_bar_item_insert")) in rel
    assert (extension_id("foo_bar"), "implements_hook", hook_id("ENTITY_TYPE_insert")) in rel
    # `foo_bar_thing_insert`: `foo_bar` + `thing` and `foo` + `bar_thing` both
    # read -- ambiguous, so it stays the candidate.
    assert hook_impl_id("foo_bar", "thing_insert") not in {n["id"] for n in result["nodes"]}
    candidates = result["hook_candidates"]
    assert [(c["kind"], c["name"]) for c in candidates] == [("variable", "thing_insert")]
    assert find_hook_candidates(path, registry) == candidates
    # The brief's `foo_bar_insert()` in foo.module: module `foo`, entity type `bar`.
    foo_path = root / FOO / "foo.module"
    foo = extract_hook_implementations(foo_path, _core_php(foo_path))
    assert (hook_impl_id("foo", "bar_insert"), "hooks_entity_type", entity_type_id("bar")) in \
        _rel(foo["edges"])
    assert foo["hook_candidates"] == []
    # A procedural form alter binds too.
    assert (hook_impl_id("foo_bar", "form_foo_plain_alter"), "alters_form",
            form_id("foo_plain")) in rel


# -- events (spec §8) ------------------------------------------------------------------


def test_subscribed_events_from_literal_and_constant_keys(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics, find_php_candidates

    root = _binding_site(tmp_path)
    registry = prepare_run(root)
    path = root / FOO / "src/EventSubscriber/FooSubscriber.php"
    core = _core_php(path)
    result = extract_php_semantics(path, core)
    cls = next(n["id"] for n in core["nodes"] if n.get("label") == "FooSubscriber")
    edges = {e["target"]: e for e in result["edges"] if e["relation"] == "subscribes_to_event"}
    assert set(edges) == {event_id("foo.literal"), event_id("bar.save"), event_id("foo.many")}
    assert all(e["source"] == cls for e in edges.values())
    literal = edges[event_id("foo.literal")]
    assert (literal["method"], literal["priority"], literal["target_name"]) == (
        "onLiteral", 0, "foo.literal")
    save = edges[event_id("bar.save")]
    assert (save["method"], save["priority"], save["target_name"]) == ("onSave", 50, "bar.save")
    many = edges[event_id("foo.many")]
    # Dispatch order, highest priority first, as the container lists them.
    assert (many["method"], many["priorities"]) == ("first,second", [10, -5])
    assert "priority" not in many
    expected = [{"kind": "unresolved_event", "module": "foo",
                 "class": "Drupal\\foo\\EventSubscriber\\FooSubscriber",
                 "event": "NopeEvents::GONE", "method": "onGone", "file": str(path), "line": 15},
                # A value naming no readable listener is not dropped silently.
                {"kind": "unresolved_event", "module": "foo",
                 "class": "Drupal\\foo\\EventSubscriber\\FooSubscriber",
                 "event": "'foo.bad'", "method": "", "file": str(path), "line": 17}]
    assert result["php_candidates"] == expected
    assert find_php_candidates(path, registry) == expected


def test_subscribed_events_from_an_events_array_built_in_steps(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics

    root = _binding_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/EventSubscriber/BazSubscriber.php"
    result = extract_php_semantics(path, _core_php(path))
    got = {e["target"]: (e["method"], e.get("priority")) for e in result["edges"]
           if e["relation"] == "subscribes_to_event"}
    assert got == {event_id("bar.save"): ("onBarSave", -20), event_id("foo.literal"): ("onIt", 0)}


# -- the whole run -----------------------------------------------------------------------


def _extract(root: Path) -> dict:
    from graphify.drupal.register import install

    install()
    import graphify.extract as core

    prepare_run(root)
    # What a real run reads: the custom code, never core or contrib.
    paths = sorted(p for p in (root / "web/modules/custom").rglob("*") if p.is_file()
                   and p.suffix in (".php", ".yml", ".module"))
    return core.extract(paths, root=root, parallel=False)


def test_the_run_binds_targets_and_leaves_nothing_pending(tmp_path):
    root = _binding_site(tmp_path)
    result = _extract(root)
    assert not [e for e in result["edges"] if "pending" in e]
    nodes = {n["id"]: n for n in result["nodes"]}
    rel = _rel(result["edges"])
    # Custom targets are the graph's own nodes; boundary ones are stubs with the registry's facts.
    assert not nodes[form_id("foo_plain")].get("boundary")
    assert not nodes[entity_type_id("foo")].get("boundary")
    assert not nodes[entity_form_id("foo", "edit")].get("boundary")
    bar = nodes[form_id("bar_settings")]
    assert (bar["type"], bar["boundary"], bar["provider"], bar["class_name"]) == (
        "drupal_form", True, "bar", "Drupal\\bar\\Form\\BarSettingsForm")
    node_type = nodes[entity_type_id("node")]
    assert (node_type["type"], node_type["boundary"], node_type["provider"]) == (
        "drupal_entity_type", True, "node")
    node_form = nodes[entity_form_id("node", "default")]
    assert (node_form["type"], node_form["boundary"], node_form["entity_form"],
            node_form["entity_type"], node_form["operation"], node_form["pattern"]) == (
        "drupal_form", True, True, "node", "default", "node_*_form")
    # Events are where custom code listens: never the boundary.
    event = nodes[event_id("bar.save")]
    assert (event["type"], event["label"], event.get("boundary")) == ("drupal_event", "bar.save", None)
    assert event["realm"] == "custom"
    assert (hook_impl_id("foo", "node_presave"), "hooks_entity_type", entity_type_id("node")) in rel
    # One `implements_hook` per (extension, declared hook), however many alters.
    assert len([e for e in result["edges"] if e["relation"] == "implements_hook"
                and e["source"] == extension_id("foo")
                and e["target"] == hook_id("form_FORM_ID_alter")]) == 1


def test_the_inventory_lists_what_stays_unbound(tmp_path):
    root = _binding_site(tmp_path)
    registry = prepare_run(root)
    detected = {str(p) for p in root.rglob("*") if p.is_file()}
    inventory = build_inventory(registry, detected, root)
    assert [(c["kind"], c["name"]) for c in inventory["hook_candidates"]] == [
        ("unbound_form", "form_bar_computed_base_alter"),
        ("unbound_form", "form_nobody_knows_alter"),
        ("unbound_form", "form_node_blog_form_alter"),
        ("variable", "widget_insert"),
        ("variable", "thing_insert"),
    ]
    assert [(c["kind"], c["event"]) for c in inventory["php_candidates"]
            if c["kind"] == "unresolved_event"] == [("unresolved_event", "NopeEvents::GONE"),
                                                   ("unresolved_event", "'foo.bad'")]


# -- the P3 overlay ----------------------------------------------------------------------


def _artifact(root: Path):
    from graphify.drupal.container import Artifact

    hooks_file = f"{FOO}/src/Hook/FooHooks.php"
    data = {
        "schema_version": 1, "services": [], "aliases": {}, "routes": [],
        "extensions": [{"name": name, "type": "module", "path": path, "status": 1, "weight": 0,
                        "dependencies": []}
                       for name, path in (("foo", FOO), ("foo_bar", FOO_BAR))],
        "hooks": {
            "form_foo_plain_alter": [
                {"module": "foo", "callable": "Drupal\\foo\\Hook\\FooHooks::plainAlter",
                 "file": hooks_file},
                {"module": "foo_bar", "callable": "foo_bar_form_foo_plain_alter",
                 "file": f"{FOO_BAR}/foo_bar.module"}],
            "node_presave": [{"module": "foo", "callable": "Drupal\\foo\\Hook\\FooHooks::nodePresave",
                              "file": hooks_file}],
            "form_node_form_alter": [
                {"module": "foo", "callable": "Drupal\\foo\\Hook\\FooHooks::nodeFormAlter",
                 "file": hooks_file}],
            # Unbound statically: still `form_FORM_ID_alter`, never its own hook node.
            "form_nobody_knows_alter": [
                {"module": "foo", "callable": "Drupal\\foo\\Hook\\FooHooks::unknownAlter",
                 "file": hooks_file}],
        },
        "plugins": {},
        "subscribers": {
            "foo.literal": [
                {"callable": "Drupal\\foo\\EventSubscriber\\FooSubscriber::onLiteral",
                 "file": f"{FOO}/src/EventSubscriber/FooSubscriber.php", "priority": 0}],
            "bar.save": [
                {"callable": "Drupal\\foo\\EventSubscriber\\FooSubscriber::onSave",
                 "file": f"{FOO}/src/EventSubscriber/FooSubscriber.php", "priority": 50}],
            # Two listeners of one class: one edge, as the static side has it.
            "foo.many": [
                {"callable": "Drupal\\foo\\EventSubscriber\\FooSubscriber::first",
                 "file": f"{FOO}/src/EventSubscriber/FooSubscriber.php", "priority": 10},
                {"callable": "Drupal\\foo\\EventSubscriber\\FooSubscriber::second",
                 "file": f"{FOO}/src/EventSubscriber/FooSubscriber.php", "priority": -5}],
        },
        "stamp": {}, "errors": [],
    }
    return Artifact(data=data, path=root / "drupal-container.json")


def _dump(G) -> str:
    import networkx as nx

    data = nx.node_link_data(G, edges="links")
    data["nodes"] = sorted(data["nodes"], key=lambda n: n["id"])
    data["links"] = sorted(data["links"], key=lambda e: (e["source"], e["target"]))
    return json.dumps(data, sort_keys=True, default=str)


def test_the_overlay_confirms_bound_hooks_and_subscribers(tmp_path):
    from graphify.drupal.container_overlay import ORIGIN, apply, undo

    root = _binding_site(tmp_path)
    result = _extract(root)
    G = build_from_json(result)
    static = _dump(G)
    subscriber = next(n["id"] for n in result["nodes"] if n.get("label") == "FooSubscriber"
                      and not str(n.get("type", "")).startswith("drupal_"))

    overlay = apply(G, _artifact(root), root)
    assert overlay.status == "fresh", overlay.reasons
    assert overlay.conflicts == []
    for u, v in ((extension_id("foo"), hook_id("form_FORM_ID_alter")),
                 (extension_id("foo"), hook_id("ENTITY_TYPE_presave")),
                 (extension_id("foo_bar"), hook_id("form_FORM_ID_alter")),
                 (extension_id("foo"), hook_id("form_BASE_FORM_ID_alter")),
                 (subscriber, event_id("foo.literal")),
                 (subscriber, event_id("bar.save")),
                 (subscriber, event_id("foo.many"))):
        data = G.edges[u, v]
        assert data["confirmed_by"] == ORIGIN and "origin" not in data, (u, v)
    # The container's concrete names meet the declared hook, never a hook of their own.
    for concrete in ("form_foo_plain_alter", "node_presave", "form_node_form_alter",
                     "form_nobody_knows_alter"):
        assert hook_id(concrete) not in G, concrete
    many = G.edges[subscriber, event_id("foo.many")]
    assert (many["method"], many["priorities"]) == ("first,second", [10, -5])

    once = _dump(G)
    apply(G, _artifact(root), root)
    assert _dump(G) == once
    undo(G)
    assert _dump(G) == static


# -- the reference corpus ------------------------------------------------------------------

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))

#: (module, hook name) -> the `alters_form` / `hooks_entity_type` target, or
#: the candidate kind it stays (see the Task 5 report).
CORPUS_BINDINGS = {
    ("eca_custom", "form_node_case_viewer_form_alter"): entity_form_id("node", "default"),
    ("eca_custom", "form_node_case_viewer_edit_form_alter"): entity_form_id("node", "edit"),
    ("webform_integrations", "form_webform_edit_form_alter"): entity_form_id("webform", "edit"),
    ("eca_custom", "node_view"): entity_type_id("node"),
    ("eca_custom", "node_access"): entity_type_id("node"),
    ("eca_custom", "webform_submission_presave"): entity_type_id("webform_submission"),
    ("eca_custom", "webform_submission_insert"): entity_type_id("webform_submission"),
    ("eca_custom", "webform_submission_update"): entity_type_id("webform_submission"),
    ("webform_integrations", "webform_submission_insert"): entity_type_id("webform_submission"),
    # Procedural ones, beyond the spec's six `#[Hook]` implementations.
    ("custom_forms", "user_access"): entity_type_id("user"),
    ("custom_forms", "user_presave"): entity_type_id("user"),
    ("webform_domain", "node_access"): entity_type_id("node"),
    ("webform_integrations_logs", "user_predelete"): entity_type_id("user"),
    ("webform_integrations", "webform_submission_presave"): entity_type_id("webform_submission"),
    ("webform_integrations", "webform_presave"): entity_type_id("webform"),
}


@pytest.mark.skipif(not (CORPUS / "web" / "core").is_dir(), reason="reference corpus not present")
def test_corpus_form_alters_and_entity_type_hooks(tmp_path):
    from graphify.drupal.register import install

    install()
    import graphify.detect as detect
    from graphify.drupal.hooks import is_procedural_file

    registry = prepare_run(CORPUS, cache_root=tmp_path)
    found = detect.detect(CORPUS, cache_root=tmp_path)
    bound: dict[tuple[str, str], str] = {}
    candidates: dict[str, list[str]] = {}
    for p in found["files"]["code"]:
        path = Path(p)
        if "/tests/" in p or (path.suffix != ".php" and not is_procedural_file(path)):
            continue
        result = extract_hook_implementations(path, _core_php(path))
        nodes = {n["id"]: n for n in result["nodes"]}
        for e in result["edges"]:
            if e["relation"] in ("alters_form", "hooks_entity_type"):
                n = nodes[e["source"]]
                bound[(n["module"], n["hook_name"])] = e["target"]
        for c in find_hook_candidates(path, registry):
            candidates.setdefault(c["kind"], []).append(c["name"])
    assert bound == CORPUS_BINDINGS
    # What stays: `preprocess_*` and `theme_suggestions_*` (P5).
    assert sorted(candidates["variable"]) == sorted(
        ["preprocess_node", "preprocess_webform_integrations_log", "preprocess_webform_confirmation",
         "preprocess_field", "preprocess_input", "preprocess_govuk_header", "preprocess_menu",
         "theme_suggestions_form_element_alter", "theme_suggestions_fieldset_alter"])
    assert "unbound_form" not in candidates
