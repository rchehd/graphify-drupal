# P3 — The container producer, the overlay, and P2b's carried fixes

Status: implemented (branch `drupal-graph`); measured and closed in §15
Date: 2026-09-25
Related: `2026-09-22-drupal-graphify-architecture-design.md` §4, §5;
`2026-09-22-drupal-graph-vocabulary.md` §3.2, §4.2–4.5, §7;
`2026-09-24-drupal-p2b-hooks-boundary-design.md` §9, §10.5;
`docs/drupal-graphify.md` §3.3, §4

---

## 1. Goal

Add what only a running Drupal knows to the graph: the compiled container,
the full route table, what is enabled, the real hook implementation order,
every plugin definition (derivatives included) and the event listeners. It
comes without making the graph depend on a site being bootable on the machine
that builds it.

Three parts, each independent of the next:

1. **A collector**: one PHP script that walks structures Drupal has already built
   and prints JSON. It runs anywhere Drupal boots.
2. **A command**, `graphify drupal container`: it finds a way to run the collector
   (ddev, lando, docker compose, a drush alias, `vendor/bin/drush`, or a
   `.graphifyrc` command), stamps the result and writes the committable
   artifact `drupal-container.json`.
3. **An overlay**: every graph build (`graphify .`, `update`, `watch`) lays the
   artifact over the assembled graph before clustering. It confirms static
   edges, adds what only the container knows, marks `runtime: present|absent`
   and writes a divergence log. Divergences are recorded, never resolved.

A normal build never calls drush. With no artifact the graph is static-only and
says so.

## 2. What the corpus says

Measured on FormsRemote, 2026-09-25:

- Drupal core 11.4.7, drush 13.8.0, ddev project `forms` running (`ddev list`
  reports OK), docroot `web`.
- `ModuleHandler` in 11.4 loads its hook lists from key-value `hook_data`
  (`hook_list`, `includes`, `group_includes`, `packed_order_operations`):
  the complete hook → ordered implementations map, readable without invoking
  anything. `invokeAllWith(string $hook, callable $callback)` is public and
  available as the fallback for Drupal 10.
- `docs/drupal-graphify.md` §3.3 measured the earlier throwaway collector at
  4,890 edges in one call, and 1,396 service id → class entries. The
  architecture spec §5.1 attributes 4 of 5 `shortest_path` misses in the G02
  benchmark to the missing container.
- Every build path assembles through `graphify.build.build_from_json`: the full
  run through `build.build` (which ends in `build_from_json`,
  `build.py:1551`), `update` at `cli.py:2101`, `watch` at `watch.py:1995`.
  Clustering and `to_json` follow in each. `build_from_json` is also how read
  commands (`query`, `path`) load graph.json, so the overlay must be gated to a
  build.
- `__main__.main()` hands every command it does not own to
  `cli.dispatch_command(cmd)` (`__main__.py:751`), a module-level function the
  seam can wrap.

## 3. Scope

### In

- The collector's six sources: services, routes, extensions, hook
  implementations, plugin definitions, event subscribers (§5).
- Runner detection and the `.graphifyrc` override (§4).
- The artifact, its stamp and staleness (§6).
- The overlay, `runtime` marks and the divergence log (§7, §8).
- The GRAPH_REPORT "Container" block (§9).
- P2b's two documented gaps (§11).
- The closing real run on FormsRemote (§13).

### Out

- Display → component, fields, Views (P6; the static config has them).
- `$config` overrides from `settings.php`, and any configuration value,
  environment variable, content or user data (§5.3).
- Resolving `\Drupal::service('x')->method()` receivers (P4 consumes the
  service → class map this phase provides).
- A committed artifact inside FormsRemote: the real run writes to the
  scratchpad only.

## 4. Runners

`graphify/drupal/runners.py` turns a project root into an argv prefix that runs
drush, plus the way to hand it the collector's code.

| Marker | Prefix |
|---|---|
| `.graphifyrc` `drupal.container.command = …` | split with `shlex`, used as given; wins over every marker |
| `.ddev/config.yaml` | `ddev drush` |
| `.lando.yml` | `lando drush` |
| `docker-compose*.yml`, `compose.yaml` | `docker compose exec -T <service> drush`, where `<service>` is `drupal.container.service` or the single service whose name is one of `php`, `web`, `app`, `drupal`, `cli`, `php-fpm`; more than one candidate or none is `AmbiguousComposeService` naming them |
| `drush/sites/*.site.yml` | `drush @<site>` when `drupal.container.alias` names it, or when exactly one alias file with one environment exists |
| `vendor/bin/drush` | that path, run from the composer root |

