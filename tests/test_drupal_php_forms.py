"""Forms (P4 Task 3, spec §6.1): `drupal_form` nodes from literal
`getFormId()` returns, entity forms from an entity type's `form.<op>`
handlers, routes' `_form` bound to the form node in the resolver, and the
P3 overlay confirming that edge rather than reporting a conflict."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graphify.build import build_from_json
from graphify.drupal import boundary
from graphify.drupal.discovery import prepare_run
from graphify.drupal.yaml_common import route_id
from graphify.ids import make_id
from tests.test_drupal_discovery import _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401
from tests.test_drupal_php_plugins import _cli, _core_php, _extract, _links, _rel

FOO = "web/modules/custom/foo"


def _form_class(name: str, form_id: str, base: str | None = None) -> str:
    base_method = (f"\n  public function getBaseFormId() {{\n    return '{base}';\n  }}\n"
                   if base else "")
    return (f"<?php\nnamespace Drupal\\foo\\Form;\n\nuse Drupal\\Core\\Form\\FormBase;\n\n"
            f"class {name} extends FormBase {{\n\n  public function getFormId() {{\n"
            f"    return {form_id};\n  }}\n{base_method}\n"
            f"  public function buildForm(array $form, $form_state) {{\n    return $form;\n  }}\n}}\n")


FOO_ENTITY = r"""<?php
namespace Drupal\foo\Entity;

use Drupal\Core\Entity\Attribute\ContentEntityType;
use Drupal\foo\Form\FooEntityForm;

#[ContentEntityType(
  id: "foo",
  handlers: [
    "form" => [
      "default" => FooEntityForm::class,
      "edit" => FooEntityForm::class,
      "delete" => "Drupal\Core\Entity\ContentEntityDeleteForm",
    ],
  ],
)]
class Foo extends ContentEntityBase {
}
"""

FOO_ENTITY_FORM = r"""<?php
namespace Drupal\foo\Form;

class FooEntityForm extends ContentEntityForm {
}
"""

ROUTING = """\
foo.plain:
  path: '/foo/plain'
  defaults:
    _form: '\\Drupal\\foo\\Form\\PlainForm'
foo.based:
  path: '/foo/based'
  defaults:
    _form: 'Drupal\\foo\\Form\\BasedForm'
foo.computed:
  path: '/foo/computed'
  defaults:
    _form: '\\Drupal\\foo\\Form\\ComputedForm'
foo.missing:
  path: '/foo/missing'
  defaults:
    _form: '\\Drupal\\foo\\Form\\Missing'
