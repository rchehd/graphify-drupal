"""Services (P4 Task 4, spec §7): `uses_service` for the three ways custom
code reaches a service, and `calls` resolved into the service class's method
through direct receivers, single-assignment locals and injected properties
(§7.3's rules 1, 1b, 2, 3 and 4), with the P3 overlay binding what only the
container knows the class of."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graphify.build import build_from_json
from graphify.drupal import boundary
from graphify.drupal.discovery import Registry, prepare_run
from graphify.drupal.yaml_common import service_id
from tests.test_drupal_discovery import _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401
from tests.test_drupal_php_plugins import _cli, _core_php, _links

FOO = "web/modules/custom/foo"

DRUPAL_PHP = r"""<?php

class Drupal {

  protected static $container;

  public static function getContainer() {
    return static::$container;
  }

  public static function service($id) {
    return static::getContainer()->get($id);
  }

  public static function entityTypeManager() {
    return static::getContainer()->get('entity_type.manager');
  }
}
"""

CORE_SERVICES = """\
services:
  entity_type.manager:
    class: Drupal\\Core\\Entity\\EntityTypeManager
  Drupal\\Core\\Entity\\EntityTypeManagerInterface: '@entity_type.manager'
"""

FOO_SERVICES = """\
parameters:
  foo.param: 1
services:
  foo.helper:
    class: Drupal\\foo\\FooHelper
  Drupal\\foo\\FooHelperInterface: '@foo.helper'
  foo.helper_alias:
    alias: foo.helper
  foo.worker:
    class: Drupal\\foo\\FooWorker
  foo.consumer:
    class: Drupal\\foo\\FooConsumer
    arguments: ['@foo.helper', '%foo.param%']
  foo.auto:
    class: Drupal\\foo\\FooAuto
    autowire: true
  foo.child:
    class: Drupal\\foo\\FooChild
    arguments: ['@foo.helper', '@entity_type.manager']
"""

FOO_HELPER_INTERFACE = r"""<?php
namespace Drupal\foo;

interface FooHelperInterface {
  public function run();
}
"""

FOO_HELPER = r"""<?php
namespace Drupal\foo;

class FooHelper implements FooHelperInterface {

  public function run() {
  }
}
"""

# A subclass without `run()`: a call on it lands on the parent's method node.
FOO_WORKER = r"""<?php
namespace Drupal\foo;

class FooWorker extends FooHelper {
}
"""

FOO_CONTROLLER = r"""<?php
namespace Drupal\foo\Controller;

class FooController {

  public function page() {
    \Drupal::service('foo.helper')->run();
    \Drupal::entityTypeManager()->getStorage('node');
    $h = \Drupal::service('foo.helper');
    $h->run();
    \Drupal::service($this->id);
  }

  public function twice() {
    $x = \Drupal::service('foo.helper');
    $x = \Drupal::service('foo.other');
    $x->run();
  }

  public function aliased() {
    \Drupal::service('foo.helper_alias')->run();
  }

  public function inherited() {
    \Drupal::service('foo.worker')->run();
  }

  public function dynamic() {
    \Drupal::service('foo.dynamic')->run();
  }
}
"""

FOO_FORM = r"""<?php
namespace Drupal\foo\Form;

use Drupal\foo\FooHelper;
use Symfony\Component\DependencyInjection\ContainerInterface;

class FooForm extends FormBase {

  public function __construct(protected FooHelper $helper) {
  }

  public static function create(ContainerInterface $container) {
    return new static($container->get('foo.helper'));
  }

  public function getFormId() {
    return 'foo_form';
  }

  public function submitForm(array &$form, $form_state) {
    $this->helper->run();
  }
}
"""

FOO_SETTER_FORM = r"""<?php
namespace Drupal\foo\Form;

use Symfony\Component\DependencyInjection\ContainerInterface;

class FooSetterForm extends FormBase {

  protected $helper;

  public static function create(ContainerInterface $container) {
    $instance = parent::create($container);
    $instance->helper = $container->get('foo.helper');
    return $instance;
  }

  public function submitForm(array &$form, $form_state) {
    $this->helper->run();
  }
}
"""

FOO_CONSUMER = r"""<?php
namespace Drupal\foo;