Markers are checked in the table's order, and the first match wins.

The collector's code is passed to `php:eval` (alias `ev`) as a single argument
and is never written into the project. Under ddev the project is mounted in
the container, so a written script would land in the checkout. The collector
is kept small enough for an argument (target < 64 KiB; `ARG_MAX` on Linux is
2 MiB).

Named errors (`graphify.drupal.runners`): `RunnerNotFound` (lists the markers
looked for, the `.graphifyrc` key and `--print-script`),
`AmbiguousComposeService`, `RunnerUnavailable` (the tool is missing, or ddev or
docker is not running, with the start command), `BootstrapFailed` (drush's
non-zero exit, with its stderr and no Python traceback).

## 5. The collector — `graphify/drupal/container_collect.php`

### 5.1 Contract

It prints one JSON object on stdout and nothing else. Each source runs in its own
`try`. A throwing source is recorded in `errors[]` as `{source, class,
message}` and leaves its key empty; the others are still collected. It
invokes no hook, rebuilds no cache, and writes nothing.

Every path it emits (a class's `ReflectionClass::getFileName()`, an extension's
pathname) is made relative to the composer root. The collector computes that root as
the nearest ancestor of `DRUPAL_ROOT` holding `composer.json`, and puts both
in `site.composer_root` and `site.drupal_root` as absolute in-container paths.
Python strips them, so the artifact never carries a machine path.

### 5.2 Sources

