# Drupal P1b Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read Drupal configuration — synced, split, shipped, schema, recipe and `settings.php` overrides — into the graph as one node per configuration object with the edges it states about itself, and never its values.

**Architecture:** A location predicate (`config_stores.py`) recognises configuration by where it lives, and the seam consults it before P1's family table. Per-file extractors emit config, schema and recipe nodes and edges; `settings*.php` is read by composing core's PHP extractor with a Drupal one. The collapse gains a store rank, and the resolver gains schema matching, domain re-targeting, the new relations and `installed`.

**Tech Stack:** Python 3.10+, PyYAML, pytest, uv.

Spec: `docs/superpowers/specs/2026-09-23-drupal-p1b-configuration-design.md`.

## Global Constraints

- Python floor `>=3.10`; ruff `line-length = 100`, `target-version = "py310"`.
- Tests run as `uv run --frozen pytest tests/ -q --tb=short`. Known pre-existing failures: the four `tests/test_ollama_retry_cap.py` tests.
- Test files are flat: `tests/test_drupal_*.py`. No files under `tests/fixtures/`; build corpora in `tmp_path`.
- **Every commit is inside `graphify/drupal/`, `tests/test_drupal_*.py` or `docs/`.** No `core:` commit is expected in P1b.
- **Never parse with `yaml.safe_load`.** Use `load_drupal_yaml` from `graphify/drupal/yaml_common.py`.
- **Configuration values never enter the graph** except structural metadata: a config entity's boolean `status`, an extension's `weight` in `core.extension.yml`, a split's `folder`, and a recipe's `name` (label) and `type` (`recipe_type`). Keys are recorded by path, never with their value.
- `*.info.yml` is the only producer of `drupal_module` / `drupal_theme` / `drupal_profile` nodes.
- At most one relation per ordered node pair.
- Anything another file may also declare needs a test through `graphify.extract.extract(paths, cache_root=<tmp>, root=<tmp>)`, not only an extractor unit test.
- Every count asserted is a count of nodes after the collapse, or of files — never of YAML keys.
- A handler imported by `graphify/drupal/families.py` must not import `families` at module level (import cycle); import inside the function.

Reference corpus: `/home/user/Projects/FormsRemote`.

## Decisions refining the spec

These were settled while writing the plan; each task that depends on one says so.

1. **`settings.php` is read per file, not by a resolver pass (spec §5.4).** The resolver receives no scan root, and core hands it `settings.php`'s `source_file` relative. The seam instead returns, for `sites/*/settings*.php`, a handler that runs core's PHP extractor and appends the Drupal settings result. Per-file also makes it cached and incremental like every other producer.
2. **Path rules precede the sync marker.** `web/core/config/install/core.extension.yml` exists; a marker-first rule would call core's default config a sync store.
3. **Guards against non-Drupal repositories.** `config/install|optional|schema` counts only when an extension (`*.info.yml`, or Drupal core's own directory) sits above `config/`; `recipe.yml` counts only at `recipes/<name>/recipe.yml`.
4. **Realm.** Sync and split stores and `settings.php` are the project's own: `realm: custom`. `*/core/config/*` and `*/core/recipes/*` join the `core` rules.
5. **Recipe-shipped configuration** is owned by its recipe: `defines_config` recipe → config, `install_mode: recipe`.

---

## File Structure

| File | Responsibility |
|---|---|
| `graphify/drupal/config_stores.py` (new) | `ConfigStore`, `config_store(path)`, `is_config_yaml(path)`, `config_name(path)` — location only, no parsing beyond split `folder:` |
| `graphify/drupal/yaml_config.py` (new) | config objects, dependencies, ownership, `core.extension`, split entities, overrides (split patch/copy, domain, language) |
| `graphify/drupal/yaml_schema.py` (new) | `*.schema.yml` → schema type nodes |
| `graphify/drupal/yaml_recipes.py` (new) | `recipe.yml` → recipe node and its edges |
| `graphify/drupal/yaml_settings.py` (new) | `settings*.php` `$config[…]` lines → settings node and `overrides_config` |
| `graphify/drupal/yaml_common.py` | + `config_id`, `schema_id`, `recipe_id`, `settings_id` |
| `graphify/drupal/paths.py` | + core realm rules for `core/config`, `core/recipes` |
| `graphify/drupal/families.py` | + `drupal_extractor(path)`, `is_drupal_file(path)` — config first, then families |
| `graphify/drupal/register.py` | seam wrappers use `drupal_extractor`; settings composition |
| `graphify/drupal/merge.py` | store rank, attribute gap-fill, `_rank` stripped |
| `graphify/drupal/resolvers.py` | new relations, type by id prefix, domain re-target, `schema_for`, `installed` / `missing` |
| `tests/test_drupal_config_stores.py`, `test_drupal_config.py`, `test_drupal_config_overrides.py`, `test_drupal_schema.py`, `test_drupal_recipes.py`, `test_drupal_settings.py`, `test_drupal_config_pipeline.py` (new) | unit and pipeline tests |
| `tests/test_drupal_corpus.py` | + P1b acceptance criteria |

---

### Task 1: Config stores

**Files:**
- Create: `graphify/drupal/config_stores.py`
- Modify: `graphify/drupal/paths.py` (two core realm rules)
- Test: `tests/test_drupal_config_stores.py`, `tests/test_drupal_paths.py`

**Interfaces:**
- Produces:
  - `ConfigStore(kind: str, directory: Path, owner: str = "", split: str = "", language: str = "")` — frozen dataclass; `kind` ∈ `sync | split | install | optional | schema | recipe`
  - `config_store(path: Path) -> ConfigStore | None`
  - `is_config_yaml(path: Path) -> bool`
  - `config_name(path: Path) -> str` — file name without `.yml`
  - `in_config_directory(path: Path) -> bool` — the file sits in a `config/{install,optional,schema}` directory, whether or not it counts as configuration
  - constants `SPLIT_PREFIX = "config_split.config_split."`, `PATCH_PREFIX = "config_split.patch."`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_config_stores.py
"""Configuration is found by where it lives (P1b spec §3)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.config_stores import config_name, config_store, is_config_yaml


def _touch(root: Path, rel: str, text: str = "x: 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _site(tmp_path: Path) -> Path:
    """A project laid out like the reference corpus."""
    _touch(tmp_path, "web/core/lib/Drupal.php", "<?php\n")
    _touch(tmp_path, "config/sync/core.extension.yml", "module:\n  node: 0\ntheme: {}\n")
    _touch(tmp_path, "config/sync/config_split.config_split.dev.yml",
           "id: dev\nfolder: ../config/splits/dev\nstatus: false\n")
    _touch(tmp_path, "web/modules/custom/foo/foo.info.yml", "name: Foo\ntype: module\n")
    return tmp_path


def test_a_directory_with_core_extension_is_a_sync_store(tmp_path):
    root = _site(tmp_path)
    store = config_store(_touch(root, "config/sync/system.site.yml"))
    assert store.kind == "sync"
    assert store.directory == root / "config/sync"


def test_split_folder_is_resolved_from_the_web_root(tmp_path):
    root = _site(tmp_path)
    store = config_store(_touch(root, "config/splits/dev/config_split.patch.user.settings.yml"))
    assert store.kind == "split"
    assert store.split == "dev"


def test_shipped_config_is_owned_by_the_extension_above_it(tmp_path):
    root = _site(tmp_path)
    install = config_store(_touch(root, "web/modules/custom/foo/config/install/foo.settings.yml"))
    optional = config_store(_touch(root, "web/modules/custom/foo/config/optional/views.view.foo.yml"))
    assert (install.kind, install.owner) == ("install", "foo")
    assert (optional.kind, optional.owner) == ("optional", "foo")


def test_core_default_config_is_not_a_sync_store(tmp_path):
    """core/config/install ships its own core.extension.yml."""
    root = _site(tmp_path)
    path = _touch(root, "web/core/config/install/core.extension.yml")
    store = config_store(path)
    assert (store.kind, store.owner) == ("install", "core")


def test_schema_files_are_a_store_of_their_own(tmp_path):
    root = _site(tmp_path)
    store = config_store(_touch(root, "web/modules/custom/foo/config/schema/foo.schema.yml"))
    assert (store.kind, store.owner) == ("schema", "foo")


def test_recipes_live_under_a_recipes_directory(tmp_path):
    root = _site(tmp_path)
    recipe = config_store(_touch(root, "web/core/recipes/standard/recipe.yml"))
    shipped = config_store(_touch(root, "web/core/recipes/standard/config/node.type.page.yml"))
    stray = config_store(_touch(root, "tools/recipe.yml"))
    assert (recipe.kind, recipe.owner) == ("recipe", "standard")
    assert (shipped.kind, shipped.owner) == ("recipe", "standard")
    assert stray is None


def test_language_directories_inherit_their_store(tmp_path):
    root = _site(tmp_path)
    store = config_store(_touch(root, "config/sync/language/fr/system.site.yml"))
    assert (store.kind, store.language) == ("sync", "fr")


def test_test_modules_are_excluded_but_core_recipe_fixtures_are_not(tmp_path):
    root = _site(tmp_path)
    fixture_module = _touch(
        root, "web/core/modules/system/tests/modules/t/config/install/t.settings.yml")
    _touch(root, "web/core/modules/system/tests/modules/t/t.info.yml", "name: T\n")
    fixture_recipe = _touch(root, "web/core/tests/fixtures/recipes/input_test/recipe.yml")
    assert config_store(fixture_module) is None
    assert config_store(fixture_recipe).kind == "recipe"


def test_config_dirs_without_an_extension_are_not_drupal(tmp_path):
    """A non-Drupal repository with config/install/*.yml is left alone."""
    assert config_store(_touch(tmp_path, "app/config/install/settings.yml")) is None


def test_graphifyrc_names_a_sync_store_without_the_marker(tmp_path):
    _touch(tmp_path, ".graphifyrc", "drupal.config.sync = exported\n")
    assert config_store(_touch(tmp_path, "exported/system.site.yml")).kind == "sync"


def test_non_yaml_and_family_files_outside_stores(tmp_path):
    root = _site(tmp_path)
    assert not is_config_yaml(_touch(root, "web/modules/custom/foo/foo.services.yml"))
    assert not is_config_yaml(_touch(root, "config/sync/readme.txt"))


def test_config_name_is_the_file_name_without_yml():
    assert config_name(Path("/s/field.field.node.page.body.yml")) == "field.field.node.page.body"


def test_config_directories_are_recognised_even_when_excluded(tmp_path):
    from graphify.drupal.config_stores import in_config_directory

    path = _touch(tmp_path, "web/core/modules/system/tests/modules/m/config/install/m.links.action.yml")
    assert config_store(path) is None
    assert in_config_directory(path)
    assert not in_config_directory(_touch(tmp_path, "web/modules/custom/foo/foo.links.action.yml"))
```

Append to the realm parametrisation in `tests/test_drupal_paths.py` (the list that starts with `("/p/core/modules/node/node.info.yml", "core"),`):

```python
    ("/p/web/core/config/install/core.extension.yml", "core"),
    ("/p/web/core/recipes/standard/recipe.yml", "core"),
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --frozen pytest tests/test_drupal_config_stores.py tests/test_drupal_paths.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.config_stores'`, and the two new realm cases.

- [ ] **Step 3: Implement**

In `graphify/drupal/paths.py`, extend the `core` tuple of `DEFAULT_REALM_RULES` after `"*/core/assets/*",`:

```python
        # Core's own default configuration and the recipes it ships.
        "*/core/config/*",
        "*/core/recipes/*",
```

```python
# graphify/drupal/config_stores.py
"""Where Drupal configuration lives, recognised from the path and its directory.

Configuration carries no family suffix — `system.site.yml` is named after the
object, not the kind — so it is found by location (P1b spec §3.1). Every rule
here is local: the file's own path, its directory, and for splits the sync store
beside it. Results are cached per directory, because classification runs once per
file of the corpus.

Rule order matters. Test modules go first; then the fixed path rules
(`config/install|optional|schema`, recipes), because Drupal core's own
`core/config/install/` ships a `core.extension.yml`; then the sync marker; then
splits, which are only knowable from a sync store's split entities.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SPLIT_PREFIX = "config_split.config_split."
PATCH_PREFIX = "config_split.patch."

#: The one test tree kept: the only recipe corpus exercising config.actions/${input}.
_RECIPE_FIXTURES = ("core", "tests", "fixtures", "recipes")
_WEB_ROOT_NAMES = ("web", "docroot", "html", "public_html")


@dataclass(frozen=True)
class ConfigStore:
    kind: str
    directory: Path
    owner: str = ""
    split: str = ""
    language: str = ""


def _in_tests(path: Path) -> bool:
    parts = path.parts
    if "tests" not in parts:
        return False
    for i in range(len(parts) - len(_RECIPE_FIXTURES) + 1):
        if parts[i:i + len(_RECIPE_FIXTURES)] == _RECIPE_FIXTURES:
            return False
    return True


@lru_cache(maxsize=None)
def _owner(extension_dir: Path) -> str:
    """Machine name of the extension whose `config/` this is, or ''."""
    infos = sorted(extension_dir.glob("*.info.yml"))
    if infos:
        return infos[0].name[: -len(".info.yml")]
    # Drupal core's own config/ has no core.info.yml; P1 names that owner `core`.
    return "core" if extension_dir.name == "core" else ""


@lru_cache(maxsize=None)
def _rc_sync_dirs_from(rc: Path) -> frozenset[Path]:
    dirs: set[Path] = set()
    for raw in rc.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key.strip() != "drupal.config.sync":
            continue
        for part in val.split(","):
            if part.strip():
                dirs.add((rc.parent / part.strip()).resolve())
    return frozenset(dirs)


def _rc_sync_dirs(directory: Path) -> frozenset[Path]:
    for ancestor in (directory, *directory.parents):
        rc = ancestor / ".graphifyrc"
        if rc.is_file():
            return _rc_sync_dirs_from(rc)
    return frozenset()


def _is_sync_dir(directory: Path) -> bool:
    if (directory / "core.extension.yml").is_file():
        return True
    return directory.resolve() in _rc_sync_dirs(directory)


@lru_cache(maxsize=None)
def _markers_under(ancestor: Path) -> tuple[Path, ...]:
    """Sync stores one or two levels below `ancestor`."""
    hits = sorted(ancestor.glob("*/core.extension.yml")) + sorted(
        ancestor.glob("*/*/core.extension.yml"))
    return tuple(
        m.parent for m in hits
        if not _in_tests(m) and m.parent.name not in ("install", "optional")
    )


