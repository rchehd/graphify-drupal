"""P4 facts follow registry changes on incremental runs (P4 Task 6, spec §9,
§10): a file whose own bytes did not change is re-extracted when a registry
map its per-file facts were read from changed -- a service's class, wiring or
alias, a `create()` up the chain, the boundary forms, entity types, bundles or
event constants, a plugin type, a class another file's edge names -- and the
inventory and GRAPH_REPORT count what P4 produced.

Each case is a real run on a synthetic site: core's incremental merge keeps
an unchanged file's edges from graph.json and hands the resolvers only the
fresh files, so only a forced re-extraction moves them. Every staleness case
runs through each incremental path (`Run`, `MODES`): the CLI's incremental
`extract`, `graphify watch`'s rebuild (which the git hooks run too) with and
without `--no-cluster`, and a full build; each incremental graph must also
equal a full build of the same tree.
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


# -- the harness: every staleness case runs through each incremental path -----------------

# `extract`: `extract --code-only --out` twice (core's CLI incremental merge).
# `watch` / `watch-nc`: `update` first, then `graphify watch`'s own rebuild
# (`watch._rebuild_code(changed_paths=…)`, which the git hooks call too), with
# and without `--no-cluster`. `full`: the second run starts from nothing.
MODES = ("extract", "watch", "watch-nc", "full")

_WATCH = (
    "import sys\n"
    "from pathlib import Path\n"
    "from graphify.watch import _rebuild_code\n"
    "ok = _rebuild_code(Path(sys.argv[1]), changed_paths=[Path(p) for p in sys.argv[3:]],\n"
    "                   no_cluster=sys.argv[2] == '1')\n"
    "sys.exit(0 if ok else 1)\n"
)

# Attributes a clustering pass or a run's layout owns, not the extraction.
_VOLATILE = {"community", "community_name", "norm_label"}
# Core's own incremental run resolves a type reference into an unchanged file
# differently from a full one (an external stub instead of the class): a plain
# edit of one PHP file, no Drupal fact involved, shows the same difference.
# Those edges and the stubs they name are core's, and are left out.
_CORE_TYPE_REFS = {"references", "imports", "imports_from", "implements", "inherits"}


def _canonical(graph: dict) -> tuple[set, set]:
    def freeze(item: dict) -> str:
        return json.dumps({k: v for k, v in item.items() if k not in _VOLATILE},
                          sort_keys=True, default=str)

    nodes = {freeze(n) for n in graph["nodes"] if n.get("source_file") or n.get("type")}
    edges = {freeze(e) for e in _links(graph) if e.get("relation") not in _CORE_TYPE_REFS}
    return nodes, edges


class Run:
    """One staleness case in one mode: `build()`, edit through `edit`/`write`/
    `unlink` (watch needs the changed paths), then `again()`. `again()` also
    checks the incremental graph equals a full build of the same tree."""

    def __init__(self, mode: str, root: Path, tmp: Path):
        self.mode, self.root, self.tmp = mode, root, tmp
        self.changed: list[Path] = []
        self.rerun: int | None = None
        self.out = root / "graphify-out" if mode.startswith("watch") else tmp / "out" / "graphify-out"

    def _cli(self, *args: str) -> None:
        proc = subprocess.run([sys.executable, "-m", "graphify", *args],
                              capture_output=True, text=True, cwd=self.root.parent, env=_env())
        assert proc.returncode == 0, proc.stdout + proc.stderr
        found = _SUMMARY.search(proc.stdout)
        self.rerun = int(found.group(1)) if found else None

    def _first(self) -> dict:
        if self.mode.startswith("watch"):
            self._cli("update", str(self.root), *(["--no-cluster"] if self.mode == "watch-nc" else []))
        else:
            self._cli("extract", str(self.root), "--code-only", "--out", str(self.out.parent))
        return json.loads((self.out / "graph.json").read_text(encoding="utf-8"))

    def build(self) -> dict:
        return self._first()

    def edit(self, path: Path, old: str, new: str) -> None:
        _edit(path, old, new)
        self.changed.append(path)

    def write(self, path: Path, text: str) -> None:
        _write(path, text)
        self.changed.append(path)

    def unlink(self, path: Path) -> None:
        path.unlink()
        self.changed.append(path)

    def again(self, *, same_as_full: bool = True) -> dict:
        import shutil

        if self.mode == "full":
            shutil.rmtree(self.out)
            return self._first()
        if self.mode == "extract":
            self._cli("extract", str(self.root), "--code-only", "--out", str(self.out.parent))
        else:
            if all(_in_boundary(self.root, p) for p in self.changed):
                # `graphify watch` ignores boundary events by design (the
                # registry prunes core and contrib from its detect, see
                # register._noise): a boundary change reaches the graph with
                # the next rebuild a tracked change triggers.
                nudge = self.root / FOO / "foo.info.yml"
                nudge.write_text(nudge.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                self.changed.append(nudge)
            proc = subprocess.run(
                [sys.executable, "-c", _WATCH, str(self.root), "1" if self.mode == "watch-nc" else "0",
                 *map(str, self.changed)],
                capture_output=True, text=True, cwd=self.root.parent, env=_env())
            assert proc.returncode == 0, proc.stdout + proc.stderr
        self.changed = []
        rerun = self.rerun
        graph = json.loads((self.out / "graph.json").read_text(encoding="utf-8"))
        if same_as_full:
            # Outside the scanned tree, so the full build does not read it.
            saved = self.tmp / "incremental-out"
            shutil.move(self.out, saved)
            full = self._first()
            shutil.rmtree(self.out)
            shutil.move(saved, self.out)
            assert _canonical(graph) == _canonical(full), _diff(graph, full)
        self.rerun = rerun
        return graph


def _in_boundary(root: Path, path: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    return rel.startswith(("web/core/", "web/modules/contrib/", "vendor/"))


def _diff(incremental: dict, full: dict) -> str:
    from collections import Counter

    (n1, e1), (n2, e2) = _canonical(incremental), _canonical(full)

    def kinds(items, key):
        return dict(Counter(json.loads(i).get(key) for i in items))

    return (f"nodes only incremental {kinds(n1 - n2, 'type')} {sorted(n1 - n2)[:3]}\n"
            f"nodes only full {kinds(n2 - n1, 'type')} {sorted(n2 - n1)[:3]}\n"
            f"edges only incremental {kinds(e1 - e2, 'relation')} {sorted(e1 - e2)[:3]}\n"
            f"edges only full {kinds(e2 - e1, 'relation')} {sorted(e2 - e1)[:3]}")


@pytest.fixture(params=MODES)
def mode(request) -> str:
    return request.param


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


def test_a_changed_service_class_re_extracts_its_users_and_their_calls_follow(tmp_path, mode):
    root = _services_with_other(tmp_path / "site")
    run = Run(mode, root, tmp_path)
    controller = "src/Controller/FooController.php"
    first = run.build()
    page = _method(first, controller, "FooController", "page")
    helper_run = _method(first, "src/FooHelper.php", "FooHelper", "run")
    other_run = _method(first, "src/FooOther.php", "FooOther", "run")
    assert helper_run in _targets(first, page, "calls")

    run.edit(root / FOO / "foo.services.yml",
             "  foo.helper:\n    class: Drupal\\foo\\FooHelper\n",
             "  foo.helper:\n    class: Drupal\\foo\\FooOther\n")
    second = run.again()

    calls = _targets(second, page, "calls")
    assert other_run in calls and helper_run not in calls
    if mode == "extract":
        # Its users (the controller among them) were pulled in, not only the YAML.
        assert run.rerun is not None and run.rerun > 1


def test_changed_arguments_move_the_injected_edge(tmp_path, mode):
    """The Task 4 re-review scenario: `foo.consumer`'s `@foo.helper` becomes
    `@foo.other`; FooConsumer.php is unchanged, its `via: injected` edge must
    move -- and under `graphify watch` the old one must not stay beside it."""
    root = _services_with_other(tmp_path / "site")
    run = Run(mode, root, tmp_path)
    first = run.build()
    consumer = _node(first, "src/FooConsumer.php", "FooConsumer")
    assert _targets(first, consumer, "uses_service", via="injected") == {service_id("foo.helper")}

    run.edit(root / FOO / "foo.services.yml", "arguments: ['@foo.helper', '%foo.param%']",
             "arguments: ['@foo.other', '%foo.param%']")
    second = run.again()

    assert _targets(second, consumer, "uses_service", via="injected") == {service_id("foo.other")}
    go = _method(second, "src/FooConsumer.php", "FooConsumer", "go")
    assert _targets(second, go, "calls") == {_method(second, "src/FooOther.php", "FooOther", "run")}


def test_changed_arguments_move_the_injected_edge_under_update(tmp_path):
    """`graphify update` (a full code rebuild through watch's path)."""
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


def test_a_changed_create_argument_moves_the_subclass_s_property_service(tmp_path, mode):
    root = _services_with_other(tmp_path / "site")
    run = Run(mode, root, tmp_path)
    first = run.build()
    child = _node(first, "src/FooStaticChild.php", "FooStaticChild")
    assert _targets(first, child, "uses_service", via="injected") == {service_id("foo.helper")}

    run.edit(root / FOO / "src/FooStaticBase.php", "$container->get('foo.helper')",
             "$container->get('foo.other')")
    second = run.again()

    assert _targets(second, child, "uses_service", via="injected") == {service_id("foo.other")}
    go = _method(second, "src/FooStaticChild.php", "FooStaticChild", "go")
    assert _targets(second, go, "calls") == {_method(second, "src/FooOther.php", "FooOther", "run")}


def test_a_retargeted_alias_moves_its_users(tmp_path, mode):
    """A direct use (`\\Drupal::service('foo.helper_alias')`, its edge keeps
    `alias`) and an injected one (`arguments: ['@foo.helper_alias']`, its
    edge names only the resolved service) both follow the alias."""
    root = _services_with_other(tmp_path / "site")
    _edit(root / FOO / "foo.services.yml", "arguments: ['@foo.helper', '%foo.param%']",
          "arguments: ['@foo.helper_alias', '%foo.param%']")
    run = Run(mode, root, tmp_path)
    controller = "src/Controller/FooController.php"
    first = run.build()
    aliased = _method(first, controller, "FooController", "aliased")
    consumer = _node(first, "src/FooConsumer.php", "FooConsumer")
    assert _targets(first, aliased, "uses_service") == {service_id("foo.helper")}
    assert _targets(first, consumer, "uses_service", via="injected") == {service_id("foo.helper")}

    run.edit(root / FOO / "foo.services.yml", "    alias: foo.helper\n", "    alias: foo.other\n")
    second = run.again()

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


def test_a_new_drupal_shortcut_names_the_service_of_an_unchanged_call(tmp_path, mode):
    root = _services_with_other(tmp_path / "site")
    _write(root / FOO / "src/FooShort.php", FOO_SHORT)
    run = Run(mode, root, tmp_path)
    first = run.build()
    go = _method(first, "src/FooShort.php", "FooShort", "go")
    assert _targets(first, go, "uses_service") == set()

    run.edit(root / "web/core/lib/Drupal.php", "  public static function entityTypeManager() {",
             "  public static function fooHelper() {\n"
             "    return static::getContainer()->get('foo.helper');\n  }\n\n"
             "  public static function entityTypeManager() {")
    second = run.again()

    assert _targets(second, go, "uses_service", via="shortcut") == {service_id("foo.helper")}


def test_a_hook_class_defined_as_a_service_loses_autowiring(tmp_path, mode):
    """Rule 3b stops applying once a `*.services.yml` defines the hook class:
    FooHooks.php is unchanged, its injected edge follows the definition."""
    root = _services_with_other(tmp_path / "site")
    run = Run(mode, root, tmp_path)
    first = run.build()
    hooks = _node(first, "src/Hook/FooHooks.php", "FooHooks")
    assert _targets(first, hooks, "uses_service", via="injected") == {service_id("foo.helper")}

    run.edit(root / FOO / "foo.services.yml", "  foo.worker:\n",
             "  Drupal\\foo\\Hook\\FooHooks:\n    arguments: ['@foo.other']\n  foo.worker:\n")
    second = run.again()

    assert _targets(second, hooks, "uses_service", via="injected") == {service_id("foo.other")}


def _carrier(graph: dict, source: str, sid: str) -> dict:
    found = [e for e in _links(graph) if e["source"] == source and e["relation"] == "uses_service"
             and e["target"] == service_id(sid)]
    assert len(found) == 1, found
    return found[0]


def test_a_method_added_to_or_removed_from_a_service_class_moves_its_calls(tmp_path, mode):
    """Final review I2: the callers of a service whose class (or a custom
    ancestor, `foo.worker` extends FooHelper) gained or lost a method are
    unchanged files; their `calls` and the carrier's `methods` must follow."""
    root = _services_with_other(tmp_path / "site")
    controller = root / FOO / "src/Controller/FooController.php"
    _edit(controller, "    \\Drupal::service('foo.worker')->run();\n",
          "    \\Drupal::service('foo.worker')->run();\n    \\Drupal::service('foo.worker')->later();\n")
    _edit(controller, "  public function aliased() {\n",
          "  public function soon() {\n    \\Drupal::service('foo.helper')->later();\n  }\n\n"
          "  public function aliased() {\n")
    run = Run(mode, root, tmp_path)
    ctl = "src/Controller/FooController.php"
    first = run.build()
    soon = _method(first, ctl, "FooController", "soon")
    inherited = _method(first, ctl, "FooController", "inherited")
    page = _method(first, ctl, "FooController", "page")
    helper_run = _method(first, "src/FooHelper.php", "FooHelper", "run")
    assert _targets(first, soon, "calls") == set()
    assert _carrier(first, soon, "foo.helper")["methods"] == ["later"]

    run.edit(root / FOO / "src/FooHelper.php", "  public function run() {\n  }\n",
             "  public function run() {\n  }\n\n  public function later() {\n  }\n")
    second = run.again()

    later = _method(second, "src/FooHelper.php", "FooHelper", "later")
    assert _targets(second, soon, "calls") == {later}
    assert _targets(second, inherited, "calls") == {helper_run, later}
    assert "methods" not in _carrier(second, soon, "foo.helper")

    run.edit(root / FOO / "src/FooHelper.php", "  public function run() {\n  }\n\n", "")
    third = run.again()

    assert _targets(third, page, "calls") == set()
    assert _targets(third, inherited, "calls") == {later}
    carrier = _carrier(third, page, "foo.helper")
    assert carrier["methods"] == ["run"]
    assert carrier["_pending_calls"] == [[page, "run"]]


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


def test_a_new_contrib_form_binds_a_previously_unbound_alter(tmp_path, mode):
    root = _binding_site(tmp_path / "site")
    settings = root / BAR / "src/Form/BarSettingsForm.php"
    settings.unlink()
    run = Run(mode, root, tmp_path)
    first = run.build()
    assert _alters(first, "form_bar_settings_alter") == set()

    run.write(settings, _form("Drupal\\bar\\Form", "BarSettingsForm", "bar_settings"))
    second = run.again()

    assert _alters(second, "form_bar_settings_alter") == {form_id("bar_settings")}
    if mode == "extract":
        assert run.rerun


def test_a_new_bundle_config_file_binds_an_entity_form_alter(tmp_path, mode):
    root = _binding_site(tmp_path / "site")
    run = Run(mode, root, tmp_path)
    first = run.build()
    assert _alters(first, "form_node_blog_form_alter") == set()

    run.write(root / "config/sync/node.type.blog.yml", "")
    second = run.again()

    assert _alters(second, "form_node_blog_form_alter") == {entity_form_id("node", "default")}


def test_a_new_event_constant_binds_the_subscriber(tmp_path, mode):
    root = _binding_site(tmp_path / "site")
    events = root / BAR / "src/BarEvents.php"
    saved = events.read_text(encoding="utf-8")
    events.unlink()
    run = Run(mode, root, tmp_path)
    first = run.build()
    subscriber = _node(first, "src/EventSubscriber/FooSubscriber.php", "FooSubscriber")
    assert event_id("bar.save") not in _targets(first, subscriber, "subscribes_to_event")

    run.write(events, saved)
    second = run.again()

    assert event_id("bar.save") in _targets(second, subscriber, "subscribes_to_event")


def test_a_changed_form_id_re_binds_the_unchanged_route(tmp_path, mode):
    root = _binding_site(tmp_path / "site")
    _write(root / "web/modules/custom/foo/foo.routing.yml",
           "foo.plain:\n  path: '/foo/plain'\n  defaults:\n"
           "    _form: '\\Drupal\\foo\\Form\\PlainForm'\n  requirements:\n"
           "    _access: 'TRUE'\n")
    run = Run(mode, root, tmp_path)
    first = run.build()
    route = make_id("drupal", "route", "foo.plain")
    assert _targets(first, route, "routes_to_form") == {form_id("foo_plain")}

    run.edit(root / "web/modules/custom/foo/src/Form/PlainForm.php", "'foo_plain'", "'foo_plainer'")
    second = run.again()

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


def test_a_new_deriver_class_binds_the_unchanged_plugin(tmp_path, mode):
    root = _plugin_site(tmp_path / "site")
    deriver = root / FOO / "src/Plugin/Derivative/FooDeriver.php"
    saved = deriver.read_text(encoding="utf-8")
    deriver.unlink()
    run = Run(mode, root, tmp_path)
    first = run.build()
    block = plugin_id("block", "foo_block")
    assert _targets(first, block, "derives_plugins") == set()

    run.write(deriver, saved)
    second = run.again()

    assert _targets(second, block, "derives_plugins") == {
        _node(second, "src/Plugin/Derivative/FooDeriver.php", "FooDeriver")}


def test_a_new_plugin_type_types_an_unchanged_class(tmp_path, mode):
    root = _plugin_site(tmp_path / "site")
    run = Run(mode, root, tmp_path)
    first = run.build()
    assert plugin_id("thing", "thing") not in {n["id"] for n in first["nodes"]}

    thing = root / "web/modules/contrib/thing"
    run.write(thing / "thing.info.yml", "name: Thing\ntype: module\n")
    run.write(thing / "thing.services.yml",
              "services:\n  plugin.manager.thing:\n    class: Drupal\\thing\\ThingManager\n"
              "    parent: default_plugin_manager\n")
    run.write(thing / "src/ThingManager.php", THING_MANAGER)
    second = run.again()

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


def test_an_edited_extractor_reads_every_php_candidate_again(tmp_path, monkeypatch):
    """The cache is namespaced by this package's code, as core's AST cache is."""
    import shutil

    from graphify.drupal import fingerprint
    from graphify.drupal import inventory as inv
    from graphify.drupal.discovery import build_registry

    package = tmp_path / "package"
    package.mkdir()
    for source in Path(fingerprint.__file__).parent.glob("*.py"):
        shutil.copy(source, package / source.name)
    monkeypatch.setattr(inv, "drupal_fingerprint", lambda: fingerprint.drupal_fingerprint(package))

    root = _plugin_site(tmp_path / "site")
    registry = build_registry(root)
    detected = {p.as_posix() for p in root.rglob("*.php")}
    reads: list[str] = []
    real = inv.find_php_candidates
    monkeypatch.setattr(inv, "find_php_candidates",
                        lambda path, registry=None: reads.append(Path(path).name) or real(path, registry))
    cache = tmp_path / "out"
    inv.build_inventory(registry, detected, root, cache)
    reads.clear()
    inv.build_inventory(registry, detected, root, cache)
    assert reads == []

    semantics = package / "php_semantics.py"
    semantics.write_text(semantics.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    inv.build_inventory(registry, detected, root, cache)
    assert len(reads) == len(detected)


def test_a_failed_graph_count_is_reported_not_zeroed():
    from graphify.drupal.inventory import graph_counts, render_section

    class Broken:
        @property
        def nodes(self):
            raise RuntimeError("boom")

    counts = graph_counts(Broken())
    assert counts["counts_error"] == "RuntimeError: boom"
    assert "counts incomplete:" in render_section({"graph": counts})
    assert "counts_error" not in graph_counts({"nodes": [], "links": []})


ONLY_SUBSCRIBER = r"""<?php

namespace Drupal\foo\EventSubscriber;

use Drupal\bar\BarEvents;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;

class OnlySubscriber implements EventSubscriberInterface {

  public static function getSubscribedEvents(): array {
    return [BarEvents::SAVE => 'onSave'];
  }

}
"""


def test_a_subscriber_known_only_as_an_unresolved_event_binds_once_the_constant_exists(tmp_path):
    """No `subscribes_to_event` edge names this file in the previous graph:
    only its `unresolved_event` candidate can bring it back."""
    from tests.test_drupal_discovery import _site

    root = _site(tmp_path / "site", {
        f"{BAR}/bar.info.yml": "name: Bar\ntype: module\n",
        "web/modules/custom/foo/foo.info.yml": "name: Foo\ntype: module\n",
        "web/modules/custom/foo/src/EventSubscriber/OnlySubscriber.php": ONLY_SUBSCRIBER,
    })
    out = tmp_path / "out"
    first, _ = _extract(root, out)
    subscriber = _node(first, "src/EventSubscriber/OnlySubscriber.php", "OnlySubscriber")
    assert _targets(first, subscriber, "subscribes_to_event") == set()
    inventory = json.loads((out / "graphify-out" / "drupal-inventory.json").read_text(encoding="utf-8"))
    assert [c["kind"] for c in inventory["php_candidates"]] == ["unresolved_event"]

    _write(root / BAR / "src/BarEvents.php",
           "<?php\n\nnamespace Drupal\\bar;\n\nfinal class BarEvents {\n\n"
           "  const SAVE = 'bar.save';\n\n}\n")
    second, _ = _extract(root, out)

    assert _targets(second, subscriber, "subscribes_to_event") == {event_id("bar.save")}


def test_the_registry_widening_extends_the_caller_s_list_in_place(tmp_path, monkeypatch):
    """`graphify watch` evicts the old edges of the files in its own
    `extract_targets` list only: a widened file must land in that list."""
    import graphify.extract as core_extract
    from graphify.drupal import register
    from graphify.drupal.register import DrupalSeamError

    changed, pulled = tmp_path / "a.php", tmp_path / "b.php"
    for path in (changed, pulled):
        _write(path, "<?php\nfunction f_" + path.stem + "() {\n}\n")
    monkeypatch.setattr(register, "_registry_widening", lambda given, context, anchor: [pulled])
    context = [{"id": "f_b", "label": "f_b()", "source_file": str(pulled), "type": "code"}]

    paths = [changed]
    core_extract.extract(paths, cache_root=tmp_path, resolution_context_nodes=list(context))
    assert paths == [changed, pulled]

    with pytest.raises(DrupalSeamError):
        core_extract.extract((changed,), cache_root=tmp_path, resolution_context_nodes=list(context))
