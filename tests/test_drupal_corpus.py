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
import contextlib
import os
import re
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
    # 2,192 since FormsRemote 60b010db (webform 6.3.0-rc1 -> 6.3.1, whose
    # security fix adds `webform_ui.route_subscriber`).
    assert len(declared) == 2192, "1628 means safe_load crept back in"


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
    # 1,138 since FormsRemote 60b010db: webform 6.3.1 ships the test module
    # `webform_scheduled_email_exception_test`.
    assert len(declared) == 1138
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
    # A templated name is never an edge target, whatever the relation.
    assert [e for e in site_extraction["edges"] if "${" in str(e.get("target_name", ""))] == []
    # Three test-fixture recipes share a directory name with a core recipe (spec §9).
    doubled = [n for n in recipes if len(n.get("declared_in") or []) == 2]
    assert len(doubled) == 3, [n["id"] for n in doubled]


def test_p1b_criterion_10_volume(site_extraction):
    drupal = [n for n in site_extraction["nodes"] if _is_drupal(n)]
    assert 9_500 < len(drupal) < 11_500, len(drupal)
    assert len([n for n in drupal if n.get("realm") == "custom"]) < 5000


def test_p1b_every_wildcard_schema_type_is_its_own_node(site_extraction):
    """1,806 top-level keys in 303 non-test schema files; `condition.plugin.
    entity_bundle:*` is declared twice, so 1,805 types, 150 of them wildcards.

    Every type is its own node (Task 11): a wildcard never shares an id with a
    literal type, and two literal pairs that differ only by `.` against `_`
    (`views.field.user`/`views_field_user`, `views.field.bulk_form`/
    `views_field_bulk_form`) no longer collide either.
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
    # Task 11: no two distinct schema types may share a node id (0 collisions).
    assert {i: s for i, s in by_id.items() if len(s) > 1} == {}

    schemas = {n["id"]: n for n in site_extraction["nodes"]
               if n.get("type") == "drupal_config_schema"}
    assert len(schemas) == 1805
    assert set(schemas) == set(by_id)
    for type_ in wildcards:
        assert (schemas[schema_id(type_)]["schema_type"], schemas[schema_id(type_)]["pattern"]) \
            == (type_, True)
    assert len([n for n in schemas.values() if n.get("pattern")]) == 150


def test_p1b_every_config_store_file_yields_at_least_one_node(site_extraction):
    """Task 11: a split patch or a language override used to return zero nodes,
    which core's zero-node heal treats as un-extracted and re-queues forever.

    Every file `config_store()` recognises (sync/split/recipe/install/optional/
    schema, and now patch and language-override files too) must contribute at
    least one node, unless its own YAML failed to parse.
    """
    import graphify  # noqa: F401
    from graphify.drupal.config_stores import config_store
    from graphify.drupal.yaml_common import load_drupal_yaml

    store_files = [p for p in _p1b_files() if p.suffix == ".yml" and config_store(p) is not None]
    declaring: set[str] = set()
    for n in site_extraction["nodes"]:
        if n.get("source_file"):
            declaring.add(n["source_file"])
        declaring.update(n.get("declared_in") or [])

    zero_node = []
    for path in store_files:
        _, error = load_drupal_yaml(path)
        if error:
            continue
        if path.relative_to(CORPUS).as_posix() not in declaring:
            zero_node.append(path)
    assert zero_node == []


# -- P2a: plugin discovery (spec 2026-09-23-drupal-p2a-plugin-discovery §6) ----
#
# The registry is built the way a default `detect()` builds it (`prepare_run`), into a
# temporary out dir so the corpus gains no graphify-out/. Everything
# `prepare_run` sets for the process (the in-memory registry, the env var a
# worker reads, the env-file cache) is restored before the fixture returns, so
# no other test -- in this module or any other -- sees this run's registry.
# A test that needs the registry live puts it back with `_live(registry)`.


#: Every key `yaml_common.node` writes, plus what the pipeline adds to a node:
#: `declared_in` (merge.py's collapse of an id several files declare).
_UNIVERSAL_NODE_KEYS = frozenset({
    "id", "label", "file_type", "type", "layer", "realm", "_origin",
    "source_file", "source_location", "declared_in",
})
_PLUGIN_KEYS = frozenset({"plugin_id", "plugin_type", "provider", "class_name", "deriver"})
_P1_PLUGIN_NODE_TYPES = ("drupal_menu_link", "drupal_local_task", "drupal_local_action",
                         "drupal_contextual_link", "drupal_breakpoint")
_MANAGER_ROOTS = frozenset({"Drupal\\Core\\Plugin\\DefaultPluginManager",
                            "Drupal\\Component\\Plugin\\PluginManagerInterface"})
_SCAN_PRUNED = frozenset({"vendor", "node_modules", "tests", "Tests"})


@contextlib.contextmanager
def _restored_discovery_state():
    """Undo what `prepare_run` sets directly (see test_drupal_discovery_seam's
    `_isolated_discovery_state`, whose teardown this mirrors)."""
    from graphify.drupal import discovery
    from graphify.drupal.inventory import set_current_inventory

    had = discovery.ENV_VAR in os.environ
    saved = os.environ.get(discovery.ENV_VAR)
    try:
        yield
    finally:
        discovery.set_current(None, None)
        set_current_inventory(None)
        discovery._env_cache_path = None
        discovery._env_cache_mtime = None
        discovery._env_cache_registry = None
        discovery._env_cache_force_miss = frozenset()
        if had:
            os.environ[discovery.ENV_VAR] = saved
        else:
            os.environ.pop(discovery.ENV_VAR, None)


@contextlib.contextmanager
def _live(registry):
    """`registry` as this process's current one, for the duration of a block."""
    from graphify.drupal import discovery

    with _restored_discovery_state():
        os.environ.pop(discovery.ENV_VAR, None)
        discovery.set_current(registry)
        yield


