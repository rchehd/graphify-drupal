# P2a Plugin Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Learn a Drupal site's plugin types from its plugin managers, emit YAML plugins of the learned types, and report what could not be recognised.

**Architecture:** A registry (`graphify/drupal/discovery.py`) is built at the start of every `detect()` from `*.services.yml`, PSR-4 and tree-sitter reads of manager classes (`graphify/drupal/php_classes.py`). Classification, dispatch, the cache, the report and `watch` read it through new wrappers in `graphify/drupal/register.py`. Plugin-type nodes are emitted by the manager class file's extraction; YAML plugins by `graphify/drupal/yaml_plugins.py`; the inventory by `graphify/drupal/inventory.py`.

**Tech Stack:** Python 3, `tree_sitter` + `tree_sitter_php` (already core dependencies), PyYAML through P1's `load_drupal_yaml`, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-23-drupal-p2a-plugin-discovery-design.md` — read the section each task names.

## Global Constraints

- graphify core (anything outside `graphify/drupal/`) is never edited. Drupal behaviour enters only through wrappers in `graphify/drupal/register.py`; every wrapped symbol is asserted and a missing one raises `DrupalSeamError`.
- Commits touch only `graphify/drupal/`, `tests/test_drupal_*.py`, `docs/superpowers/`. Commit messages are conventional (`feat(drupal): …`, `fix(drupal): …`, `test(drupal): …`, `docs(drupal): …`) and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests are flat `tests/test_drupal_*.py`; synthetic corpora are built in `tmp_path`; the reference corpus is `/home/user/Projects/FormsRemote` (`DRUPAL_CORPUS` env var), and corpus tests skip when it is absent.
- Node/edge helpers: `graphify.drupal.yaml_common.node(...)` / `edge(...)`; ids via `graphify.ids.make_id`. Plugin-type id `make_id("drupal", "plugin_type", <type>)`; plugin id `make_id("drupal", "plugin", <type>, <plugin_id>)`; both `layer: "plugin"`.
- Values never reach the graph: a `drupal_plugin` carries only `plugin_id`, `plugin_type`, `provider`, `class_name`, `deriver` beyond the universal attributes.
- One relation per ordered node pair (vocabulary §1.4).
- Nothing in the registry or inventory raises on bad input; it becomes an inventory entry.
- Measured numbers that differ from the plan are investigated and reported, never forced.
- Commands: `uv run --frozen pytest …`, `uv run --frozen ruff check graphify/drupal tests/test_drupal_*.py`. An `rtk` shell hook reformats `ls`/`grep`/`find`; use `/usr/bin/grep`, `/usr/bin/find` or python for exact lists.
- Four `tests/test_ollama_retry_cap.py` failures (missing `openai`) are pre-existing and unrelated.

## File Structure

| File | Responsibility |
|---|---|
| `graphify/drupal/php_classes.py` (new) | tree-sitter read of one PHP class file into a `PhpClass` record |
| `graphify/drupal/discovery.py` (new) | web root, extensions, PSR-4, services → managers, type facts, `Registry` (JSON round trip), process state, `extract_plugin_types` |
| `graphify/drupal/yaml_plugins.py` (new) | YAML plugins of learned families |
| `graphify/drupal/inventory.py` (new) | `drupal-inventory.json` and the report section |
| `graphify/drupal/families.py` | learned-family classification and dispatch |
| `graphify/drupal/yaml_links.py`, `yaml_assets.py` | `plugin_of_type` on P1 link and breakpoint nodes |
| `graphify/drupal/resolvers.py` | endpoints of the new relations |
| `graphify/drupal/register.py` | wrappers: `detect.detect`, `_get_extractor` (manager files), `extract()` (registry diff), `cache.load_cached`, `report.generate`, `watch._batch_triggers_rebuild`, `watch._has_non_code` |

---

### Task 1: Read a PHP class with tree-sitter

**Files:**
- Create: `graphify/drupal/php_classes.py`
- Test: `tests/test_drupal_php_classes.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Arg:
      kind: str      # "string" | "class" | "other"
      value: str     # string literal text, or resolved FQCN for `X::class`, or "" for other

  @dataclass(frozen=True)
  class Discovery:
      cls: str       # resolved FQCN of the `new X(...)` class
      args: tuple[Arg, ...]

  @dataclass(frozen=True)
  class PhpClass:
      fqcn: str
      kind: str                      # "class" | "abstract" | "interface"
      line: int                      # 1-based line of the declaration
      extends: tuple[str, ...]       # resolved FQCNs (class: 0-1, interface: 0-n)
      implements: tuple[str, ...]    # resolved FQCNs
      construct_args: tuple[Arg, ...] | None   # args of `parent::__construct(...)` in __construct, None if absent
      has_get_discovery: bool
      discoveries: tuple[Discovery, ...]       # every `new …Discovery…(...)` inside getDiscovery()
      alter_info: str | None                   # literal of `$this->alterInfo('x')` anywhere in the class

  def read_php_class(path: Path) -> PhpClass | None: ...   # first class/interface in the file; None if none; raises nothing
  def resolve_name(name: str, namespace: str, uses: dict[str, str]) -> str: ...
  ```