| Key | API | Fields |
|---|---|---|
| `services` | `\Drupal::service('kernel')->getCachedContainerDefinition()`; each `services[id]` is `unserialize()`d | `id`, `class`, `file`, `arguments` (only `@service` references and `%parameter%` names, walked recursively), `tags` (`[{name, attributes}]`, scalar attributes only), `decorates`, `provider` (the first `\Drupal\<ext>\` namespace segment, when it is an installed extension) |
| `aliases` | the same definition's `aliases` | `{alias: target}` |
| `routes` | `\Drupal::service('router.route_provider')->getAllRoutes()` | `name`, `path`, `defaults` keys `_controller`, `_form`, `_entity_form`, `_entity_list`, `_entity_view`, `_title_callback`; `requirements` keys `_permission`, `_custom_access`, `_entity_access`, `_access_*`; `provider` via the route's `_module`/controller namespace when determinable |
| `extensions` | `\Drupal::service('extension.list.module')`, `…theme`, `…profile`, `getList()` plus `getAllInstalledInfo()` | `name`, `type`, `path`, `status`, `weight`, `dependencies` |
| `hooks` | 11.1+: `\Drupal::keyValue('hook_data')->get('hook_list')`. Otherwise `invokeAllWith($hook, fn(callable $l, string $m) => record)` for each hook name in the static registry, passed in the script's header | `{hook: [{module, callable, file}]}` in execution order; `callable` is `Class::method` or a function name |
| `plugins` | every container service whose id starts with `plugin.manager.` and whose instance is a `PluginManagerInterface`, `getDefinitions()` | `{type_id: [{id, class, file, provider, deriver, base_plugin_id}]}`; `type_id` follows the vocabulary §3.4 rule (service id without `plugin.manager.`) |
| `subscribers` | `\Drupal::service('event_dispatcher')->getListeners()` with `getListenerPriority()` | `{event: [{callable, file, priority}]}` for array callables on objects (closures are counted, not listed); `file` is the listener object's class file, not the file declaring an inherited method (§15.4) |
| `site` | `\Drupal::VERSION`, the installed-extension list | `drupal_version`, `enabled_extensions_sha` (sha256 of the sorted `type:name` list), `composer_root`, `drupal_root` |
| `errors` | — | `[{source, class, message}]` |

### 5.3 Never collected

Configuration values, `settings.php`, environment variables, state and
key-value other than `hook_data`, entities and content, users, and scalar
service arguments (they occasionally hold keys). A plugin definition keeps only
the fields above: no labels (`TranslatableMarkup`), no settings.

## 6. The artifact

### 6.1 Location

The default is `<project root>/drupal-container.json`, beside `.graphifyrc`,
meant to be committed. It can be moved with `.graphifyrc`
`drupal.container.artifact = <path>` (relative to the root), and for the
command with `--out`. For a build it can also be set with the environment
variable `GRAPHIFY_DRUPAL_CONTAINER=<path>`, which wins. The real run uses that
variable so nothing is written into FormsRemote. `graphify-out/` is not a
location: core rewrites it and projects gitignore it.

### 6.2 Shape

```json
{
  "schema_version": 1,
  "stamp": {
    "created_at": "2026-09-25T12:00:00Z",
    "runner": "ddev",
    "git_commit": "…", "git_dirty": false,
    "drupal_version": "11.4.7",
    "composer_lock_sha": "…",
    "enabled_extensions_sha": "…",
    "sources_sha": "…"
  },
  "services": [], "aliases": {}, "routes": [], "extensions": [],
  "hooks": {}, "plugins": {}, "subscribers": {}, "errors": []
}
```

The command writes it atomically (temp file, then `os.replace`) with sorted keys
and a stable order of every list, so re-collecting an unchanged site gives an
identical file apart from `created_at`.

### 6.3 Stamp and staleness

`git_commit`, `git_dirty`, `composer_lock_sha` and `sources_sha` are computed
by Python on the host, so a build can recompute them the same way.
`sources_sha` is the sha256 of `(relative path, content sha256)` pairs over
the custom files that shape the container: `*.info.yml`, `*.services.yml`,
`*.routing.yml`, `*.module` and the other procedural suffixes, and every PHP
file the registry knows as a plugin, a plugin manager, a service class, a
`*ServiceProvider` or an event subscriber. It is taken from the current
registry (P2a/P2b) and the boundary's `realm_of`, so only `custom` counts.

A build compares `composer_lock_sha` and `sources_sha` with what it recomputes:

- equal → `fresh`;
- different → `stale`, with reasons (`composer.lock changed`, `N container
  source files changed`, with up to 10 paths);
- the overlay is applied in both cases.

`enabled_extensions_sha` is compared with the static `core.extension.yml` (when
the site has one). A mismatch is not staleness: it is a divergence of kind
`extension_state` per extension (§8).

## 7. The overlay — `graphify/drupal/container_overlay.py`

`apply(G, artifact, context) -> OverlayResult` mutates the assembled graph in
place. It reads no file other than the artifact (already loaded) and never
raises into the build: an internal error leaves `G` as it came in, the status
becomes `container: error (<message>)`, and the build goes on.

### 7.1 Gating

The seam wraps `build.build_from_json`. The overlay runs only when a Drupal
build is current in the process: `discovery.prepare_run` has set a registry for
a root. It runs once per build, guarded against re-entry. `query`, `path` and
the other readers call `build_from_json` without a run and are untouched.

The raw `--no-cluster` write (`extract --no-cluster` in `cli`, `update` and
`watch` with `--no-cluster` in `watch._rebuild_code`) serialises the merged
extraction without `build_from_json`. Both paths pass it through
`build.dedupe_nodes` and then `build.dedupe_edges`, which nothing else in core
calls, so the seam wraps that pair: the second undoes a previous overlay on
the lists, builds a throw-away undirected graph from copies of them through
`build_from_json` (the seam, so the whole step runs: artifact, staleness,
boundary facts, divergence log, report block) and carries back what the
overlay did: the nodes and edges it made, and the keys `_overlay_attrs` names
on every other record. One limitation stays: an incremental `extract
--no-cluster` with no changed file exits before that pair ("no incremental
changes detected"), so an artifact-only change reaches a `--no-cluster` graph
with the next run that changes a file (a clustered build always lays it).

Core's shrink guards never count what the overlay made as a lost node (the
next overlay makes it again, or the artifact no longer has it). `update` and
`watch` compare node counts in `watch._check_shrink`; its wrapper leaves nodes
carrying `_overlay` or `_overlay_file` out of both graphs in a Drupal run.
`build_merge` (and `merge_raw_extraction`) load the baseline through
`build._load_existing_graph`; its wrapper undoes the previous overlay on the
loaded lists (the same rule as §7.2), so `extract --no-dedup`'s #479 guard
compares static with static. Without either, a removed artifact, or one with
a service fewer, made `update` refuse the write and `extract --no-dedup` fail.

### 7.2 Undo first

Before applying, the overlay removes what a previous overlay left in `G`
(graph.json carries it into `update` and `watch`):

- edges with `origin: container`;
- nodes with `_overlay: true` (created only by the overlay);
- on every other node and edge, the attributes listed in its `_overlay_attrs`,
  and then `_overlay_attrs` itself.

The result is that the overlay applied twice equals the overlay applied once,
and the overlay applied then undone equals the static graph.

### 7.3 Binding to existing nodes

Drupal ids are built with the producers' own `make_id` rules
(`drupal:service:<id>`, `drupal:route:<name>`, `drupal:extension:<name>`,
`drupal:plugin:<type>:<id>`, `drupal:hook:<name>`, `drupal:event:<name>`). A PHP class or
method is found in `G` by `source_file` equal to the fact's `file` and the
node's short name equal to the class, method or function name. The fact's
`file` is relative to the composer root (§5.1) and a node's `source_file` to
the scan root, which can sit below it (a scan of `web/`); both are resolved to
the same absolute path before they are compared, and realms are read from the
fact's path under the composer root. When there is no such node, no edge is made. The name goes on the Drupal
node as `class_name` (or `callable`), as P2a and P2b already do.

### 7.4 Mapping

The vocabulary's relations, nothing new:

| Fact | Edge or attribute |
|---|---|
| service → class | `service_implemented_by` |
| argument `@x` / `%p%` | `injects_service` / `injects_parameter` |
| tag | `tagged_as`, scalar tag attributes on the edge |
| `decorates` | `decorates` |
| alias | `aliases: [...]` on the target service node |
| service provider module | `declares_service` |
| route | `declares_route`, `routes_to` (→ method, else class), `routes_to_form`, `requires_permission`, `access_checked_by` |
| extension | `depends_on_module` |
| hook implementation | P2b's shape: `drupal_hook_impl` node (`make_id("drupal", "hook_impl", module, hook)`, `module`, `hook_name`, `function` or `class_name`+`method`), `implements_hook` (extension → hook), `hook_implemented_by` (impl → function or method node); `runtime_order` (index in the list) on the impl node, beside P2b's own `order` (the `#[Hook(order: …)]` text), which it leaves alone |
| plugin | `provides_plugin`, `plugin_of_type`, `plugin_implemented_by`, `derives_plugins`; a plugin of a type whose `yaml_name` is a P1 links family (`links.menu`, `links.task`, `links.action`, `links.contextual`) is P1's link node (`drupal:menu_link:<id>`, …), never a `drupal_plugin` beside it (§15.4) |
| subscriber | `subscribes_to_event` (class → event), `priority` on the edge |

`plugin_implemented_by` and `derives_plugins` were scheduled for P4 (vocabulary
§4.4). For container-seen plugins they come now, because the container gives
class and file. P4's static ones will meet them through `confirmed_by`.

### 7.5 Three outcomes per edge

- **Static has the same edge** (same pair, same relation): the edge gets
  `confirmed_by: container`. The graph collapses parallel edges, so confirmation
  is an attribute, never a second edge.
- **Container only**: a new edge, `origin: container`, `confidence:
  EXTRACTED`, `confidence_score: 1.0`, `source_file: drupal-container.json`.

`origin` (no underscore) is the overlay's marker, and `_origin` is left to
core. `extract()` stamps `_origin: "ast"` on every node and edge it returns
(`extract.py:8202`), and `watch` scopes eviction by it (`_origin == "ast"`),
so a `_origin: container` would change how core's incremental paths treat
the edge. Overlay edges and nodes carry `_origin: "ast"` like everything
else the Drupal producers emit. The vocabulary's `_origin: container`
(§1.2) is realised as `origin: container`.
- **Conflict**: static says one target, the container another, for a relation
  that has one target per source (`service_implemented_by`,
  `plugin_implemented_by`, `routes_to`, `routes_to_form`, `decorates`).
  Both edges stay, and the pair goes to the divergence log. An attribute the
  overlay would add (`class_name`, `route_path`) that already holds another
  value is a conflict too, recorded and never overwritten; a class with or
  without its leading `\` and a route path with or without its leading `/`
  (Symfony's `Route::setPath` prepends it) are the same value.

### 7.6 The boundary

The P2b rule holds. A fact whose subject is `custom` is applied in full. A fact
about a boundary thing (core, contrib, vendor, by `realm_of` of its file or
provider) is applied only to a node already in `G`. It adds facts such as
`class_name`, a route's `path` and a hook implementation's `runtime_order`, and marks it. A new
boundary node is created only as the direct target of a custom fact, for
example a custom service injecting `entity_type.manager`. Such a node is a stub
like P2b's (`boundary: true`, `realm`, `_overlay: true`). `drupal.include`
widens "custom" exactly as in P2b.

### 7.7 `runtime`

When an artifact is applied, every node of type `drupal_service`,
`drupal_route`, `drupal_extension`, `drupal_plugin` and `drupal_hook_impl`
gets `runtime: present` if the container knows it, else `runtime: absent`.
With no artifact, no node carries `runtime`: absent means unknown, not gone.
A theme's `drupal_hook_impl` gets no `runtime` either: the collector reads
module hook lists (`hook_data`, `invokeAllWith`), which never hold a theme's
implementations, so the container cannot say (§15.4).

### 7.8 P2b's boundary facts ride the same step

P2b §9's "boundary facts go stale on a registry-only change" (a stub
re-materialised only when a referencing file is re-extracted) is closed here.
The overlay also re-applies the current registry's facts to every boundary
stub in `G`, with or without an artifact, recorded in `_overlay_attrs` like
the container's. A registry-only change then reaches the graph on the next
build without re-extracting anything.

## 8. Divergences — `graphify/drupal/divergence.py`

The log lives at `<out>/drupal-divergence.json`, written beside
`drupal-discovery.json` on every build that has an artifact and removed on one
that has none. Each record is:

```json
{"kind": "static_only", "subject": "drupal_route_forms_core_admin",
 "type": "drupal_route", "static": {...}, "container": null,
 "possibly_stale": false}
```

| Kind | Meaning |
|---|---|
| `static_only` | a static node of a covered type the container does not know (a shipped but disabled module's service or route; a documented but unimplemented hook) |
| `container_only` | a container fact with no static counterpart (a derivative plugin, a `RouteSubscriber` route, a `ServiceProvider` service) |
| `conflict` | §7.5's one-target relations disagreeing |
| `extension_state` | an extension enabled in one of `core.extension.yml` and the container but not the other |

Only custom subjects are logged, plus boundary subjects already in `G`.
`possibly_stale` is true for every record when the artifact is `stale` and the
record's subject lives in a file among the stale reasons, or anywhere when
`composer.lock` changed. Records are sorted by `(kind, subject)`.

## 9. Report

GRAPH_REPORT's "Drupal coverage" section gains a "Container" block:

- status: `fresh`, `stale (reasons)`, `unavailable`, `invalid (reason)`
  or `error (message)`;
- stamp: commit, `created_at`, runner, Drupal version;
- per source: total in the artifact / custom / applied to the graph;
- edges: confirmed, container-only, conflicts;
- `runtime: absent` count per type;
- divergence counts per kind, and the first 10 records;
- the collector's `errors[]`;
- when `unavailable`, the vocabulary §7 list of what a static-only graph cannot
  know.

## 10. The command and the seam

`graphify drupal container [PATH] [--out FILE] [--print-script]
[--runner-command CMD]`:

- `--print-script` prints the collector and exits 0, for running it by hand
  anywhere;
- otherwise it resolves the runner (§4), runs the collector, validates
  `schema_version` and the top-level keys, strips the absolute roots, adds the
  host half of the stamp, writes atomically, and prints a one-screen summary
  (counts per source, errors, path written). On any failure the exit code is
  non-zero and an existing artifact is left untouched. The registry it builds
  for the hook list and the stamp (`prepare_run`) is written to a temp dir,
  never beside the artifact or into the site's out dir.

The collector ships as package data: `pyproject.toml`'s
`[tool.setuptools.package-data]` gains `"graphify.drupal" =
["container_collect.php"]`.

`register.py` gains two wrappers, each asserting the shape it relies on and
raising `DrupalSeamError` otherwise:

- `cli.dispatch_command`: `cmd == "drupal"` goes to
  `graphify.drupal.container.main(sys.argv[2:])`; everything else goes to core
  unchanged;
- `build.build_from_json`: the gated overlay (§7.1) runs on the returned graph;
- `build._load_existing_graph`, `build.dedupe_nodes`/`build.dedupe_edges` and
  `watch._check_shrink`: the incremental baseline, the raw `--no-cluster`
  write and core's shrink guards (§7.1).

Core's help guard answers `graphify drupal container --help` with its generic
line, so the command's usage is printed on a bad argument instead.

## 11. Carried fixes (P2b)

1. **An `invokes_hook`-only file is not forced on a boundary move** (P2b
   §10.5, I3). When `boundary_digest` changes, the forced set also takes every
   in-graph file that is the `source_file` of an `invokes_hook` edge, read from
   the previous graph.json. A test reproduces the gap first: toggle
   `drupal.include` on a fixture whose only cross-boundary edge is an
   `invokes_hook`, and assert that the file is re-extracted.
2. **Boundary facts go stale on a registry-only change** (P2b §9): closed by
   §7.8. A test changes a boundary service's class in the registry with no
   custom file changed, and asserts that the stub's `class_name` follows on the
   next build.

## 12. Acceptance

On the fixtures:

- overlay idempotence: apply twice gives a byte-identical graph.json to apply
  once; apply then undo gives the static graph;
- no artifact: no `runtime`, no `origin: container`, report says
  `unavailable`, no divergence file;
- an invalid artifact (bad JSON, wrong `schema_version`) gives `invalid`, and
  the build succeeds;
- changing only the artifact, `update` and `watch` re-extract zero files and
  the overlay reflects the new artifact;
- each of the four divergence kinds appears on a fixture built for it;
- runner detection per marker, the `.graphifyrc` override, and each named error,
  with `subprocess` mocked;
- the collector's output contract: synthetic artifacts are built inside the
  tests (the allow-list admits only `tests/test_drupal_*.py`, so there is no
  fixture directory); a corpus test, skipped without the corpus and a
  running ddev, collects from FormsRemote into `tmp_path` and validates the
  schema;
- seam: removing either wrapped symbol raises `DrupalSeamError`;
- §11's two tests.

On FormsRemote (§13):

- the command collects through ddev with `errors[]` empty or each entry
  explained;
- the overlay adds container-only edges, among them derivative plugins, and
  confirms static ones;
- the overlay costs under 1 s over the static build;
- an unchanged rerun re-extracts no more than P2b's two core files;
- FormsRemote's `git status --porcelain` and its `graphify-out/` mtimes are
  unchanged.

## 13. The closing real run

1. Ask the user before the first drush call: bootstrapping Drupal in their
   running ddev may warm caches inside the container or database.
2. `uv run --frozen graphify drupal container /home/user/Projects/FormsRemote
   --out <scratchpad>/drupal-container.json`, timed.
3. `GRAPHIFY_DRUPAL_CONTAINER=<scratchpad>/drupal-container.json uv run
   --frozen graphify extract /home/user/Projects/FormsRemote --code-only --out
   <scratchpad>/p3-run`, twice (full, then unchanged), then once more without
   the variable.
4. Record counts: per source (total, custom, applied), confirmed,
   container-only, conflicts, `runtime: absent` per type, divergences per kind,
   timings, and the nodes and edges against P2b's 4,808 / 10,327.
5. Verify FormsRemote is untouched.

The findings go in §15 of this document at close.

## 14. Risks

- **The collector meets a Drupal version it does not know.** Each source is
  guarded. `hook_data` is 11.1+ with an `invokeAllWith` fallback, and
  `schema_version` lets the Python side refuse what it cannot read.
- **A plugin manager throws in `getDefinitions()`** (a broken contrib
  deriver). It becomes a per-type error, not a failed run.
- **The artifact is large.** Services, routes and plugins of a whole site
  amount to a few MB of JSON. It is committed deliberately; the overlay reads
  it once per build.
- **Id agreement for PHP nodes**: binding by `source_file` and short name
  (§7.3) survives core's id scheme changes. What it cannot bind becomes an
  attribute, never a guessed edge.
- **`build_from_json` is core's reader as well as its builder.** §7.1's
  gating is what keeps `query` from mutating what it reads. A test pins that a
  read never applies the overlay.
- **The stamp misses database-only changes** (a module enabled in the UI without
  a config export). `extension_state` divergence is the net for exactly that.

## 15. Measured and the closing real run

2026-09-25, FormsRemote at `60b010db` (plus the untracked `docs/` the user
keeps there), ddev project `forms` running, Drupal 11.4.7. The user approved
one live collector run; everything after it reads that artifact.

### 15.1 The collector

`uv run --frozen graphify drupal container /home/user/Projects/FormsRemote
--out <scratch>/drupal-container.json`: exit 0, **4.1 s** wall (2.3 s user),
2.4 MB, runner `ddev`, stamp `git_commit 60b010db…`, `git_dirty: true` (the
untracked `docs/`).

| source | entries |
|---|---|
| services | 1,317 |
| aliases | 363 |
| routes | 1,479 |
| extensions | 392 (modules 206 enabled / 170 not, themes 8 / 5, profiles 1 / 2) |
| hooks | 652 names, 1,387 implementations (no `ProceduralCall` entries: 11.4's `hook_list` names functions) |
| plugins | 109 types, 3,810 definitions (1,776 derivatives) |
| subscribers | 46 events, 193 listeners, 0 closures |

`errors[]` has one entry, explained: `plugins: Error: plugin.manager.font_colors:
Class "Drupal\ckeditor5_font\FontColorsManager" not found` — contrib
`ckeditor5_plugin_pack_font`'s services file declares a manager class the
package does not ship. Every other source and plugin type was collected.

Ledger deviations confirmed on the live container: every service's `tags` is
`[]` (the runtime dump carries none; `tagged_as` stays static-only), and no
service carries `decorates` (§15.5).

### 15.2 The overlay (after §15.4's fixes)

`GRAPHIFY_DRUPAL_CONTAINER=<scratch>/drupal-container.json uv run --frozen
graphify extract /home/user/Projects/FormsRemote --code-only --out
<scratch>/run`, twice, then once without the variable into `<scratch>/static`.
Status `fresh`.

| source | total | custom | applied |
|---|---|---|---|
| services | 1,317 | 73 | 116 |
| aliases | 363 | 25 | 64 |
| routes | 1,479 | 36 | 70 |
| extensions | 392 | 23 | 215 |
| hooks | 1,387 | 63 | 63 |
| plugins | 3,810 | 122 | 127 |
| subscribers | 193 | 2 | 2 |

`applied` above `custom` is boundary facts annotating boundary nodes already
in the graph (§7.6): extension stubs, core services custom code injects, core
link stubs named as parents.

- Edges: **confirmed 492** (`injects_service` 138, `plugin_of_type` 82 — P1's
  links —, `depends_on_module` 66, `declares_service` 58,
  `hook_implemented_by` 40, `implements_hook` 37, `declares_route` 36,
  `requires_permission` 35); **container-only 329**
  (`service_implemented_by` 73, `injects_service` 40, `plugin_of_type` 40,
  `plugin_implemented_by` 38, `provides_plugin` 37, `routes_to_form` 25,
  `hook_implemented_by` 23, `implements_hook` 23, `declares_service` 15,
  `routes_to` 11, `subscribes_to_event` 2, `access_checked_by` 1,
  `derives_plugins` 1); **conflict 0**; **pair_taken 86** (82 are
  `provides_plugin` yielding to P1's `declares_*` on the same pair).
- Nodes the overlay made: 121 — `drupal_plugin` 40 (4 derivatives: three
  `rest` resources and `eca_event:…webform_submission_insert`),
  `drupal_service` 24 (15 the `#[Hook]` classes Drupal registers as services,
  9 boundary stubs they inject), `drupal_hook_impl` 23, `drupal_hook` 19,
  `drupal_plugin_type` 14, `drupal_event` 1 (`routing.route_alter`, from
  the two custom route subscribers).