class FooConsumer {

  protected $h;

  public function __construct(FooHelperInterface $helper, $param) {
    $this->h = $helper;
  }

  public function go() {
    $this->h->run();
  }
}
"""

FOO_AUTO = r"""<?php
namespace Drupal\foo;

use Drupal\Core\Entity\EntityTypeManagerInterface;

class FooAuto {

  public function __construct(private FooHelperInterface $helper, private EntityTypeManagerInterface $etm) {
  }

  public function go() {
    $this->helper->run();
    $this->etm->getStorage('node');
  }
}
"""

FOO_BASE = r"""<?php
namespace Drupal\foo;

class FooBase {

  protected $helper;

  public function __construct(FooHelperInterface $helper) {
    $this->helper = $helper;
  }
}
"""

FOO_CHILD = r"""<?php
namespace Drupal\foo;

use Drupal\Core\Entity\EntityTypeManagerInterface;

class FooChild extends FooBase {

  public function __construct(FooHelperInterface $helper, protected EntityTypeManagerInterface $etm) {
    parent::__construct($helper);
  }

  public function go() {
    $this->helper->run();
  }
}
"""

# Not a service: `helper` is typed with a known service type but nothing says
# which service it is (a candidate); `when` is a value object (nothing).
FOO_LOOSE = r"""<?php
namespace Drupal\foo;

class FooLoose {

  public function __construct(protected FooHelperInterface $helper, protected \DateTimeImmutable $when) {
  }

  public function go() {
    $this->helper->run();
    $this->when->format('c');
  }
}
"""

FOO_MODULE = r"""<?php