Name resolution: a leading `\` is fully qualified; otherwise the first segment is looked up in `use` aliases (`use A\B;` aliases `B`, `use A\B as C;` aliases `C`), else the name is prefixed with the file's namespace. Returned FQCNs have no leading `\`. A string argument naming a class (`'Drupal\foo\Annotation\Foo'`) is returned as `kind="string"` with its text; callers decide. `new` inside `getDiscovery()` counts only when the class's short name contains `Discovery` (`YamlDiscovery`, `YamlDiscoveryDecorator`, `YamlDirectoryDiscovery`, `AnnotatedClassDiscovery`, `ContainerDerivativeDiscoveryDecorator`, …). Nested `new` expressions (a decorator wrapping a discovery) all count, outermost first. Files over 1 MiB, unreadable files and files tree-sitter cannot parse return `None`.

Parser: `tree_sitter.Parser(tree_sitter.Language(tree_sitter_php.language_php()))`, created once per process.

- [ ] **Step 1: Write the failing tests** in `tests/test_drupal_php_classes.py`:

```python
from pathlib import Path

from graphify.drupal.php_classes import Arg, read_php_class, resolve_name

D11_MANAGER = r"""<?php
namespace Drupal\foo;

use Drupal\Core\Plugin\DefaultPluginManager;
use Drupal\foo\Attribute\Foo as FooAttribute;

/**
 * A manager.
 */
class FooManager extends DefaultPluginManager implements FooManagerInterface {

  public function __construct(\Traversable $namespaces, $cache, $module_handler) {
    parent::__construct(
      'Plugin/Foo',            // a comment in between
      $namespaces,
      $module_handler,
      FooInterface::class,
      FooAttribute::class,
      'Drupal\foo\Annotation\Foo'
    );
    $this->alterInfo('foo_info');
  }
}
"""

YAML_MANAGER = r"""<?php
namespace Drupal\bar;

use Drupal\Core\Plugin\Discovery\YamlDiscoveryDecorator;
use Drupal\Core\Plugin\Discovery\YamlDiscovery;

class BarManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  protected function getDiscovery() {
    if (!$this->discovery) {
      $discovery = new AnnotatedClassDiscovery($this->subdir, $this->namespaces);
      $discovery = new YamlDiscoveryDecorator($discovery, 'bar', $this->moduleHandler->getModuleDirectories());
      $this->discovery = new ContainerDerivativeDiscoveryDecorator($discovery);
    }
    return $this->discovery;
  }
}
"""

INTERFACE = r"""<?php
namespace Drupal\Core\Menu;
use Drupal\Component\Plugin\PluginManagerInterface;
interface MenuLinkManagerInterface extends PluginManagerInterface, \Countable {}
"""

ABSTRACT = "<?php\nnamespace A;\nabstract class Base extends \\Drupal\\Core\\Plugin\\DefaultPluginManager {}\n"


def _write(tmp_path: Path, text: str, name: str = "X.php") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_d11_constructor_is_read_with_names_resolved(tmp_path):
    cls = read_php_class(_write(tmp_path, D11_MANAGER))
    assert cls.fqcn == "Drupal\\foo\\FooManager"
    assert cls.kind == "class"
    assert cls.line == 10
    assert cls.extends == ("Drupal\\Core\\Plugin\\DefaultPluginManager",)
    assert cls.implements == ("Drupal\\foo\\FooManagerInterface",)
    assert cls.construct_args == (
        Arg("string", "Plugin/Foo"),
        Arg("other", ""),
        Arg("other", ""),
        Arg("class", "Drupal\\foo\\FooInterface"),
        Arg("class", "Drupal\\foo\\Attribute\\Foo"),
        Arg("string", "Drupal\\foo\\Annotation\\Foo"),
    )
    assert cls.alter_info == "foo_info"
    assert cls.has_get_discovery is False
    assert cls.discoveries == ()


def test_get_discovery_lists_every_discovery_outermost_first(tmp_path):
    cls = read_php_class(_write(tmp_path, YAML_MANAGER))
    assert cls.extends == ("Drupal\\Core\\Plugin\\DefaultPluginManager",)
    assert cls.has_get_discovery is True
    assert [d.cls for d in cls.discoveries] == [
        "Drupal\\bar\\AnnotatedClassDiscovery",
        "Drupal\\Core\\Plugin\\Discovery\\YamlDiscoveryDecorator",
        "Drupal\\bar\\ContainerDerivativeDiscoveryDecorator",
    ]
    assert cls.discoveries[1].args[1] == Arg("string", "bar")
    assert cls.construct_args is None


def test_an_interface_lists_all_its_parents(tmp_path):
    cls = read_php_class(_write(tmp_path, INTERFACE))
    assert cls.kind == "interface"
    assert cls.extends == ("Drupal\\Component\\Plugin\\PluginManagerInterface", "Countable")


def test_an_abstract_class_is_marked(tmp_path):
    assert read_php_class(_write(tmp_path, ABSTRACT)).kind == "abstract"


def test_unparsable_or_classless_files_return_none(tmp_path):
    assert read_php_class(_write(tmp_path, "<?php\nfunction f() {}\n")) is None
    assert read_php_class(tmp_path / "missing.php") is None


def test_resolve_name():
    uses = {"B": "A\\B", "C": "X\\Y"}
    assert resolve_name("\\Q\\R", "N", uses) == "Q\\R"
    assert resolve_name("B\\Sub", "N", uses) == "A\\B\\Sub"
    assert resolve_name("C", "N", uses) == "X\\Y"
    assert resolve_name("Local", "N", uses) == "N\\Local"
    assert resolve_name("Local", "", uses) == "Local"
