# P3 Container Producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running Drupal's container, routes, extensions, hook implementations, plugin definitions and event listeners reach the graph through a committable artifact. The artifact is laid over every build, confirming static edges, adding container-only ones, marking `runtime` and logging divergences. P2b's two documented gaps land.

**Architecture:** `container_collect.php` walks what Drupal has built and prints JSON. `runners.py` finds how to run it (ddev, lando, compose, drush alias, `vendor/bin/drush`, `.graphifyrc`). `container.py` is the `graphify drupal container` command, the artifact model and the stamp. `container_overlay.py` is an idempotent pass over the assembled graph, run by a gated wrapper on `build.build_from_json`. `divergence.py` compares the two sources and writes the log. The report block comes through the P2a inventory.

**Tech Stack:** Python 3, PHP 8.1+ (collector, run by drush `php:eval`), networkx (the graph core builds), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-25-drupal-p3-container-design.md`. Read the sections each task names.

## Global Constraints

- graphify core (anything outside `graphify/drupal/`) is never edited. Drupal behaviour enters only through wrappers in `graphify/drupal/register.py`. Every wrapped or mutated core symbol is asserted, and a missing one raises `DrupalSeamError`.
- Commits touch only `graphify/drupal/`, `tests/test_drupal_*.py`, `docs/superpowers/`, and, in Task 3 only, `pyproject.toml` (one package-data line). Conventional messages (`feat(drupal): …`, `fix(drupal): …`, `test(drupal): …`, `docs(drupal): …`) end exactly with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never stage `docs/superpowers/plans/2026-09-23-drupal-p2a-plugin-discovery.md` (the user's uncommitted edit).
- Tests are flat `tests/test_drupal_*.py`. Synthetic sites and artifacts are built in `tmp_path` inside the test (no fixture directories). The reference corpus is `/home/user/Projects/FormsRemote` (`DRUPAL_CORPUS`), and corpus tests skip when it is absent. Tests that need a live site also skip unless `DRUPAL_CONTAINER_LIVE=1`. Tests restore process state and env (`_isolated_discovery_state` in `tests/test_drupal_discovery_seam.py`).
- Nothing is ever written into FormsRemote. Real runs use `--out <scratchpad>` and `GRAPHIFY_DRUPAL_CONTAINER=<scratchpad file>`.
- The overlay's marker is `origin: "container"` (no underscore). `_origin` stays `"ast"` on everything the overlay adds (spec §7.5). Internal undo markers: `_overlay: True` on overlay-created nodes, and `_overlay_attrs: [keys]` on touched nodes and edges.
- Ids via existing helpers: `yaml_common.service_id`, `route_id`, `permission_id`, `parameter_id`, `tag_id`, `plugin_id(type, id)`; `discovery.type_id`; `hooks.hook_id`, `hooks.hook_impl_id(module, hook)`; `yaml_extract.extension_id(name)`. An event id is `make_id("drupal", "event", name)`.
- Relations are only the vocabulary's (spec §7.4). There is one relation per ordered node pair, and confirmation is an attribute (`confirmed_by: "container"`), never a second edge.
- The artifact never carries configuration values, scalar service arguments, labels, content, env or absolute machine paths (spec §5.3).
- A normal build never runs drush. The overlay never raises into a build (status `error (…)` instead). The command never overwrites an artifact on failure.
- Measured numbers that differ from the plan are investigated and reported, never forced.
- Commands: `uv run --frozen pytest …`, `uv run --frozen ruff check graphify/drupal tests/test_drupal_*.py`. The `rtk` hook reformats `ls`/`grep`/`find`/`sed` output; use `rtk proxy <cmd>`, `/usr/bin/grep`, `/usr/bin/git` or python for exact results.
- Four `tests/test_ollama_retry_cap.py` failures (missing `openai`) are pre-existing.

## File Structure

| File | Responsibility |
|---|---|
| `graphify/drupal/container.py` (new) | artifact model (`load_artifact`, validation), host stamp (`compute_host_stamp`), staleness (`staleness`), artifact path resolution, the `graphify drupal container` command (`main`) |
| `graphify/drupal/runners.py` (new) | runner detection, argv building, running the collector, named errors |
| `graphify/drupal/container_collect.php` (new) | the collector (spec §5) |
| `graphify/drupal/container_overlay.py` (new) | undo, binding, mapping, boundary rule, `runtime`, boundary-facts re-apply (spec §7) |
| `graphify/drupal/divergence.py` (new) | divergence records, log file (spec §8) |
| `graphify/drupal/discovery.py` | `current_run()` — the root and out dir `prepare_run` set; `affected_files` gains `invokes_hook` sources (Task 8) |
| `graphify/drupal/inventory.py` | `render_section` renders the "Container" block from `inventory["container"]` |
| `graphify/drupal/register.py` | wrappers `cli.dispatch_command`, `build.build_from_json`; watch triggers on the artifact |
| `pyproject.toml` | package data `"graphify.drupal" = ["container_collect.php"]` |

---

### Task 1: The artifact, its path and the stamp

**Files:** Create `graphify/drupal/container.py`. Test `tests/test_drupal_container_artifact.py`.

**Interfaces, produces:**
```python
SCHEMA_VERSION = 1
SOURCES = ("services", "aliases", "routes", "extensions", "hooks", "plugins", "subscribers")
ENV_ARTIFACT = "GRAPHIFY_DRUPAL_CONTAINER"
RC_ARTIFACT = "drupal.container.artifact"
class ArtifactError(ValueError): ...            # message names what is wrong
@dataclass(frozen=True)
class Artifact:
    data: dict                                  # the whole validated JSON
    path: Path
    @property
    def stamp(self) -> dict: ...