@pytest.fixture(scope="module")
def p2a(family_files, tmp_path_factory):
    """The registry `detect()` would build for the corpus, and one extraction
    of every file it makes graph-bearing: P1's families, the learned-family
    files and the manager class files (which carry the type nodes)."""
    import graphify  # noqa: F401
    from graphify.drupal.discovery import prepare_run
    from graphify.drupal.yaml_plugins import learned_family
    from graphify.extract import extract

    out = tmp_path_factory.mktemp("p2a-out")
    with _restored_discovery_state():
        # The default run. The corpus's .gitignore excludes /web/core and
        # /web/modules/contrib (composer-managed); the registry does not honour
        # .gitignore, so it still learns every type they define.
        registry = prepare_run(CORPUS, cache_root=out)
        assert registry is not None
        learned = sorted(Path(p) for p in registry.root_yaml if learned_family(Path(p)))
        managers = sorted(Path(p) for p in registry.by_class_file())
        paths = sorted({*family_files, *learned, *managers})
        extraction = extract(paths, cache_root=out, root=CORPUS)
    return {"registry": registry, "learned": learned, "managers": managers,
            "extraction": extraction}


def _scan_managers() -> set[str]:
    """Spec §2's full-tree scan, independent of PSR-4 and of services: every
    class under `web/` outside pruned trees, resolved against every other
    class the scan read. The two roots themselves are not managers of a type."""
    import graphify  # noqa: F401
    from graphify.drupal.php_classes import read_php_class

    classes = {}
    for dirpath, dirnames, filenames in os.walk(CORPUS / "web"):
        dirnames[:] = [d for d in dirnames if d not in _SCAN_PRUNED and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".php"):
                continue
            path = Path(dirpath) / name
            text = path.read_text(encoding="utf-8", errors="replace")
            if "extends" not in text and "implements" not in text:
                continue
            cls = read_php_class(path)
            if cls is not None:
                classes.setdefault(cls.fqcn, cls)

    memo: dict[str, bool] = {}

    def reaches(fqcn: str, depth: int = 0) -> bool:
        if fqcn in _MANAGER_ROOTS:
            return True
        if fqcn in memo or depth > 16:
            return memo.get(fqcn, False)
        memo[fqcn] = False
        cls = classes.get(fqcn)
        memo[fqcn] = cls is not None and any(
            reaches(p, depth + 1) for p in (*cls.extends, *cls.implements))
        return memo[fqcn]

    return {f for f, c in classes.items()
            if c.kind == "class" and f not in _MANAGER_ROOTS and reaches(f)}