def _nearby_sync_dirs(directory: Path) -> list[Path]:
    found: list[Path] = []
    for ancestor in [directory, *directory.parents][:4]:
        if len(ancestor.parts) <= 2:  # never glob the filesystem root
            break
        for sync in _markers_under(ancestor):
            if sync not in found:
                found.append(sync)
    return found


def _web_root(sync_dir: Path) -> Path:
    """Drupal resolves a split `folder:` against DRUPAL_ROOT, the web root."""
    project = sync_dir.parent.parent
    for candidate in [project / name for name in _WEB_ROOT_NAMES] + [project]:
        if (candidate / "core" / "lib" / "Drupal.php").is_file():
            return candidate
    return sync_dir.parent


@lru_cache(maxsize=None)
def _split_folders(sync_dir: Path) -> tuple[tuple[str, Path], ...]:
    from graphify.drupal.yaml_common import load_drupal_yaml

    web = _web_root(sync_dir)
    folders: list[tuple[str, Path]] = []
    for entity in sorted(sync_dir.glob(f"{SPLIT_PREFIX}*.yml")):
        data, _ = load_drupal_yaml(entity)
        folder = (data or {}).get("folder")
        if not isinstance(folder, str) or not folder.strip():
            continue
        target = Path(folder.strip())
        split_id = entity.name[len(SPLIT_PREFIX):-len(".yml")]
        folders.append((split_id, (target if target.is_absolute() else web / target).resolve()))
    return tuple(folders)


@lru_cache(maxsize=None)
def _store_of_dir(directory: Path) -> ConfigStore | None:
    if directory.name in ("install", "optional") and directory.parent.name == "config":
        owner = _owner(directory.parent.parent)
        return ConfigStore(directory.name, directory, owner=owner) if owner else None
    if directory.name == "config" and (directory.parent / "recipe.yml").is_file() \
            and directory.parent.parent.name == "recipes":
        return ConfigStore("recipe", directory, owner=directory.parent.name)
    if _is_sync_dir(directory):
        return ConfigStore("sync", directory)
    resolved = directory.resolve()
    for sync in _nearby_sync_dirs(directory):
        for split_id, folder in _split_folders(sync):
            if folder == resolved:
                return ConfigStore("split", directory, split=split_id)
    return None


def config_store(path: Path) -> ConfigStore | None:
    """The store `path` belongs to, or None when it is not Drupal configuration."""
    if path.suffix != ".yml" or _in_tests(path):
        return None
    parent = path.parent
    if parent.parent.name == "language":
        base = _store_of_dir(parent.parent.parent)
        if base is None:
            return None
        return ConfigStore(base.kind, base.directory, base.owner, base.split, parent.name)
    if path.name == "recipe.yml":
        if parent.parent.name != "recipes":
            return None
        return ConfigStore("recipe", parent, owner=parent.name)
    if parent.name == "schema" and parent.parent.name == "config":
        if not path.name.endswith(".schema.yml"):
            return None
        owner = _owner(parent.parent.parent)
        return ConfigStore("schema", parent, owner=owner) if owner else None
    return _store_of_dir(parent)


def is_config_yaml(path: Path) -> bool:
    return config_store(path) is not None


def config_name(path: Path) -> str:
    return path.name[: -len(".yml")] if path.name.endswith(".yml") else path.name


def in_config_directory(path: Path) -> bool:
    """True for any file under `config/{install,optional,schema}/`.

    Such a file is never a P1 family file, even when it is excluded here as a
    test module's configuration: `menu_test/config/install/menu_test.links.action.yml`
    is a config object whatever its suffix says.
    """
    return path.parent.name in ("install", "optional", "schema") \
        and path.parent.parent.name == "config"
```

The caches live for the process. That is right for `graphify extract` and
`graphify update`, which are one process per run; a long-running watcher that
sees a new `core.extension.yml` appear is P7's concern.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --frozen pytest tests/test_drupal_config_stores.py tests/test_drupal_paths.py -q`
Expected: PASS.

- [ ] **Step 5: Verify on the reference corpus**

```bash
uv run --frozen python -c "
from pathlib import Path
from collections import Counter
from graphify.drupal.config_stores import config_store
root = Path('/home/user/Projects/FormsRemote')
c = Counter()
for p in list(root.glob('config/**/*.yml')) + list(root.glob('web/**/*.yml')):
    if 'node_modules' in p.parts: continue
    s = config_store(p)
    if s: c[s.kind] += 1
print(dict(c))
"
```

Measured (Task 1): `sync` 613, `split` 8, `install` + `optional` 857, `schema` 303, `recipe` 152 (49 recognised `recipe.yml` + recipe config). Of 58 `recipe.yml` files, 4 Composer-unpack fixtures are test fixtures and 5 sit one level deeper (`recipes/<group>/<name>/`), outside the `recipes/<name>/recipe.yml` rule. Record the measured numbers in the commit message; a gap is a store-detection bug, not a number to edit.

- [ ] **Step 6: Commit**

```bash
git add graphify/drupal/config_stores.py graphify/drupal/paths.py \
        tests/test_drupal_config_stores.py tests/test_drupal_paths.py
git commit -m "feat(drupal): recognise configuration stores by location"
```

---

### Task 2: Config objects

**Files:**
- Create: `graphify/drupal/yaml_config.py`
- Modify: `graphify/drupal/yaml_common.py` (four id helpers)
- Test: `tests/test_drupal_config.py`

**Interfaces:**
- Consumes: `config_store`, `config_name`, `SPLIT_PREFIX` (Task 1); `load_drupal_yaml`, `node`, `edge` (P1); `extension_id` (`yaml_extract`).
- Produces:
  - `config_id(name) -> str`, `schema_id(type_) -> str`, `recipe_id(directory) -> str`, `settings_id(site_file) -> str` in `yaml_common`
  - `extract_drupal_config(path: Path) -> dict` — the single handler for every config-store file; routes schema and `recipe.yml` to Tasks 4–5 (until those exist it returns empty results for them)
  - node attributes: `config_name`, `store`, `active`, `install_mode`, `status`, `folder`, `content_dependencies`, `_rank`
  - `STORE_RANK: dict[str, int]` — `sync 0, split 1, recipe 2, optional 3, install 4`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_config.py
"""Configuration objects and what they state about themselves (P1b spec §5)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id
from graphify.drupal.yaml_config import extract_drupal_config
from graphify.drupal.yaml_extract import extension_id


def _touch(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _site(root: Path) -> Path:
    _touch(root, "web/core/lib/Drupal.php", "<?php\n")
    _touch(root, "config/sync/core.extension.yml",
           "module:\n  node: 0\n  foo: 0\n  standard: 1000\ntheme:\n  olivero: 0\nprofile: standard\n")
    _touch(root, "web/modules/custom/foo/foo.info.yml", "name: Foo\ntype: module\n")
    return root


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


VIEW = """\
uuid: 1
status: true
dependencies:
  config:
    - node.type.page
  module:
    - node
    - views
  enforced:
    module:
      - foo
  content:
    - block_content:basic:abc