def artifact_path(root: Path) -> Path: ...      # env var > .graphifyrc key (relative to root) > root/"drupal-container.json"
def load_artifact(root: Path) -> Artifact | None: ...   # None when the file is absent; ArtifactError when invalid
def validate(data: object) -> dict: ...         # schema_version == 1, every SOURCES key + "stamp", "errors" of the right JSON type
def container_sources(root: Path) -> list[Path]: ...   # spec §6.3 file set, custom only, sorted
def compute_host_stamp(root: Path) -> dict: ...  # git_commit, git_dirty, composer_lock_sha, sources_sha
def staleness(artifact: Artifact, root: Path) -> tuple[str, list[str]]: ...   # ("fresh"|"stale", reasons)
```

Behaviour (spec §6):
- `container_sources` uses `discovery.current_registry()` (extension dirs, manager class files, service classes) plus a walk of the custom extension dirs for `*.info.yml`, `*.services.yml`, `*.routing.yml`, procedural files (`hooks.is_procedural_file`), `src/**/*ServiceProvider.php`, `src/**/EventSubscriber/**/*.php`, `src/Plugin/**/*.php`, `src/Hook/**/*.php`. It keeps only paths with `boundary.realm_of(p) == "custom"`.
- `sources_sha` is the sha256 over `"<relposix>\0<sha256 of bytes>\n"` lines in sorted order.
- `git_commit`/`git_dirty` come from `git -C root rev-parse HEAD` / `status --porcelain`, and are `None`/`False` outside git. `composer_lock_sha` is the sha256 of the composer root's `composer.lock` (`boundary.install_map(root).project_root`), else `None`.
- Reasons are `"composer.lock changed"` and `"N container source files changed: a, b, …"` (up to 10 paths). To name the paths, the stamp also stores `sources: {relpath: sha}` (paths and hashes only).

- [ ] Step 1: failing tests. A synthetic composer site (reuse the Task 1 shape of `tests/test_drupal_boundary.py`) with a custom module holding `foo.info.yml`, `foo.services.yml`, `foo.routing.yml`, `foo.module`, `src/FooServiceProvider.php`, `src/EventSubscriber/FooSubscriber.php`, `src/Plugin/Block/FooBlock.php`, `src/Hook/FooHooks.php`, `src/Util.php`, and a contrib module with a `.services.yml`. Assert that `container_sources` is exactly the eight custom files (not `Util.php`, not contrib). `artifact_path` precedence is env > rc > default. `validate` rejects: non-object, missing key, `schema_version` 2, `services` not a list, with the offending key in the message. `load_artifact` returns `None` when absent and raises `ArtifactError` on bad JSON. `staleness` is `fresh` right after `compute_host_stamp`. Touching `foo.services.yml` gives `stale` with a reason naming it, and changing `composer.lock` gives `"composer.lock changed"`. The stamp holds no absolute path (assert that no value contains `str(tmp_path)`).
- [ ] Step 2: run it and see it fail. Step 3: implement. Step 4: run the new tests, `-k drupal`, ruff. Step 5: commit `feat(drupal): the container artifact and its stamp`.

---

### Task 2: Runners

**Files:** Create `graphify/drupal/runners.py`. Test `tests/test_drupal_runners.py`.

**Interfaces, produces:**
```python
class RunnerError(RuntimeError): ...
class RunnerNotFound(RunnerError): ...
class AmbiguousComposeService(RunnerError): ...
class RunnerUnavailable(RunnerError): ...
class BootstrapFailed(RunnerError): ...
@dataclass(frozen=True)
class Runner:
    name: str            # "config" | "ddev" | "lando" | "compose" | "alias" | "vendor"
    prefix: tuple[str, ...]   # argv that runs drush, e.g. ("ddev", "drush")
    cwd: Path