- `runtime: absent`: `drupal_service` 9, `drupal_extension` 2 (present:
  extension 192, service 141, route 70, hook_impl 60, plugin 40).
- Divergences: `container_only` 78 (plugin 40, hook_impl 23, service 15),
  `static_only` 11, `conflict` 7, `extension_state` 0. Examples:
  - `container_only`: `drupal_hook_impl_eca_custom_preprocess_node`,
    `…_custom_forms_user_presave`, `…_eca_custom_node_access` — the
    variable-segment implementations P2b leaves as candidates;
    `drupal_service_drupal_eca_custom_hook_caseviewerhooks`;
    `drupal_plugin_webform_handler_webform_integration`.
  - `static_only`: the four decorator `*.inner` ids and the abstract parents
    `logger.channel_base`, `default_plugin_manager` (§15.5);
    `cache.backend.null`, `config.schema_checker`,
    `logger.channel.config_schema` from `web/sites/development.services.yml`,
    which this site does not load; the extensions `default` and
    `development`, owners `web/sites/*.services.yml` imply (P2b §10.2).
  - `conflict`: seven `route_path`s under `/admin/structure/webform/…` that
    contrib webform's `WebformRouteSubscriber` moves to `/admin/webform/…`
    (for example `drupal_route_webform_sync_settings`). Real divergences.
