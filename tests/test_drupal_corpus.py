"""P1's acceptance criteria, measured on a real Drupal tree.

Skipped when the corpus is absent, so CI stays hermetic. The numbers are the
ones in the P1 spec; a change in them is either a regression or a corpus that
moved, and both deserve a look.

Measured through `graphify.extract.extract`, not by calling each family's
extractor: collapsing duplicate ids and materialising undeclared endpoints both
happen in the pipeline, and a per-file loop would test neither.
"""
from __future__ import annotations

import collections
import os
from pathlib import Path

import pytest

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))

pytestmark = pytest.mark.skipif(
    not (CORPUS / "web" / "core").is_dir(),
    reason="reference Drupal corpus not present",
)


def _family_files() -> list[Path]:
    import graphify  # noqa: F401  (installs the seam)
    from graphify.drupal.families import is_drupal_yaml

    return sorted(
        p for p in CORPUS.glob("web/**/*.yml")
        if "node_modules" not in p.parts and is_drupal_yaml(p)
    )


def _p1b_files() -> list[Path]:
    import graphify  # noqa: F401
    from graphify.drupal.families import is_drupal_file

    candidates = (list(CORPUS.glob("config/**/*.yml")) + list(CORPUS.glob("web/**/*.yml"))
                  + list(CORPUS.glob("web/sites/*/settings*.php")))
    return sorted(p for p in candidates if "node_modules" not in p.parts
                  and (p.suffix == ".php" or is_drupal_file(p)))


def _is_drupal(node: dict) -> bool:
    return str(node.get("type", "")).startswith("drupal")


@pytest.fixture(scope="module")
def family_files() -> list[Path]:
    return _family_files()


@pytest.fixture(scope="module")
def corpus_extraction(family_files, tmp_path_factory):
    from graphify.extract import extract

    # A fresh cache: an entry written by an older extractor must not stand in
    # for what this one produces, and the corpus must not gain a graphify-out/.
    cache = tmp_path_factory.mktemp("corpus-cache")
    return extract(family_files, cache_root=cache, root=CORPUS)


def test_criterion_1_only_the_malformed_core_fixture_fails(family_files):
    from graphify.drupal.families import family_extractor

    errors = [p.name for p in family_files if family_extractor(p)(p).get("error")]
    assert errors == ["invalid_file.libraries.yml"]


def test_criterion_2_the_tolerant_loader_finds_every_service(corpus_extraction):
    services = [n for n in corpus_extraction["nodes"] if n["type"] == "drupal_service"]
    declared = [n for n in services if not n.get("external")]
    # 2,205 declarations; 14 are overrides of a service declared elsewhere.
    assert len(declared) == 2191, "1628 means safe_load crept back in"


def test_criterion_3_no_family_file_is_dropped_as_a_secret(family_files):
    from graphify.detect import _is_sensitive

    assert [p for p in family_files if _is_sensitive(p)] == []


def test_criterion_3b_real_secret_stores_are_still_caught():
    import graphify  # noqa: F401
    from graphify.detect import _is_sensitive

    for name in ("token.yml", "token.json", "credentials.yaml", "secrets.yml"):
        assert _is_sensitive(CORPUS / name), name


def test_criterion_4_only_info_yml_declares_extensions(corpus_extraction):
    declared = [
        n for n in corpus_extraction["nodes"]
        if n["type"] in ("drupal_module", "drupal_theme", "drupal_profile")
    ]
    # 1,140 files; three pairs are core's name-collision fixtures, one node each.
    assert len(declared) == 1137
    assert all(n["source_file"].endswith(".info.yml") for n in declared)
    # Any other extension node is one the resolver made for a name nothing declares.
    others = [n for n in corpus_extraction["nodes"]
              if n["id"].startswith("drupal_extension_") and n not in declared]
    assert all(n.get("external") for n in others)


def test_criterion_4b_no_drupal_id_is_salted(corpus_extraction):
    salted = [n["id"] for n in corpus_extraction["nodes"]
              if _is_drupal(n) and not n["id"].startswith("drupal_")]
    assert salted == []


def test_criterion_5_nothing_dangles_after_the_resolver(corpus_extraction):
    ids = {n["id"] for n in corpus_extraction["nodes"]}
    dangling = collections.Counter(
        e["relation"] for e in corpus_extraction["edges"]
        if e["source"] not in ids or e["target"] not in ids
    )
    assert dangling == {}


def test_criterion_6_every_entity_is_owned_once_per_declaring_file(corpus_extraction):
    declares = collections.Counter(
        e["target"] for e in corpus_extraction["edges"] if e["relation"].startswith("declares_")
    )
    declared_in = {n["id"]: len(n.get("declared_in") or [n]) for n in corpus_extraction["nodes"]}
    # More than one owner only where that many files declare the entity (Task 5b).
    mismatched = {t: n for t, n in declares.items() if n != declared_in.get(t)}
    assert mismatched == {}