def detect_runner(root: Path, override: str | None = None) -> Runner: ...   # spec §4 table order; override is --runner-command
def run_php(runner: Runner, code: str, timeout: float = 600.0) -> str: ...   # [*prefix, "php:eval", code]; returns stdout
```

Behaviour (spec §4):
- `.graphifyrc` keys are `drupal.container.command`, `drupal.container.service`, `drupal.container.alias`. Read them the way `boundary` reads `drupal.include` (the same `key = value` line format, ignoring others).
- Compose files: `docker-compose.yml`, `docker-compose.yaml`, `compose.yaml`, `compose.yml` at the root, parsed with `yaml.safe_load`. Candidate names are `php`, `web`, `app`, `drupal`, `cli`, `php-fpm`, and the prefix is `("docker", "compose", "exec", "-T", svc, "drush")`.
- Alias: `drush/sites/*.site.yml`. It needs `drupal.container.alias`, or exactly one file with exactly one top-level environment. The prefix is `("drush", f"@{site}.{env}")`. In the fallback case prefer `vendor/bin/drush` as the drush binary when present.
- `vendor/bin/drush` resolves under the composer root (`boundary.install_map`), falling back to root.
- `run_php` behaviour:
  - `FileNotFoundError` → `RunnerUnavailable("… is not installed")`;
  - a non-zero exit whose stderr mentions ddev/docker not running (`not running`, `Cannot connect to the Docker daemon`, `is not currently running`) → `RunnerUnavailable` with the start command (`ddev start`, `lando start`, `docker compose up -d`);
  - any other non-zero exit → `BootstrapFailed` carrying the last 40 stderr lines;
  - a timeout → `BootstrapFailed("timed out after …")`.
- The code is passed as one argv element. Nothing is written to disk.

- [ ] Step 1: failing tests (`subprocess.run` monkeypatched to record argv/cwd and return canned results). Cover:
  - each marker yields its prefix;
  - override and rc command win over `.ddev`;
  - two compose services `web` and `php` → `AmbiguousComposeService` listing both, and `drupal.container.service = php` resolves it;
  - a compose file with only `db` → `AmbiguousComposeService` (none);
  - no marker → `RunnerNotFound` whose message names `.graphifyrc` `drupal.container.command` and `--print-script`;
  - the argv's last two elements are `php:eval` and the code, verbatim;
  - the ddev-not-running stderr → `RunnerUnavailable` mentioning `ddev start`;
  - exit 1 with a PHP fatal → `BootstrapFailed` carrying it;
  - `FileNotFoundError` → `RunnerUnavailable`;
  - no file is created under `tmp_path` by `run_php`.
- [ ] Steps 2–5, then commit `feat(drupal): find how to run drush for a site`.

---

### Task 3: The collector and the command

**Files:** Create `graphify/drupal/container_collect.php`. Modify `graphify/drupal/container.py` (`main`, `collect`), `graphify/drupal/register.py` (wrap `cli.dispatch_command`), `pyproject.toml` (package data). Test `tests/test_drupal_container_command.py`, plus a live test in `tests/test_drupal_corpus.py`.

**Interfaces:**
- Consumes: Task 1 (`validate`, `compute_host_stamp`, `artifact_path`), Task 2 (`detect_runner`, `run_php`, errors).
- Produces:
```python
def collector_code(hook_names: list[str] | None = None) -> str: ...  # the PHP file's text with "<?php" stripped, prefixed by `$GRAPHIFY_HOOKS = [...];` (JSON-encoded list)
def collect(root: Path, runner_override: str | None = None) -> dict: ...   # runs, parses stdout JSON, strips site.composer_root/drupal_root from every path, validates, merges the host stamp; raises RunnerError / ArtifactError
def write_artifact(data: dict, path: Path) -> None: ...   # sorted keys, indent=1, stable list order, tmp + os.replace
def main(argv: list[str]) -> int: ...   # `container [PATH] [--out FILE] [--print-script] [--runner-command CMD]`
```

Collector (spec §5, every source in its own `try { … } catch (\Throwable $e)`):
- Services: `\Drupal::service('kernel')->getCachedContainerDefinition()`. Each `$def['services'][$id]` is a serialized string: `unserialize($s, ['allowed_classes' => false])` (Drupal stores plain arrays; objects become `__PHP_Incomplete_Class` and are skipped). Arguments are walked recursively. From a string, keep it if it starts with `@` (strip `@`, `@?`) or matches `/^%[^%]+%$/`. From an array `['type' => 'service', 'id' => …]`, which is Drupal's dumped form, keep the id. Also record `class`, `tags` (name plus scalar attributes), `decorates` (`decorated_service`/`decorates`), and `file` via `ReflectionClass` (skip if missing or not loadable, `class_exists($c, true)` in `try`). `provider` is the first `Drupal\<ext>\` segment if it is in `\Drupal::moduleHandler()->getModuleList()` or the theme list.
- Aliases: `$def['aliases']`.
- Routes: `getAllRoutes()`, then for each route `getPath()`, the listed `getDefaults()` keys and `getRequirements()` keys (all string values). `provider` is taken from `_controller`/`_form`'s `Drupal\<ext>\` segment when determinable.
- Extensions: modules, themes and profiles via `\Drupal::service('extension.list.module')->getList()` (and `theme`, `profile`). For each: `getPathname()` dir, `status`, `weight` (`$e->weight ?? 0`), and `info['dependencies']` names without the `project:` prefix.
- Hooks: first `\Drupal::keyValue('hook_data')->get('hook_list')`. When it is an array, its shape is hook → [identifier → module], where the identifier is `Class::method` or a function name; the collector reads it defensively, using key → value pairs in order. Otherwise, for each name in `$GRAPHIFY_HOOKS`, call `\Drupal::moduleHandler()->invokeAllWith($h, function (callable $l, string $m) use (&$out, $h) { $out[$h][] = [...]; })`, which never calls `$l`. The listener identifier comes from `is_array($l)` (`get_class($l[0]).'::'.$l[1]`), a string, or a `Closure` via `ReflectionFunction`. `file` comes from Reflection.
- Plugins: each service id starting with `plugin.manager.`. `\Drupal::service($id)` must be a `\Drupal\Component\Plugin\PluginManagerInterface`. For each definition take `id`, `class`, `provider`, `deriver`, and `base_plugin_id` (strings only; array or object definitions via `getProperty`-free access: `is_array($d) ? $d[...] : (method_exists($d, 'get') ? $d->get(...) : null)`). `file` comes from Reflection of `class`.
- Subscribers: `getListeners()`, which with no argument returns event → listeners. Use `getListenerPriority($event, $listener)`. Array listeners give `get_class($l[0]).'::'.$l[1]`, and closures are counted in `closures`.
- `site` holds `\Drupal::VERSION`, `drupal_root` (`DRUPAL_ROOT`), `composer_root` (walk up from `DRUPAL_ROOT` to the first dir with `composer.json`), and `enabled_extensions_sha` (sha256 of the sorted `type:name` of every extension with status 1).
- Output: `echo json_encode($out, JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR);`. `file` values are absolute here, and Python makes them relative (strip `composer_root + "/"`; a path outside it becomes `null`).

Command and seam:
- `--print-script` prints `collector_code()` with `<?php` restored and returns 0.
- Otherwise it resolves `PATH` (default `.`), `collect`, writes to `--out` or `artifact_path(root)`, prints counts per source, `errors[]` and the path. It returns 0, or 1 on `RunnerError`/`ArtifactError` with the message on stderr (no traceback).
- The hook list for the D10 fallback is the current registry's hook names. `collect` calls `discovery.prepare_run` only if no registry is current (read-only; `prepare_run` writes only under the out dir). When `--out` is given, use `discovery.using_out_dir(<out's parent>)` so no `graphify-out/` appears in the site.
- `register._patch_cli` also wraps `cli.dispatch_command(cmd)`: when `cmd == "drupal"`, it runs `sys.exit(container.main(sys.argv[3:]))` if `sys.argv[2] == "container"`, else prints usage to stderr and exits 2. Assert that `dispatch_command` is callable.
- `pyproject.toml`: add `"graphify.drupal" = ["container_collect.php"]` under `[tool.setuptools.package-data]`, and check that `graphify/drupal/allowlist.txt` already admits `pyproject.toml`.

- [ ] Step 1: failing tests:
  - `collector_code(["cron"])` starts with `$GRAPHIFY_HOOKS = ["cron"];` and has no `<?php`;
  - `php -l` on the collector passes (skip if `php` is absent);
  - `collect` with `run_php` monkeypatched to return a canned collector JSON (built in the test, with absolute paths under a fake `/var/www/html`) yields relative `file`s, `null` for outside paths, and the host stamp merged; garbage stdout raises `ArtifactError`;
  - `main(["container", str(root), "--out", str(out)])` writes a file byte-identical across two runs with the same canned output (apart from `created_at`, which is frozen by monkeypatching the clock);
  - on `BootstrapFailed` it returns 1 and the previous artifact is unchanged;
  - `--print-script` returns 0 and prints PHP;
  - the seam: `python -m graphify drupal container --print-script` in a subprocess exits 0 and prints `<?php`; `graphify drupal bogus` exits 2;
  - removing `dispatch_command` raises `DrupalSeamError`.
  - Live, in `tests/test_drupal_corpus.py` and gated as in Global Constraints: `collect(CORPUS)` validates. Services > 1,000, routes > 500, `hooks` non-empty, and every `file` is relative. Write to `tmp_path` only.
- [ ] Steps 2–5, then commit `feat(drupal): collect the container through drush`.

---

### Task 4: The overlay — undo, binding, services, routes, extensions

**Files:** Create `graphify/drupal/container_overlay.py`. Test `tests/test_drupal_container_overlay.py`.

**Interfaces:**
- Consumes: `Artifact` (Task 1), `boundary.realm_of`, `boundary.included_realms`, the id helpers (Global Constraints).
- Produces:
```python
ORIGIN = "container"
@dataclass
class OverlayResult:
    status: str                                  # "fresh" | "stale" | "unavailable" | "invalid" | "error"
    reasons: list[str]
    counts: dict[str, dict[str, int]]            # source -> {"total", "custom", "applied"}
    edges: dict[str, int]                        # {"confirmed", "container_only", "conflict"}
    runtime_absent: dict[str, int]               # node type -> count
    conflicts: list[dict]                        # {"relation", "source", "static_target", "container_target"}
    seen: dict[str, set[str]]                    # node type -> ids the container knows (for divergence)
def undo(G) -> None: ...                         # spec §7.2
def apply(G, artifact: Artifact | None, root: Path, *, status: str = "fresh", reasons: list[str] | None = None) -> OverlayResult: ...
class _Binder:                                   # built once per apply from G
    def php_node(self, file: str | None, name: str) -> str | None: ...   # node whose source_file == file and label in {name, f".{name}()", f"{name}()"}; for "Class::method" the method node under that class
```

Behaviour (spec §7.2–7.7, for `services`, `aliases`, `routes`, `extensions` in this task):
- `apply` always calls `undo` first.
- `artifact is None` → status `unavailable`, and nothing else is added. Task 6 adds the boundary-facts re-apply.
- The subject realm is `realm_of(root / file)` for a fact with a `file`. Otherwise it is the provider extension's dir realm (the registry's `extension_info`, or the artifact's `extensions[].path`). "Custom" means realm `custom` or a realm in `included_realms(root)`.
- A custom fact is applied in full. A boundary fact is applied only to a node already in `G`, as attributes. A new boundary stub (`boundary: True`, `external: True`, `file_type: "concept"`, `realm`, `layer`, `type`, `_overlay: True`, `origin: "container"`) is created only as the target of a custom fact's edge.
- Edge add: if `G.has_edge(u, v)` with the same `relation`, set `confirmed_by` and add it to `_overlay_attrs`. Else if `G.has_edge(u, v)` with another relation, do not add (one relation per pair) and count it under `edges["pair_taken"]`. Else add the edge with `relation`, `origin`, `_origin: "ast"`, `confidence: "EXTRACTED"`, `confidence_score: 1.0`, `source_file` = the artifact's path relative to root (else its name), and `source_location: "L1"`. For a directed graph use `(u, v)` as given. For an undirected one, preserve `_src`/`_tgt` exactly as core does: look at how `build_from_json` stores direction on undirected graphs and copy it.
- Conflict (spec §7.5 list): the static edge of that relation from `u` exists to a different `v`. Keep both, and record it.
- Attribute enrichment: each key set is added to `_overlay_attrs`. An existing value is never overwritten; a different value is recorded in `OverlayResult.conflicts` with relation `attribute:<key>`.
- `runtime`: after mapping, every node of the five types (spec §7.7) gets `runtime` = `"present"` if its id is in `seen[type]`, else `"absent"`, and the key goes in `_overlay_attrs`.
- Mapping for this task:
  - service: `service_implemented_by` → `_Binder.php_node(file, short class)`, else `class_name` attr; `injects_service`, `injects_parameter`, `tagged_as` (tag attrs on the edge), `decorates`, `declares_service` from `extension_id(provider)`; `aliases` attr on the target;
  - route: `declares_route` from provider; `routes_to` → method node (`Class::method` of `_controller`), else class node, else `controller` attr; `routes_to_form` → the `_form` class node (the form node is P4); `requires_permission` per `+`/`,`-split `_permission`; `access_checked_by` for `_custom_access` (`Class::method`); `path` attr;
  - extension: `depends_on_module` to each dependency; attrs `weight`, `enabled` (status).

- [ ] Step 1: failing tests on graphs built with `graphify.build.build_from_json` from a hand-written extraction dict (nodes as the Drupal producers emit them: a custom service `foo.bar` with `class_name`, a boundary stub `entity_type.manager`, a custom route `foo.page`, a PHP class node `FooController` with method node `.page()` at `web/modules/custom/foo/src/Controller/FooController.php`, extension `foo`) plus an artifact dict built in the test. Cover:
  - `service_implemented_by` added with `origin: container`;
  - an existing static `injects_service` gets `confirmed_by`;
  - a container-only `injects_service` to the core service is added;
  - the core service stub gains `class_name` only if absent;
  - a core service not referenced by custom code creates no node;
  - `routes_to` targets the method node;
  - a conflicting `service_implemented_by` is recorded and both edges stay;
  - `runtime` present/absent;
  - **idempotence**: `apply` twice → `nx.node_link_data` equal to once;
  - **undo**: `apply` then `undo` → equal to the untouched graph (compare `node_link_data`, sorted);
  - `artifact=None` → no `runtime`, no `origin` anywhere;
  - `drupal.include = contrib` in `.graphifyrc` makes a contrib fact apply in full.
- [ ] Steps 2–5, then commit `feat(drupal): lay services, routes and extensions from the container over the graph`.

---

### Task 5: The overlay — hooks, plugins, subscribers

**Files:** Modify `graphify/drupal/container_overlay.py`. Test `tests/test_drupal_container_overlay_runtime.py`.

**Interfaces:** Consumes Task 4's `apply`, `_Binder`, the edge and attr helpers. Produces the mapping of `hooks`, `plugins` and `subscribers` into the same `OverlayResult`.

Behaviour (spec §7.4):
- Hook implementations follow P2b's shape.
  - For a custom module's implementation of hook `h`: the `drupal_hook_impl` node `hook_impl_id(module, h)`. If absent, it is created with `_overlay: True`, `module`, `hook_name`, `function` or `class_name`+`method`, `layer: "hook"`, `type: "drupal_hook_impl"`, `realm`, `source_file` = the implementation's file.
  - Edges: `implements_hook` (`extension_id(module)` → `hook_id(h)`, creating a boundary hook stub if absent, as §7.6 allows), and `hook_implemented_by` (impl → `_Binder.php_node(file, method or function)`). The impl node gets `order` (its index in the hook's list).
  - Boundary implementations only annotate existing impl nodes with `order`.
- Plugins: for a custom plugin (`realm_of(file)` custom, or provider custom):
  - the `drupal_plugin` node `plugin_id(type, id)`, created if absent with `_overlay`, `plugin_type`, `class_name`, `provider`, `deriver`, `base_plugin_id`, `derivative: True` when `base_plugin_id` differs from `id`;
  - `plugin_of_type` → `type_id(type)` (creating a boundary stub if the type node is absent);
  - `provides_plugin` from `extension_id(provider)`;
  - `plugin_implemented_by` → the class node;
  - `derives_plugins` → the deriver class node when bound.
- Subscribers: `subscribes_to_event` from the class node (bound by file plus short class) → `make_id("drupal", "event", event)`. The event node is created as `drupal_event` (`layer: "di"`, `_overlay: True`, `realm` of the event's first custom subscriber, never boundary). `priority` goes on the edge.

- [ ] Step 1: failing tests (same construction as Task 4):
  - a static impl node plus the container's list for `form_alter` [core `system`, custom `foo`] → `foo`'s impl gets `order: 1`, and its `hook_implemented_by` is confirmed;
  - a container-only impl (a class method static missed) creates the impl node and both edges;
  - a derivative plugin `foo_block:bar` (`base_plugin_id: foo_block`, `deriver` class bound) creates the node with `derivative: True`, `plugin_of_type`, `plugin_implemented_by`, `derives_plugins`;
  - a core block plugin creates nothing;
  - a subscriber creates `drupal_event` and `subscribes_to_event` with `priority`;
  - idempotence and undo tests as in Task 4, for these sources.
- [ ] Steps 2–5, then commit `feat(drupal): hook order, plugins and event subscribers from the container`.

---

### Task 6: The build seam, boundary facts, divergence and the report

**Files:** Create `graphify/drupal/divergence.py`. Modify `graphify/drupal/container_overlay.py` (boundary facts), `graphify/drupal/discovery.py` (`current_run`), `graphify/drupal/register.py` (wrap `build.build_from_json`; watch trigger), `graphify/drupal/inventory.py` (Container block). Test `tests/test_drupal_container_seam.py`.

**Interfaces, produces:**
```python
# discovery.py
def current_run() -> tuple[Path, Path] | None: ...   # (resolved root, out dir) set by prepare_run for a Drupal tree, cleared with set_current(None)
# divergence.py
DIVERGENCE_FILENAME = "drupal-divergence.json"
def compute(G, result: OverlayResult, artifact: Artifact, root: Path) -> list[dict]: ...   # spec §8 records, sorted by (kind, subject)
def write(records: list[dict], out: Path) -> Path: ...
def remove(out: Path) -> None: ...
# container_overlay.py
def apply_boundary_facts(G, root: Path) -> None: ...   # spec §7.8, recorded in _overlay_attrs
def run_for_build(G) -> OverlayResult | None: ...     # current_run() or None → None; loads artifact, staleness, apply, apply_boundary_facts, divergence, inventory["container"] = summary; never raises
```

Behaviour:
- `register._patch_build(build)`: assert `build.build_from_json` is callable. The wrapper calls the original, then `container_overlay.run_for_build(G)` under a module-level re-entry guard, and returns `G`. Register it in `_PATCHERS` for `graphify.build`.
- `run_for_build` returns immediately when `current_run()` is None, which is how `query`/`path` reads stay untouched.
- Artifact errors: `ArtifactError` → status `invalid (<msg>)`, with no overlay but boundary facts applied. Any other exception inside → `undo(G)`, then status `error (<msg>)`, logged once.
- `apply_boundary_facts` re-applies to every node with `boundary: True` the facts `resolvers` gives a materialised stub from the current registry. Reuse the resolver's own facts: the class in `resolvers.py` whose `facts(node) -> dict` builds a stub's registry facts (the "boundary facts (P2b spec §4.3)" block) — call it, do not duplicate it. A key is set when the registry's value differs from or is missing on the node; it replaces the value, and the key is recorded in `_overlay_attrs` with the previous value in `_overlay_prev` so that `undo` restores it.
- Divergence (spec §8):
  - `static_only` covers nodes of the five types with `runtime: absent` that are custom, or boundary nodes in `G`;
  - `container_only` covers facts in `result.seen` whose static node did not exist before the apply (record the pre-apply id set in `apply`);
  - `conflict` comes from `result.conflicts`;
  - `extension_state` compares enabled extensions in the artifact against `core.extension.yml` `module:`/`theme:` keys (find it in the config sync dir through the registry or P1b's config store helper).
  - `possibly_stale` as in the spec.
  - Written when an artifact was applied and removed otherwise, in `current_run()[1]`.
- Report: `run_for_build` puts `inventory["container"]` (status, reasons, stamp subset, counts, edges, `runtime_absent`, divergence counts per kind, the first 10 records, collector `errors`) and rewrites the inventory file (`inventory.write_inventory`). `render_section` renders a `### Container` block after the existing content. With `unavailable` it lists the vocabulary §7 table rows as bullets.
- Watch: `_batch_triggers_rebuild` also returns True when a path in the batch equals `container.artifact_path(root)` (root from `current_run()`, else skip).

- [ ] Step 1: failing tests:
  - a real CLI run (`python -m graphify extract <site> --code-only --out <out>`, as in `tests/test_drupal_pipeline.py`) over a synthetic Drupal site with a hand-written artifact at `GRAPHIFY_DRUPAL_CONTAINER`: `graph.json` has `origin: container` edges and `runtime`; `<out>/drupal-divergence.json` exists with one record of each kind the fixture was built for (`static_only`, `container_only`, `conflict`, `extension_state`); GRAPH_REPORT has `### Container` with status `fresh`;
  - a rerun with no change gives an identical `graph.json` (idempotence through core's merge);
  - changing only the artifact, then `update` → the CLI summary shows 0 re-extracted and the new artifact's fact is in `graph.json`;
  - without the env var → no `origin: container`, no `runtime`, report `unavailable` with the §7 list, no divergence file;
  - a broken artifact → `invalid`, build exit 0;
  - `graphify.build.build_from_json` called outside a run (after `set_current(None)`) adds nothing;
  - §11.2: a boundary service stub whose registry class changes, with no custom file changed → the next build's stub has the new `class_name`;
  - removing `build_from_json` raises `DrupalSeamError`;
  - watch: `_batch_triggers_rebuild([artifact_path])` is True.
- [ ] Steps 2–5 (`-k drupal`, `tests/test_build*.py`, ruff), then commit `feat(drupal): overlay the container on every build, with the divergence log and report`.

---

### Task 7: Carried fix — an `invokes_hook`-only file on a boundary move

**Files:** Modify `graphify/drupal/discovery.py` (`affected_files` / `_boundary_dependent_files`), `graphify/drupal/register.py` if the previous graph.json path must be passed. Test `tests/test_drupal_discovery_incremental.py` (extend).

Behaviour (spec §11.1): when `boundary_changed`, the forced set also contains every file that is the `source_file` of an `invokes_hook` edge in the previous `<out>/graph.json`. Absolute paths are resolved against the root, and only files that exist are kept. A missing or unreadable graph.json adds nothing.

- [ ] Step 1: failing test, reproducing the gap first. A synthetic site with a custom PHP class whose only Drupal edge is `invokes_hook` to a hook declared in contrib's `*.api.php`. A full CLI run, then toggle `drupal.include = contrib` and run `update` → assert that the class file is re-extracted (the incremental summary names it, or the forced set in `drupal-discovery.json` contains it). Confirm the test fails before the fix.
- [ ] Steps 2–5, then commit `fix(drupal): re-extract invokes_hook-only files when the boundary moves`.

---

### Task 8: Acceptance, docs, the closing real run

**Files:** Modify `tests/test_drupal_corpus.py`, `docs/superpowers/specs/2026-09-22-drupal-graph-vocabulary.md`, `docs/superpowers/specs/2026-09-22-drupal-graphify-architecture-design.md`, `docs/superpowers/specs/2026-09-25-drupal-p3-container-design.md`.

- [ ] Step 1: corpus tests for spec §12's FormsRemote criteria that need no live site. The static build of FormsRemote with no artifact gives `unavailable` and no `runtime`. With the live gate: collect into `tmp_path`, build with it, and assert container-only edges > 0 (among them `derivative: True` plugins), confirmed > 0, and overlay time < 1 s (time `run_for_build` directly on the built graph).
- [ ] Step 2: docs.
  - Vocabulary: `origin: container` (and why not `_origin`), `runtime`, `confirmed_by`, `derivative`, `drupal_event` now emitted, `order` on `drupal_hook_impl`, and §4.4's `plugin_implemented_by`/`derives_plugins` now emitted for container-seen plugins.
  - Architecture §4: as built (runners, `php:eval`, artifact path, env var), and the §5 P3 row.
- [ ] Step 3: **closing real run** (spec §13). **Ask the user before the first drush call**, then:
  - `uv run --frozen graphify drupal container /home/user/Projects/FormsRemote --out <scratch>/drupal-container.json` (timed);
  - `GRAPHIFY_DRUPAL_CONTAINER=<scratch>/drupal-container.json uv run --frozen graphify extract /home/user/Projects/FormsRemote --code-only --out <scratch>/p3` twice, then once more without the variable into `<scratch>/p3-static`.
  - Record in spec §15, "Measured and the closing real run": per-source counts (total/custom/applied), confirmed / container-only / conflict / pair_taken, `runtime: absent` per type, divergences per kind with examples, timings, nodes/edges against P2b's 4,808 / 10,327, and the second run's incremental summary line.
  - Check `git -C /home/user/Projects/FormsRemote status --porcelain` before and after (unchanged), and that FormsRemote's `graphify-out/` mtimes are unchanged.
  - Any defect → a `fix(drupal):` commit with a test.
- [ ] Step 4: full suite once, `-k drupal`, ruff. Step 5: commits `test(drupal): P3 acceptance on the reference corpus`, `docs(drupal): P3 vocabulary, architecture, measurements and real run`.