- Nodes / edges: **4,946 / 10,724** with the artifact; **4,825 / 10,395**
  static (P2b: 4,808 / 10,327). The overlay's +121 / +329 are exactly the
  nodes above and the container-only edges. The static +17 / +68 over P2b
  are all FormsRemote `9f933cdb` ("Change SharePoint from own API to Microsoft
  Graph": `SharepointClient.php`, the new `SharepointGraphUploadTest.php`,
  the deleted `SharepointCredentialsTest.php`, an `@http_client` argument),
  diffed id by id against P2b's closing graph.json; no static node or edge
  changed for another reason.
- The static run: container `unavailable`, no node with `runtime`, no
  `origin: container`, no `drupal-divergence.json`.
- `graph.json` 7.6 MB (static 7.1 MB).

Timings: full build with the artifact 10.8 s, unchanged rerun 8.5 s, static
10.8 s (P2b measured 7.5 s / 6.9 s; the static build without an artifact
takes as long as the one with it, so the difference is not the overlay). `run_for_build` on the built graph (artifact load, staleness,
`apply`, divergence log, report block): **0.60 s** (0.31 s of it the staleness
`sources_sha`), under §12's 1 s.

Second run: `incremental summary: 1142 files cached/unchanged, 2
re-extracted, 0 deleted` — core's two never-stamped re-queues, as in P2b
§10.2 (`webform_integrations_logs.links.action.yml`, empty, and
`docker/mssql/seed.sql`, no `tree_sitter_sql`). Same node and edge counts as
the first run.