def test_criterion_7_every_node_is_filterable(corpus_extraction):
    for node in corpus_extraction["nodes"]:
        if not _is_drupal(node):
            continue
        assert node.get("realm") in ("core", "contrib", "custom", "unknown"), node["id"]
        assert node.get("layer"), node["id"]
    # External nodes are named, not located, so `unknown` is their honest realm.
    unknown = [n["id"] for n in corpus_extraction["nodes"]
               if _is_drupal(n) and n["realm"] == "unknown" and not n.get("external")]
    assert unknown == []


def test_criterion_7b_the_custom_slice_renders_without_aggregation(corpus_extraction):
    custom = [n for n in corpus_extraction["nodes"] if n.get("realm") == "custom"]
    assert len(custom) < 5000, "the custom slice must stay visually readable"


@pytest.fixture(scope="module")
def site_extraction(tmp_path_factory):
    from graphify.extract import extract

    return extract(_p1b_files(), cache_root=tmp_path_factory.mktemp("site-cache"), root=CORPUS)


def _config_nodes(extraction) -> list[dict]:
    return [n for n in extraction["nodes"] if n.get("config_name")]


def test_p1b_criterion_2_every_synced_file_is_one_active_config(site_extraction):
    active = [n for n in _config_nodes(site_extraction) if n.get("active")]
    assert len(active) == 613


def test_p1b_criterion_3_no_config_file_is_dropped_as_a_secret():
    from graphify.detect import _is_sensitive

    assert [p for p in _p1b_files() if p.suffix == ".yml" and _is_sensitive(p)] == []


def test_p1b_criterion_4_splits_and_their_overrides(site_extraction):
    from graphify.drupal.yaml_common import config_id

    splits = {n["config_name"]: n for n in site_extraction["nodes"]
              if n.get("type") == "drupal_config_split"}
    assert {s: n["folder"] for s, n in splits.items()} == {
        f"config_split.config_split.{e}": f"../config/splits/{e}" for e in ("dev", "test", "prod")}
    overrides = [e for e in site_extraction["edges"] if e["relation"] == "overrides_config"]
    patches = [e for e in overrides if e.get("override_source") == "split"]
    assert len(patches) == 8
    settings = {e["target"] for e in overrides if e.get("override_source") == "settings_php"
                and "status" in e.get("keys", [])}
    assert settings == {config_id(f"config_split.config_split.{e}") for e in ("dev", "test", "prod")}
    assert all(e["confidence"] == "AMBIGUOUS" for e in overrides
               if e.get("override_source") == "settings_php")


def test_p1b_criterion_5_core_extension_installs_what_it_lists(site_extraction):
    from graphify.drupal.yaml_common import config_id

    ids = {n["id"] for n in site_extraction["nodes"]}
    installs = [e for e in site_extraction["edges"] if e["relation"] == "installs_extension"
                and e["source"] == config_id("core.extension")]
    # 207 modules (the profile `standard` among them) and 8 themes.
    assert len(installs) == 215
    assert len({e["target"] for e in installs}) == 215
    assert [e["target"] for e in installs if e["target"] not in ids] == []
    # The fact lives in the edges: nodes of unchanged files cannot be rewritten
    # by an incremental run, so no node carries `installed` (spec §5.1, deviation 4).
    assert [n["id"] for n in site_extraction["nodes"] if "installed" in n] == []
    extensions = [n for n in site_extraction["nodes"]
                  if n.get("type") in ("drupal_module", "drupal_theme", "drupal_profile",
                                       "drupal_extension")]
    assert all(n.get("external") for n in extensions if n.get("missing"))


def test_p1b_criterion_6_integrity(site_extraction):
    from graphify.drupal.yaml_common import config_id

    ids = {n["id"] for n in site_extraction["nodes"]}
    assert [e["relation"] for e in site_extraction["edges"]
            if e["source"] not in ids or e["target"] not in ids] == []
    assert [n["id"] for n in site_extraction["nodes"] if _is_drupal(n)
            and not n["id"].startswith("drupal_")] == []
    # menu_test is a test module: its config is excluded, and it is not local actions.
    # (The module's own menu_test.links.action.yml beside its info file is a real
    # local-actions file and rightly yields drupal_local_action nodes.)
    config_copy = "menu_test/config/install/menu_test.links.action.yml"
    assert not [p for p in _p1b_files() if str(p).endswith(config_copy)]
    assert not [n for n in site_extraction["nodes"]
                if any(str(f).endswith(config_copy)
                       for f in [n.get("source_file", ""), *(n.get("declared_in") or [])])]
    assert config_id("menu_test.links.action") not in ids


def _patch_files() -> list[Path]:
    return sorted(CORPUS.glob("config/splits/*/config_split.patch.*.yml"))


