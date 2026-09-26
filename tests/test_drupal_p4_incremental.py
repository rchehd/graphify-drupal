"""P4 facts follow registry changes on incremental runs (P4 Task 6, spec §9,
§10): a file whose own bytes did not change is re-extracted when a registry
map its per-file facts were read from changed -- a service's class, wiring or
alias, a `create()` up the chain, the boundary forms, entity types, bundles or
event constants, a plugin type, a class another file's edge names -- and the
inventory and GRAPH_REPORT count what P4 produced.

Each case is a real CLI run (`extract`, then `update` or `extract` again) on a
synthetic site: core's incremental merge keeps an unchanged file's edges from
graph.json and hands the resolvers only the fresh files, so only a forced
re-extraction moves them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal import boundary, discovery
from graphify.drupal.yaml_common import plugin_id, service_id
from graphify.ids import make_id
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401
from tests.test_drupal_php_hook_binding import _binding_site, _form, entity_form_id, event_id, form_id
from tests.test_drupal_php_plugins import _plugin_site
from tests.test_drupal_php_services import FOO, _services_site

_SUMMARY = re.compile(r"incremental summary: .*?(\d+) re-extracted")

FOO_OTHER = r"""<?php
namespace Drupal\foo;

class FooOther implements FooHelperInterface {

  public function run() {
  }
}
"""


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state, monkeypatch):  # noqa: F811
    from graphify.drupal.container import ENV_ARTIFACT

    monkeypatch.delenv(ENV_ARTIFACT, raising=False)
    boundary.clear_caches()
    yield
    boundary.clear_caches()


def _env() -> dict:
    from graphify.drupal.container import ENV_ARTIFACT

    return {k: v for k, v in os.environ.items() if k not in (discovery.ENV_VAR, ENV_ARTIFACT)}


def _extract(root: Path, out: Path) -> tuple[dict, int | None]:
    """`extract --code-only --out`: the graph and how many files the run
    re-extracted (None on a first, full run)."""
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--out", str(out)],
        capture_output=True, text=True, cwd=root.parent, env=_env())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = _SUMMARY.search(proc.stdout)
    graph = json.loads((out / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    return graph, int(found.group(1)) if found else None


def _update(root: Path) -> dict:
    proc = subprocess.run([sys.executable, "-m", "graphify", "update", str(root)],
                          capture_output=True, text=True, cwd=root.parent, env=_env())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))


def _links(graph: dict) -> list[dict]:
    return graph.get("links") or graph.get("edges") or []


def _node(graph: dict, file: str, label: str) -> str:
    found = [n["id"] for n in graph["nodes"] if n.get("label") == label
             and not str(n.get("type") or "").startswith("drupal_")
             and str(n.get("source_file") or "").endswith(file)]
    assert len(found) == 1, (file, label, found)
    return found[0]


def _targets(graph: dict, source: str, relation: str, **attrs) -> set[str]:
    return {e["target"] for e in _links(graph) if e["source"] == source
            and e["relation"] == relation and all(e.get(k) == v for k, v in attrs.items())}


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, (path, old)
    path.write_text(text.replace(old, new), encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# -- services ----------------------------------------------------------------------------


def _services_with_other(root: Path) -> Path:
    root = _services_site(root)
    _write(root / FOO / "src/FooOther.php", FOO_OTHER)
    _edit(root / FOO / "foo.services.yml", "  foo.worker:\n",
          "  foo.other:\n    class: Drupal\\foo\\FooOther\n  foo.worker:\n")
    return root


def _method(graph: dict, file: str, cls: str, method: str) -> str:
    from graphify.extractors.base import _make_id

    return _make_id(_node(graph, file, cls), method)


def test_a_changed_service_class_re_extracts_its_users_and_their_calls_follow(tmp_path):
    root = _services_with_other(tmp_path / "site")
    out = tmp_path / "out"
    controller = "src/Controller/FooController.php"
    first, _ = _extract(root, out)
    page = _method(first, controller, "FooController", "page")
    helper_run = _method(first, "src/FooHelper.php", "FooHelper", "run")
    other_run = _method(first, "src/FooOther.php", "FooOther", "run")
    assert helper_run in _targets(first, page, "calls")

    _edit(root / FOO / "foo.services.yml",
          "  foo.helper:\n    class: Drupal\\foo\\FooHelper\n",
          "  foo.helper:\n    class: Drupal\\foo\\FooOther\n")
    second, rerun = _extract(root, out)

    calls = _targets(second, page, "calls")
    assert other_run in calls and helper_run not in calls
    # Its users (the controller among them) were pulled in, not only the YAML.
    assert rerun is not None and rerun > 1


def test_changed_arguments_move_the_injected_edge_under_update(tmp_path):
    """The Task 4 re-review scenario: `foo.consumer`'s `@foo.helper` becomes
    `@foo.other`; FooConsumer.php is unchanged, its `via: injected` edge must move."""
    root = _services_with_other(tmp_path / "site")
    first = _update(root)
    consumer = _node(first, "src/FooConsumer.php", "FooConsumer")
    assert _targets(first, consumer, "uses_service", via="injected") == {service_id("foo.helper")}

    _edit(root / FOO / "foo.services.yml", "arguments: ['@foo.helper', '%foo.param%']",
          "arguments: ['@foo.other', '%foo.param%']")
    second = _update(root)

    assert _targets(second, consumer, "uses_service", via="injected") == {service_id("foo.other")}
    go = _method(second, "src/FooConsumer.php", "FooConsumer", "go")
    assert _targets(second, go, "calls") == {_method(second, "src/FooOther.php", "FooOther", "run")}


def test_a_changed_create_argument_moves_the_subclass_s_property_service(tmp_path):
    root = _services_with_other(tmp_path / "site")
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    child = _node(first, "src/FooStaticChild.php", "FooStaticChild")
    assert _targets(first, child, "uses_service", via="injected") == {service_id("foo.helper")}

    _edit(root / FOO / "src/FooStaticBase.php", "$container->get('foo.helper')",
          "$container->get('foo.other')")
    second, _ = _extract(root, out)

    assert _targets(second, child, "uses_service", via="injected") == {service_id("foo.other")}
    go = _method(second, "src/FooStaticChild.php", "FooStaticChild", "go")
    assert _targets(second, go, "calls") == {_method(second, "src/FooOther.php", "FooOther", "run")}


def test_a_retargeted_alias_moves_its_users(tmp_path):
    """A direct use (`\\Drupal::service('foo.helper_alias')`, its edge keeps
    `alias`) and an injected one (`arguments: ['@foo.helper_alias']`, its
    edge names only the resolved service) both follow the alias."""
    root = _services_with_other(tmp_path / "site")
    _edit(root / FOO / "foo.services.yml", "arguments: ['@foo.helper', '%foo.param%']",
          "arguments: ['@foo.helper_alias', '%foo.param%']")
    out = tmp_path / "out"
    controller = "src/Controller/FooController.php"
    first, _ = _extract(root, out)
    aliased = _method(first, controller, "FooController", "aliased")
    consumer = _node(first, "src/FooConsumer.php", "FooConsumer")
    assert _targets(first, aliased, "uses_service") == {service_id("foo.helper")}
    assert _targets(first, consumer, "uses_service", via="injected") == {service_id("foo.helper")}

    _edit(root / FOO / "foo.services.yml", "    alias: foo.helper\n", "    alias: foo.other\n")
    second, _ = _extract(root, out)

    assert _targets(second, aliased, "uses_service") == {service_id("foo.other")}
    assert _targets(second, aliased, "calls") == {_method(second, "src/FooOther.php", "FooOther", "run")}
    assert _targets(second, consumer, "uses_service", via="injected") == {service_id("foo.other")}


FOO_SHORT = r"""<?php
namespace Drupal\foo;