function foo_cron() {
  \Drupal::service('foo.helper')->run();
}
"""


def _services_site(root: Path) -> Path:
    return _site(root, {
        "web/core/lib/Drupal.php": DRUPAL_PHP,
        "web/core/core.services.yml": CORE_SERVICES,
        f"{FOO}/foo.info.yml": "name: Foo\ntype: module\n",
        f"{FOO}/foo.services.yml": FOO_SERVICES,
        f"{FOO}/foo.module": FOO_MODULE,
        f"{FOO}/src/FooHelperInterface.php": FOO_HELPER_INTERFACE,
        f"{FOO}/src/FooHelper.php": FOO_HELPER,
        f"{FOO}/src/FooWorker.php": FOO_WORKER,
        f"{FOO}/src/Controller/FooController.php": FOO_CONTROLLER,
        f"{FOO}/src/Form/FooForm.php": FOO_FORM,
        f"{FOO}/src/Form/FooSetterForm.php": FOO_SETTER_FORM,
        f"{FOO}/src/FooConsumer.php": FOO_CONSUMER,
        f"{FOO}/src/FooAuto.php": FOO_AUTO,
        f"{FOO}/src/FooBase.php": FOO_BASE,
        f"{FOO}/src/FooChild.php": FOO_CHILD,
        f"{FOO}/src/FooLoose.php": FOO_LOOSE,
    })


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state, monkeypatch):  # noqa: F811
    from graphify.drupal.container import ENV_ARTIFACT

    monkeypatch.delenv(ENV_ARTIFACT, raising=False)
    boundary.clear_caches()
    yield
    boundary.clear_caches()


def _extract(root: Path) -> dict:
    from graphify.drupal.register import install

    install()
    import graphify.extract as core

    prepare_run(root)
    paths = sorted(p for p in root.rglob("*") if p.is_file()
                   and p.suffix in (".php", ".yml", ".module"))
    return core.extract(paths, root=root, parallel=False)


def _php_node(result: dict, file: str, label: str) -> str:
    found = [n["id"] for n in result["nodes"] if n.get("label") == label
             and not str(n.get("type", "")).startswith("drupal_")
             and str(n.get("source_file", "")).endswith(file)]
    assert len(found) == 1, (file, label, found)
    return found[0]


def _method(result: dict, file: str, cls: str, method: str) -> str:
    """The method node `cls::method` of `file`: joined to its class by `method`."""
    class_id = _php_node(result, file, cls)
    found = [e["target"] for e in result["edges"] if e["relation"] == "method"
             and e["source"] == class_id and e["target"].endswith("_" + method.lower())]
    assert len(found) == 1, (cls, method, found)
    return found[0]


# -- the registry ------------------------------------------------------------------------


def test_the_registry_keeps_aliases_and_custom_service_wiring(tmp_path):
    registry = prepare_run(_services_site(tmp_path))
    assert registry.service_aliases == {
        "Drupal\\Core\\Entity\\EntityTypeManagerInterface": "entity_type.manager",
        "Drupal\\foo\\FooHelperInterface": "foo.helper",
        "foo.helper_alias": "foo.helper",
    }
    wiring = registry.service_wiring
    # Custom services only: core's `entity_type.manager` is not wired here.
    assert set(wiring) == {"foo.helper", "foo.worker", "foo.consumer", "foo.auto", "foo.child"}
    assert wiring["foo.consumer"] == {"arguments": ["foo.helper", ""], "autowire": False}
    assert wiring["foo.auto"] == {"arguments": [], "autowire": True}
    assert wiring["foo.child"]["arguments"] == ["foo.helper", "entity_type.manager"]
    again = Registry.from_json(json.loads(json.dumps(registry.to_json())))
    assert again.service_aliases == registry.service_aliases
    assert again.service_wiring == registry.service_wiring
    # An older registry file without the maps still loads.
    old = registry.to_json()
    del old["service_aliases"], old["service_wiring"]
    assert Registry.from_json(old).service_wiring == {}


def test_defaults_autowire_applies_to_every_service_of_the_file(tmp_path):
    root = _site(tmp_path, {
        f"{FOO}/foo.info.yml": "name: Foo\ntype: module\n",
        f"{FOO}/foo.services.yml": (
            "services:\n  _defaults:\n    autowire: true\n"
            "  foo.a:\n    class: Drupal\\foo\\A\n"
            "  foo.b:\n    class: Drupal\\foo\\B\n    autowire: false\n"),
    })
    wiring = prepare_run(root).service_wiring
    assert wiring["foo.a"]["autowire"] is True
    assert wiring["foo.b"]["autowire"] is False


# -- property resolution (§7.3) ------------------------------------------------------------


def test_the_rules_resolve_properties_in_order(tmp_path):
    from graphify.drupal.php_services import property_service

    registry = prepare_run(_services_site(tmp_path))
    helper = "Drupal\\foo\\FooHelperInterface"
    # Rule 1: `create()` passes the service at the promoted parameter's position.
    assert property_service(registry, "Drupal\\foo\\Form\\FooForm", "helper") == (
        "foo.helper", "Drupal\\foo\\FooHelper")
    # Rule 1b: setter injection in `create()`.
    assert property_service(registry, "Drupal\\foo\\Form\\FooSetterForm", "helper") == (
        "foo.helper", "")
    # Rule 2: `arguments:` of the class's service, at the assigned parameter's position.
    assert property_service(registry, "Drupal\\foo\\FooConsumer", "h") == ("foo.helper", helper)
    # Rule 3: autowiring by the parameter's type, an alias of a service.
    assert property_service(registry, "Drupal\\foo\\FooAuto", "helper") == ("foo.helper", helper)
    assert property_service(registry, "Drupal\\foo\\FooAuto", "etm") == (
        "entity_type.manager", "Drupal\\Core\\Entity\\EntityTypeManagerInterface")
    # Rule 4: the parent's constructor assigns what the child passes through.
    assert property_service(registry, "Drupal\\foo\\FooChild", "helper") == ("foo.helper", helper)
    # Unresolved: typed, but no rule names the service.
    assert property_service(registry, "Drupal\\foo\\FooLoose", "helper") == ("", helper)
    assert property_service(registry, "Drupal\\foo\\FooLoose", "when") == ("", "DateTimeImmutable")
    assert property_service(registry, "Drupal\\foo\\Nope", "x") == ("", "")


# -- the per-file extractor ------------------------------------------------------------------


def _uses(edges: list[dict]) -> set[tuple[str, str, str]]:
    return {(e["source"], e["target"], e["via"]) for e in edges if e["relation"] == "uses_service"}


def test_the_extractor_emits_uses_service_and_pending_calls(tmp_path):
    from graphify.drupal.php_semantics import PENDING, extract_php_semantics, find_php_candidates

    root = _services_site(tmp_path)
    registry = prepare_run(root)
    path = root / FOO / "src/Controller/FooController.php"
    core = _core_php(path)
    result = extract_php_semantics(path, core)

    def method(name: str) -> str:
        return _method(core, "FooController.php", "FooController", name)

    assert _uses(result["edges"]) == {
        (method("page"), service_id("foo.helper"), "service"),
        (method("page"), service_id("entity_type.manager"), "shortcut"),
        (method("twice"), service_id("foo.helper"), "service"),
        (method("twice"), service_id("foo.other"), "service"),
        # An alias is followed to the service it names.
        (method("aliased"), service_id("foo.helper"), "service"),
        (method("inherited"), service_id("foo.worker"), "service"),
        (method("dynamic"), service_id("foo.dynamic"), "service"),
    }
    shortcut = next(e for e in result["edges"] if e.get("via") == "shortcut")
    assert (shortcut["shortcut"], shortcut["target_name"]) == ("entityTypeManager",
                                                               "entity_type.manager")
    aliased = next(e for e in result["edges"] if e["source"] == method("aliased"))
    assert (aliased["target_name"], aliased["alias"]) == ("foo.helper", "foo.helper_alias")

    pending = {(e["source"], e["service"], e["method"]) for e in result["edges"]
               if e.get(PENDING) == "service_call"}
    assert pending == {
        (method("page"), "foo.helper", "run"),
        (method("page"), "entity_type.manager", "getStorage"),
        (method("aliased"), "foo.helper", "run"),
        (method("inherited"), "foo.worker", "run"),
        (method("dynamic"), "foo.dynamic", "run"),
    }
    assert all(e["relation"] == "calls" for e in result["edges"] if e.get(PENDING) == "service_call")
    # `$x` is assigned twice in `twice()`: its call is not resolved.
    assert not [e for e in result["edges"] if e["source"] == method("twice") and e.get(PENDING)]

    (candidate,) = result["php_candidates"]
    assert {k: candidate[k] for k in ("kind", "module", "via", "argument")} == {
        "kind": "non_literal_service", "module": "foo", "via": "service",
        "argument": "$this->id"}
    assert find_php_candidates(path, registry) == result["php_candidates"]


def test_create_gives_a_class_level_uses_service_and_property_calls(tmp_path):
    from graphify.drupal.php_semantics import PENDING, extract_php_semantics

    root = _services_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/Form/FooForm.php"
    core = _core_php(path)
    result = extract_php_semantics(path, core)
    cls = _php_node(core, "FooForm.php", "FooForm")
    assert _uses(result["edges"]) == {(cls, service_id("foo.helper"), "create")}
    (call,) = [e for e in result["edges"] if e.get(PENDING) == "property_call"]
    assert {k: call[k] for k in ("source", "relation", "class", "property", "method")} == {
        "source": _method(core, "FooForm.php", "FooForm", "submitForm"), "relation": "calls",
        "class": "Drupal\\foo\\Form\\FooForm", "property": "helper", "method": "run"}


def test_a_procedural_function_uses_a_service(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics, is_semantics_file

    root = _services_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "foo.module"
    assert is_semantics_file(path)
    core = _core_php(path)
    result = extract_php_semantics(path, core)
    function = _php_node(core, "foo.module", "foo_cron()")
    assert _uses(result["edges"]) == {(function, service_id("foo.helper"), "service")}


def test_unresolved_receivers_are_candidates_only_for_service_types(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics

    root = _services_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/FooLoose.php"
    result = extract_php_semantics(path, _core_php(path))
    assert [{k: c[k] for k in ("kind", "class", "property", "type", "method")}
            for c in result["php_candidates"]] == [{
        "kind": "unresolved_receiver", "class": "Drupal\\foo\\FooLoose", "property": "helper",
        "type": "Drupal\\foo\\FooHelperInterface", "method": "run"}]
    for resolved in ("FooConsumer.php", "FooAuto.php", "FooChild.php"):
        path = root / FOO / "src" / resolved
        assert extract_php_semantics(path, _core_php(path))["php_candidates"] == [], resolved


# -- the resolver ------------------------------------------------------------------------------


def test_calls_are_bound_to_the_service_class_s_method(tmp_path):
    root = _services_site(tmp_path)
    result = _extract(root)
    assert not [e for e in result["edges"] if "pending" in e]
    run = _method(result, "FooHelper.php", "FooHelper", "run")

    def m(file: str, cls: str, name: str) -> str:
        return _method(result, file, cls, name)

    service_calls = {(e["source"], e["target"]) for e in result["edges"]
                     if e["relation"] == "calls" and "service" in e}
    assert service_calls == {
        (m("FooController.php", "FooController", "page"), run),
        (m("FooController.php", "FooController", "aliased"), run),
        # `FooWorker` has no `run()`: its parent's method node, inside the graph.
        (m("FooController.php", "FooController", "inherited"), run),
        (m("FooForm.php", "FooForm", "submitForm"), run),                  # rule 1
        (m("FooSetterForm.php", "FooSetterForm", "submitForm"), run),      # rule 1b
        (m("FooConsumer.php", "FooConsumer", "go"), run),                  # rule 2
        (m("FooAuto.php", "FooAuto", "go"), run),                          # rule 3
        (m("FooChild.php", "FooChild", "go"), run),                        # rule 4
        (_php_node(result, "foo.module", "foo_cron()"), run),
    }
    nodes = {n["id"]: n for n in result["nodes"]}
    # Never a `calls` into the boundary: every service call lands on a custom method node.
    assert all(str(nodes[t].get("source_file", "")).endswith(".php") for _s, t in service_calls)

    uses = {(e["source"], e["target"]): e for e in result["edges"] if e["relation"] == "uses_service"}
    page = m("FooController.php", "FooController", "page")
    # The boundary class's methods go on the `uses_service` edge instead.
    assert uses[(page, service_id("entity_type.manager"))]["methods"] == ["getStorage"]
    assert "methods" not in uses[(page, service_id("foo.helper"))]
    # A service the static map has no class for: its method waits for the container.
    dynamic = uses[(m("FooController.php", "FooController", "dynamic"), service_id("foo.dynamic"))]
    assert dynamic["methods"] == ["run"]
    assert dynamic["_pending_calls"] == [[m("FooController.php", "FooController", "dynamic"), "run"]]
    # A service no `*.services.yml` in the graph declares is a boundary stub.
    stub = nodes[service_id("foo.dynamic")]
    assert (stub["type"], stub["boundary"]) == ("drupal_service", True)


def test_a_cli_run_writes_no_pending_key(tmp_path):
    root = _services_site(tmp_path / "site")
    out = tmp_path / "out"
    graph = _cli(root, out)
    links = _links(graph)
    assert not [e for e in links if "pending" in e]
    assert sum(1 for e in links if e["relation"] == "calls" and "service" in e) == 9
    assert {e["via"] for e in links if e["relation"] == "uses_service"} == {
        "service", "shortcut", "create"}
    inventory = json.loads((out / "graphify-out" / "drupal-inventory.json").read_text(encoding="utf-8"))
    assert sorted(c["kind"] for c in inventory["php_candidates"]) == [
        "non_literal_service", "unresolved_receiver"]
    # An unchanged rerun gives the same edges.
    again = _cli(root, out)
    assert sorted((e["source"], e["relation"], e["target"]) for e in _links(again)) == sorted(
        (e["source"], e["relation"], e["target"]) for e in links)


# -- the P3 overlay ----------------------------------------------------------------------------


def _artifact(root: Path):
    from graphify.drupal.container import Artifact

    data = {
        "schema_version": 1,
        "services": [
            {"id": "foo.helper", "class": "Drupal\\foo\\FooHelper", "provider": "foo",
             "file": f"{FOO}/src/FooHelper.php", "arguments": [], "tags": []},
            # Only the container knows this service's class.
            {"id": "foo.dynamic", "class": "Drupal\\foo\\FooHelper", "provider": "foo",
             "file": f"{FOO}/src/FooHelper.php", "arguments": [], "tags": []},
        ],
        "aliases": {},
        "routes": [],
        "extensions": [{"name": "foo", "type": "module", "path": FOO, "status": 1, "weight": 0,
                        "dependencies": []}],
        "hooks": {}, "plugins": {}, "subscribers": {}, "stamp": {}, "errors": [],
    }
    return Artifact(data=data, path=root / "drupal-container.json")


def _dump(G) -> str:
    import networkx as nx

    data = nx.node_link_data(G, edges="links")
    data["nodes"] = sorted(data["nodes"], key=lambda n: n["id"])
    data["links"] = sorted(data["links"], key=lambda e: (e["source"], e["target"]))
    return json.dumps(data, sort_keys=True, default=str)


def test_the_overlay_binds_calls_on_a_container_only_class(tmp_path):
    from graphify.drupal.container_overlay import ORIGIN, apply, undo

    root = _services_site(tmp_path)
    result = _extract(root)
    G = build_from_json(result)
    static = _dump(G)
    dynamic = _method(result, "FooController.php", "FooController", "dynamic")
    run = _method(result, "FooHelper.php", "FooHelper", "run")
    assert not G.has_edge(dynamic, run)

    overlay = apply(G, _artifact(root), root)
    assert overlay.status == "fresh", overlay.reasons
    data = G.edges[dynamic, run]
    assert (data["relation"], data["origin"], data["service"]) == ("calls", ORIGIN, "foo.dynamic")
    # A statically bound call is left alone.
    page = _method(result, "FooController.php", "FooController", "page")
    assert "origin" not in G.edges[page, run] and "confirmed_by" not in G.edges[page, run]
    assert overlay.counts["service_calls"]["applied"] == 1

    once = _dump(G)
    apply(G, _artifact(root), root)
    assert _dump(G) == once
    undo(G)
    assert _dump(G) == static


# -- the reference corpus ------------------------------------------------------------------------

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))


@pytest.mark.skipif(not (CORPUS / "web").is_dir(), reason="reference corpus not present")
def test_corpus_service_use_and_calls(tmp_path):
    from collections import Counter

    from graphify.drupal import resolvers
    from graphify.drupal.hooks import is_procedural_file
    from graphify.drupal.php_semantics import extract_php_semantics, is_semantics_file

    registry = prepare_run(CORPUS, cache_root=tmp_path)
    files = set()
    for ext in ("modules/custom", "themes/custom", "profiles"):
        base = CORPUS / "web" / ext
        files |= {p for p in base.rglob("*") if p.is_file() and "/tests/" not in p.as_posix()
                  and (p.suffix == ".php" or is_procedural_file(p))}
    nodes: list[dict] = []
    edges: list[dict] = []
    kinds: Counter = Counter()
    for path in sorted(files):
        if not is_semantics_file(path):
            continue
        core = _core_php(path)
        semantics = extract_php_semantics(path, core)
        nodes += core["nodes"] + semantics["nodes"]
        edges += core["edges"] + semantics["edges"]
        kinds.update(c["kind"] for c in semantics["php_candidates"])
    via = Counter(e["via"] for e in edges if e["relation"] == "uses_service")
    resolvers.bind_service_calls(nodes, edges)
    calls = sum(1 for e in edges if e["relation"] == "calls" and "service" in e
                and "pending" not in e)
    assert dict(via) == CORPUS_USES_BY_VIA
    assert calls == CORPUS_CALLS
    assert calls > 0
    assert {k: kinds[k] for k in ("non_literal_service", "unresolved_receiver")} == CORPUS_CANDIDATES


#: Measured on FormsRemote (see the Task 4 report): `uses_service` edges,
#: one per (caller, service) pair -- 37 `\Drupal::service()`, 109 shortcut
#: and 194 `create()` sites; `calls` 145 through properties, 15 direct.
CORPUS_USES_BY_VIA = {"service": 35, "shortcut": 96, "create": 193}
CORPUS_CALLS = 160
CORPUS_CANDIDATES = {"non_literal_service": 3, "unresolved_receiver": 71}