def _config_names() -> set[str]:
    return {p.name[:-4] for p in CORPUS.glob("config/sync/*.yml")}


def _walk(value, found_values: set[str], found_keys: set[str]) -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            found_keys.add(str(key))
            _walk(inner, found_values, found_keys)
    elif isinstance(value, list):
        for inner in value:
            _walk(inner, found_values, found_keys)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        found_values.add(str(value))


def _secret_values() -> set[str]:
    """Every scalar string of 8+ characters in the split patches and smtp.settings,
    minus anything that is also a config name (ids legitimately contain those)."""
    from graphify.drupal.yaml_common import load_drupal_yaml

    files = [*CORPUS.glob("config/splits/*/*.yml"), CORPUS / "config/sync/smtp.settings.yml"]
    values: set[str] = set()
    for path in files:
        if path.is_file():
            _walk(load_drupal_yaml(path)[0], values, set())
    names = _config_names()
    return {v for v in values if len(v) >= 8 and not any(v in n for n in names)}


def _patch_values() -> set[str]:
    """Every int/str value under a split patch's `adding:`/`removing:`, of any
    length, minus values that are also config names or keys."""
    from graphify.drupal.yaml_common import load_drupal_yaml

    values: set[str] = set()
    keys: set[str] = set()
    for path in _patch_files():
        data = load_drupal_yaml(path)[0] or {}
        for block in ("adding", "removing"):
            _walk(data.get(block), values, keys)
    return values - keys - _config_names()


#: Numeric metadata P1 records on purpose (menu/extension weights, library versions).
_STRUCTURAL = frozenset({"weight", "version", "confidence_score"})


def _attribute_values(extraction) -> set[str]:
    found: set[str] = set()
    for item in [*extraction["nodes"], *extraction["edges"]]:
        for key, value in item.items():
            if key in _STRUCTURAL:
                continue
            for inner in value if isinstance(value, list) else [value]:
                if isinstance(inner, (str, int)) and not isinstance(inner, bool):
                    found.add(str(inner))
    return found


def test_p1b_criterion_7_no_configuration_value_reaches_the_graph(site_extraction):
    import json

    dumped = json.dumps(site_extraction, default=str)
    values = _secret_values()
    assert values, "the scan needs something to look for"
    assert sorted(v for v in values if v in dumped) == []
    # Short settings (timeouts, counters) cannot be searched as substrings; no
    # attribute of any node or edge may equal one.
    patch_values = _patch_values()
    assert {"0", "60", "86400", "999999999"} <= patch_values
    assert sorted(patch_values & _attribute_values(site_extraction)) == []


def test_p1b_criterion_8_recipes(site_extraction):
    recipes = [n for n in site_extraction["nodes"] if n.get("type") == "drupal_recipe"
               and not n.get("external")]
    assert len(recipes) == 46
    for e in site_extraction["edges"]:
        if e["relation"] == "config_action" and e.get("confidence") != "AMBIGUOUS":
            assert "${" not in repr(e), e


def test_p1b_criterion_10_volume(site_extraction):
    drupal = [n for n in site_extraction["nodes"] if _is_drupal(n)]
    assert 9_500 < len(drupal) < 11_500, len(drupal)
    assert len([n for n in drupal if n.get("realm") == "custom"]) < 5000


def test_p1b_every_wildcard_schema_type_is_its_own_node(site_extraction):
    """1,806 top-level keys in 303 non-test schema files; `condition.plugin.
    entity_bundle:*` is declared twice, so 1,805 types, 150 of them wildcards.

    A wildcard never shares an id with a literal type. Two literal pairs still
    differ only by `.` against `_` and share an id; they are pinned by name so a
    new collision fails here.
    """
    import collections

    import graphify  # noqa: F401
    from graphify.drupal.yaml_common import load_drupal_yaml, schema_id

    types = {str(k) for p in _p1b_files() if p.name.endswith(".schema.yml")
             for k in (load_drupal_yaml(p)[0] or {})}
    assert len(types) == 1805
    wildcards = {t for t in types if "*" in t}
    assert len(wildcards) == 150

    by_id = collections.defaultdict(set)
    for type_ in types:
        by_id[schema_id(type_)].add(type_)
    assert {i: s for i, s in by_id.items() if len(s) > 1} == {
        schema_id("views.field.user"): {"views.field.user", "views_field_user"},
        schema_id("views.field.bulk_form"): {"views.field.bulk_form", "views_field_bulk_form"},
    }

    schemas = {n["id"]: n for n in site_extraction["nodes"]
               if n.get("type") == "drupal_config_schema"}
    assert len(schemas) == 1803
    assert set(schemas) == set(by_id)
    for type_ in wildcards:
        assert (schemas[schema_id(type_)]["schema_type"], schemas[schema_id(type_)]["pattern"]) \
            == (type_, True)
    assert len([n for n in schemas.values() if n.get("pattern")]) == 150