```

- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_drupal_php_classes.py -q` — expect `ModuleNotFoundError: graphify.drupal.php_classes`.
- [ ] **Step 3: Implement** `graphify/drupal/php_classes.py` to the interface above. Walk the tree-sitter tree: `namespace_definition`, `namespace_use_declaration` (including grouped `use A\{B, C}`), the first `class_declaration` / `interface_declaration` (abstract via its `abstract_modifier` child), `base_clause`, `class_interface_clause`, the `method_declaration` named `__construct` and within it the `scoped_call_expression` `parent::__construct` arguments, the method `getDiscovery` and every `object_creation_expression` inside it (pre-order), and any `member_call_expression` `$this->alterInfo(<string>)`. Use the parser's node types; print a parse of the fixtures (`tree.root_node` S-expression) to confirm the names before coding. String literals: `string` / `encapsed_string` content without quotes, PHP escapes `\\` → `\` in single-quoted strings. Class constants: `class_constant_access_expression` whose right side is `class`.
- [ ] **Step 4: Run** the tests — expect all pass. Run `uv run --frozen ruff check graphify/drupal tests/test_drupal_*.py`.
- [ ] **Step 5: Commit** `feat(drupal): read PHP class declarations with tree-sitter`.

---

### Task 2: The registry

**Files:**
- Create: `graphify/drupal/discovery.py`
- Test: `tests/test_drupal_discovery.py`

**Interfaces:**
- Consumes: `read_php_class`, `PhpClass`, `Arg`, `Discovery` (Task 1); `graphify.drupal.yaml_common.load_drupal_yaml`; `graphify.drupal.paths.is_drupal_info_yaml`, `extension_machine_name`.
- Produces:
  ```python
  DEFAULT_PLUGIN_MANAGER = "Drupal\\Core\\Plugin\\DefaultPluginManager"
  PLUGIN_MANAGER_INTERFACE = "Drupal\\Component\\Plugin\\PluginManagerInterface"

  @dataclass(frozen=True)
  class PluginType:
      plugin_type: str
      manager_class: str
      class_file: str          # absolute POSIX path
      line: int
      owner: str               # extension machine name ("core" for core/lib and core.services.yml)
      registered: bool
      manager_service: str = ""
      discovery: str = "annotation"   # annotation|attribute|yaml|mixed|dynamic
      subdir: str = ""
      interface: str = ""
      annotation_class: str = ""
      attribute_class: str = ""
      yaml_name: str = ""
      alter_hook: str = ""
      deferred_to: str = ""

  @dataclass
  class Registry:
      web_root: str | None
      types: dict[str, PluginType]              # by plugin_type
      unresolved: list[dict[str, str]]          # {"class", "file", "reason"}
      extensions: dict[str, str]                # machine name -> absolute dir
      root_yaml: list[str]                      # absolute paths of <ext>.<name>.yml beside <ext>.info.yml (and core/core.*.yml)
      def by_yaml_name(self) -> dict[str, PluginType]: ...        # non-deferred types with a yaml_name
      def by_class_file(self) -> dict[str, list[PluginType]]: ...
      def to_json(self) -> dict: ...
      @classmethod
      def from_json(cls, data: dict) -> "Registry": ...

  def find_web_root(scan_root: Path) -> Path | None: ...
  def build_registry(scan_root: Path) -> Registry: ...
  def type_id(plugin_type: str) -> str: ...     # make_id("drupal", "plugin_type", plugin_type)
  ```

Rules (spec §5.1, verbatim requirements):