def test_p2a_criterion_1_no_silent_manager(p2a):
    registry = p2a["registry"]
    known = ({t.manager_class for t in registry.types.values()}
             | {u["class"] for u in registry.unresolved})
    scanned = _scan_managers()
    # 121 type classes, plus symfony_mailer's MailManagerReplacement, which only
    # SymfonyMailerServiceProvider::alter() installs (a service_provider_alter entry).
    assert len(scanned) == 122
    assert sorted(scanned - known) == []
    assert collections.Counter(u["reason"] for u in registry.unresolved) == {
        "service_provider_alter": 14, "dynamic_discovery": 3, "not_a_service": 3,
        "parse_error": 1, "psr4_unresolved": 1}


def _family_of(path: Path, registry) -> str:
    for ext, directory in registry.extensions.items():
        if Path(directory) == path.parent and path.name.startswith(f"{ext}."):
            return path.name[len(ext) + 1:-len(".yml")]
    return ""


def test_p2a_criterion_2_every_root_yaml_file_is_in_exactly_one_bucket(p2a):
    from graphify.drupal.config_stores import is_config_yaml
    from graphify.drupal.families import family_extractor
    from graphify.drupal.inventory import DEFERRED_FAMILIES, build_inventory
    from graphify.drupal.yaml_plugins import learned_family

    registry = p2a["registry"]
    files = [Path(p) for p in registry.root_yaml]
    with _live(registry):
        inventory = build_inventory(registry, set(registry.root_yaml), CORPUS)
        unrecognised = {e["family"] for e in inventory["unrecognised_yaml"]}
        buckets = collections.Counter()
        misplaced = []
        for path in files:
            claims = [
                name for name, claimed in (
                    ("p1", family_extractor(path) is not None or is_config_yaml(path)),
                    ("learned", learned_family(path) is not None),
                    ("deferred", _family_of(path, registry) in DEFERRED_FAMILIES),
                    ("unrecognised", _family_of(path, registry) in unrecognised),
                ) if claimed
            ]
            if len(claims) != 1:
                misplaced.append((path.relative_to(CORPUS).as_posix(), claims))
            else:
                buckets[claims[0]] += 1
    assert misplaced == []
    assert buckets["unrecognised"] == inventory["summary"]["unrecognised_files"]
    assert sum(buckets.values()) == len(files)
    # No root-level file is deferred: SDC and migrations live below the root.
    assert buckets == {"p1": 1005, "learned": 72, "unrecognised": 4}


def test_p2a_criterion_3_counts(p2a):
    registry = p2a["registry"]
    types = registry.types.values()
    assert len(registry.types) == 143
    assert sum(1 for t in types if t.registered) == 140
    assert len({t.class_file for t in types}) == 121
    assert len(p2a["learned"]) == 72
    plugins = [n for n in p2a["extraction"]["nodes"] if n.get("type") == "drupal_plugin"]
    assert len(plugins) == 260
    type_nodes = [n for n in p2a["extraction"]["nodes"] if n.get("type") == "drupal_plugin_type"]
    assert len(type_nodes) == 143