FormsRemote untouched: `git status --porcelain` (`?? docs/`) identical
before the collector and after every run and the corpus tests; the
pre-existing `graphify-out/` (`cache/stat-index.json`) has the same mtimes
and sizes, file for file; no file under the checkout outside `.git/` is newer
than a marker touched before the collector (only `.git/`'s own mtime moved,
from the `git status` checks); no `drupal-container.json` or
`drupal-divergence.json` there.

### 15.3 The first overlay, before the fixes

The same artifact through the code of `469a579`: 5,027 / 10,886; confirmed
410, container-only 491, pair_taken 4; subscribers custom 0 / applied 0;
divergences `container_only` 160, `static_only` 13, `conflict` 10;
`runtime: absent` service 9, extension 2, hook_impl 2; `run_for_build` 1.54 s.

### 15.4 Defects found and fixed

`fix(drupal): what the FormsRemote container showed the overlay got wrong`:

1. **Custom route subscribers read as core.** A listener's `file` was the
   file declaring its method, and a `RouteSubscriberBase` subclass inherits
   `onAlterRoutes`, so both custom subscribers were filed under
   `core/lib/Drupal/Core/Routing/RouteSubscriberBase.php` (subscribers custom
   0). The collector now emits the listener object's class file; the overlay
   prefers the class file (from the services' classes, else PSR-4), so an
   artifact collected before the fix binds too.
2. **82 duplicated link plugins.** `menu.link`, `menu.local_task`,
   `menu.local_action` and `menu.contextual_link` definitions made
   `drupal_plugin` nodes beside P1's `drupal_menu_link` / `local_task` /
   `local_action` / `contextual_link` nodes, and 82 false `container_only`
   records. A type whose `yaml_name` is a P1 links family now maps to P1's id
   and type: the 82 static `plugin_of_type` edges are confirmed instead.
3. **Route paths without a leading slash conflicted.** Three custom routes
   (`entity.system.settings`, `entity.webform_integrations_token.settings`,
   `webform_integrations_printable.print_pdf`) write `path: 'admin/…'` /
   `'print/…'`; the container's paths start with `/`. They
   are one value now; the static text is kept.
4. **Theme hook implementations marked `absent`.** `govuk_forms_theme()` and
   `govuk_forms_theme_suggestions_alter()` are not in any module hook list.
   They get no `runtime` and no `static_only` record.
5. **The overlay took 1.5 s.** 26,198 `realm_of` calls (two phases over
   ~4,000 artifact files); memoised per overlay, 4,754 calls, 0.60 s.

### 15.5 Found, not fixed (for review)

- **`decorates` never comes from a live container.** Drupal's
  `OptimizedPhpArrayDumper` gives every private service the id
  `private__<hash>`, so the decorator's `<id>.inner` argument that §5.2's
  heuristic looks for is never visible: all four custom decorators (webform_integrations' message manager
  decorator, eca_custom's three filters) carry
  `arguments: []`, `decorates: null`. The static `decorates` edges stand
  unconfirmed. The decoration is still in the artifact as its result — the
  decorated id is an alias of the decorator (`webform.message_manager →
  webform_integrations.webform_message_manager_decorator`) — so a later
  change could confirm a static `decorates` edge from the alias; it needs a
  collector change and a second live run to verify, so it is left for review.
  The four `*.inner` `static_only` records and `runtime: absent` marks follow
  from the same hashing.
- **Abstract parents** (`logger.channel_base`, `default_plugin_manager`) are
  `absent`: the compiled container drops abstract definitions. Accurate, and
  expected noise in `static_only`.
- An artifact outside the scan root (the real run's scratch file) gives
  overlay items a `source_file` that climbs out of the root
  (`../../../../tmp/…/drupal-container.json`): the Task 6 ruling
  (relative path) applied to an out-of-root file. Harmless to the
  incremental runs (identical graph, 0 deleted); a committed artifact at the
  root gives `drupal-container.json`.
- Recorded rulings, as built: the runtime hook index is `runtime_order`
  (P2b's `order` keeps the attribute text); boundary facts (§7.8) are applied
  inside `apply`, after undo, with or without an artifact; `tags` are always
  `[]` from a live D11 container.