1. `find_web_root`: scan root if `core/lib/Drupal.php` exists under it; else `web/` or `docroot/` child that has it; else the nearest ancestor that has it; else `None`. With `None`, `build_registry` scans `scan_root` and records `{"class": "", "file": "", "reason": "no_drupal_core"}` in `unresolved`.
2. Walk the base directory once (`os.walk`, pruning `vendor`, `node_modules`, `tests`, `Tests`, and directories starting with `.`): collect `*.info.yml` (extensions; `core` → `<web_root>/core`), `*.services.yml`, `src/**/*Manager.php`, `*ServiceProvider.php`, and `root_yaml` (a file `<ext>.<name>.yml` whose directory contains `<ext>.info.yml`, or `core/core.<name>.yml`; not the info file itself; not dot-files).
3. PSR-4: `Drupal\<ext>\` → `<dir>/src`; `Drupal\Core\` → `core/lib/Drupal/Core`; `Drupal\Component\` → `core/lib/Drupal/Component`; longest prefix wins.
4. Service class: `class:` value, else the id when it contains `\`, else the same-file `parent:` service's class. Skip `_defaults`, `_instanceof`, `abstract: true` services and aliases (string values / `alias:`).
5. Manager test (memoised per FQCN, depth ≤ 16): `DefaultPluginManager` reached through `extends`, or `PluginManagerInterface` reached through `implements` of the class or any ancestor, or through any interface's `extends` chain. `abstract` and `interface` kinds are never managers themselves.
6. Owner: the services file's extension (`<ext>.services.yml` → `<ext>`, `core.services.yml` → `core`); for a second-net manager, the extension whose directory contains the class file (longest match), `core` under `core/lib`.
7. `plugin_type`: service id without leading `plugin.manager.`; a second-net manager uses `class:<FQCN>`. Two services with the same class give two types.
8. Facts from the class and its ancestor classes (nearest first): `construct_args` of the nearest class that has one — arg 0 string → `subdir`, arg 3 `class`/string → `interface`, then remaining args: a `class`/string whose FQCN has a segment `Attribute` → `attribute_class`, one with segment `Annotation` → `annotation_class`; `alter_info` nearest first; `getDiscovery()` nearest first.
9. `discovery`: with no `getDiscovery()` → `attribute` if `attribute_class` and not `annotation_class`, `annotation` if only `annotation_class`, `mixed` if both, `annotation` if neither (DefaultPluginManager's default). With `getDiscovery()`: `YamlDiscovery` arg 0 string or `YamlDiscoveryDecorator` arg 1 string → `yaml_name`; kinds seen = {`yaml`} plus `annotation`/`attribute` when (matched by short class name) an `AnnotatedClassDiscovery`/`AttributeClassDiscovery`/`AttributeDiscoveryWithAnnotations` is constructed; one kind → it, several → `mixed`; a `getDiscovery()` whose discoveries include none of these, or whose yaml name is not a literal → `dynamic` and an `unresolved` entry `dynamic_discovery`.
10. Deferred: `yaml_name == "component"` or a manager class ending `\ComponentPluginManager` → `deferred_to="P5"`; a `YamlDirectoryDiscovery` or manager class ending `\MigrationPluginManager` → `deferred_to="P6"` (not `dynamic`).
11. `unresolved` reasons: `not_a_service` (every second-net manager — it is still a type), `psr4_unresolved` (a service class starting `Drupal\` whose file is not found), `service_provider_alter` (a `*ServiceProvider.php` whose text contains `function alter(` and `->setClass(`; `class` is the provider's FQCN), `dynamic_discovery`, `parse_error` (a services file with a load error, or a manager-candidate file `read_php_class` returns `None` for).

- [ ] **Step 1: Write failing tests** in `tests/test_drupal_discovery.py`. Build a synthetic site in `tmp_path` with a helper:

```python
from pathlib import Path

from graphify.drupal.discovery import build_registry, find_web_root, type_id

CORE_DPM = r"""<?php
namespace Drupal\Core\Plugin;
use Drupal\Component\Plugin\PluginManagerInterface;
class DefaultPluginManager implements PluginManagerInterface {}
"""
PMI = "<?php\nnamespace Drupal\\Component\\Plugin;\ninterface PluginManagerInterface {}\n"


def _site(root: Path, files: dict[str, str]) -> Path:
    base = {
        "web/core/lib/Drupal.php": "<?php\n",
        "web/core/lib/Drupal/Core/Plugin/DefaultPluginManager.php": CORE_DPM,
        "web/core/lib/Drupal/Component/Plugin/PluginManagerInterface.php": PMI,
        "web/core/core.services.yml": "services: {}\n",
    }
    for rel, text in {**base, **files}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root
```

Cases (one test each; assert exact `PluginType` fields):
- a custom module `foo` with `foo.services.yml` declaring `plugin.manager.foo: {class: Drupal\foo\FooManager, parent: default_plugin_manager}` and the D11 manager from Task 1 at `web/modules/custom/foo/src/FooManager.php` → type `foo`, `owner="foo"`, `registered=True`, `manager_service="plugin.manager.foo"`, `subdir="Plugin/Foo"`, `interface="Drupal\foo\FooInterface"`, `attribute_class="Drupal\foo\Attribute\Foo"`, `annotation_class="Drupal\foo\Annotation\Foo"`, `discovery="mixed"`, `alter_hook="foo_info"`, `class_file` absolute, `line` 10.
- a manager reached through an intermediate class (`FooManager extends BaseManager`, `BaseManager extends DefaultPluginManager`, constructor only in `BaseManager`) → type found, facts inherited.
- an interface-only manager (`MenuLinkManager implements MenuLinkManagerInterface`, the interface extends `PluginManagerInterface`) with `getDiscovery()` building `new YamlDiscovery('links.menu', …)` → `discovery="yaml"`, `yaml_name="links.menu"`.
- the YAML_MANAGER decorator shape → `discovery="mixed"`, `yaml_name="bar"`.
- a service whose id is the FQCN and has no `class:`; a service taking its class from a same-file `parent:`.
- a `src/Plugin/QuxManager.php` manager that no services file names → type `class:Drupal\qux\Plugin\QuxManager`, `registered=False`, and `unresolved` has `not_a_service` for it.
- a service `Drupal\nope\Missing` → `unresolved` `psr4_unresolved`; a `Symfony\…` class → nothing.
- a `FooServiceProvider.php` with `public function alter(ContainerBuilder $c) { $c->getDefinition('x')->setClass(Y::class); }` → `service_provider_alter`.
- a manager whose `getDiscovery()` returns `$this->buildDiscovery()` → `discovery="dynamic"` and `dynamic_discovery`.
- SDC: `ComponentPluginManager` class name → `deferred_to="P5"`; a manager with `new YamlDirectoryDiscovery($dirs, 'migrate')` → `deferred_to="P6"`, discovery not `dynamic`, no unresolved entry.
- `find_web_root`: returns `<tmp>/web` from `<tmp>`, from `<tmp>/web/modules/custom/foo`, and `None` for an empty dir; with `None`, `unresolved` holds `no_drupal_core`.
- `root_yaml` lists `web/modules/custom/foo/foo.bar.yml` and `web/core/core.link_relation_types.yml`, not `foo.info.yml`, not `.gitlab-ci.yml`, not `web/modules/custom/foo/config/x.yml`.
- `Registry.from_json(r.to_json()) == r`; `type_id("menu.link") == "drupal_plugin_type_menu_link"`.

- [ ] **Step 2: Run** — expect import failure.
- [ ] **Step 3: Implement** `discovery.py` to the rules above.
- [ ] **Step 4: Run** the tests and ruff; all pass.
- [ ] **Step 5: Measure on the corpus** (not a test yet): `uv run --frozen python -c "import time; from pathlib import Path; from graphify.drupal.discovery import build_registry; t=time.time(); r=build_registry(Path('/home/user/Projects/FormsRemote')); print(round(time.time()-t,2), len(r.types), sum(t.registered for t in r.types.values()), sorted({u['reason'] for u in r.unresolved}), sorted(r.by_yaml_name()))"`. Expected: under 5 s; 117 types plus the second-net ones (spec §2: `MetatagViewsCachePluginManager`, `backup_migrate…\PluginManager`, `MigrationPluginManager` of `migrate_drupal` if a class of that name exists outside services); `by_yaml_name` includes `ckeditor5`, `layouts`, `link_relation_types`, `field_type_categories`, `config_translation`, `icons`, `oauth2_scopes`, `entity_print_export_types`, `modeler_api.contexts`, `modeler_api.template_tokens`, `modeler_api.dependencies`, `notification_senders`, `notification_messages_factory`, `help_topics`, `links.menu`, `links.task`, `links.action`, `links.contextual`, `breakpoints`; does **not** include `routing`, `permissions`, `migrate_drupal`. Record the numbers in the report; explain every difference.
- [ ] **Step 6: Commit** `feat(drupal): learn plugin types from plugin managers`.

---

### Task 3: The registry in the pipeline

**Files:**
- Modify: `graphify/drupal/discovery.py` (process state), `graphify/drupal/register.py`
- Test: `tests/test_drupal_discovery_seam.py`

**Interfaces:**
- Consumes: `build_registry`, `Registry` (Task 2).
- Produces:
  ```python
  ENV_VAR = "GRAPHIFY_DRUPAL_DISCOVERY"
  def out_dir(root: Path, cache_root: Path | None = None) -> Path: ...   # graphify-out for this run, core's resolution (graphify.paths.GRAPHIFY_OUT; absolute → as is)
  def set_current(registry: Registry | None, previous: Registry | None = None) -> None: ...
  def current_registry() -> Registry | None: ...     # in-process, else loads ENV_VAR's file once per process (cached by path+mtime); None if neither
  def previous_registry() -> Registry | None: ...    # the prior run's registry read before overwrite, in-process only
  def prepare_run(root: Path, cache_root: Path | None = None) -> Registry: ...
      # reads <out>/drupal-discovery.json as previous (ignored if unreadable), builds the registry,
      # writes <out>/drupal-discovery.json (sorted keys, indent 1), sets ENV_VAR to its absolute path,
      # calls set_current(new, previous); returns new
  ```
- Seam: new patcher `_patch_detect` also wraps `detect.detect(root, *, …, cache_root=None, …)` so that `prepare_run(root, cache_root)` runs before the original. Assert `detect.detect` exists (`DrupalSeamError`).

- [ ] **Step 1: Failing tests:** (a) running `graphify.detect.detect(site_root)` on the Task-2-style site writes `site_root/graphify-out/drupal-discovery.json` containing type `foo`, and sets `os.environ[ENV_VAR]` (use `monkeypatch.delenv(ENV_VAR, raising=False)` before, and restore); (b) a second `detect` call makes `previous_registry()` equal the first registry; (c) in a fresh subprocess with only `ENV_VAR` set (`subprocess.run([sys.executable, "-c", "import graphify; from graphify.drupal.discovery import current_registry; print(sorted(current_registry().types))"], env=…)`) the types are printed; (d) `GRAPHIFY_OUT` set to an absolute tmp dir moves the file there; (e) deleting `detect.detect` on a stub module and calling `_patch_detect` raises `DrupalSeamError` (follow the existing seam-assertion tests in `tests/test_drupal_register.py` or the nearest equivalent — `/usr/bin/grep -ln DrupalSeamError tests`).
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** these tests, the full `-k drupal` suite, ruff.
- [ ] **Step 5: Commit** `feat(drupal): build the plugin registry at the start of detect`.

---

### Task 4: YAML plugins, and `plugin_of_type` on P1 nodes

**Files:**
- Create: `graphify/drupal/yaml_plugins.py`
- Modify: `graphify/drupal/families.py`, `graphify/drupal/yaml_links.py`, `graphify/drupal/yaml_assets.py`, `graphify/drupal/resolvers.py`
- Test: `tests/test_drupal_yaml_plugins.py`

**Interfaces:**
- Consumes: `current_registry()`, `Registry.by_yaml_name()`, `type_id` (Tasks 2–3); `node`, `edge`, `load_drupal_yaml`, `key_lines` (`yaml_common`); `extension_id` (`yaml_extract`).
- Produces:
  ```python
  def plugin_id(plugin_type: str, name: str) -> str: ...        # make_id("drupal", "plugin", plugin_type, name), in yaml_common
  def learned_family(path: Path) -> tuple[str, PluginType] | None: ...   # in yaml_plugins: (owner, type) when path is <ext>.<yaml_name>.yml in an extension root and yaml_name is learned, non-deferred, and not a P1 family suffix; longest yaml_name wins
  def extract_drupal_yaml_plugins(path: Path) -> dict: ...
  def plugin_type_for_family(yaml_name: str) -> str | None: ...  # type id for a P1 family (links.menu …), None without a registry
  ```

Behaviour (spec §4.1, §4.2, §5.2):
- `families.drupal_extractor`: after configuration and P1's table, `learned_family(path)` → `extract_drupal_yaml_plugins`. `is_drupal_file` follows automatically.
- "Extension root" = the file's directory contains `<ext>.info.yml` (`<ext>` = the filename's first dot-segment), or the file is `core/core.<name>.yml`.
- One `drupal_plugin` per top-level key whose value is a mapping (or `null`), line from `key_lines`: `plugin_id` = key, `plugin_type` = type's `plugin_type`, `provider` = owner, `class_name` = `class` when a string, `deriver` = `deriver` when a string. Label = key. Edges `provides_plugin` (`extension_id(owner)` → plugin) and `plugin_of_type` (plugin → `type_id`). A parse error returns `{"nodes": [], "edges": [], "error": …}` as other extractors do.
- P1: `_extract_links` in `yaml_links.py` and `extract_drupal_breakpoints` add `plugin_of_type` from each node to `plugin_type_for_family(<family>)` (`links.menu`, `links.task`, `links.action`, `links.contextual`, `breakpoints`) when it returns a type id; no edge otherwise.
- Resolver: the new relations' endpoints must never dangle in a corpus where the other side was not scanned — add `provides_plugin` to `_OWNER_RELATIONS` (owner materialised as for `declares_*`), and handle `plugin_of_type` targets like `_BY_PREFIX_RELATIONS` with a `("drupal_plugin_type_", "drupal_plugin_type", "plugin")` entry placed before any shorter prefix. Materialised type nodes carry `missing: True`.

- [ ] **Step 1: Failing tests** (synthetic site from Task 2's helper plus `prepare_run(root)` in the test to set the registry, then `set_current(None)` in a finalizer):
  - `web/modules/custom/foo/foo.bar.yml` with `one: {class: Drupal\foo\One, label: One}` and `two: {deriver: Drupal\foo\D, weight: 3}` and the YAML_MANAGER type `bar` → two nodes with exactly the attributes above (assert `label`/`weight` absent), two `provides_plugin`, two `plugin_of_type` to `type_id("bar")`.
  - `learned_family` returns `None` for `foo.bar.yml` outside the extension root, for `foo.links.menu.yml` (P1), for a deferred family, and when no registry is set.
  - `families.is_drupal_file(foo.bar.yml)` is true only while the registry is set.
  - a `foo.links.menu.yml` extracted with a registry containing a `menu.link` type with `yaml_name="links.menu"` gets one `plugin_of_type` per link; without it, none.
  - resolver: a graph with a `plugin_of_type` to an unknown type id gets a materialised `drupal_plugin_type` node with `missing: True`.
  - pipeline: `graphify.extract.extract([...])` over the synthetic site (all Drupal files) leaves no dangling edge endpoints for the new relations.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** new tests, full `-k drupal` (P1 link tests must still pass: without a registry P1 output is unchanged), ruff.
- [ ] **Step 5: Commit** `feat(drupal): emit YAML plugins of learned types`.

---

### Task 5: Plugin-type nodes from manager class files

**Files:**
- Modify: `graphify/drupal/discovery.py`, `graphify/drupal/register.py`, `graphify/drupal/resolvers.py`
- Test: `tests/test_drupal_plugin_types.py`

**Interfaces:**
- Consumes: `current_registry()`, `Registry.by_class_file()`, `type_id`; `service_id` (`yaml_common`), `extension_id`.
- Produces: `def extract_plugin_types(path: Path) -> dict: ...` in `discovery.py` — for a PHP file that is some type's `class_file`, one `drupal_plugin_type` node per type with every non-empty §4.1 attribute (`plugin_type`, `discovery`, `manager_service`, `manager_class`, `registered`, `subdir`, `interface`, `annotation_class`, `attribute_class`, `yaml_name`, `alter_hook`, `deferred_to`), at the type's `line`; edges `defines_plugin_type` (`extension_id(owner)` → type) and, when registered, `plugin_manager_for` (`service_id(manager_service)` → type). Empty result for any other file.
- Seam: the `_get_extractor` wrapper composes core's PHP handler with `extract_plugin_types` for a `.php` path that is a manager class file (same composition as `settings.php`; keep core's nodes first). Keep the settings composition working.
- Resolver: `defines_plugin_type` owner materialised like `declares_*` (`_OWNER_RELATIONS`; the owner is known from the node, so look at how `_materialise_owner` derives the name and make it work for a PHP source file — e.g. read the type node's `owner` attribute… the node does not carry `owner`; pass it on the edge as `owner` attribute instead and use it); `plugin_manager_for` source must exist as the P1 service node — if absent, materialise a `drupal_service` with `missing: True`. Check `resolver_registry` suffixes: the Drupal resolver is registered for `.yml`; verify whether it runs when a batch holds only PHP files and, if not, add `.php` so manager-only incremental runs resolve.

- [ ] **Step 1: Failing tests:** extract the synthetic D11 manager through `graphify.extract.extract` after `prepare_run`: core's PHP class node for `FooManager` is still present, one `drupal_plugin_type` `foo` with the exact attributes, the two edges; a non-manager PHP file yields no type node; a second-net manager yields `registered: False` and no `plugin_manager_for`; a `core/lib` manager's `defines_plugin_type` source `extension_id("core")` exists after resolution.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** new tests, full `-k drupal`, ruff.
- [ ] **Step 5: Commit** `feat(drupal): emit plugin types from their manager class files`.

---

### Task 6: A changed registry re-extracts what it affects

**Files:**
- Modify: `graphify/drupal/discovery.py`, `graphify/drupal/register.py`
- Test: `tests/test_drupal_discovery_incremental.py`

**Interfaces:**
- Consumes: `previous_registry()`, `current_registry()`, `Registry.root_yaml`, `by_yaml_name`, `by_class_file` (Tasks 2–3).
- Produces:
  ```python
  def affected_files(previous: Registry | None, current: Registry | None) -> set[str]: ...
      # absolute paths: for every yaml_name whose mapped PluginType differs (added, removed or any field changed),
      # the root_yaml files named *.<yaml_name>.yml (from both registries); for every class_file whose list of
      # PluginTypes differs, that file. Empty when previous is None.
  def force_miss() -> frozenset[str]: ...   # the set computed for this run by prepare_run (set_current stores it)
  ```
- Seam:
  - new patcher for `graphify.cache`: wrap `cache.load_cached(path, root=…, kind="ast", …)` to return `None` when `kind == "ast"` and the resolved absolute path is in `force_miss()`; assert it exists. (Keep `_patch_cache`'s version fingerprint as is.)
  - the `extract()` wrapper, on incremental runs (`resolution_context_nodes` present), adds `force_miss()` files that exist and are not in `paths` to `paths`, and strips them from the read-only context exactly as the collision-group widening does — factor that stripping into one helper used by both.

- [ ] **Step 1: Failing tests** (real CLI, `_run_cli` pattern from `tests/test_drupal_pipeline.py`: `subprocess.run([sys.executable, "-m", "graphify", "extract", str(root), "--code-only"], cwd=root)`):
  - unit: `affected_files` for a renamed `yaml_name` (`bar` → `baz`) returns both `foo.bar.yml` and `foo.baz.yml` and the manager file; for identical registries returns `set()`; for `previous=None` returns `set()`.
  - pipeline: synthetic site with the YAML_MANAGER (`bar`) and `foo.bar.yml` + `foo.baz.yml`; run 1 → plugins of type `bar` from `foo.bar.yml`, none from `foo.baz.yml`; edit only the manager (`'bar'` → `'baz'`); run 2 → no node from `foo.bar.yml` of type `bar`, plugins from `foo.baz.yml` present, the type node's `yaml_name == "baz"`.
  - pipeline: two runs with no change produce identical `graph.json` node and edge sets.
  - pipeline: change a service id in `foo.services.yml` (`plugin.manager.bar` → `plugin.manager.bar2`) with the class file untouched; run 2 → the type node is `bar2` and no `bar` type remains.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** new tests, full `-k drupal` (P1b's `test_a_collapsed_node_keeps_its_attributes_across_reruns` must stay green), ruff.
- [ ] **Step 5: Commit** `feat(drupal): re-extract what a changed plugin registry affects`.

---

### Task 7: The inventory and the report section

**Files:**
- Create: `graphify/drupal/inventory.py`
- Modify: `graphify/drupal/register.py`, `graphify/drupal/discovery.py` (call site in `prepare_run` or the detect wrapper)
- Test: `tests/test_drupal_inventory.py`

**Interfaces:**
- Consumes: `Registry` (root_yaml, types, unresolved), `families.is_drupal_file`, `config_stores.in_config_directory`, `discovery.out_dir`.
- Produces:
  ```python
  DEFERRED_FAMILIES = {"component": "P5", "migrate_drupal": "P6"}
  def build_inventory(registry: Registry, detected_files: set[str]) -> dict: ...
  def write_inventory(inventory: dict, out: Path) -> Path: ...     # <out>/drupal-inventory.json
  def current_inventory() -> dict | None: ...
  def load_inventory(out: Path) -> dict | None: ...
  def render_section(inventory: dict) -> str: ...                  # markdown, starts "## Drupal coverage"
  ```
- Seam: the detect wrapper, after the original returns, collects every path in the result (`files` of every category plus `unclassified` — read `detect.detect`'s return shape), builds and writes the inventory, stores it in process state. New patcher for `graphify.report`: wrap `report.generate(...)` to append `"\n" + render_section(inv)` when `current_inventory()` or `load_inventory(out_dir(Path(root)))` returns one; assert it exists.

Inventory shape (spec §5.7):
```json
{
  "unrecognised_yaml": [{"family": "starterkit", "files": 1, "owners": ["starterkit_theme"], "examples": ["web/core/themes/starterkit_theme/starterkit_theme.starterkit.yml"]}],
  "deferred": [{"family": "component", "phase": "P5", "files": 14}],
  "managers_unresolved": [{"class": "…", "file": "…", "reason": "not_a_service"}],
  "summary": {"types": 0, "registered_types": 0, "yaml_plugins": 0, "deferred_files": 0, "unrecognised_families": 0, "unrecognised_files": 0, "filtered": 0}
}
```
- `unrecognised_yaml` lists `root_yaml` files that detect returned, for which `families.is_drupal_file` is false and whose `<name>` is not a deferred family; `examples` are paths relative to the scan root, sorted, at most 3; families sorted by file count descending then name.
- `component` counts `*.component.yml` anywhere under an extension directory; `migrate_drupal` counts root files; P6 also counts `migrations/*.yml` under extension dirs as family `migrations`.
- `filtered` = `*.yml` files detect returned that are neither `root_yaml`, nor Drupal files, nor under a config directory, nor deferred.
- `yaml_plugins` counts plugins the learned families declare (top-level keys), computed without extraction by loading each learned-family file.
- `render_section`: a heading, the summary as a short table, the top 15 unrecognised families with counts and one example each, deferred families, and unresolved managers grouped by reason (count plus up to 5 classes each).

- [ ] **Step 1: Failing tests:** synthetic site with one learned family file, one unrecognised `foo.qux.yml`, one `foo.component.yml` in `components/x/`, a `.gitlab-ci.yml`, a second-net manager → exact inventory dict; the real CLI run writes `graphify-out/drupal-inventory.json` and `GRAPH_REPORT.md` contains `## Drupal coverage` and `qux`; `render_section` of an empty inventory still renders the heading and zeros.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** new tests, full `-k drupal`, ruff.
- [ ] **Step 5: Commit** `feat(drupal): report what plugin discovery did not recognise`.

---

### Task 8: `watch` rebuilds on Drupal YAML

**Files:**
- Modify: `graphify/drupal/register.py`
- Test: `tests/test_drupal_watch.py`

**Interfaces:**
- Consumes: `families.is_drupal_file`.
- Seam: new patcher for `graphify.watch`: wrap `_batch_triggers_rebuild(batch)` → `original(batch) or any(p.exists() and is_drupal_file(p) for p in batch)`; wrap `_has_non_code(changed_paths)` → `original([p for p in changed_paths if not is_drupal_file(p)])`. Assert both exist. Note in a comment that `watch._WATCHED_EXTENSIONS` already includes `.yml`.

- [ ] **Step 1: Failing tests:** with the seam installed (`import graphify`, then `import graphify.watch as w`): `w._batch_triggers_rebuild([<foo.services.yml>])` is true for an existing Drupal services file; false for an existing `docker-compose.yml`; `w._batch_needs_llm_flag([<foo.services.yml>])` is false while `[<README.md>]` stays true; `_patch_watch` on a stub module missing `_has_non_code` raises `DrupalSeamError`.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** new tests, full `-k drupal`, ruff.
- [ ] **Step 5: Commit** `fix(drupal): rebuild the graph in watch when Drupal YAML changes`.

---

### Task 9: Corpus acceptance and documentation

**Files:**
- Modify: `tests/test_drupal_corpus.py`, `docs/superpowers/specs/2026-09-22-drupal-graph-vocabulary.md`, `docs/superpowers/specs/2026-09-22-drupal-graphify-architecture-design.md`, `docs/superpowers/specs/2026-09-23-drupal-p2a-plugin-discovery-design.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Corpus tests** in `tests/test_drupal_corpus.py` (reuse its existing extract-once fixture if one exists; otherwise run `prepare_run(CORPUS)` then `graphify.extract.extract` over the Drupal files plus the manager class files, module-scoped), one test per spec §6 criterion 1–5 and 8:
  1. full-tree scan (the spec §2 method: every non-abstract class under `web/` outside pruned dirs whose `extends`/`implements` chain reaches a manager root, via `read_php_class`) ⊆ `{t.manager_class for t in types} ∪ {u["class"] for u in unresolved}`;
  2. every `root_yaml` file is in exactly one bucket (P1/P1b `is_drupal_file` without learned families / learned family / deferred / unrecognised);
  3. counts: types, learned-family files (72 expected), YAML plugins (260 expected) — assert the measured values and state them in the spec §2 if they differ, with the reason;
  4. every P1 `drupal_menu_link`, `drupal_local_task`, `drupal_local_action`, `drupal_contextual_link`, `drupal_breakpoint` node has exactly one outgoing `plugin_of_type`;
  5. every `drupal_plugin` node's keys ⊆ universal attributes ∪ {`plugin_id`, `plugin_type`, `provider`, `class_name`, `deriver`} (read the universal set from `yaml_common.node`'s payload keys plus `declared_in` and anything the collapse adds — list it explicitly in the test);
  8. `build_registry(CORPUS)` under 5 s.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_drupal_corpus.py -q` and fix any real defects found (a defect fix gets its own `fix(drupal):` commit with a unit test).
- [ ] **Step 3: Docs.**
  - vocabulary: §3.4 add the `drupal_plugin_type` attribute table (spec §4.1) and the `drupal_plugin` attribute list; §4.4 note which relations P2a emits and that `plugin_implemented_by`/`derives_plugins`/`configures_plugin` come in P4/P6; §5.3 rewrite: the registry is built at the start of every `detect` and `drupal-discovery.json` is an **output**, not an input, with the reason (spec §5.1, §5.5); add a line on `drupal-inventory.json`.
  - architecture §5: split the P2 row into P2a (plugins, inventory, watch YAML rebuild) and P2b (hooks); in §5.1 note that the `watch` YAML gap moved from P7 to P2a.
  - P2a spec: add §8 "Measured" with the Task 2 and Task 9 numbers.
- [ ] **Step 4: Run** full `-k drupal` and ruff.
- [ ] **Step 5: Commit** `test(drupal): P2a acceptance on the reference corpus` and `docs(drupal): P2a vocabulary, architecture and measurements`.