def test_p2a_criterion_4_every_p1_plugin_has_one_type(p2a):
    extraction = p2a["extraction"]
    out = collections.Counter(e["source"] for e in extraction["edges"]
                              if e["relation"] == "plugin_of_type")
    targets = collections.defaultdict(set)
    for e in extraction["edges"]:
        if e["relation"] == "plugin_of_type":
            targets[e["source"]].add(e["target"])
    p1 = [n for n in extraction["nodes"]
          if n.get("type") in _P1_PLUGIN_NODE_TYPES and not n.get("external")]
    assert len(p1) == 924
    assert {n["id"]: sorted(targets[n["id"]]) for n in p1 if len(targets[n["id"]]) != 1} == {}
    # A plugin several files declare (merge.py's collapse keeps one node) keeps
    # one identical edge per declaring file; the reader folds parallel edges of
    # one relation into one, so the graph still has exactly one.
    assert {n["id"]: out[n["id"]] for n in p1
            if out[n["id"]] != len(n.get("declared_in") or [n])} == {}


def test_p2a_criterion_5_no_plugin_carries_a_value(p2a):
    allowed = _UNIVERSAL_NODE_KEYS | _PLUGIN_KEYS
    plugins = [n for n in p2a["extraction"]["nodes"] if n.get("type") == "drupal_plugin"]
    assert plugins
    assert {n["id"]: sorted(set(n) - allowed) for n in plugins if set(n) - allowed} == {}


def test_p2a_criterion_8_registry_build_is_under_five_seconds(tmp_path):
    """What `detect()` pays: `prepare_run`, the build plus core's ignore
    predicate on directories and collected files."""
    import time

    import graphify  # noqa: F401
    from graphify.drupal.discovery import prepare_run

    with _restored_discovery_state():
        started = time.perf_counter()
        registry = prepare_run(CORPUS, cache_root=tmp_path)
        elapsed = time.perf_counter() - started
    assert registry is not None and len(registry.types) == 143
    assert elapsed < 5.0


# -- P2b: hooks and the boundary (spec 2026-09-24-drupal-p2b-hooks-boundary §8) --
#
# Through the seam's `detect()` -- `prepare_run` plus core's walk with the
# boundary pruned -- and then `extract()` of the code files it returns, which is
# what `graphify extract --code-only` does. Everything goes to a temporary out
# dir, and the process state `prepare_run` and the inventory set is restored.

#: Composer package types whose install paths criterion 1 checks (spec §4.1).
_CONTRIB_TYPES = ("drupal-module", "drupal-theme", "drupal-profile", "drupal-recipe")
_HOOK_ATTRIBUTE = re.compile(r"#\[\s*(?:\\?Drupal\\Core\\Hook\\Attribute\\)?Hook\s*\(")
_TOP_LEVEL_FUNCTION = re.compile(r"^function\s+(\w+)\s*\(", re.MULTILINE)
_CUSTOM_ROOTS = ("web/modules/custom", "web/themes/custom", "web/profiles/custom")


@pytest.fixture(scope="module")
def p2b(tmp_path_factory):
    import graphify  # noqa: F401
    from graphify.detect import detect
    from graphify.drupal.discovery import current_registry
    from graphify.drupal.inventory import current_inventory
    from graphify.extract import extract

    out = tmp_path_factory.mktemp("p2b-out")
    with _restored_discovery_state():
        detected = detect(CORPUS, cache_root=out)
        registry = current_registry()
        inventory = current_inventory()
        assert registry is not None and inventory is not None
        code = sorted(Path(p) for p in detected["files"]["code"])
        extraction = extract(code, cache_root=out, root=CORPUS)
    return {"detected": detected, "registry": registry, "inventory": inventory,
            "extraction": extraction}


def _composer_install_dirs() -> dict[str, list[Path]]:
    """Spec §4.1 re-derived from the corpus's own composer files: every
    installed package of a contrib type -> its install dir, by type."""
    import json

    manifest = json.loads((CORPUS / "composer.json").read_text(encoding="utf-8"))
    lock = json.loads((CORPUS / "composer.lock").read_text(encoding="utf-8"))
    patterns = manifest["extra"]["installer-paths"]
    found: dict[str, list[Path]] = collections.defaultdict(list)
    for package in [*lock["packages"], *lock.get("packages-dev", [])]:
        kind = package.get("type")
        if kind not in _CONTRIB_TYPES:
            continue
        name = (package.get("extra") or {}).get("installer-name") or package["name"].split("/")[1]
        for pattern, selectors in patterns.items():
            if f"type:{kind}" in selectors or package["name"] in selectors:
                found[kind].append(CORPUS / pattern.replace("{$name}", name))
                break
    return found