class FooShort {

  public function go() {
    \Drupal::fooHelper()->run();
  }
}
"""


def test_a_new_drupal_shortcut_names_the_service_of_an_unchanged_call(tmp_path):
    root = _services_with_other(tmp_path / "site")
    _write(root / FOO / "src/FooShort.php", FOO_SHORT)
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    go = _method(first, "src/FooShort.php", "FooShort", "go")
    assert _targets(first, go, "uses_service") == set()

    _edit(root / "web/core/lib/Drupal.php", "  public static function entityTypeManager() {",
          "  public static function fooHelper() {\n"
          "    return static::getContainer()->get('foo.helper');\n  }\n\n"
          "  public static function entityTypeManager() {")
    second, _ = _extract(root, out)

    assert _targets(second, go, "uses_service", via="shortcut") == {service_id("foo.helper")}


def test_a_hook_class_defined_as_a_service_loses_autowiring(tmp_path):
    """Rule 3b stops applying once a `*.services.yml` defines the hook class:
    FooHooks.php is unchanged, its injected edge follows the definition."""
    root = _services_with_other(tmp_path / "site")
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    hooks = _node(first, "src/Hook/FooHooks.php", "FooHooks")
    assert _targets(first, hooks, "uses_service", via="injected") == {service_id("foo.helper")}

    _edit(root / FOO / "foo.services.yml", "  foo.worker:\n",
          "  Drupal\\foo\\Hook\\FooHooks:\n    arguments: ['@foo.other']\n  foo.worker:\n")
    second, _ = _extract(root, out)

    assert _targets(second, hooks, "uses_service", via="injected") == {service_id("foo.other")}


def test_an_unchanged_services_site_re_extracts_nothing(tmp_path):
    root = _services_with_other(tmp_path / "site")
    out = tmp_path / "out"
    _extract(root, out)
    _, rerun = _extract(root, out)
    assert rerun == 0


# -- forms, entity types, bundles, events ------------------------------------------------

BAR = "web/modules/contrib/bar"


def _alters(graph: dict, impl_hook: str) -> set[str]:
    from graphify.drupal.hooks import hook_impl_id

    return _targets(graph, hook_impl_id("foo", impl_hook), "alters_form")


def test_a_new_contrib_form_binds_a_previously_unbound_alter(tmp_path):
    root = _binding_site(tmp_path / "site")
    settings = root / BAR / "src/Form/BarSettingsForm.php"
    settings.unlink()
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    assert _alters(first, "form_bar_settings_alter") == set()

    _write(settings, _form("Drupal\\bar\\Form", "BarSettingsForm", "bar_settings"))
    second, rerun = _extract(root, out)

    assert _alters(second, "form_bar_settings_alter") == {form_id("bar_settings")}
    assert rerun


def test_a_new_bundle_config_file_binds_an_entity_form_alter(tmp_path):
    root = _binding_site(tmp_path / "site")
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    assert _alters(first, "form_node_blog_form_alter") == set()

    _write(root / "config/sync/node.type.blog.yml", "")
    second, _ = _extract(root, out)

    assert _alters(second, "form_node_blog_form_alter") == {entity_form_id("node", "default")}


def test_a_new_event_constant_binds_the_subscriber(tmp_path):
    root = _binding_site(tmp_path / "site")
    events = root / BAR / "src/BarEvents.php"
    saved = events.read_text(encoding="utf-8")
    events.unlink()
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    subscriber = _node(first, "src/EventSubscriber/FooSubscriber.php", "FooSubscriber")
    assert event_id("bar.save") not in _targets(first, subscriber, "subscribes_to_event")

    _write(events, saved)
    second, _ = _extract(root, out)

    assert event_id("bar.save") in _targets(second, subscriber, "subscribes_to_event")


def test_a_changed_form_id_re_binds_the_unchanged_route(tmp_path):
    root = _binding_site(tmp_path / "site")
    _write(root / "web/modules/custom/foo/foo.routing.yml",
           "foo.plain:\n  path: '/foo/plain'\n  defaults:\n"
           "    _form: '\\Drupal\\foo\\Form\\PlainForm'\n  requirements:\n"
           "    _access: 'TRUE'\n")
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    route = make_id("drupal", "route", "foo.plain")
    assert _targets(first, route, "routes_to_form") == {form_id("foo_plain")}

    _edit(root / "web/modules/custom/foo/src/Form/PlainForm.php", "'foo_plain'", "'foo_plainer'")
    second, _ = _extract(root, out)

    assert _targets(second, route, "routes_to_form") == {form_id("foo_plainer")}


def test_an_unchanged_binding_site_re_extracts_nothing(tmp_path):
    root = _binding_site(tmp_path / "site")
    out = tmp_path / "out"
    _extract(root, out)
    _, rerun = _extract(root, out)
    assert rerun == 0


# -- plugins: a class another file names, a new plugin type ---------------------------------

THING_MANAGER = r"""<?php
namespace Drupal\thing;