id: frontpage
label: Frontpage
"""


def test_a_synced_config_is_an_active_node(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(root, "config/sync/views.view.frontpage.yml", VIEW))
    cfg = next(n for n in result["nodes"] if n["id"] == config_id("views.view.frontpage"))
    assert cfg["type"] == "drupal_config"
    assert cfg["layer"] == "config"
    assert cfg["config_name"] == "views.view.frontpage"
    assert cfg["active"] is True
    assert cfg["status"] is True
    assert cfg["realm"] == "custom"
    assert cfg["content_dependencies"] == ["block_content:basic:abc"]


def test_dependencies_become_edges_and_enforced_wins(tmp_path):
    root = _site(tmp_path)
    text = VIEW.replace("  enforced:\n    module:\n      - foo\n",
                        "  enforced:\n    module:\n      - views\n")
    result = extract_drupal_config(_touch(root, "config/sync/views.view.frontpage.yml", text))
    rel = _rel(result)
    own = config_id("views.view.frontpage")
    assert (own, "config_depends_on", config_id("node.type.page")) in rel
    assert (own, "config_depends_on", extension_id("node")) in rel
    assert (own, "enforced_dependency", extension_id("views")) in rel
    assert (own, "config_depends_on", extension_id("views")) not in rel


def test_values_are_not_recorded(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/smtp.settings.yml", "smtp_host: mail.secret.example\nsmtp_port: 25\n"))
    assert "mail.secret.example" not in repr(result)


def test_shipped_config_is_defined_by_its_extension(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "web/modules/custom/foo/config/optional/foo.settings.yml", "a: 1\n"))
    cfg = result["nodes"][0]
    assert cfg["active"] is False
    assert cfg["install_mode"] == "optional"
    assert (extension_id("foo"), "defines_config", config_id("foo.settings")) in _rel(result)


def test_core_extension_installs_modules_and_themes(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(root / "config/sync/core.extension.yml")
    own = config_id("core.extension")
    installs = {e["target"]: e for e in result["edges"] if e["relation"] == "installs_extension"}
    assert set(installs) == {extension_id(n) for n in ("node", "foo", "standard", "olivero")}
    assert installs[extension_id("standard")]["weight"] == 1000
    assert all(e["source"] == own for e in installs.values())


def test_shipped_core_extension_installs_nothing(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "web/core/config/install/core.extension.yml", "module: {}\ntheme: {}\n"))
    assert not any(e["relation"] == "installs_extension" for e in result["edges"])


def test_split_entity_is_typed_and_lists_what_it_splits(tmp_path):
    root = _site(tmp_path)
    text = (
        "id: dev\nstatus: false\nfolder: ../config/splits/dev\n"
        "module:\n  devel: 0\ntheme: {}\n"
        "complete_list:\n  - devel.settings\n"
        "partial_list:\n  - system.site\n  - 'webform.*'\n"
    )
    result = extract_drupal_config(_touch(root, "config/sync/config_split.config_split.dev.yml", text))
    split = result["nodes"][0]
    own = config_id("config_split.config_split.dev")
    assert split["type"] == "drupal_config_split"
    assert split["folder"] == "../config/splits/dev"
    assert split["status"] is False
    assert split["split_patterns"] == ["webform.*"]
    kinds = {(e["relation"], e["target"], e.get("split_kind")) for e in result["edges"]}
    assert ("splits_extension", extension_id("devel"), None) in kinds
    assert ("splits_config", config_id("devel.settings"), "complete") in kinds
    assert ("splits_config", config_id("system.site"), "partial") in kinds
    assert all(e["source"] == own for e in result["edges"])


def test_domain_record_is_typed(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/domain.record.forms_public.yml", "id: forms_public\nhostname: x.example\n"))
    assert result["nodes"][0]["type"] == "drupal_domain"
    assert "x.example" not in repr(result)


def test_empty_config_is_still_a_config_object(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(root, "config/sync/empty.settings.yml", ""))
    assert [n["id"] for n in result["nodes"]] == [config_id("empty.settings")]


def test_malformed_config_reports_instead_of_raising(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(root, "config/sync/bad.yml", "a: [\n"))
    assert result["nodes"] == [] and "parse error" in result["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen pytest tests/test_drupal_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'config_id'`.

- [ ] **Step 3: Implement**

Append to `graphify/drupal/yaml_common.py`:

```python
def config_id(name: str) -> str:
    return make_id("drupal", "config", name)


def schema_id(type_: str) -> str:
    return make_id("drupal", "config_schema", type_)


def recipe_id(directory: str) -> str:
    return make_id("drupal", "recipe", directory)


def settings_id(site_file: str) -> str:
    return make_id("drupal", "settings", site_file)
```

```python
# graphify/drupal/yaml_config.py
"""Configuration objects: one node per config name, and what the file states.

What a view queries or a field formats is P6's. Here a config file contributes
its identity, where it is stored, whether it is active, what it depends on, who
ships it — and, for a few objects, one more fact: `core.extension` installs
extensions, a split entity lists what it splits.

Values never reach the graph (P1b spec §4). The node carries `status` when it is
a boolean and nothing else from the file body.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.config_stores import SPLIT_PREFIX, ConfigStore, config_name, config_store
from graphify.drupal.yaml_common import config_id, edge, load_drupal_yaml, node, recipe_id
from graphify.drupal.yaml_extract import extension_id

#: The collapse keeps the copy with the lowest rank (spec §6.1).
STORE_RANK = {"sync": 0, "split": 1, "recipe": 2, "optional": 3, "install": 4}

_EMPTY: dict[str, Any] = {"nodes": [], "edges": []}


class _Edges:
    """One relation per ordered pair; the first relation added for a pair wins."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: list[dict[str, Any]] = []
        self._pairs: set[tuple[str, str]] = set()

    def add(self, source: str, target: str, relation: str, line: int = 1, **extra: Any) -> None:
        if not source or not target or source == target or (source, target) in self._pairs:
            return
        self._pairs.add((source, target))
        self.items.append(edge(source, target, relation, path=self.path, line=line, **extra))


def _names(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int))]
    return []


def _dependency_targets(block: Any) -> list[tuple[str, str, str]]:
    """(dependency_kind, target id, raw name) for config/module/theme entries."""
    if not isinstance(block, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for kind in ("config", "module", "theme"):
        for name in _names(block.get(kind)):
            out.append((kind, config_id(name) if kind == "config" else extension_id(name), name))
    return out


def _node_type(name: str) -> str:
    if name.startswith(SPLIT_PREFIX):
        return "drupal_config_split"
    if name.startswith("domain.record."):
        return "drupal_domain"
    return "drupal_config"


def _config_object(path: Path, store: ConfigStore, name: str, data: dict) -> dict[str, Any]:
    own = config_id(name)
    extra: dict[str, Any] = {
        "config_name": name,
        "store": store.kind,
        "active": store.kind == "sync",
        "_rank": STORE_RANK[store.kind],
    }
    if store.kind in ("install", "optional"):
        extra["install_mode"] = store.kind
    if store.kind in ("sync", "split"):
        # The site's own export: project-owned whatever module it configures.
        extra["realm"] = "custom"
    if isinstance(data.get("status"), bool):
        extra["status"] = data["status"]
    deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    content = _names(deps.get("content"))
    if content:
        extra["content_dependencies"] = content

    type_ = _node_type(name)
    edges = _Edges(path)
    if type_ == "drupal_config_split":
        if isinstance(data.get("folder"), str):
            extra["folder"] = data["folder"]
        patterns = [n for key in ("complete_list", "partial_list")
                    for n in _names(data.get(key)) if "*" in n]
        if patterns:
            extra["split_patterns"] = patterns

    nodes = [node(own, name, type=type_, layer="config", path=path, line=1, **extra)]

    if store.kind in ("install", "optional") and store.owner:
        edges.add(extension_id(store.owner), own, "defines_config", install_mode=store.kind)
    if store.kind == "recipe" and store.owner:
        edges.add(recipe_id(store.owner), own, "defines_config", install_mode="recipe")

    # Enforced first: the same pair keeps the stronger relation.
    for kind, target, raw in _dependency_targets(deps.get("enforced")):
        edges.add(own, target, "enforced_dependency", dependency_kind=kind, target_name=raw)
    for kind, target, raw in _dependency_targets(deps):
        edges.add(own, target, "config_depends_on", dependency_kind=kind, target_name=raw)

    if name == "core.extension" and store.kind == "sync":
        for key in ("module", "theme"):
            block = data.get(key)
            if isinstance(block, dict):
                for ext, weight in block.items():
                    edges.add(own, extension_id(str(ext)), "installs_extension",
                              target_name=str(ext),
                              weight=weight if isinstance(weight, int) else None)

    if type_ == "drupal_config_split":
        for key in ("module", "theme"):
            for ext in _names(data.get(key)):
                edges.add(own, extension_id(ext), "splits_extension", target_name=ext)
        for key, kind in (("complete_list", "complete"), ("partial_list", "partial")):
            for target in _names(data.get(key)):
                if "*" not in target:
                    edges.add(own, config_id(target), "splits_config",
                              split_kind=kind, target_name=target)

    return {"nodes": nodes, "edges": edges.items}


def extract_drupal_config(path: Path) -> dict[str, Any]:
    store = config_store(path)
    if store is None:
        return dict(_EMPTY)
    if store.kind == "schema":
        return dict(_EMPTY)      # Task 4
    if path.name == "recipe.yml":
        return dict(_EMPTY)      # Task 5
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    return _config_object(path, store, config_name(path), data or {})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen pytest tests/test_drupal_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add graphify/drupal/yaml_config.py graphify/drupal/yaml_common.py tests/test_drupal_config.py
git commit -m "feat(drupal): extract configuration objects, dependencies and ownership"
```

---

### Task 3: Overrides — split patches, split copies, domains, languages

**Files:**
- Modify: `graphify/drupal/yaml_config.py`
- Test: `tests/test_drupal_config_overrides.py`

**Interfaces:**
- Consumes: Task 1–2.
- Produces: `overrides_config` edges with `override_source` ∈ `split | domain | language`, `keys: list[str]` (dotted key paths, sorted), `target_name`; domain edges may carry `alt_target` / `alt_target_name` for Task 9 to choose from; split copies carry `whole: True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_config_overrides.py
"""overrides_config: the value in the file is not the value in production (spec §5.3)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id
from graphify.drupal.yaml_config import extract_drupal_config