def test_p2b_criterion_1_realm_comes_from_composer():
    import graphify  # noqa: F401
    from graphify.drupal.boundary import clear_caches, realm_of

    clear_caches()
    installed = _composer_install_dirs()
    # 93 modules and 2 themes in composer.lock; the corpus has no contrib
    # profile or recipe, so those two types simply contribute nothing.
    assert {k: len(v) for k, v in installed.items()} == {"drupal-module": 93, "drupal-theme": 2}
    wrong = {d.relative_to(CORPUS).as_posix(): realm_of(d / "x.info.yml")
             for dirs in installed.values() for d in dirs
             if realm_of(d / "x.info.yml") != "contrib"}
    assert wrong == {}

    def realms_under(relative: str) -> collections.Counter:
        counts: collections.Counter = collections.Counter()
        for dirpath, dirnames, _files in os.walk(CORPUS / relative):
            # `tests/` excluded, as throughout the spec: package_manager's
            # fixtures hold composer projects of their own (fake_site/ with a
            # composer.json and lock), whose nearest-project answer is not core.
            dirnames[:] = [d for d in dirnames if d not in _SCAN_PRUNED]
            counts[realm_of(Path(dirpath) / "f.php")] += 1
        return counts

    assert set(realms_under("web/core")) == {"core"}
    assert set(realms_under("vendor/symfony")) == {"vendor"}
    for custom in ("web/modules/custom", "web/themes/custom", "config", "web/sites"):
        assert set(realms_under(custom)) == {"custom"}, custom


def test_p2b_criterion_1b_detect_prunes_the_boundary(p2b):
    from graphify.drupal.boundary import realm_of

    detected = [p for paths in p2b["detected"]["files"].values() for p in paths]
    assert detected
    assert collections.Counter(realm_of(Path(p)) for p in detected
                               if realm_of(Path(p)) != "custom") == {}
    summary = p2b["inventory"]["summary"]
    # web/core, the 101 install dirs under web/modules/contrib, web/themes/contrib
    # and web/libraries that exist, vendor/, and web/sites/default/files.
    assert summary["boundary"] == {"core": 1, "contrib": 101, "vendor": 1, "files": 1}
    assert summary["boundary_reasons"] == {"composer": 102, "site_files": 1, "vendor_dir": 1}


def test_p2b_criterion_2_the_hook_registry(p2b):
    hooks = p2b["registry"].hooks
    # Spec §2: 298 core + 137 contrib stubs, 435 distinct names, 51 variable.
    assert len(hooks) == 435
    assert sum(1 for d in hooks.values() if d.pattern) == 51


