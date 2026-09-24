# P2b Hooks and Core Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The graph holds a Drupal site's own code plus typed boundary nodes for the core/contrib/vendor code it touches, and it gains hooks (declarations, invocations, implementations); P2a's two carried fixes land.

**Architecture:** `graphify/drupal/boundary.py` derives realm from composer (installer-paths + lock) with P0's path rules as fallback; the seam prunes boundary trees from `detect()`'s walk through `detect._is_noise_dir`, except while the registry walks. The P2a registry (`discovery.py`) also collects hook declarations, a services index and an extension index; `graphify/drupal/hooks.py` extracts hook nodes/edges from `*.api.php`, procedural files (made PHP through the seam) and `#[Hook]` classes; the resolver enriches materialised stubs from the registry.

**Tech Stack:** Python 3, tree-sitter PHP via `graphify/drupal/php_classes.py` (extend, don't duplicate), PyYAML via `load_drupal_yaml`, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-24-drupal-p2b-hooks-boundary-design.md` — read the sections each task names.

## Global Constraints

- graphify core (anything outside `graphify/drupal/`) is never edited. Drupal behaviour enters only through wrappers in `graphify/drupal/register.py`; every wrapped or mutated core symbol is asserted and a missing one raises `DrupalSeamError`.
- Commits touch only `graphify/drupal/`, `tests/test_drupal_*.py`, `docs/superpowers/`. Conventional messages (`feat(drupal): …`, `fix(drupal): …`, `test(drupal): …`, `docs(drupal): …`) ending exactly with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests are flat `tests/test_drupal_*.py`; synthetic corpora in `tmp_path`; the reference corpus is `/home/user/Projects/FormsRemote` (`DRUPAL_CORPUS`), corpus tests skip when absent. Tests restore process state and env (`_isolated_discovery_state` in `tests/test_drupal_discovery_seam.py`).
- Ids via `graphify.ids.make_id`: hook `make_id("drupal", "hook", <name>)`, hook impl `make_id("drupal", "hook_impl", <module>, <hook>)`; `layer: "hook"`. Nodes/edges through `yaml_common.node` / `edge` (they set `_origin`, `realm`, `source_location`).
- Values never reach the graph (no hook bodies, no `order` evaluation — the argument's source text only).
- One relation per ordered node pair.
- No guessing: an implementation edge only for a literal, declared hook name; everything else is a `hook_candidates` inventory entry.
- Nothing in boundary/registry/hooks/inventory raises on bad input.
- `prepare_run(FormsRemote)` stays under 5 s.
- Measured numbers that differ from the plan are investigated and reported, never forced.
- Commands: `uv run --frozen pytest …`, `uv run --frozen ruff check graphify/drupal tests/test_drupal_*.py`. The `rtk` hook reformats `ls`/`grep`/`find` output; use `/usr/bin/grep`, `/usr/bin/find`, `/usr/bin/git` or python for exact results.
- Four `tests/test_ollama_retry_cap.py` failures (missing `openai`) are pre-existing.

## File Structure

| File | Responsibility |
|---|---|
| `graphify/drupal/boundary.py` (new) | composer install map, `realm_of`, `boundary_dir`, `.graphifyrc` `drupal.include` |
| `graphify/drupal/hooks.py` (new) | hook declarations from `*.api.php`; implementations (`#[Hook]`, procedural); invocations; hook node/edge emission |
| `graphify/drupal/php_classes.py` | extended reads needed by `hooks.py` (attributes, functions, call sites) |
| `graphify/drupal/discovery.py` | registry gains `hooks`, `services`, `extension_info`; `affected_files` covers hooks |
| `graphify/drupal/yaml_common.py` | `node()` realm through `boundary.realm_of` |
| `graphify/drupal/resolvers.py` | boundary enrichment of materialised stubs; menu-link stub `plugin_of_type` |
| `graphify/drupal/inventory.py` | `boundary` summary, `hook_candidates`, `composer_unreadable` |
| `graphify/drupal/register.py` | wrappers: `detect._is_noise_dir`, `CODE_EXTENSIONS` + classification/dispatch of procedural files, `cli._zero_node_stamped_code_sources` |

---

### Task 1: Realm from composer

**Files:** Create `graphify/drupal/boundary.py`; modify `graphify/drupal/yaml_common.py`; test `tests/test_drupal_boundary.py`.

**Interfaces — produces:**
```python
REALMS = ("core", "contrib", "vendor", "custom")
@dataclass(frozen=True)
class InstallMap:
    project_root: str                  # dir holding composer.json (absolute POSIX)
    paths: tuple[tuple[str, str, str], ...]   # (absolute install dir, realm, reason) longest first; reason "composer" | "vendor_dir"
    error: str = ""                    # non-empty when composer.json/lock was unreadable
def install_map(path: Path) -> InstallMap | None: ...   # nearest composer.json at/above path; cached per project root; None without composer.json
def realm_of(path: Path) -> str: ...                    # spec §4.1 order
def boundary_dir(path: Path) -> tuple[str, str] | None: ...  # (realm, reason) when path IS or is INSIDE a core/contrib/vendor install dir (after drupal.include), else None
def included_realms(project_root: Path) -> frozenset[str]: ...   # `.graphifyrc` `drupal.include = contrib, core, vendor` (default empty)
def clear_caches() -> None: ...
```

Rules (spec §4.1 verbatim): `.graphifyrc` `drupal.realm.*` first (reuse `paths.load_realm_rules` / `resolve_realm` with those rules — only when the rc file defines a realm key); then composer: packages from `composer.lock` `packages` + `packages-dev`; for each, the first `extra.installer-paths` entry (JSON object order) whose selector list contains `type:<type>` or the exact package name; substitute `{$name}` (after `/`) and `{$vendor}` (before `/`); relative to the project root. No match → `<vendor-dir>/<vendor>/<name>` (`config.vendor-dir`, default `vendor`). Type → realm: `drupal-core` core; `drupal-module|drupal-theme|drupal-profile|drupal-recipe|drupal-drush|drupal-library|npm-asset|bower-asset` contrib; `drupal-custom-*` custom; else vendor. The vendor dir itself → vendor (reason `vendor_dir`). A path takes the realm of the longest install dir containing it. Without composer: `paths.resolve_realm(path)` for core/contrib, a `vendor` directory that is a sibling of the web root (`discovery.find_web_root`) → vendor, else custom. `unknown` is never returned. Unreadable `composer.json`/`composer.lock` → path rules, and `InstallMap.error` set.

`yaml_common.node()` uses `realm_of(path)` instead of `resolve_realm(path)`. Keep `paths.resolve_realm` unchanged (P0 API, and the fallback).

- [ ] Step 1: failing tests — synthetic project in `tmp_path` with `composer.json` (FormsRemote's `installer-paths` shape: `web/core` `type:drupal-core`; `web/modules/contrib/{$name}` `type:drupal-module`; `web/modules/custom/{$name}` `type:drupal-custom-module`; `recipes/{$name}` `type:drupal-recipe`; `web/libraries/{$name}` `type:drupal-library`) and a `composer.lock` with `drupal/core` (drupal-core), `drupal/token` (drupal-module), `acme/mymod` (drupal-custom-module), `drupal/standard_recipe` (drupal-recipe), `symfony/yaml` (library): assert realms for `web/core/lib/Drupal.php` core, `web/modules/contrib/token/token.info.yml` contrib, `web/modules/custom/mymod/x.php` custom, `recipes/standard_recipe/recipe.yml` contrib, `recipes/my_own/recipe.yml` custom, `vendor/symfony/yaml/x.php` vendor, `web/sites/default/settings.php` custom, `config/sync/system.site.yml` custom; `boundary_dir` for `web/modules/contrib/token` → ("contrib","composer"), for `vendor` → ("vendor","vendor_dir"), None for custom; a custom `vendor-dir: lib/vendor`; package-name selector (`"web/libraries/ace": ["npm-asset/ace-builds"]`); `drupal.include = contrib` makes `boundary_dir` None for contrib; broken lock JSON → path-rule fallback + `error`; no composer.json → path rules; `realm_of` never `unknown`; P1 nodes built by `node()` for a contrib path carry `realm: "contrib"`.
- [ ] Step 2: run — fail. Step 3: implement. Step 4: run new tests + full `-k drupal` (update P0/P1 tests that asserted `unknown`/path realms only if the new answer is the spec's; list each such change in the report). Step 5: measure on FormsRemote (`install_map` package count, realm of the corpus paths above) and commit `feat(drupal): derive realm from composer`.

---

### Task 2: Boundary trees are not walked

**Files:** Modify `graphify/drupal/register.py`, `graphify/drupal/discovery.py`, `graphify/drupal/inventory.py`; test `tests/test_drupal_boundary_detect.py`.

**Interfaces:** Consumes `boundary_dir`, `included_realms` (Task 1). Produces `discovery.walking_registry() -> bool` (context-managed process flag set by `build_registry` around its walk).

Behaviour (spec §4.2): wrap `detect._is_noise_dir(part, parent=None)`: `original(part, parent) or (parent is not None and not walking_registry() and boundary_dir(Path(parent) / part) is not None)`. Assert it exists. The registry walk and `ignored_predicate` calls made by the registry run inside `walking_registry()`. Inventory `summary.boundary = {"core": n, "contrib": n, "vendor": n}` counts install dirs pruned (from the install map, not from a walk) and `boundary_reasons` counts by reason; `composer_unreadable: <error>` when set.

- [ ] Step 1: failing tests — synthetic composer site (Task 1 shape, plus a manager in `web/core/lib` and one in `web/modules/contrib/token/src`): `detect(root)` result lists no file under `web/core`, `web/modules/contrib`, `vendor`; custom files present; the registry still holds the core and contrib types; with `drupal.include = contrib` contrib files are listed; with core and contrib committed (no `.gitignore`) the same exclusion holds; `tests/test_detect.py` all green; the real CLI (`python -m graphify extract <root> --code-only`) graph has no `source_file` under a boundary dir; inventory summary has the boundary counts.
- [ ] Steps 2–5: run fail → implement → `tests/test_detect.py` + `-k drupal` + ruff → commit `feat(drupal): keep core, contrib and vendor out of the graph`.

---

### Task 3: The hook registry and in-graph declarations

**Files:** Create `graphify/drupal/hooks.py`; modify `graphify/drupal/discovery.py`, `graphify/drupal/families.py` (dispatch of `*.api.php`), `graphify/drupal/register.py` (compose for `*.api.php` — it is `.php`, core extracts it); test `tests/test_drupal_hooks_registry.py`.

**Interfaces — produces:**
```python
@dataclass(frozen=True)
class HookDecl:
    name: str; provider: str; file: str; line: int; pattern: str = ""   # pattern: UPPERCASE runs → "*", e.g. "form_*_alter"
# Registry gains: hooks: dict[str, HookDecl]; services: dict[str, tuple[str, str]]  (service id -> (class, provider));
#                 extension_info: dict[str, tuple[str, str]]  (name -> (type module|theme|profile, dir))
def hook_id(name: str) -> str: ...                       # make_id("drupal", "hook", name)
def read_hook_stubs(path: Path) -> list[tuple[str, int]]: ...   # (name, line) for each top-level `function hook_<name>(`; tree-sitter via php_classes
def extract_hook_declarations(path: Path) -> dict: ...   # for an in-graph *.api.php: drupal_hook per stub + declares_hook from extension_id(provider)
```
`Registry.to_json`/`from_json` round-trip the new fields; `affected_files` adds (spec §5.5) every in-graph procedural file (Task 4's predicate `hooks.is_procedural_file`) and every `src/Hook/**/*.php` under an in-graph extension when `hooks` differ — in this task implement the comparison and return the `*.api.php` files whose declarations changed; Task 4 extends the set. The provider of `*.api.php` is the extension whose directory contains it (longest match; `core` for `core/lib`/`core/core.api.php`).

- [ ] Step 1: failing tests — synthetic site with `web/core/core.api.php` (boundary) declaring `hook_cron`, `hook_form_FORM_ID_alter`; `web/modules/custom/foo/foo.api.php` declaring `hook_foo_info`: registry hooks = 3 with providers `core`, `core`, `foo`, `pattern` `form_*_alter` only for the second; the in-graph `foo.api.php` extraction yields core's PHP nodes plus one `drupal_hook` (`hook_name`, `provider`) and `declares_hook` foo → hook; round trip; the registry's services and extension_info indexes filled from the boundary too; corpus measurement: hook count (435 expected), `prepare_run` time.
- [ ] Steps 2–5 → commit `feat(drupal): learn hooks from api.php stubs`.

---

### Task 4: Implementations

**Files:** Modify `graphify/drupal/hooks.py`, `graphify/drupal/php_classes.py`, `graphify/drupal/register.py`, `graphify/drupal/families.py`, `graphify/drupal/inventory.py`, `graphify/drupal/discovery.py` (`affected_files`); test `tests/test_drupal_hook_impls.py`.

**Interfaces — produces:**
```python
def is_procedural_file(path: Path) -> bool: ...   # <ext>.module|.install|.theme|.profile, or <ext>.*.inc, directly in the directory of <ext>.info.yml
def hook_impl_id(module: str, hook: str) -> str: ...
def extract_hook_implementations(path: Path, core_result: dict) -> dict: ...   # nodes/edges + "hook_candidates" list for the inventory
```
Seam (spec §5.2): add `.module`, `.install`, `.theme`, `.profile`, `.inc` to `detect.CODE_EXTENSIONS` in place (assert it is a set) — but classification stays gated: `classify_file` returns CODE for these only when `is_procedural_file`; `_get_extractor` returns core's PHP handler (`extract._DISPATCH[".php"]`) composed with `extract_hook_implementations` for them, and composes `extract_hook_implementations` (+ Task 3's declarations for `*.api.php`) onto core's handler for `src/Hook/**/*.php` and any `.php` with `#[Hook`. Watch's `_CODE_EXTENSIONS` is the same set object (architecture §2.2) — assert identity.

Recognition (spec §5.4): `#[Hook('x')]` / `#[\Drupal\Core\Hook\Attribute\Hook('x', …)]` on a method; on a class with `method: 'm'`; on a class with `__invoke`; `module:`, `order:` (source text). Procedural `function <ext>_<rest>()` with `<ext>` the owning extension and `<rest>` a registry hook name. Emits `drupal_hook_impl` (`module`, `hook_name`, `via`, `function` | `class_name`+`method`, `order`), `implements_hook` (extension_id(owner) → hook_id), `hook_implemented_by` (impl → PHP node id) **only when that id is among `core_result`'s node ids** — compute the candidate id with core's own helpers (find them: `graphify.extract._make_id` and the file-stem helper core's PHP extractor uses; the stem is relative to the extraction root — derive it the same way core does for this path, and prove equality in a test). Inventory `hook_candidates`: `{kind: "variable" | "undeclared" | "non_literal", module, name, pattern?, file, line}`; `affected_files` gains every in-graph procedural file and `src/Hook/**/*.php` under in-graph extensions when the hook set differs.

- [ ] Step 1: failing tests — synthetic site (registry with `cron`, `form_FORM_ID_alter`, `entity_insert` declared in boundary core): `foo.module` with `foo_cron()`, `foo_form_user_login_form_alter()`, `foo_helper()`; `foo.views.inc` with `foo_views_data()` (not declared → nothing, since `views_data` undeclared in this registry: a `hook_candidates` `undeclared` entry); `src/Hook/FooHooks.php` with a method `#[Hook('entity_insert', order: Order::First)]`, a class-level `#[Hook('cron', method: 'run')]`, an `__invoke` class `#[Hook('cron')]`, and `#[Hook('node_insert', module: 'bar')]` (undeclared → candidate): assert exact nodes/edges; `hook_implemented_by` targets exist in the same extraction; `foo.module` classified as code, `foo.inc` without an extension beside it not; `foo_helper` produces nothing; watch's `_CODE_EXTENSIONS` contains `.module`; P1/P1b tests green; corpus: custom implementations (expected ≈ 19 attribute + 23 procedural), candidates listed.
- [ ] Steps 2–5 → commit `feat(drupal): hook implementations from attributes and procedural files`.

---

### Task 5: Invocations

**Files:** Modify `graphify/drupal/hooks.py`, `graphify/drupal/php_classes.py`, `graphify/drupal/discovery.py` (`extract_plugin_types` adds the alter edge); test `tests/test_drupal_hook_invocations.py`.

**Interfaces — produces:** `def extract_hook_invocations(path: Path, core_result: dict) -> dict` (composed wherever Task 4 composes, plus every in-graph `.php`/procedural file containing `->invoke`/`->alter`/`->hasImplementations` — cheap text precheck before parsing).

Behaviour (spec §5.3): calls `invokeAll`, `invoke`, `invokeAllWith`, `alter`, `hasImplementations`, `invokeDeprecated`, `invokeAllDeprecated`, `alterDeprecated` on any receiver; hook name = first string-literal argument (for `invoke(module, hook)` / `invokeDeprecated(msg, module, hook)` the hook argument position per Drupal's `ModuleHandlerInterface` — look it up in `web/core/lib/Drupal/Core/Extension/ModuleHandlerInterface.php` of the corpus and encode it); `alter('x')` → `x_alter`, `alter(['a', 'b'])` → `a_alter`, `b_alter`. Source = the enclosing function/method node id core emitted (same id helpers as Task 4; skip the edge if not in `core_result`). Target hook id always (a boundary stub if undeclared — mark the edge `undeclared: true` then). Non-literal → `hook_candidates` `non_literal`. Plugin types: `extract_plugin_types` emits `invokes_hook` type → `hook_id(alter_hook + "_alter")`.

- [ ] Step 1: failing tests — a custom class calling `$this->moduleHandler->invokeAll('foo_info')`, `->alter('foo_info', $x)`, `->alter(['a', 'b'], $x)`, `->invoke('bar', 'cron')`, `->invokeAll($name)`; a procedural function calling `\Drupal::moduleHandler()->invokeAll('cron')`: exact edges and candidate; a P2a manager with `alterInfo('foo_info')` gets `invokes_hook` to `foo_info_alter`; corpus: custom invocations (0 expected) and the number of type → alter edges.
- [ ] Steps 2–5 → commit `feat(drupal): hook invocation sites`.

---

### Task 6: Boundary nodes and the carried fixes

**Files:** Modify `graphify/drupal/resolvers.py`, `graphify/drupal/register.py`; test `tests/test_drupal_boundary_nodes.py`.

Behaviour (spec §4.3, §6): every node the resolver materialises gets `boundary: true`; with `current_registry()` it gets the §4.3 facts (`drupal_extension`: `extension_type`, `path`, `realm` via `realm_of(dir)`; `drupal_service`: `class_name`, `provider`, `realm` of the provider's dir; `drupal_plugin_type`: every P2a attribute; `drupal_hook`: `provider`, `declared_file`, `line`, `pattern`, `realm`). A materialised `drupal_menu_link` (and local task/action/contextual link) gets `plugin_of_type` to the learned type of its family. Wrap `cli._zero_node_stamped_code_sources(graph_path, scan_root, unchanged_code)` (assert it; import `graphify.cli` lazily in a new `_patch_cli` patcher registered in `_PATCHERS`): remove from its result every path whose root-relative POSIX spelling appears in any node's `declared_in` in `graph_path` (read once per call; tolerate a missing or bad file by returning the original result).

- [ ] Step 1: failing tests — synthetic site where a custom module injects a core service, depends on a contrib module, implements a core hook, and has a menu link with a core `parent`: the stubs carry exactly the facts; the P1b shadowed-copy scenario (`config/sync` + `config/install` copy of one config): real CLI run twice, the second run's summary re-extracts 0 files (assert via the CLI's `incremental summary` line: `0 re-extracted`) — confirm first that it re-extracts the copy before the fix.
- [ ] Steps 2–5 → commit `feat(drupal): typed boundary nodes; stop re-queuing shadowed config copies`.

---

### Task 7: Acceptance, docs, the closing real run

**Files:** Modify `tests/test_drupal_corpus.py`, `docs/superpowers/specs/2026-09-22-drupal-graph-vocabulary.md`, `docs/superpowers/specs/2026-09-22-drupal-graphify-architecture-design.md`, `docs/superpowers/specs/2026-09-24-drupal-p2b-hooks-boundary-design.md`.

- [ ] Step 1: corpus tests for spec §8 criteria 1–5 (reuse `tests/test_drupal_corpus.py` fixtures; the corpus extraction must go through `prepare_run` and the seam's detect so boundary pruning applies).
- [ ] Step 2: docs — vocabulary: §1.2 realm now from composer (with the fallback), a new "Boundary" subsection (`boundary: true`, which facts per type), §3.5/§4.5 as emitted in P2b and what stays for P4–P6; architecture §5: P2b row, note `.module` family moved from P4 to P2b and the boundary model; P2b spec §10 "Measured".
- [ ] Step 3: **closing real run** (spec §8): `uv run --frozen graphify extract /home/user/Projects/FormsRemote --code-only --out <scratch>/p2b` twice; record in spec §10: time, nodes/edges, `realm` counts (report `unknown`), hook / hook impl / boundary node counts, the second run's `incremental summary` line, `git -C /home/user/Projects/FormsRemote status --porcelain` empty and no `graphify-out/` in the project. Any defect found → `fix(drupal):` commit with a test.
- [ ] Step 4: full suite once, `-k drupal`, ruff. Step 5: commits `test(drupal): P2b acceptance on the reference corpus`, `docs(drupal): P2b vocabulary, architecture, measurements and real run`.