"""


def _forms_site(root: Path, extra: dict[str, str] | None = None) -> Path:
    return _site(root, {
        f"{FOO}/foo.info.yml": "name: Foo\ntype: module\n",
        f"{FOO}/foo.routing.yml": ROUTING,
        f"{FOO}/src/Form/PlainForm.php": _form_class("PlainForm", "'foo_plain'"),
        f"{FOO}/src/Form/BasedForm.php": _form_class("BasedForm", "'foo_based'", "foo_base"),
        f"{FOO}/src/Form/ComputedForm.php": _form_class("ComputedForm", "'foo_' . $this->x"),
        f"{FOO}/src/Form/FooEntityForm.php": FOO_ENTITY_FORM,
        f"{FOO}/src/Entity/Foo.php": FOO_ENTITY,
        **(extra or {}),
    })


def form_id(name: str) -> str:
    return make_id("drupal", "form", name)


def entity_form_id(entity_type: str, op: str) -> str:
    return make_id("drupal", "form", "entity", entity_type, op)


@pytest.fixture(autouse=True)
def _clean(_isolated_discovery_state, monkeypatch):  # noqa: F811
    from graphify.drupal.container import ENV_ARTIFACT

    monkeypatch.delenv(ENV_ARTIFACT, raising=False)
    boundary.clear_caches()
    yield
    boundary.clear_caches()


def _class_node(result: dict, label: str) -> str:
    found = [n["id"] for n in result["nodes"]
             if n.get("label") == label and not str(n.get("type", "")).startswith("drupal_")
             and str(n.get("source_file", "")).endswith(".php")]
    assert len(found) == 1, (label, found)
    return found[0]


# -- the per-file extractor ------------------------------------------------------------


def test_a_literal_form_id_gives_a_form_node(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics

    root = _forms_site(tmp_path)
    prepare_run(root)
    plain_path = root / FOO / "src/Form/PlainForm.php"
    plain_core = _core_php(plain_path)
    plain = extract_php_semantics(plain_path, plain_core)

    (node,) = plain["nodes"]
    assert {k: node[k] for k in ("id", "type", "layer", "label", "form_id", "class_name")} == {
        "id": form_id("foo_plain"), "type": "drupal_form", "layer": "hook", "label": "foo_plain",
        "form_id": "foo_plain", "class_name": "Drupal\\foo\\Form\\PlainForm"}
    assert "base_form_id" not in node and "entity_form" not in node
    assert _rel(plain["edges"]) == {
        (form_id("foo_plain"), "form_implemented_by", _class_node(plain_core, "PlainForm"))}
    assert plain["php_candidates"] == []


def test_a_base_form_id_is_carried(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics

    root = _forms_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/Form/BasedForm.php"
    (node,) = extract_php_semantics(path, _core_php(path))["nodes"]
    assert (node["id"], node["form_id"], node["base_form_id"]) == (
        form_id("foo_based"), "foo_based", "foo_base")


def test_a_computed_form_id_is_neither_a_node_nor_a_candidate(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics, find_php_candidates

    root = _forms_site(tmp_path)
    registry = prepare_run(root)
    path = root / FOO / "src/Form/ComputedForm.php"
    assert extract_php_semantics(path, _core_php(path)) == {
        "nodes": [], "edges": [], "php_candidates": []}
    assert find_php_candidates(path, registry) == []


def test_an_entity_type_s_form_handlers_are_entity_forms(tmp_path):
    from graphify.drupal.php_semantics import PENDING, extract_php_semantics

    root = _forms_site(tmp_path)
    prepare_run(root)
    path = root / FOO / "src/Entity/Foo.php"
    result = extract_php_semantics(path, _core_php(path))
    forms = {n["id"]: n for n in result["nodes"] if n["type"] == "drupal_form"}
    assert set(forms) == {entity_form_id("foo", op) for op in ("default", "edit", "delete")}
    edit = forms[entity_form_id("foo", "edit")]
    assert {k: edit[k] for k in ("layer", "entity_form", "pattern", "entity_type", "operation",
                                 "class_name")} == {
        "layer": "hook", "entity_form": True, "pattern": "foo_*_edit_form", "entity_type": "foo",
        "operation": "edit", "class_name": "Drupal\\foo\\Form\\FooEntityForm"}
    # Drupal's EntityForm::getFormId() leaves the `default` operation out.
    assert forms[entity_form_id("foo", "default")]["pattern"] == "foo_*_form"
    assert "form_id" not in edit
    implemented = {e["source"]: e for e in result["edges"] if e["relation"] == "form_implemented_by"}
    assert set(implemented) == set(forms)
    assert all(e[PENDING] for e in implemented.values())
    assert implemented[entity_form_id("foo", "edit")]["target_name"] == \
        "Drupal\\foo\\Form\\FooEntityForm"
    # The entity type's own handler edges are unchanged (Task 2).
    assert sorted(e["handler"] for e in result["edges"] if e["relation"] == "entity_handler") == [
        "form.default,form.edit", "form.delete"]


# -- the resolver: routes and entity forms bound ------------------------------------------


def test_routes_target_the_form_node_else_the_class(tmp_path):
    root = _forms_site(tmp_path)
    result = _extract(root)
    assert not [e for e in result["edges"] if "pending" in e]
    to_form = {e["source"]: e for e in result["edges"] if e["relation"] == "routes_to_form"}
    assert {k: e["target"] for k, e in to_form.items()} == {
        route_id("foo.plain"): form_id("foo_plain"),
        route_id("foo.based"): form_id("foo_based"),
        # No form node for a computed id: the class node is the target.
        route_id("foo.computed"): _class_node(result, "ComputedForm"),
    }
    edge = to_form[route_id("foo.plain")]
    assert edge["source_file"].endswith("foo.routing.yml")
    assert edge["target_name"] == "Drupal\\foo\\Form\\PlainForm"
    # Nothing to bind: the route keeps only its P1 `form` attribute.
    nodes = {n["id"]: n for n in result["nodes"]}
    assert nodes[route_id("foo.missing")]["form"] == "\\Drupal\\foo\\Form\\Missing"
    assert route_id("foo.missing") not in to_form


def test_entity_forms_bind_to_the_handler_class_or_are_dropped(tmp_path):
    root = _forms_site(tmp_path)
    result = _extract(root)
    implemented = {e["source"]: e["target"] for e in result["edges"]
                   if e["relation"] == "form_implemented_by"}
    entity_class = _class_node(result, "FooEntityForm")
    assert implemented[entity_form_id("foo", "default")] == entity_class
    assert implemented[entity_form_id("foo", "edit")] == entity_class
    # The core delete form is outside the graph: its node stays, its edge is dropped.
    assert entity_form_id("foo", "delete") not in implemented
    assert entity_form_id("foo", "delete") in {n["id"] for n in result["nodes"]}
    assert implemented[form_id("foo_plain")] == _class_node(result, "PlainForm")


def test_an_incremental_run_binds_a_changed_route_to_an_unchanged_form(tmp_path):
    root = _forms_site(tmp_path / "site")
    out = tmp_path / "out"
    first = _cli(root, out)
    routing = root / FOO / "foo.routing.yml"
    routing.write_text(routing.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
    graph = _cli(root, out)
    to_form = {e["source"]: e["target"] for e in _links(graph) if e["relation"] == "routes_to_form"}
    assert to_form[route_id("foo.plain")] == form_id("foo_plain")
    assert to_form[route_id("foo.based")] == form_id("foo_based")
    assert _rel(_links(graph)) == _rel(_links(first))
    assert not [e for e in _links(graph) if "pending" in e]


# -- the P3 overlay --------------------------------------------------------------------


def _artifact(root: Path):
    from graphify.drupal.container import Artifact

    def route(name, form):
        return {"name": name, "path": f"/{name}", "defaults": {"_form": form},
                "requirements": {}, "provider": "foo"}

    data = {
        "schema_version": 1, "services": [], "aliases": {},
        "routes": [route("foo.plain", "\\Drupal\\foo\\Form\\PlainForm"),
                   route("foo.based", "\\Drupal\\foo\\Form\\BasedForm"),
                   route("foo.computed", "\\Drupal\\foo\\Form\\ComputedForm"),
                   # Container-only: no static route names it.
                   route("foo.extra", "\\Drupal\\foo\\Form\\PlainForm")],
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


def test_the_overlay_confirms_the_form_node_without_a_conflict(tmp_path):
    from graphify.drupal.container_overlay import ORIGIN, apply, undo

    root = _forms_site(tmp_path)
    result = _extract(root)
    G = build_from_json(result)
    static = _dump(G)
    computed_class = _class_node(result, "ComputedForm")

    overlay = apply(G, _artifact(root), root)
    assert overlay.status == "fresh", overlay.reasons
    assert [c for c in overlay.conflicts if c["relation"] == "routes_to_form"] == []
    assert overlay.edges["conflict"] == 0
    for route, target in ((route_id("foo.plain"), form_id("foo_plain")),
                          (route_id("foo.based"), form_id("foo_based")),
                          (route_id("foo.computed"), computed_class)):
        data = G.edges[route, target]
        assert data["relation"] == "routes_to_form"
        assert data["confirmed_by"] == ORIGIN and "origin" not in data
    # A container-only route goes to the form node too, never to the class.
    extra = G.edges[route_id("foo.extra"), form_id("foo_plain")]
    assert extra["relation"] == "routes_to_form" and extra["origin"] == ORIGIN
    assert not G.has_edge(route_id("foo.extra"), _class_node(result, "PlainForm"))

    once = _dump(G)
    apply(G, _artifact(root), root)
    assert _dump(G) == once
    undo(G)
    assert _dump(G) == static


# -- the reference corpus ------------------------------------------------------------------

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))


@pytest.mark.skipif(not (CORPUS / "web").is_dir(), reason="reference corpus not present")
def test_corpus_forms(tmp_path):
    from graphify.drupal.php_semantics import extract_php_semantics, is_semantics_file

    registry = prepare_run(CORPUS, cache_root=tmp_path)
    files = sorted({Path(f["file"]) for f in registry.class_facts.values()})
    forms: set[str] = set()
    entity_forms: set[str] = set()
    for path in files:
        if not is_semantics_file(path):
            continue
        for n in extract_php_semantics(path, _core_php(path))["nodes"]:
            if n["type"] != "drupal_form":
                continue
            (entity_forms if n.get("entity_form") else forms).add(n["id"])
    literal = {f["form_id"] for f in registry.class_facts.values() if f.get("form_id")}
    assert forms == {form_id(x) for x in literal}
    assert len(forms) == FORMS_IN_CORPUS
    assert len(entity_forms) == ENTITY_FORMS_IN_CORPUS


#: Measured on FormsRemote (see the Task 3 report).
FORMS_IN_CORPUS = 34
ENTITY_FORMS_IN_CORPUS = 28