def _custom_hook_sites(declared: set[str]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Spec §2's custom counts, by a text scan independent of the extractor:
    every `#[Hook(` in a custom `.php` file, and every procedural
    `<ext>_<declared hook>()` in a custom extension's procedural file
    (`tests/` excluded, as in the spec)."""
    attributes, procedural = [], []
    for root in _CUSTOM_ROOTS:
        for dirpath, dirnames, filenames in os.walk(CORPUS / root):
            dirnames[:] = [d for d in dirnames if d not in _SCAN_PRUNED and not d.startswith(".")]
            for name in filenames:
                path = Path(dirpath) / name
                relative = path.relative_to(CORPUS).as_posix()
                parts = name.split(".")
                if name.endswith(".php"):
                    text = path.read_text(encoding="utf-8", errors="replace")
                    attributes += [(relative, text.count("\n", 0, m.start()) + 1)
                                   for m in _HOOK_ATTRIBUTE.finditer(text)]
                elif (parts[-1] in ("module", "install", "theme", "profile", "inc")
                      and (Path(dirpath) / f"{parts[0]}.info.yml").is_file()):
                    text = path.read_text(encoding="utf-8", errors="replace")
                    prefix = parts[0] + "_"
                    procedural += [(relative, text.count("\n", 0, m.start()) + 1)
                                   for m in _TOP_LEVEL_FUNCTION.finditer(text)
                                   if m.group(1).startswith(prefix)
                                   and m.group(1)[len(prefix):] in declared]
    return attributes, procedural


def _line(located: dict) -> int:
    return int(str(located["source_location"]).lstrip("L"))


def test_p2b_criterion_3_no_custom_hook_is_silent(p2b):
    extraction, inventory = p2b["extraction"], p2b["inventory"]
    attributes, procedural = _custom_hook_sites(set(p2b["registry"].hooks))
    assert (len(attributes), len(procedural)) == (31, 23)

    # Every implementation has its own `hook_implemented_by` edge at its line;
    # everything else is a candidate at its line.
    implemented = {(e["source_file"], _line(e)) for e in extraction["edges"]
                   if e["relation"] == "hook_implemented_by"}
    candidates = {(c["file"], c["line"]) for c in inventory["hook_candidates"]}
    assert [s for s in attributes + procedural if s not in implemented | candidates] == []
    # 19 literal, declared hooks + 8 variable ones P4 Task 5 binds (2
    # `form_*_alter`, 6 `ENTITY_TYPE_*`).
    assert len({s for s in attributes if s in implemented}) == 27
    assert {s for s in procedural if s not in implemented} == set()

    by_kind = collections.Counter(c["kind"] for c in inventory["hook_candidates"])
    # 9 variable-segment names (preprocess_*, theme_suggestions_*: P5) -- the
    # 15 form alters and entity-type hooks are bound since P4 Task 5 -- and
    # 3 plugin-info alters no `*.api.php` declares. The invocation-site
    # entries (`unknown_receiver`) are in test_p2b_invocations.
    assert (by_kind["variable"], by_kind["undeclared"]) == (9, 3)
    assert inventory["summary"]["hook_candidates"] == len(inventory["hook_candidates"])


def test_p2b_criterion_4_every_hook_implemented_by_target_exists(p2b):
    extraction = p2b["extraction"]
    ids = {n["id"] for n in extraction["nodes"]}
    by = [e for e in extraction["edges"] if e["relation"] == "hook_implemented_by"]
    # 42 literal, declared hooks + 15 variable ones bound (P4 Task 5).
    assert len(by) == 57
    assert [e["target"] for e in by if e["target"] not in ids] == []
    assert [e["source"] for e in by if e["source"] not in ids] == []
    impls = [n for n in extraction["nodes"] if n.get("type") == "drupal_hook_impl"]
    # One node per (module, hook): 57 implementations share 54 nodes.
    assert len(impls) == 54
    assert collections.Counter(n["via"] for n in impls) == {"attribute": 25, "procedural": 29}


def test_p2b_criterion_5_registry_and_boundary_under_five_seconds(tmp_path):
    import time

    import graphify  # noqa: F401
    from graphify.drupal.boundary import clear_caches
    from graphify.drupal.discovery import prepare_run

    with _restored_discovery_state():
        clear_caches()
        started = time.perf_counter()
        registry = prepare_run(CORPUS, cache_root=tmp_path)
        elapsed = time.perf_counter() - started
    assert registry is not None and len(registry.hooks) == 435
    assert elapsed < 5.0


def test_p2b_the_graph_is_own_code_plus_a_named_boundary(p2b):
    from graphify.drupal.boundary import realm_of

    nodes = p2b["extraction"]["nodes"]
    located = {n["source_file"] for n in nodes if n.get("source_file") and not n.get("boundary")}
    assert collections.Counter(realm_of(CORPUS / s) for s in located) == {"custom": len(located)}
    drupal = [n for n in nodes if _is_drupal(n)]
    # `unknown` only on boundary stubs whose realm nothing derivable gives.
    assert [n["id"] for n in drupal if n.get("realm") == "unknown" and not n.get("boundary")] == []
    # Every boundary extension the registry knows carries its facts; the other
    # two are the owners `web/sites/*.services.yml` imply (`default`,
    # `development`), which no info file declares.
    known = p2b["registry"].extension_info
    extensions = [n for n in drupal if n.get("boundary") and n["type"] == "drupal_extension"]
    assert len(extensions) == 194
    assert sorted(n["label"] for n in extensions if n["label"] not in known) \
        == ["default", "development"]
    assert [n["id"] for n in extensions if n["label"] in known
            and not (n.get("extension_type") and n.get("extension_path")
                     and n["realm"] in ("core", "contrib"))] == []


def test_p2b_invocations(p2b):
    edges = [e for e in p2b["extraction"]["edges"] if e["relation"] == "invokes_hook"]
    # Two custom plugin managers' alter hooks and one kernel test's invokeAll.
    assert sorted(e["target_name"] for e in edges) == [
        "system_type_info_alter", "webform_integration_type_info_alter",
        "webform_submission_insert"]
    by_kind = collections.Counter(c["kind"] for c in p2b["inventory"]["hook_candidates"])
    # `invoke`/`alter` on no handler: ReflectionMethod::invoke and a unit test's
    # own `alter()` helper, all in custom tests/ trees; no literal-less handler call.
    unknown = [c for c in p2b["inventory"]["hook_candidates"] if c["kind"] == "unknown_receiver"]
    # 14 since FormsRemote 9f933cdb deleted SharepointCredentialsTest.php and
    # its `$method->invoke($client, ...)` (ReflectionMethod::invoke).
    assert (len(unknown), by_kind["non_literal"]) == (14, 0)
    assert all("/tests/" in c["file"] for c in unknown)


def test_p2b_every_drupal_node_survives_the_build(p2b):
    """Core's label dedup in `build()` must not fold one Drupal node into
    another (the core module `toolbar` into the hook `toolbar`, a local task
    into the menu link with its label)."""
    from graphify.build import build

    extraction = p2b["extraction"]
    graph = build([extraction], directed=True, root=CORPUS)
    drupal = {n["id"] for n in extraction["nodes"] if _is_drupal(n)}
    assert sorted(drupal - set(graph.nodes)) == []
    assert graph.nodes["drupal_extension_toolbar"]["type"] == "drupal_extension"
    assert graph.nodes["drupal_hook_toolbar"]["type"] == "drupal_hook"


# -- P3: the container overlay (spec 2026-09-25-drupal-p3-container §12) --
#
# A normal build never runs drush, and neither do these tests: the live half
# reads an artifact already collected from the running site
# (`graphify drupal container <corpus> --out <file>`) named by
# DRUPAL_CONTAINER_ARTIFACT, and skips without it. The build is the one
# `graphify extract --code-only` does, under a temporary out dir.

_ARTIFACT = os.environ.get("DRUPAL_CONTAINER_ARTIFACT")
_needs_artifact = pytest.mark.skipif(
    not (_ARTIFACT and Path(_ARTIFACT).is_file()),
    reason="needs an artifact collected from the running corpus (DRUPAL_CONTAINER_ARTIFACT=<file>)")


@contextlib.contextmanager
def _artifact_env(value: str | None):
    from graphify.drupal.container import ENV_ARTIFACT

    saved = os.environ.get(ENV_ARTIFACT)
    if value is None:
        os.environ.pop(ENV_ARTIFACT, None)
    else:
        os.environ[ENV_ARTIFACT] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(ENV_ARTIFACT, None)
        else:
            os.environ[ENV_ARTIFACT] = saved


def _p3_build(out: Path, artifact: str | None) -> dict:
    """detect -> extract -> build, as `extract --code-only`; the seam's
    build wrapper lays the artifact (if any) over the graph. Also times
    `run_for_build` on the built graph: the overlay's own cost."""
    import time

    import graphify  # noqa: F401
    from graphify.build import build
    from graphify.detect import detect
    from graphify.drupal.container_overlay import run_for_build
    from graphify.drupal.inventory import current_inventory
    from graphify.extract import extract

    with _restored_discovery_state(), _artifact_env(artifact):
        detected = detect(CORPUS, cache_root=out)
        code = sorted(Path(p) for p in detected["files"]["code"])
        extraction = extract(code, cache_root=out, root=CORPUS)
        graph = build([extraction], directed=True, root=CORPUS)
        container = dict(current_inventory()["container"])
        started = time.perf_counter()
        again = run_for_build(graph)
        elapsed = time.perf_counter() - started
    return {"graph": graph, "container": container, "again": again, "elapsed": elapsed}


def test_p3_without_an_artifact_the_graph_is_static_and_says_so(tmp_path):
    built = _p3_build(tmp_path, None)
    graph = built["graph"]
    assert built["container"]["status"] == "unavailable"
    assert [n for n, d in graph.nodes(data=True) if "runtime" in d] == []
    assert [e for *e, d in graph.edges(data=True) if d.get("origin") == "container"] == []
    assert list(tmp_path.rglob("drupal-divergence.json")) == []


@_needs_artifact
def test_p3_the_collected_artifact_is_valid_and_carries_no_machine_path():
    import json

    from graphify.drupal.container import validate

    text = Path(_ARTIFACT).read_text(encoding="utf-8")
    data = validate(json.loads(text))
    assert len(data["services"]) > 1000 and len(data["routes"]) > 500 and data["hooks"]
    files = [s["file"] for s in data["services"]]
    files += [e["file"] for entries in data["hooks"].values() for e in entries]
    files += [p["file"] for entries in data["plugins"].values() for p in entries]
    files += [s["file"] for entries in data["subscribers"].values() for s in entries]
    files += [e["path"] for e in data["extensions"]]
    assert [f for f in files if f is not None and (f.startswith("/") or ":" in f[:3])] == []
    assert str(CORPUS) not in text and "/var/www/html" not in text
    # The one collector error on the corpus is contrib's: ckeditor5_plugin_pack_font
    # declares `plugin.manager.font_colors` with a class it does not ship.
    assert [(e["source"], "font_colors" in e["message"]) for e in data["errors"]] \
        == [("plugins", True)]


@_needs_artifact
def test_p3_the_overlay_on_the_corpus(tmp_path):
    built = _p3_build(tmp_path, _ARTIFACT)
    graph, container = built["graph"], built["container"]
    assert container["status"] in ("fresh", "stale")

    overlay_edges = [d for *_e, d in graph.edges(data=True) if d.get("origin") == "container"]
    confirmed = [d for *_e, d in graph.edges(data=True) if d.get("confirmed_by") == "container"]
    assert overlay_edges and confirmed
    assert container["edges"]["container_only"] == len(overlay_edges)
    # Container-only knowledge the static graph cannot have: derivative plugins.
    derivatives = [n for n, d in graph.nodes(data=True)
                   if d.get("derivative") is True and d.get("origin") == "container"]
    assert derivatives
    # Custom route subscribers inherit their listener from core's
    # RouteSubscriberBase; they are still custom subscribers.
    assert container["counts"]["subscribers"]["applied"] >= 2
    # A links-family plugin is P1's link node, never a second plugin node.
    assert [n for n, d in graph.nodes(data=True)
            if d.get("type") == "drupal_plugin" and str(d.get("plugin_type", "")).startswith("menu.")] == []
    # Every node of a runtime type is marked, except a theme's hook implementations.
    from graphify.drupal.container_overlay import RUNTIME_TYPES

    unmarked = [n for n, d in graph.nodes(data=True)
                if d.get("type") in RUNTIME_TYPES and "runtime" not in d]
    assert all(graph.nodes[n]["type"] == "drupal_hook_impl" for n in unmarked)
    assert len(list(tmp_path.rglob("drupal-divergence.json"))) == 1

    # Applied again on the built graph: the same result, well under a second.
    assert built["again"] is not None and built["again"].edges == container["edges"]
    assert built["elapsed"] < 1.0