def _touch(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _site(root: Path) -> Path:
    _touch(root, "web/core/lib/Drupal.php", "<?php\n")
    _touch(root, "config/sync/core.extension.yml", "module: {}\ntheme: {}\n")
    _touch(root, "config/sync/config_split.config_split.prod.yml",
           "id: prod\nfolder: ../config/splits/prod\nstatus: false\n")
    return root


def _overrides(result):
    return [e for e in result["edges"] if e["relation"] == "overrides_config"]


def test_a_split_patch_overrides_keys_and_emits_no_node(tmp_path):
    root = _site(tmp_path)
    patch = _touch(root, "config/splits/prod/config_split.patch.domain.record.forms_public.yml",
                   "adding:\n  hostname: live.example\n  name: Live\n"
                   "removing:\n  hostname: dev.example\n")
    result = extract_drupal_config(patch)
    assert result["nodes"] == []
    [edge] = _overrides(result)
    assert edge["source"] == config_id("config_split.config_split.prod")
    assert edge["target"] == config_id("domain.record.forms_public")
    assert edge["override_source"] == "split"
    assert edge["keys"] == ["hostname", "name"]
    assert "live.example" not in repr(result) and "dev.example" not in repr(result)


def test_a_split_copy_is_a_config_node_and_a_whole_override(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(root, "config/splits/prod/devel.settings.yml", "a: 1\n"))
    cfg = result["nodes"][0]
    assert (cfg["id"], cfg["store"], cfg["active"]) == (config_id("devel.settings"), "split", False)
    [edge] = _overrides(result)
    assert edge["source"] == config_id("config_split.config_split.prod")
    assert edge["whole"] is True


def test_a_domain_config_file_overrides_the_named_config(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/domain.config.forms_public.system.site.yml", "name: Public\n"))
    [edge] = _overrides(result)
    assert edge["source"] == config_id("domain.record.forms_public")
    assert edge["target"] == config_id("system.site")
    assert edge["override_source"] == "domain"
    assert edge["keys"] == ["name"]
    assert "alt_target" not in edge
    # The file is itself a synced config object.
    assert result["nodes"][0]["id"] == config_id("domain.config.forms_public.system.site")


def test_a_domain_file_with_a_language_segment_offers_both_readings(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/domain.config.forms_public.fr.system.site.yml", "name: Public\n"))
    [edge] = _overrides(result)
    assert edge["target"] == config_id("fr.system.site")
    assert edge["alt_target"] == config_id("system.site")
    assert edge["alt_target_name"] == "system.site"


def test_a_language_override_emits_no_node(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/sync/language/fr/system.site.yml", "name: Site\nslogan: S\n"))
    assert result["nodes"] == []
    [edge] = _overrides(result)
    assert edge["source"] == config_id("language.entity.fr")
    assert edge["target"] == config_id("system.site")
    assert (edge["override_source"], edge["keys"]) == ("language", ["name", "slogan"])


def test_nested_keys_are_dotted_paths(tmp_path):
    root = _site(tmp_path)
    result = extract_drupal_config(_touch(
        root, "config/splits/prod/config_split.patch.system.mail.yml",
        "adding:\n  interface:\n    default: smtp\n"))
    assert _overrides(result)[0]["keys"] == ["interface.default"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen pytest tests/test_drupal_config_overrides.py -q`
Expected: FAIL — patches currently produce a config node and no override.

- [ ] **Step 3: Implement**

In `graphify/drupal/yaml_config.py`, add `import re` to the imports at the top of
the module (after `from __future__ import annotations`), then add after `_node_type`:

```python
#: `fr`, `pt-br`, `zh-hans`: a leading segment that may be a langcode.
_LANGCODE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})?")


def _key_paths(value: Any, prefix: str = "") -> list[str]:
    """Dotted leaf key paths; a list is a leaf. Values are never returned."""
    if not isinstance(value, dict):
        return [prefix] if prefix else []
    paths: list[str] = []
    for key, inner in value.items():
        if str(key) == "_core":
            continue
        dotted = f"{prefix}.{key}" if prefix else str(key)
        paths.extend(_key_paths(inner, dotted) if isinstance(inner, dict) else [dotted])
    return sorted(set(paths))


def _split_source(store: ConfigStore) -> str:
    return config_id(f"{SPLIT_PREFIX}{store.split}")


def _patch(path: Path, store: ConfigStore, name: str, data: dict) -> dict[str, Any]:
    target = name[len(PATCH_PREFIX):]
    keys = sorted(set(_key_paths(data.get("adding")) + _key_paths(data.get("removing"))))
    edges = _Edges(path)
    edges.add(_split_source(store), config_id(target), "overrides_config",
              override_source="split", keys=keys, target_name=target)
    return {"nodes": [], "edges": edges.items}


def _language_override(path: Path, store: ConfigStore, name: str, data: dict) -> dict[str, Any]:
    edges = _Edges(path)
    edges.add(config_id(f"language.entity.{store.language}"), config_id(name), "overrides_config",
              override_source="language", keys=_key_paths(data), target_name=name)
    return {"nodes": [], "edges": edges.items}


def _domain_override(edges: _Edges, name: str, data: dict) -> None:
    rest = name[len("domain.config."):]
    domain, _, target = rest.partition(".")
    if not domain or not target:
        return
    extra: dict[str, Any] = {}
    segment, _, after = target.partition(".")
    if after and _LANGCODE.fullmatch(segment):
        # `<domain>.<langcode>.<config>` or `<domain>.<config starting fr.>`:
        # the resolver keeps whichever target the corpus declares (Task 9).
        extra = {"alt_target": config_id(after), "alt_target_name": after}
    edges.add(config_id(f"domain.record.{domain}"), config_id(target), "overrides_config",
              override_source="domain", keys=_key_paths(data), target_name=target, **extra)
```

Change the import line to include `PATCH_PREFIX`:

```python
from graphify.drupal.config_stores import (
    PATCH_PREFIX,
    SPLIT_PREFIX,
    ConfigStore,
    config_name,
    config_store,
)
```

In `_config_object`, just before `return {"nodes": nodes, "edges": edges.items}`, add:

```python
    if store.kind == "split":
        edges.add(_split_source(store), own, "overrides_config",
                  override_source="split", whole=True, target_name=name)
    if name.startswith("domain.config.") and store.kind in ("sync", "split"):
        _domain_override(edges, name, data)
```

In `extract_drupal_config`, replace the last line with:

```python
    name = config_name(path)
    data = data or {}
    if store.language:
        return _language_override(path, store, name, data)
    if store.kind == "split" and name.startswith(PATCH_PREFIX):
        return _patch(path, store, name, data)
    return _config_object(path, store, name, data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --frozen pytest tests/test_drupal_config.py tests/test_drupal_config_overrides.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add graphify/drupal/yaml_config.py tests/test_drupal_config_overrides.py
git commit -m "feat(drupal): read split patches, split copies, domain and language overrides"
```

---

### Task 4: Schemas

**Files:**
- Create: `graphify/drupal/yaml_schema.py`
- Modify: `graphify/drupal/yaml_config.py` (route schema stores)
- Test: `tests/test_drupal_schema.py`

**Interfaces:**
- Consumes: `ConfigStore` (Task 1), `schema_id` (Task 2), `load_drupal_yaml`, `key_lines`, `node`, `edge`, `extension_id`.
- Produces: `extract_drupal_schema(path: Path, store: ConfigStore) -> dict`; nodes `drupal_config_schema` with `schema_type`, `pattern: bool`; edge `defines_schema` extension → schema.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_schema.py
"""config/schema/*.schema.yml: one node per schema type (spec §5.1)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import schema_id
from graphify.drupal.yaml_config import extract_drupal_config
from graphify.drupal.yaml_extract import extension_id

SCHEMA = """\
foo.settings:
  type: config_object
  mapping:
    enabled:
      type: boolean
field.field.*.*.*.third_party.foo:
  type: mapping
"""


def _write(root: Path) -> Path:
    (root / "web/modules/custom/foo").mkdir(parents=True)
    (root / "web/modules/custom/foo/foo.info.yml").write_text("name: Foo\n", encoding="utf-8")
    path = root / "web/modules/custom/foo/config/schema/foo.schema.yml"
    path.parent.mkdir(parents=True)
    path.write_text(SCHEMA, encoding="utf-8")
    return path


def test_each_top_level_key_is_a_schema_type(tmp_path):
    result = extract_drupal_config(_write(tmp_path))
    types = {n["schema_type"]: n for n in result["nodes"]}
    assert set(types) == {"foo.settings", "field.field.*.*.*.third_party.foo"}
    assert types["foo.settings"]["pattern"] is False
    assert types["field.field.*.*.*.third_party.foo"]["pattern"] is True
    assert types["foo.settings"]["type"] == "drupal_config_schema"
    assert types["foo.settings"]["source_location"] == "L1"


def test_the_extension_defines_its_schemas(tmp_path):
    result = extract_drupal_config(_write(tmp_path))
    assert (extension_id("foo"), "defines_schema", schema_id("foo.settings")) in {
        (e["source"], e["relation"], e["target"]) for e in result["edges"]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen pytest tests/test_drupal_schema.py -q`
Expected: FAIL — schema stores return empty results.

- [ ] **Step 3: Implement**

```python
# graphify/drupal/yaml_schema.py
"""config/schema/*.schema.yml — the types configuration is validated against.

Each top-level key is a type. A key containing `*` is a pattern that Drupal
matches segment by segment against config names; `schema_for` is drawn by the
resolver (Task 9), which sees every config node.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.config_stores import ConfigStore
from graphify.drupal.yaml_common import edge, key_lines, load_drupal_yaml, node, schema_id
from graphify.drupal.yaml_extract import extension_id


def extract_drupal_schema(path: Path, store: ConfigStore) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    owner_id = extension_id(store.owner) if store.owner else ""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for key in data:
        type_ = str(key)
        sid = schema_id(type_)
        line = lines.get(type_, 1)
        nodes.append(node(sid, type_, type="drupal_config_schema", layer="config",
                          path=path, line=line, schema_type=type_, pattern="*" in type_))
        if owner_id:
            edges.append(edge(owner_id, sid, "defines_schema", path=path, line=line))
    return {"nodes": nodes, "edges": edges}
```

In `extract_drupal_config` (`yaml_config.py`), replace `return dict(_EMPTY)      # Task 4` with:

```python
        from graphify.drupal.yaml_schema import extract_drupal_schema

        return extract_drupal_schema(path, store)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen pytest tests/test_drupal_schema.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add graphify/drupal/yaml_schema.py graphify/drupal/yaml_config.py tests/test_drupal_schema.py
git commit -m "feat(drupal): extract configuration schema types"
```

---

### Task 5: Recipes

**Files:**
- Create: `graphify/drupal/yaml_recipes.py`
- Modify: `graphify/drupal/yaml_config.py` (route `recipe.yml`)
- Test: `tests/test_drupal_recipes.py`

**Interfaces:**
- Consumes: Task 1–2 (`recipe_id`, `config_id`), `extension_id`.
- Produces: `extract_drupal_recipe(path: Path, store: ConfigStore) -> dict`; node `drupal_recipe` (`layer: extension`, `recipe_type`, `has_content`); edges `installs_extension`, `applies_recipe`, `imports_config` (`wildcard: True` when the list is `'*'`), `config_action` (`AMBIGUOUS` with `${`, attribute `actions`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_recipes.py
"""recipe.yml (spec §5.2)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id, recipe_id
from graphify.drupal.yaml_config import extract_drupal_config
from graphify.drupal.yaml_extract import extension_id

RECIPE = """\
name: 'Blog'
type: 'Content type'
recipes:
  - core/recipes/tags_taxonomy
  - basic_html_format_editor
install:
  - node
  - claro
config:
  strict: false
  import:
    node:
      - node.type.article
    claro: '*'
  actions:
    node.type.article:
      setDescription: '${description}'
    user.role.editor:
      grantPermissions:
        - 'create article content'
"""


def _write(root: Path, text: str = RECIPE) -> Path:
    path = root / "recipes/blog/recipe.yml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    (root / "recipes/blog/content").mkdir()
    return path


def _by_target(result):
    return {e["target"]: e for e in result["edges"]}


def test_recipe_node(tmp_path):
    result = extract_drupal_config(_write(tmp_path))
    [recipe] = result["nodes"]
    assert recipe["id"] == recipe_id("blog")
    assert (recipe["label"], recipe["layer"]) == ("Blog", "extension")
    assert recipe["recipe_type"] == "Content type"
    assert recipe["has_content"] is True


def test_recipe_edges(tmp_path):
    edges = _by_target(extract_drupal_config(_write(tmp_path)))
    assert edges[extension_id("node")]["relation"] == "installs_extension"
    assert edges[recipe_id("tags_taxonomy")]["relation"] == "applies_recipe"
    assert edges[recipe_id("basic_html_format_editor")]["relation"] == "applies_recipe"
    assert edges[config_id("user.role.editor")]["relation"] == "config_action"
    assert edges[config_id("user.role.editor")]["confidence"] == "EXTRACTED"


def test_an_action_with_input_is_ambiguous_and_wins_over_import(tmp_path):
    article = _by_target(extract_drupal_config(_write(tmp_path)))[config_id("node.type.article")]
    assert article["relation"] == "config_action"
    assert article["confidence"] == "AMBIGUOUS"
    assert article["actions"] == ["setDescription"]


def test_a_wildcard_import_targets_the_extension(tmp_path):
    """claro is both installed and imported whole: install wins the pair, wildcard is kept."""
    text = RECIPE.replace("  - claro\n", "")
    claro = _by_target(extract_drupal_config(_write(tmp_path, text)))[extension_id("claro")]
    assert (claro["relation"], claro["wildcard"]) == ("imports_config", True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen pytest tests/test_drupal_recipes.py -q`
Expected: FAIL — `recipe.yml` returns empty results.

- [ ] **Step 3: Implement**

```python
# graphify/drupal/yaml_recipes.py
"""recipe.yml — what a recipe installs, applies and does to configuration.

A recipe names recipes by directory (`basic_html_format_editor`) or by path
(`core/recipes/tags_taxonomy`); both reduce to the directory name, which is the
recipe id. `install:` does not say module or theme, so it is one relation.

Per pair, the more specific relation wins: install before import, action before
import. `config.actions` whose arguments use `${…}` depend on recipe input, so
the action is AMBIGUOUS.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.config_stores import ConfigStore
from graphify.drupal.yaml_common import config_id, edge, load_drupal_yaml, node, recipe_id
from graphify.drupal.yaml_extract import extension_id


def extract_drupal_recipe(path: Path, store: ConfigStore) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    data = data or {}
    rid = recipe_id(store.owner)
    extra: dict[str, Any] = {"has_content": (path.parent / "content").is_dir()}
    if isinstance(data.get("type"), str):
        extra["recipe_type"] = data["type"]
    nodes = [node(rid, str(data.get("name") or store.owner), type="drupal_recipe",
                  layer="extension", path=path, line=1, **extra)]

    edges: list[dict[str, Any]] = []
    pairs: set[str] = set()

    def add(target: str, relation: str, **attrs: Any) -> None:
        if target in pairs or target == rid:
            return
        pairs.add(target)
        edges.append(edge(rid, target, relation, path=path, line=1, **attrs))

    for ext in data.get("install") or []:
        if isinstance(ext, str) and ext:
            add(extension_id(ext), "installs_extension", target_name=ext)
    for ref in data.get("recipes") or []:
        if isinstance(ref, str) and ref:
            name = Path(ref).name
            add(recipe_id(name), "applies_recipe", target_name=name)

    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    actions = config.get("actions") if isinstance(config.get("actions"), dict) else {}
    for name, action in actions.items():
        add(config_id(str(name)), "config_action", target_name=str(name),
            confidence="AMBIGUOUS" if "${" in repr(action) else "EXTRACTED",
            actions=sorted(str(k) for k in action) if isinstance(action, dict) else [])
    imports = config.get("import") if isinstance(config.get("import"), dict) else {}
    for ext, names in imports.items():
        if names == "*":
            add(extension_id(str(ext)), "imports_config", target_name=str(ext), wildcard=True)
        elif isinstance(names, list):
            for name in names:
                add(config_id(str(name)), "imports_config", target_name=str(name))
    return {"nodes": nodes, "edges": edges}
```

In `extract_drupal_config`, replace `return dict(_EMPTY)      # Task 5` with:

```python
        from graphify.drupal.yaml_recipes import extract_drupal_recipe

        return extract_drupal_recipe(path, store)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen pytest tests/test_drupal_recipes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add graphify/drupal/yaml_recipes.py graphify/drupal/yaml_config.py tests/test_drupal_recipes.py
git commit -m "feat(drupal): extract recipes, their installs, imports and actions"
```

---

### Task 6: Seam — configuration before families, and `settings.php`

**Files:**
- Create: `graphify/drupal/yaml_settings.py`
- Modify: `graphify/drupal/families.py`, `graphify/drupal/register.py`
- Test: `tests/test_drupal_settings.py`, `tests/test_drupal_config_pipeline.py`, `tests/test_drupal_seam.py`

**Interfaces:**
- Consumes: `is_config_yaml` (Task 1), `extract_drupal_config` (Task 2), `settings_id`, `config_id`.
- Produces:
  - `families.drupal_extractor(path) -> Callable | None` — config first, then the family table
  - `families.is_drupal_file(path) -> bool`
  - `yaml_settings.is_settings_php(path) -> bool`, `yaml_settings.extract_drupal_settings(path) -> dict`
  - register: `classify_file`, `_is_graphable_source`, `_get_extractor` consult `drupal_extractor`; `_get_extractor` composes core's handler with `extract_drupal_settings` for settings files

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_drupal_settings.py
"""settings*.php `$config[…]` lines (spec §5.4, plan decision 1)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id, settings_id
from graphify.drupal.yaml_settings import extract_drupal_settings, is_settings_php

SETTINGS = """\
<?php
// $config['system.site']['name'] = 'commented';
# $config['system.site']['slogan'] = 'commented';
$config['config_split.config_split.prod']['status'] = FALSE;
if (getenv('ENV') === 'prod') {
  $config['config_split.config_split.prod']['status'] = TRUE;
}
$config['smtp.settings']['smtp_host'] = 'mail.secret.example';
$config['smtp.settings']['smtp_password'] = 'hunter2';
$config["system.logging"]["error_level"] = 'hide';
if ($x == $config['system.site']['name']) {}
"""


def _write(root: Path, name: str = "settings.php", text: str = SETTINGS) -> Path:
    path = root / "web/sites/default" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_only_site_settings_files_are_recognised(tmp_path):
    assert is_settings_php(_write(tmp_path))
    assert is_settings_php(_write(tmp_path, "settings.local.php"))
    assert not is_settings_php(tmp_path / "web/modules/custom/foo/settings.php")
    assert not is_settings_php(_write(tmp_path, "services.php"))


def test_assignments_become_ambiguous_overrides_with_keys(tmp_path):
    result = extract_drupal_settings(_write(tmp_path))
    [settings] = result["nodes"]
    assert settings["id"] == settings_id("default/settings.php")
    assert (settings["type"], settings["layer"], settings["realm"]) == (
        "drupal_settings", "config", "custom")
    edges = {e["target"]: e for e in result["edges"]}
    assert set(edges) == {config_id("config_split.config_split.prod"),
                          config_id("smtp.settings"), config_id("system.logging")}
    prod = edges[config_id("config_split.config_split.prod")]
    assert (prod["relation"], prod["confidence"]) == ("overrides_config", "AMBIGUOUS")
    assert (prod["override_source"], prod["keys"]) == ("settings_php", ["status"])
    assert prod["source_location"] == "L4"
    assert edges[config_id("smtp.settings")]["keys"] == ["smtp_host", "smtp_password"]


def test_values_never_leave_the_file(tmp_path):
    text = repr(extract_drupal_settings(_write(tmp_path)))
    assert "mail.secret.example" not in text and "hunter2" not in text and "hide" not in text


def test_a_file_without_config_lines_yields_nothing(tmp_path):
    assert extract_drupal_settings(_write(tmp_path, text="<?php\n$settings['x'] = 1;\n")) == {
        "nodes": [], "edges": []}
```

```python
# tests/test_drupal_config_pipeline.py
"""P1b through the real pipeline: detection, dispatch, secret screen."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id, settings_id


def _touch(root: Path, rel: str, text: str = "a: 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _site(root: Path) -> list[Path]:
    return [
        _touch(root, "web/core/lib/Drupal.php", "<?php\n"),
        _touch(root, "config/sync/core.extension.yml", "module:\n  node: 0\ntheme: {}\n"),
        _touch(root, "config/sync/system.site.yml", "name: Site\n"),
        _touch(root, "config/sync/simple_oauth.oauth2_token.settings.yml", "expiration: 1\n"),
        _touch(root, "config/sync/key.key.api_token.yml", "key_provider: config\n"),
        _touch(root, "web/modules/custom/menu_test/menu_test.info.yml", "name: M\ntype: module\n"),
        _touch(root, "web/modules/custom/menu_test/config/install/menu_test.links.action.yml",
               "langcode: en\ntitle: Original\n"),
        _touch(root, "web/sites/default/settings.php",
               "<?php\n$config['system.site']['name'] = 'Secret Name';\n"),
    ]


def test_config_files_are_code_and_escape_the_secret_screen(tmp_path):
    import graphify  # noqa: F401  (installs the seam)
    from graphify.detect import FileType, _is_sensitive, classify_file

    _site(tmp_path)
    for rel in ("config/sync/system.site.yml",
                "config/sync/simple_oauth.oauth2_token.settings.yml",
                "config/sync/key.key.api_token.yml"):
        assert classify_file(tmp_path / rel) == FileType.CODE, rel
        assert _is_sensitive(tmp_path / rel) is False, rel


def test_a_config_file_with_a_family_suffix_is_configuration(tmp_path):
    from graphify.extract import extract

    paths = _site(tmp_path)
    result = extract([p for p in paths if p.suffix == ".yml"],
                     cache_root=tmp_path / ".cache", root=tmp_path)
    types = {n["id"]: n.get("type") for n in result["nodes"]}
    assert types[config_id("menu_test.links.action")] == "drupal_config"
    assert not any(t == "drupal_local_action" for t in types.values())


def test_a_test_modules_config_is_neither_config_nor_a_family(tmp_path):
    from graphify.drupal.families import is_drupal_file

    _site(tmp_path)
    path = _touch(tmp_path, "web/core/modules/system/tests/modules/menu_test/config/install/"
                            "menu_test.links.action.yml", "langcode: en\n")
    assert not is_drupal_file(path)


def test_settings_php_keeps_its_php_nodes_and_gains_overrides(tmp_path):
    from graphify.extract import extract

    paths = _site(tmp_path)
    result = extract(paths, cache_root=tmp_path / ".cache", root=tmp_path)
    ids = {n["id"] for n in result["nodes"]}
    assert settings_id("default/settings.php") in ids
    assert any(n.get("source_file", "").endswith("settings.php") and
               n["id"] != settings_id("default/settings.php") for n in result["nodes"])
    assert (settings_id("default/settings.php"), "overrides_config", config_id("system.site")) in {
        (e["source"], e["relation"], e["target"]) for e in result["edges"]}
    assert "Secret Name" not in repr(result)
```

Append to `tests/test_drupal_seam.py`:

```python
def test_the_seam_dispatches_configuration(tmp_path):
    install()
    import graphify.extract as extract
    from graphify.drupal.yaml_config import extract_drupal_config

    (tmp_path / "config/sync").mkdir(parents=True)
    (tmp_path / "config/sync/core.extension.yml").write_text("module: {}\n", encoding="utf-8")
    path = tmp_path / "config/sync/system.site.yml"
    path.write_text("name: x\n", encoding="utf-8")
    assert extract._get_extractor(path) is extract_drupal_config
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --frozen pytest tests/test_drupal_settings.py tests/test_drupal_config_pipeline.py tests/test_drupal_seam.py -q`
Expected: FAIL — `ModuleNotFoundError: graphify.drupal.yaml_settings`; the seam does not know configuration.

- [ ] **Step 3: Implement `yaml_settings.py`**

```python
# graphify/drupal/yaml_settings.py
"""settings*.php — the `$config[…]` assignments that override configuration.

Read per line with a pattern, not parsed as PHP: conditions around an assignment
are not evaluated, so every edge is AMBIGUOUS (spec §5.4). The right-hand side
is never read; only the config name and the key path are kept.

Registered by composing core's PHP handler with this one (plan decision 1), so
`settings.php` keeps its PHP nodes and is cached and re-read like any file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graphify.drupal.yaml_common import config_id, edge, node, settings_id

_ASSIGNMENT = re.compile(
    r"""^\s*\$config\[\s*['"](?P<name>[^'"]+)['"]\s*\](?P<keys>(?:\s*\[\s*['"][^'"]+['"]\s*\])*)\s*=(?!=)"""
)
_KEY = re.compile(r"""\[\s*['"]([^'"]+)['"]\s*\]""")
_MAX_BYTES = 1024 * 1024


def is_settings_php(path: Path) -> bool:
    return (path.suffix == ".php" and path.name.startswith("settings")
            and path.parent.parent.name == "sites")


def extract_drupal_settings(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return {"nodes": [], "edges": []}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": []}

    found: dict[str, tuple[int, set[str]]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        keys = ".".join(_KEY.findall(match.group("keys")))
        first, seen = found.setdefault(match.group("name"), (number, set()))
        if keys:
            seen.add(keys)
    if not found:
        return {"nodes": [], "edges": []}

    sid = settings_id(f"{path.parent.name}/{path.name}")
    nodes = [node(sid, f"{path.parent.name}/{path.name}", type="drupal_settings",
                  layer="config", path=path, line=1, realm="custom")]
    edges = [
        edge(sid, config_id(name), "overrides_config", path=path, line=line,
             confidence="AMBIGUOUS", override_source="settings_php",
             keys=sorted(keys), target_name=name)
        for name, (line, keys) in found.items()
    ]
    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 4: Implement the dispatch in `families.py`**

Append to `graphify/drupal/families.py`:

```python
def drupal_extractor(path: Path) -> Callable[[Path], dict] | None:
    """Configuration first: a file inside a config store is configuration
    whatever its name ends in (P1b spec §3.2)."""
    from graphify.drupal.config_stores import in_config_directory, is_config_yaml

    if is_config_yaml(path):
        from graphify.drupal.yaml_config import extract_drupal_config

        return extract_drupal_config
    if in_config_directory(path):
        # Excluded configuration (a test module's) is still not a family file.
        return None
    return family_extractor(path)


def is_drupal_file(path: Path) -> bool:
    return drupal_extractor(path) is not None
```

- [ ] **Step 5: Implement the seam changes in `register.py`**

In `_patch_detect`, replace `from graphify.drupal.families import is_drupal_yaml` with
`from graphify.drupal.families import is_drupal_file`, and replace both
`if is_drupal_yaml(path):` with `if is_drupal_file(path):`.

In `_patch_extract`, replace `from graphify.drupal.families import family_extractor` with:

```python
    from graphify.drupal.families import drupal_extractor
    from graphify.drupal.yaml_settings import extract_drupal_settings, is_settings_php
```

and replace the `_dispatch` factory with:

```python
    def _dispatch(original):
        def _get_extractor(path: Path):
            handler = drupal_extractor(path)
            if handler is not None:
                return handler
            base = original(path)
            if is_settings_php(path):
                # Keep core's PHP nodes; add the `$config[…]` overrides.
                def settings_handler(p: Path, _base=base):
                    result = dict(_base(p)) if _base else {"nodes": [], "edges": []}
                    ours = extract_drupal_settings(p)
                    result["nodes"] = list(result.get("nodes", [])) + ours["nodes"]
                    result["edges"] = list(result.get("edges", [])) + ours["edges"]
                    return result
                return settings_handler
            return base
        return _get_extractor
```

Update the comment in `_graphable` that says "A file in the family table is an
extension declaration" to read "A family file or a configuration file (P1b) has
a fixed schema and is never a credential store; its values never reach the graph".

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run --frozen pytest tests/test_drupal_*.py -q`
Expected: PASS (all P1 tests included).

If `test_settings_php_keeps_its_php_nodes_and_gains_overrides` fails because
`settings.php` produced no PHP node, check whether the PHP grammar is installed
(`uv run --frozen python -c "import tree_sitter_php"`); the assertion on core's
node is the point of the composition and must not be dropped.

- [ ] **Step 7: Commit**

```bash
git add graphify/drupal/yaml_settings.py graphify/drupal/families.py graphify/drupal/register.py \
        tests/test_drupal_settings.py tests/test_drupal_config_pipeline.py tests/test_drupal_seam.py
git commit -m "feat(drupal): dispatch configuration before families and read settings.php overrides"
```

---

### Task 7: Ranked collapse

**Files:**
- Modify: `graphify/drupal/merge.py`
- Test: `tests/test_drupal_merge.py`, `tests/test_drupal_config_pipeline.py`

**Interfaces:**
- Consumes: `_rank` set by `yaml_config` (Task 2).
- Produces: `collapse_drupal_duplicates` sorts a group by `(_rank, source_file)`, fills the survivor's missing attributes from the other copies in that order, and removes `_rank` from every Drupal node.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_drupal_merge.py`:

```python
def test_the_lowest_rank_survives_and_gaps_are_filled():
    nodes = [
        _node("drupal_config_system_site", "web/core/modules/system/config/install/system.site.yml",
              _rank=4, install_mode="install", active=False),
        _node("drupal_config_system_site", "config/sync/system.site.yml", _rank=0, active=True),
    ]
    collapse_drupal_duplicates(nodes)
    [kept] = nodes
    assert kept["source_file"] == "config/sync/system.site.yml"
    assert kept["active"] is True
    assert kept["install_mode"] == "install"
    assert "_rank" not in kept


def test_rank_is_removed_from_single_nodes_too():
    nodes = [_node("drupal_config_a", "config/sync/a.yml", _rank=0)]
    collapse_drupal_duplicates(nodes)
    assert "_rank" not in nodes[0]
```

Append to `tests/test_drupal_config_pipeline.py`:

```python
def test_synced_and_shipped_copies_are_one_active_node(tmp_path):
    from graphify.extract import extract

    paths = [
        _touch(tmp_path, "config/sync/core.extension.yml", "module:\n  system: 0\n"),
        _touch(tmp_path, "config/sync/system.site.yml", "name: Site\n"),
        _touch(tmp_path, "web/core/modules/system/system.info.yml", "name: System\ntype: module\n"),
        _touch(tmp_path, "web/core/modules/system/config/install/system.site.yml", "name: ''\n"),
    ]
    result = extract(paths, cache_root=tmp_path / ".cache", root=tmp_path)
    [site] = [n for n in result["nodes"] if n["id"] == config_id("system.site")]
    assert site["active"] is True
    assert site["install_mode"] == "install"
    assert site["declared_in"] == ["config/sync/system.site.yml",
                                   "web/core/modules/system/config/install/system.site.yml"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --frozen pytest tests/test_drupal_merge.py tests/test_drupal_config_pipeline.py -q`
Expected: FAIL — the survivor lacks `install_mode` (no gap-fill yet) and still carries `_rank`.

- [ ] **Step 3: Implement**

In `graphify/drupal/merge.py`, replace the body of `collapse_drupal_duplicates` from `drop: set[int] = set()` to the end with:

```python
    drop: set[int] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        # Configuration ranks its stores (sync > split > recipe > optional >
        # install, P1b spec §6.1); every other type has no rank and keeps the
        # path order.
        group.sort(key=lambda n: (n.get("_rank", _NO_RANK), str(n.get("source_file", ""))))
        survivor = group[0]
        for other in group[1:]:
            for key, value in other.items():
                if key not in survivor and key != "_rank":
                    survivor[key] = value
        survivor["declared_in"] = sorted(
            {_relative(str(n.get("source_file", "")), root) for n in group}
        )
        drop.update(id(n) for n in group[1:])

    if drop:
        nodes[:] = [n for n in nodes if id(n) not in drop]
    for node in nodes:
        if node.get("_origin") == _ORIGIN:
            node.pop("_rank", None)
```

Add below `_ORIGIN`:

```python
#: Nodes without a store rank sort after every ranked one and among themselves by path.
_NO_RANK = 99
```

Update the docstring's second paragraph to: "The survivor is the copy with the lowest `_rank`, then the lowest `source_file`; attributes it lacks are taken from the other copies in that order."

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --frozen pytest tests/test_drupal_*.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add graphify/drupal/merge.py tests/test_drupal_merge.py tests/test_drupal_config_pipeline.py
git commit -m "feat(drupal): rank configuration stores in the id collapse"
```

---

### Task 8: Profiles install extensions

**Files:**
- Modify: `graphify/drupal/yaml_extract.py`
- Test: `tests/test_drupal_info_extract.py`

**Interfaces:**
- Produces: `installs_extension` profile → extension from a profile's `install:` list (spec §5.2 row `installs_extension`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_drupal_info_extract.py`:

```python
def test_a_profile_installs_what_it_lists(tmp_path):
    path = tmp_path / "web/core/profiles/standard/standard.info.yml"
    path.parent.mkdir(parents=True)
    path.write_text("name: Standard\ntype: profile\ninstall:\n  - node\n  - drupal:history\n",
                    encoding="utf-8")
    result = extract_drupal_info(path)
    installs = {e["target"] for e in result["edges"] if e["relation"] == "installs_extension"}
    assert installs == {extension_id("node"), extension_id("history")}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen pytest tests/test_drupal_info_extract.py -q`
Expected: FAIL — no `installs_extension` edges.

- [ ] **Step 3: Implement**

In `extract_drupal_info`, before the `base = data.get("base theme")` block, add:

```python
    # A profile's `install:` names extensions it enables, without saying which
    # kind (P1b spec §5.1, deviation 2).
    raw_install = data.get("install") or []
    if isinstance(raw_install, list):
        for raw in raw_install:
            add(_normalise_dependency(raw), "installs_extension")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen pytest tests/test_drupal_info_extract.py tests/test_drupal_pipeline.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add graphify/drupal/yaml_extract.py tests/test_drupal_info_extract.py
git commit -m "feat(drupal): read a profile's install list"
```

---

### Task 9: Resolver — P1b relations, schemas, domains, `installed`

**Files:**
- Modify: `graphify/drupal/resolvers.py`
- Test: `tests/test_drupal_config_resolver.py` (new), `tests/test_drupal_config_pipeline.py`

**Interfaces:**
- Consumes: every relation of Tasks 2–8; `config_id`, `schema_id`, `recipe_id`.
- Produces, in `resolve_missing_targets`, in this order:
  1. `_retarget_domain_overrides(all_nodes, all_edges)` — an `overrides_config` edge with `alt_target` keeps whichever of `target`/`alt_target` is declared; if neither, it keeps `target` and becomes `INFERRED`; `alt_target*` keys are removed.
  2. `_draw_schema_for(all_nodes, all_edges)` — one `schema_for` per config node: exact schema type `EXTRACTED`, else the most specific segment-wise pattern `INFERRED`.
  3. materialisation of missing targets — types for P1b relations derived from the target id prefix; `missing: True` on an extension that `core.extension` installs but nothing declares.
  4. `_mark_installed(all_nodes, all_edges)` — only when a node `config_id("core.extension")` exists.
- `_OWNER_RELATIONS` gains `defines_config`, `defines_schema`.
- `_materialise_owner` labels an owner from `config_store(source_file).owner` when the family table has no answer (a `config/install` file is not a family file).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_config_resolver.py
"""The P1b cross-file decisions (spec §6.3)."""
from __future__ import annotations

from graphify.drupal.resolvers import resolve_missing_targets
from graphify.drupal.yaml_common import config_id, recipe_id, schema_id
from graphify.drupal.yaml_extract import extension_id


def _cfg(name):
    return {"id": config_id(name), "type": "drupal_config", "config_name": name, "label": name}


def _schema(type_):
    return {"id": schema_id(type_), "type": "drupal_config_schema", "schema_type": type_,
            "pattern": "*" in type_, "label": type_}


def _edge(source, relation, target, **extra):
    return {"source": source, "relation": relation, "target": target,
            "source_file": "config/sync/x.yml", "confidence": "EXTRACTED", **extra}


def test_schema_for_prefers_the_exact_type_then_the_most_specific_pattern():
    nodes = [_cfg("system.site"), _cfg("field.field.node.page.body"), _cfg("views.view.x"),
             _schema("system.site"), _schema("field.field.*.*.*"),
             _schema("field.field.node.*.*"), _schema("views.view.*.*")]
    edges: list[dict] = []
    resolve_missing_targets([], nodes, edges)
    schema_for = {(e["source"], e["target"]): e["confidence"]
                  for e in edges if e["relation"] == "schema_for"}
    assert schema_for == {
        (schema_id("system.site"), config_id("system.site")): "EXTRACTED",
        (schema_id("field.field.node.*.*"), config_id("field.field.node.page.body")): "INFERRED",
    }


def test_a_domain_override_keeps_the_declared_reading():
    nodes = [_cfg("system.site"), _cfg("domain.record.d")]
    edges = [_edge(config_id("domain.record.d"), "overrides_config", config_id("fr.system.site"),
                   target_name="fr.system.site", alt_target=config_id("system.site"),
                   alt_target_name="system.site")]
    resolve_missing_targets([], nodes, edges)
    [edge] = edges
    assert edge["target"] == config_id("system.site")
    assert "alt_target" not in edge and "alt_target_name" not in edge
    assert not any(n.get("external") for n in nodes)


def test_undeclared_p1b_targets_get_the_type_their_id_names():
    nodes = [_cfg("views.view.x"), {"id": recipe_id("blog"), "type": "drupal_recipe"}]
    edges = [
        _edge(config_id("views.view.x"), "config_depends_on", config_id("node.type.page"),
              target_name="node.type.page"),
        _edge(recipe_id("blog"), "applies_recipe", recipe_id("gone"), target_name="gone"),
        _edge(recipe_id("blog"), "imports_config", extension_id("claro"), target_name="claro"),
    ]
    resolve_missing_targets([], nodes, edges)
    created = {n["id"]: n for n in nodes if n.get("external")}
    assert created[config_id("node.type.page")]["type"] == "drupal_config"
    assert created[config_id("node.type.page")]["layer"] == "config"
    assert created[recipe_id("gone")]["type"] == "drupal_recipe"
    assert created[extension_id("claro")]["type"] == "drupal_extension"


def test_installed_and_missing_follow_core_extension():
    core_ext = config_id("core.extension")
    nodes = [_cfg("core.extension"),
             {"id": extension_id("node"), "type": "drupal_module"},
             {"id": extension_id("devel"), "type": "drupal_module"}]
    edges = [_edge(core_ext, "installs_extension", extension_id("node"), target_name="node"),
             _edge(core_ext, "installs_extension", extension_id("gone"), target_name="gone")]
    resolve_missing_targets([], nodes, edges)
    by_id = {n["id"]: n for n in nodes}
    assert by_id[extension_id("node")]["installed"] is True
    assert by_id[extension_id("devel")]["installed"] is False
    assert by_id[extension_id("gone")]["installed"] is True
    assert by_id[extension_id("gone")]["missing"] is True


def test_without_core_extension_nothing_carries_installed():
    nodes = [{"id": extension_id("node"), "type": "drupal_module"}]
    resolve_missing_targets([], nodes, [])
    assert "installed" not in nodes[0]
```

Append to `tests/test_drupal_config_pipeline.py`:

```python
def test_nothing_dangles_in_a_small_site(tmp_path):
    from graphify.extract import extract

    paths = [
        _touch(tmp_path, "config/sync/core.extension.yml", "module:\n  node: 0\n  gone: 0\n"),
        _touch(tmp_path, "config/sync/views.view.x.yml",
               "dependencies:\n  config:\n    - node.type.absent\n  module:\n    - views\n"),
        _touch(tmp_path, "web/core/modules/node/node.info.yml", "name: Node\ntype: module\n"),
    ]
    result = extract(paths, cache_root=tmp_path / ".cache", root=tmp_path)
    ids = {n["id"] for n in result["nodes"]}
    assert [e for e in result["edges"] if e["source"] not in ids or e["target"] not in ids] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --frozen pytest tests/test_drupal_config_resolver.py tests/test_drupal_config_pipeline.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `graphify/drupal/resolvers.py`:

Add `"defines_config", "defines_schema"` to `_OWNER_RELATIONS`.

In `_materialise_owner`, replace the `"label": …` line with:

```python
        "label": _owner_name(source_file) or edge["source"],
```

and add above `_materialise_owner`:

```python
def _owner_name(source_file: Path) -> str:
    from graphify.drupal.config_stores import config_store
    from graphify.drupal.families import extension_owner

    owner = extension_owner(source_file)
    if owner:
        return owner
    store = config_store(source_file)
    return store.owner if store else ""
```

Add to `tests/test_drupal_config_resolver.py`:

```python
def test_a_shipped_configs_missing_owner_is_named_from_its_store(tmp_path):
    info_less = tmp_path / "web/core/config/install/core.menu.static_menu_link_overrides.yml"
    info_less.parent.mkdir(parents=True)
    info_less.write_text("definitions: {}\n", encoding="utf-8")
    nodes = [_cfg("core.menu.static_menu_link_overrides")]
    edges = [_edge(extension_id("core"), "defines_config",
                   config_id("core.menu.static_menu_link_overrides"),
                   source_file=str(info_less))]
    resolve_missing_targets([], nodes, edges)
    owner = next(n for n in nodes if n["id"] == extension_id("core"))
    assert owner["label"] == "core"
```

Add after `_RESOLVABLE`:

```python
#: P1b relations whose target may be a config, an extension or a recipe; the
#: target's type is read from its id (plan Task 9).
_BY_PREFIX_RELATIONS = frozenset({
    "config_depends_on", "enforced_dependency", "installs_extension", "splits_extension",
    "splits_config", "imports_config", "config_action", "overrides_config", "applies_recipe",
})

#: Longest first: a schema id also starts with `drupal_config_`.
_PREFIX_TYPES: tuple[tuple[str, str, str], ...] = (
    ("drupal_config_schema_", "drupal_config_schema", "config"),
    ("drupal_config_", "drupal_config", "config"),
    ("drupal_extension_", "drupal_extension", "extension"),
    ("drupal_recipe_", "drupal_recipe", "extension"),
)


def _type_from_id(target: str) -> tuple[str, str] | None:
    for prefix, node_type, layer in _PREFIX_TYPES:
        if target.startswith(prefix):
            return node_type, layer
    return None


def _retarget_domain_overrides(all_nodes: list[dict], all_edges: list[dict]) -> None:
    known = {n.get("id") for n in all_nodes}
    for edge in all_edges:
        alt = edge.pop("alt_target", None)
        alt_name = edge.pop("alt_target_name", None)
        if alt is None:
            continue
        if edge.get("target") in known:
            continue
        if alt in known:
            edge["target"], edge["target_name"] = alt, alt_name
        else:
            edge["confidence"] = "INFERRED"


def _segments_match(pattern: list[str], name: list[str]) -> bool:
    return len(pattern) == len(name) and all(p in ("*", n) for p, n in zip(pattern, name))


def _specificity(pattern: list[str]) -> tuple[int, int]:
    """More literal segments first, then a longer literal prefix."""
    literal = sum(1 for p in pattern if p != "*")
    prefix = next((i for i, p in enumerate(pattern) if p == "*"), len(pattern))
    return literal, prefix


def _draw_schema_for(all_nodes: list[dict], all_edges: list[dict]) -> None:
    schemas = [n for n in all_nodes if n.get("type") == "drupal_config_schema"]
    exact = {n.get("schema_type"): n["id"] for n in schemas if not n.get("pattern")}
    # Group patterns by their first segment so each config tests only its own family.
    patterns: dict[str, list[tuple[list[str], str]]] = {}
    for n in schemas:
        if n.get("pattern"):
            parts = str(n.get("schema_type", "")).split(".")
            patterns.setdefault(parts[0], []).append((parts, n["id"]))
    for config in all_nodes:
        name = config.get("config_name")
        if not isinstance(name, str) or not config.get("type", "").startswith("drupal_"):
            continue
        if name in exact:
            all_edges.append(_schema_edge(exact[name], config, "EXTRACTED"))
            continue
        parts = name.split(".")
        family = patterns.get(parts[0], []) + patterns.get("*", [])
        candidates = [(p, sid) for p, sid in family if _segments_match(p, parts)]
        if candidates:
            _, sid = max(candidates, key=lambda c: _specificity(c[0]))
            all_edges.append(_schema_edge(sid, config, "INFERRED"))


def _schema_edge(schema: str, config: dict, confidence: str) -> dict[str, Any]:
    return {
        "source": schema, "target": config["id"], "relation": "schema_for",
        "confidence": confidence, "_origin": "static_yaml",
        "source_file": config.get("source_file", ""), "source_location": "L1",
    }


_EXTENSION_TYPES = frozenset({"drupal_module", "drupal_theme", "drupal_profile", "drupal_extension"})


def _mark_installed(all_nodes: list[dict], all_edges: list[dict]) -> None:
    from graphify.drupal.yaml_common import config_id

    core_extension = config_id("core.extension")
    if not any(n.get("id") == core_extension for n in all_nodes):
        return
    installed = {e["target"] for e in all_edges
                 if e.get("relation") == "installs_extension" and e.get("source") == core_extension}
    for n in all_nodes:
        if n.get("type") in _EXTENSION_TYPES:
            n["installed"] = n.get("id") in installed
```

Change `resolve_missing_targets` to:

```python
def resolve_missing_targets(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Materialise an external node for every endpoint named but never declared.

    Targets of the relations in `_RESOLVABLE` and `_BY_PREFIX_RELATIONS`, and
    owners (sources) of the `declares_*` / `defines_*` relations whose file has no
    `*.info.yml`. Configuration decisions that need the whole corpus run first.
    """
    from graphify.drupal.yaml_common import config_id

    _retarget_domain_overrides(all_nodes, all_edges)
    _draw_schema_for(all_nodes, all_edges)

    known = {node.get("id") for node in all_nodes}
    type_of = {node.get("id"): node.get("type") for node in all_nodes}
    core_extension = config_id("core.extension")
    created: dict[str, dict[str, Any]] = {}

    for edge in all_edges:
        source = edge.get("source")
        relation = edge.get("relation")
        if (relation in _OWNER_RELATIONS and source
                and source not in known and source not in created):
            created[source] = _materialise_owner(edge)

        target = edge.get("target")
        if not target or target in known or target in created:
            continue
        if relation in _BY_PREFIX_RELATIONS:
            spec = _type_from_id(target)
        else:
            spec = _RESOLVABLE.get(relation)
        if spec is None:
            continue
        node_type, layer = spec
        if relation == "parent_link":
            node_type = type_of.get(source) or node_type
        created[target] = {
            "id": target,
            "label": edge.get("target_name") or target,
            # Not in the repository, so "concept" + external — the same shape
            # build.py gives its own external nodes, rather than a synthetic
            # source_file invented to satisfy the schema.
            "file_type": "concept",
            "type": node_type,
            "layer": layer,
            "realm": "unknown",
            "external": True,
            "_origin": "static_yaml",
            "source_file": edge.get("source_file", ""),
            "source_location": edge.get("source_location", "L1"),
        }
        if node_type == "drupal_config":
            created[target]["config_name"] = edge.get("target_name") or target
        if relation == "installs_extension" and source == core_extension:
            # The site installs an extension the code base does not contain.
            created[target]["missing"] = True

    all_nodes.extend(created.values())
    _mark_installed(all_nodes, all_edges)
```

Note: `_draw_schema_for` must not draw an edge to an external config materialised later — it runs before materialisation by design, so only declared configuration is matched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --frozen pytest tests/test_drupal_*.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add graphify/drupal/resolvers.py tests/test_drupal_config_resolver.py tests/test_drupal_config_pipeline.py
git commit -m "feat(drupal): resolve configuration targets, schemas, domain overrides and installed"
```

---

### Task 10: Acceptance on the reference corpus

**Files:**
- Modify: `tests/test_drupal_corpus.py`, `tests/test_drupal_pipeline.py`
- Modify: `docs/superpowers/specs/2026-09-23-drupal-p1b-configuration-design.md` (measured numbers, decisions 1–5)

**Interfaces:**
- Consumes: everything above.
- Produces: evidence.

- [ ] **Step 1: Measure the unknowns before asserting them**

```bash
uv run --frozen python -c "
from pathlib import Path
from collections import Counter
import graphify  # noqa
from graphify.drupal.families import is_drupal_file, drupal_extractor
from graphify.extract import extract
root = Path('/home/user/Projects/FormsRemote')
paths = sorted(p for p in list(root.glob('config/**/*.yml')) + list(root.glob('web/**/*.yml'))
               + list(root.glob('web/sites/*/settings*.php'))
               if 'node_modules' not in p.parts and (is_drupal_file(p) or p.suffix == '.php'))
errors = [p.name for p in paths if p.suffix == '.yml' and drupal_extractor(p)(p).get('error')]
print('errors', errors)
r = extract(paths, cache_root=Path('/tmp/p1b-cache'), root=root)
print('nodes', len(r['nodes']), 'edges', len(r['edges']))
print(Counter(n.get('type') for n in r['nodes'] if str(n.get('type','')).startswith('drupal')))
"
```

Record the `errors` list, the node total and the per-type counts. Every
discrepancy with the spec (§7, §8) is investigated before a number goes into a
test.

- [ ] **Step 2: Extend the corpus test**

In `tests/test_drupal_corpus.py`, add below `_family_files`:

```python
def _p1b_files() -> list[Path]:
    import graphify  # noqa: F401
    from graphify.drupal.families import is_drupal_file

    candidates = (list(CORPUS.glob("config/**/*.yml")) + list(CORPUS.glob("web/**/*.yml"))
                  + list(CORPUS.glob("web/sites/*/settings*.php")))
    return sorted(p for p in candidates if "node_modules" not in p.parts
                  and (p.suffix == ".php" or is_drupal_file(p)))


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


def test_p1b_criterion_5_installed_matches_core_extension(site_extraction):
    extensions = [n for n in site_extraction["nodes"]
                  if n.get("type") in ("drupal_module", "drupal_theme", "drupal_profile",
                                       "drupal_extension")]
    installed = [n for n in extensions if n.get("installed")]
    assert len(installed) == 215
    assert all("installed" in n for n in extensions)
    assert all(n.get("external") for n in extensions if n.get("missing"))


def test_p1b_criterion_6_integrity(site_extraction):
    from graphify.drupal.yaml_common import config_id

    ids = {n["id"] for n in site_extraction["nodes"]}
    assert [e["relation"] for e in site_extraction["edges"]
            if e["source"] not in ids or e["target"] not in ids] == []
    assert [n["id"] for n in site_extraction["nodes"] if _is_drupal(n)
            and not n["id"].startswith("drupal_")] == []
    # menu_test is a test module: its config is excluded, and it is not local actions.
    assert not [n for n in site_extraction["nodes"]
                if str(n.get("source_file", "")).endswith("menu_test.links.action.yml")]
    assert config_id("menu_test.links.action") not in ids


def _secret_values() -> set[str]:
    """Every scalar string of 8+ characters in the split patches and smtp.settings,
    minus anything that is also a config name (ids legitimately contain those)."""
    from graphify.drupal.yaml_common import load_drupal_yaml

    files = list(CORPUS.glob("config/splits/*/*.yml")) + [CORPUS / "config/sync/smtp.settings.yml"]
    names = {p.name[:-4] for p in CORPUS.glob("config/sync/*.yml")}
    values: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            for inner in value.values():
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)
        elif isinstance(value, (str, int)) and len(str(value)) >= 8:
            values.add(str(value))

    for path in files:
        if path.is_file():
            walk(load_drupal_yaml(path)[0])
    return {v for v in values if not any(v in n for n in names)}


def test_p1b_criterion_7_no_configuration_value_reaches_the_graph(site_extraction):
    import json

    dumped = json.dumps(site_extraction, default=str)
    values = _secret_values()
    assert values, "the scan needs something to look for"
    assert sorted(v for v in values if v in dumped) == []


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
```

Replace the assertion in `test_criterion_1_only_the_malformed_core_fixture_fails`
only if Step 1 measured additional malformed **configuration** fixtures; add a
separate `test_p1b_criterion_1_only_known_fixtures_fail` asserting the measured
list by name rather than changing P1's.

- [ ] **Step 3: Add the incremental checks (criterion 9)**

Append to `tests/test_drupal_pipeline.py`:

```python
def _site_corpus(root: Path) -> None:
    files = {
        "web/core/lib/Drupal.php": "<?php\n",
        "web/core/modules/node/node.info.yml": "name: Node\ntype: module\n",
        "web/core/modules/views/views.info.yml": "name: Views\ntype: module\n",
        "config/sync/core.extension.yml": "module:\n  node: 0\n  views: 0\ntheme: {}\n",
        "config/sync/config_split.config_split.prod.yml":
            "id: prod\nfolder: ../config/splits/prod\nstatus: false\n",
        "config/sync/user.settings.yml": "password_reset_timeout: 86400\n",
        "config/splits/prod/config_split.patch.user.settings.yml":
            "adding:\n  password_reset_timeout: 1\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_editing_a_split_patch_changes_only_its_override(tmp_path):
    _site_corpus(tmp_path)
    before = _run_cli(tmp_path)
    patch = tmp_path / "config/splits/prod/config_split.patch.user.settings.yml"
    patch.write_text("adding:\n  password_reset_timeout: 1\n  anonymous: x\n", encoding="utf-8")
    after = _run_cli(tmp_path)

    def overrides(graph):
        links = graph.get("links", graph.get("edges", []))
        return {(e["source"], e["target"], tuple(e.get("keys", []))) for e in links
                if e.get("relation") == "overrides_config"}

    assert _relations(before) == _relations(after)
    assert overrides(before) != overrides(after)


def test_removing_a_module_from_core_extension_flips_installed(tmp_path):
    from graphify.drupal.yaml_common import config_id

    _site_corpus(tmp_path)
    before = _run_cli(tmp_path)
    (tmp_path / "config/sync/core.extension.yml").write_text(
        "module:\n  node: 0\ntheme: {}\n", encoding="utf-8")
    after = _run_cli(tmp_path)

    assert _relations(before) - _relations(after) == {
        (config_id("core.extension"), "installs_extension", extension_id("views"))}
    assert _relations(after) - _relations(before) == set()

    def installed(graph):
        return next(n for n in graph["nodes"] if n["id"] == extension_id("views"))["installed"]

    assert installed(before) is True and installed(after) is False
```

- [ ] **Step 4: Run the corpus and pipeline tests**

Run: `uv run --frozen pytest tests/test_drupal_corpus.py tests/test_drupal_pipeline.py -q --tb=short`
Expected: PASS. Read every failure as information about the corpus or the
extractors, not as a number to edit — P1 found three bugs this way.

- [ ] **Step 5: Run the whole suite**

Run: `uv run --frozen pytest tests/ -q --tb=short`
Expected: green apart from the four `tests/test_ollama_retry_cap.py` failures.

- [ ] **Step 6: Confirm no upstream file moved**

Run: `git diff --name-only v8..HEAD | grep -v '^graphify/drupal/\|^tests/test_drupal_\|^docs/' ; git log 8de3571..HEAD --oneline --grep '^core:'`
Expected: both empty apart from the files P0 already carries on the allow-list.

- [ ] **Step 7: Record the measured numbers in the spec**

In `docs/superpowers/specs/2026-09-23-drupal-p1b-configuration-design.md`, add to
§7 the measured node total and per-type counts from Step 1, and a §6.5 "Decided
during planning" listing plan decisions 1–5 verbatim.

- [ ] **Step 8: Commit**

```bash
git add tests/test_drupal_corpus.py tests/test_drupal_pipeline.py \
        docs/superpowers/specs/2026-09-23-drupal-p1b-configuration-design.md
git commit -m "test(drupal): assert P1b's acceptance criteria against a real Drupal tree"
```

---

## Definition of done

- `uv run --frozen pytest tests/ -q` green apart from the four known `openai` failures.
- `tests/test_drupal_corpus.py` passes P1's and P1b's criteria against the reference corpus, measured through `extract()` with a fresh cache.
- No configuration value from the split patches or `smtp.settings` appears in the extraction output.
- No `core:` commit since `8de3571`.
- The graph carries roughly 10,600 Drupal nodes; the `realm: custom` slice stays under 5,000.