use Drupal\Core\Plugin\DefaultPluginManager;
use Drupal\foo\Attribute\Thing;

class ThingManager extends DefaultPluginManager {

  public function __construct(\Traversable $namespaces, $cache, $module_handler) {
    parent::__construct('Plugin/X', $namespaces, $module_handler, NULL, Thing::class);
  }
}
"""


def test_a_new_deriver_class_binds_the_unchanged_plugin(tmp_path):
    root = _plugin_site(tmp_path / "site")
    deriver = root / FOO / "src/Plugin/Derivative/FooDeriver.php"
    saved = deriver.read_text(encoding="utf-8")
    deriver.unlink()
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    block = plugin_id("block", "foo_block")
    assert _targets(first, block, "derives_plugins") == set()

    _write(deriver, saved)
    second, _ = _extract(root, out)

    assert _targets(second, block, "derives_plugins") == {
        _node(second, "src/Plugin/Derivative/FooDeriver.php", "FooDeriver")}


def test_a_new_plugin_type_types_an_unchanged_class(tmp_path):
    root = _plugin_site(tmp_path / "site")
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    assert plugin_id("thing", "thing") not in {n["id"] for n in first["nodes"]}

    thing = root / "web/modules/contrib/thing"
    _write(thing / "thing.info.yml", "name: Thing\ntype: module\n")
    _write(thing / "thing.services.yml",
           "services:\n  plugin.manager.thing:\n    class: Drupal\\thing\\ThingManager\n"
           "    parent: default_plugin_manager\n")
    _write(thing / "src/ThingManager.php", THING_MANAGER)
    second, _ = _extract(root, out)

    assert plugin_id("thing", "thing") in {n["id"] for n in second["nodes"]}


def test_an_unchanged_plugin_site_re_extracts_nothing(tmp_path):
    root = _plugin_site(tmp_path / "site")
    out = tmp_path / "out"
    _extract(root, out)
    _, rerun = _extract(root, out)
    assert rerun == 0


# -- the report (spec §10) ------------------------------------------------------------------


def _report_rows(report: str) -> dict[str, str]:
    section = report.split("### PHP semantics", 1)[1].split("###", 1)[0]
    return {m.group(1): m.group(2) for m in re.finditer(r"^\| ([^|]+?) \| ([^|]*?) \|$", section, re.M)}


def test_the_report_counts_what_p4_put_in_the_graph(tmp_path):
    root = _binding_site(tmp_path / "site")
    graph = _update(root)
    links = _links(graph)
    report = (root / "graphify-out" / "GRAPH_REPORT.md").read_text(encoding="utf-8")
    rows = _report_rows(report)

    def count(relation: str) -> str:
        return str(sum(1 for e in links if e["relation"] == relation))

    nodes = [n for n in graph["nodes"] if not n.get("boundary")]
    assert rows["alters_form"] == count("alters_form") != "0"
    assert rows["hooks_entity_type"] == count("hooks_entity_type") != "0"
    assert rows["subscribes_to_event"] == count("subscribes_to_event") != "0"
    assert rows["entity types"] == str(sum(1 for n in nodes if n.get("type") == "drupal_entity_type"))
    assert rows["forms"] == str(sum(1 for n in nodes if n.get("type") == "drupal_form"
                                    and not n.get("entity_form")))
    assert rows["entity forms"] == str(sum(1 for n in nodes if n.get("entity_form"))) != "0"
    inventory = json.loads((root / "graphify-out" / "drupal-inventory.json").read_text(encoding="utf-8"))
    assert inventory["graph"]["alters_form"] == int(rows["alters_form"])


def test_the_report_counts_service_use_by_via_and_bound_calls(tmp_path):
    root = _services_with_other(tmp_path / "site")
    graph = _update(root)
    links = _links(graph)
    rows = _report_rows((root / "graphify-out" / "GRAPH_REPORT.md").read_text(encoding="utf-8"))

    from collections import Counter

    via = Counter(e.get("via") for e in links if e["relation"] == "uses_service")
    assert {"create", "injected", "service", "shortcut"} <= set(via)
    assert rows["uses_service by via"] == ", ".join(f"{k} {via[k]}" for k in sorted(via))
    bound = sum(1 for e in links if e["relation"] == "calls" and e.get("service"))
    assert rows["calls bound (static)"] == str(bound) != "0"


# -- the inventory's PHP candidates are cached (Task 2 minor) -------------------------------


def test_php_candidates_are_read_again_only_for_a_changed_file_or_registry(tmp_path, monkeypatch):
    from graphify.drupal import inventory as inv
    from graphify.drupal.discovery import build_registry

    root = _plugin_site(tmp_path / "site")
    registry = build_registry(root)
    detected = {p.as_posix() for p in root.rglob("*.php")}
    reads: list[str] = []
    real = inv.find_php_candidates

    def counting(path, registry=None):
        reads.append(Path(path).name)
        return real(path, registry)

    monkeypatch.setattr(inv, "find_php_candidates", counting)
    cache = tmp_path / "out"
    first = inv.build_inventory(registry, detected, root, cache)["php_candidates"]
    assert first and len(reads) == len(detected)

    reads.clear()
    assert inv.build_inventory(registry, detected, root, cache)["php_candidates"] == first
    assert reads == []

    thing = root / FOO / "src/Plugin/X/Thing1.php"
    thing.write_text(thing.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    inv.build_inventory(registry, detected, root, cache)
    assert reads == ["Thing1.php"]

    reads.clear()
    registry.shortcuts["x"] = "y"
    inv.build_inventory(registry, detected, root, cache)
    assert len(reads) == len(detected)


def test_only_a_binding_change_forces_the_hook_files(tmp_path):
    from graphify.drupal.discovery import affected_files, build_registry

    root = _binding_site(tmp_path / "site")
    hooks = (root / "web/modules/custom/foo/src/Hook/FooHooks.php").as_posix()
    before = build_registry(root)
    _write(root / "web/modules/custom/foo/src/Plain.php",
           "<?php\nnamespace Drupal\\foo;\n\nclass Plain {\n}\n")
    plain = build_registry(root)
    forced = affected_files(before, plain)
    assert hooks not in forced
    assert (root / "web/modules/custom/foo/src/Plain.php").as_posix() in forced

    (root / BAR / "src/Form/BarSettingsForm.php").unlink()
    assert hooks in affected_files(plain, build_registry(root))
