# P2a — Plugin discovery: learned plugin types, YAML plugins, the inventory

Status: approved in brainstorming, 2026-09-23
Date: 2026-09-23
Related: `2026-09-22-drupal-graph-vocabulary.md` (§3.4, §4.4, §5),
`2026-09-22-drupal-graphify-architecture-design.md` (§5 — P2 is split into P2a
plugins and P2b hooks), `2026-09-23-drupal-p1-module-yaml-design.md`,
`2026-09-23-drupal-p1b-configuration-design.md`

All figures were measured on `/home/user/Projects/FormsRemote` (Drupal 11.2,
`web/` docroot), excluding `vendor/`, `node_modules/` and `tests/` trees.

---

## 1. Goal

Any Drupal module may invent a plugin type. P1 knew a fixed list of YAML
families; P2a **learns** the plugin types a site has from its plugin managers,
turns the YAML families those managers read into graph nodes, and reports —
on every run — what it could not recognise, so that "not found" is never
presented as "absent".

## 2. What the corpus says

| Measured | core | contrib | custom | total |
|---|---|---|---|---|
| services with `parent: default_plugin_manager` | 21 | 44 | 2 | 67 |
| concrete plugin-manager classes (full-tree scan) | — | — | — | 117 (measured 122, §8) |
| … reached from `*.services.yml` by class → PSR-4 → `extends` chain | — | — | — | 117 − 3 (measured 118, §8) |
| managers overriding `getDiscovery()` | 14 | 4 | 0 | 18 |
| YAML plugin families read by a manager, not P1's | — | — | — | 14 families, 72 files, 260 plugins |
| module-root `*.yml` no P1/P1b family claims | — | — | — | 243 (80 are `.gitlab-ci.yml`) |

The three managers the services chain misses, and why:

| Manager | Why the chain misses it |
|---|---|
| `migrate_drupal`'s `MigrationPluginManager` | swapped in by `ServiceProvider::alter()` in PHP |
| `MetatagViewsCachePluginManager`, `backup_migrate\Core\Plugin\PluginManager` | not services; constructed with `new` |

Corrected in §8: `backup_migrate`'s `PluginManager` is not a Drupal manager; the
chain also misses `mailsystem`'s `MailsystemManager` and `symfony_mailer`'s
`MailManagerReplacement`, both swapped in by a `ServiceProvider::alter()`.

Plus two shapes a naive chain would miss, present on the corpus:
`MenuLinkManager` is a manager only through `implements MenuLinkManagerInterface`
(which extends `PluginManagerInterface`), and `Drupal\ckeditor5_font\…` lives in
module `ckeditor5_plugin_pack_font`, so PSR-4 by machine name cannot find it
(corrected in §8: that class is absent from the corpus — a dead service).

`YamlDiscovery` is also used by classes that are **not** plugin managers:
`RouteBuilder` (`routing`), `PermissionHandler` (`permissions`), `MigrationState`
(`migrate_drupal`). Only managers contribute learned families.

Learned YAML families on the corpus (files / plugins): `ckeditor5` 43/109,
`link_relation_types` 6/107, `layouts` 4/10, `field_type_categories` 5/9,
`config_translation` 5/6, `oauth2_scopes` 1/5, `entity_print_export_types` 1/3,
`modeler_api.contexts` 1/3, `modeler_api.template_tokens` 1/2, `icons` 2/2,
`notification_senders` 1/2, `modeler_api.dependencies` 1/1,
`notification_messages_factory` 1/1, `help_topics` 0/0. P1 families that are
plugin types: `links.menu` 122/250, `links.task` 108/393, `links.action` 60/95,
`links.contextual` 16/28, `breakpoints` 5/22.

## 3. Scope

### In
- `drupal_plugin_type` for every plugin manager, learned from source.
- `drupal_plugin` for every YAML-discovered plugin of a learned type whose
  family no other phase owns.
- `plugin_of_type` from P1's link and breakpoint nodes to their learned type.
- `graphify-out/drupal-discovery.json` (the registry, an output) and
  `graphify-out/drupal-inventory.json` plus a section in `GRAPH_REPORT.md`.
- `graphify watch` rebuilds on Drupal YAML changes (a P1/P1b gap: `.yml` is not
  in core's `_CODE_EXTENSIONS`, so a YAML-only batch only set the
  `needs_update` flag).

### Out, and why
- PHP-discovered plugin instances (annotations, attributes) and binding
  `class_name`/`deriver` to PHP class nodes → P4.
- Hooks, including `alter_hook` as a hook node → P2b (P2a records the name).
- SDC (`*.component.yml`) instances → P5; migrations (`migrations/*.yml`,
  `*.migrate_drupal.yml`) → P6. Their types are recognised and marked deferred.
- Following `ServiceProvider::alter()` beyond an inventory entry → P3, the
  container, which is authoritative for anything the container rewrites.

## 4. Vocabulary

### 4.1 Nodes — `layer: plugin`

**`drupal_plugin_type`**, id `make_id("drupal", "plugin_type", <type>)`.
`<type>` is the manager's service id without a leading `plugin.manager.`
(`block`, `menu.link`, `ckeditor5.plugin`); a service id without that prefix is
used whole; a manager that is not a service uses `class:<FQCN>`. Label `<type>`.

| Attribute | Value |
|---|---|
| `plugin_type` | `<type>` |
| `discovery` | `annotation`, `attribute`, `yaml`, `mixed` (more than one), or `dynamic` (`getDiscovery()` present but not statically readable) |
| `manager_service` | service id, or absent |
| `manager_class` | FQCN |
| `registered` | `true` when reached from a service, else `false` |
| `subdir` | `Plugin/Block`, from `parent::__construct`'s first argument when a literal |
| `interface`, `annotation_class`, `attribute_class` | FQCNs from `parent::__construct`, when literal or `::class` |
| `yaml_name` | the `YamlDiscovery`/`YamlDiscoveryDecorator` name, when any |
| `alter_hook` | the `alterInfo('x')` literal, when any |
| `deferred_to` | `P5` for SDC, `P6` for migrations, else absent |

`source_file`/`line` point at the manager class declaration; `realm` follows the
file.

**`drupal_plugin`**, id `make_id("drupal", "plugin", <type>, <plugin_id>)`,
one per top-level key of a learned-family file. Label `<plugin_id>`.
Attributes `plugin_id`, `plugin_type`, `provider` (the owning extension),
`class_name` (from `class:`), `deriver` (from `deriver:`). Nothing else from the
definition reaches the graph (labels, weights and settings are values).

### 4.2 Edges

| Relation | Source → target | Emitted by |
|---|---|---|
| `defines_plugin_type` | extension → plugin_type | the manager's owner: the extension of its services file, or of its class file when not a service; `core` for `core/lib` and `core.services.yml` |
| `plugin_manager_for` | service (P1 id) → plugin_type | registered managers |
| `provides_plugin` | extension → plugin | the learned-family file's owner |
| `plugin_of_type` | plugin → plugin_type | every `drupal_plugin`, and every P1 `drupal_menu_link`, `drupal_local_task`, `drupal_local_action`, `drupal_contextual_link`, `drupal_breakpoint` whose family is some type's `yaml_name` |

Plugin-type nodes are emitted by a synthetic extraction of the registry (§5.4),
so they obey the same "a file owns its nodes" rule as everything else.

## 5. Mechanism

### 5.1 The registry is built at the start of every `detect`

`discovery.build_registry(web_root) -> Registry`:

0. **Only Drupal trees.** A registry is built only when the scan root has a
   Drupal marker: a web root at, below or above it (step 1), or a `*.info.yml`
   directly in it (a single extension checkout). Any other tree is not walked:
   no registry, no `drupal-discovery.json`, no inventory — and a stale
   `drupal-inventory.json` in the out dir is removed.
1. **Web root.** The scan root if it holds `core/lib/Drupal.php`; else its
   `web/` or `docroot/` child if that does; else the nearest ancestor that
   does; else none — the scan root is used alone and the inventory records
   `no_drupal_core`.

   **Scope.** The registry is knowledge about the site, not graph content. Its
   walk honours explicit user intent — `.graphifyignore`, `--exclude`
   (`extra_excludes`) — and core's noise-dir pruning, through
   `graphify.detect.ignored_predicate(root, extra_excludes=…, gitignore=False)`,
   but **not `.gitignore`**, by design and whatever `detect`'s `gitignore` says:
   a composer-managed site gitignores `web/core` and `web/modules/contrib`,
   which define almost every plugin type. The graph itself still honours
   `.gitignore`; a `plugin_of_type` edge from a custom file to a type defined
   in an ignored tree points at a materialised type node (`missing: true`,
   labelled by the type's name). An ignored directory is never descended; the
   predicate is asked only about directories before descending and about the
   files the registry collects (`*.info.yml`, `*.services.yml`,
   `src/**/*Manager.php`, `*ServiceProvider.php`, extension-root `*.yml`), never
   about every walked file. When the web root is an ancestor of the scan root
   the walk covers the web root, outside the scan root, and those paths are not
   subject to the scan's ignore rules (the predicate is anchored at the scan
   root).
2. **Extensions.** Every `<name>.info.yml` gives a machine name and directory;
   PSR-4 maps `Drupal\<name>\` → `<dir>/src`, `Drupal\Core\` →
   `core/lib/Drupal/Core`, `Drupal\Component\` → `core/lib/Drupal/Component`.
3. **Services.** Every `*.services.yml` (P1's loader). A service's class is its
   `class:`, else its id when the id is an FQCN, else its same-file `parent:`'s
   class. `_defaults`/`_instanceof` are skipped (P1).
4. **Manager test.** A class is a manager when its `extends` chain reaches
   `Drupal\Core\Plugin\DefaultPluginManager`, or its `implements` list — or an
   interface's `extends` chain — reaches
   `Drupal\Component\Plugin\PluginManagerInterface`. Names resolve through the
   file's `namespace` and `use` statements. Abstract classes are not managers.
5. **Second net.** Every `src/**/*Manager.php` not already reached is tested
   the same way; a manager found here is a type with `registered: false`.
6. **Reading a manager** (tree-sitter PHP, `tree_sitter_php.language_php`, as
   core uses): the `parent::__construct(...)` arguments; an overridden
   `getDiscovery()` — `new YamlDiscovery('<name>', …)` (first argument),
   `new YamlDiscoveryDecorator($d, '<name>', …)` (second argument),
   `YamlDirectoryDiscovery` (directory discovery: `deferred_to` P6 when the
   manager is migrations', else `discovery: dynamic`), any other discovery
   class (`dynamic`); `alterInfo('<name>')`. Inherited `getDiscovery()` from a
   parent manager class in the chain counts. A discovery built in
   `__construct()` (assigned to `$this->discovery`, as `modeler_api`'s managers
   do) is read the same way; when a class has both, `getDiscovery()` wins.
   `read_php_class` reads the declaration named after the file (PSR-4), else
   the first one, so a BC shim declared before the class does not win.
7. **Deferred families** are a fixed table: `component` (SDC) → P5, the
   migrations manager and `migrate_drupal` → P6. A deferred type is never
   `dynamic` and never `managers_unresolved`: it is listed as deferred (both
   families are YAML-defined, and P5/P6 read them).
8. **Service providers.** A `*ServiceProvider.php` whose `alter()` calls
   `->setClass()` is a `service_provider_alter` entry; each class it sets that
   is a manager no type covers gets its own entry of that reason, naming the
   class (P3 follows it).

Performance budget: under 5 seconds on the corpus; measured in the plan.

### 5.2 Classification and dispatch

`families.is_drupal_file` and `families.drupal_extractor` gain one more case,
after P1b's configuration and P1's table: `<ext>.<yaml_name>.yml` in an
extension root, where `<yaml_name>` is a learned, non-deferred family and not a
P1 family → `yaml_plugins.extract_drupal_yaml_plugins`. The family is an exact
lookup on the name after the owner's first dot segment: `eca.modeler_api.contexts.yml`
is looked up as `modeler_api.contexts`, whole, and never as `contexts`.

### 5.3 Worker processes

Core extracts uncached files in a `ProcessPoolExecutor`; a worker does not see
the parent's in-process registry. The `detect` wrapper writes
`drupal-discovery.json` and sets `GRAPHIFY_DRUPAL_DISCOVERY` to its absolute
path; `discovery.current_registry()` returns the in-process registry, else loads
that file once per process. Both `fork` and `spawn` inherit the environment.
The file also carries `force_miss` (§5.5), so a spawn/forkserver worker, which
re-checks the cache before extracting, misses the same files as the parent.

### 5.4 Plugin-type nodes come from a synthetic file

Plugin-type nodes must have an owning `source_file`, or an incremental run
cannot keep or evict them. Each type node is emitted by the extraction of its
**manager class file**: the `_get_extractor` wrapper composes core's PHP handler
with `discovery.extract_plugin_types(path)` for a PHP file that is some type's
manager (exactly as P1b composes `settings.php`). The type's edges
(`defines_plugin_type`, `plugin_manager_for`) are emitted there too.

### 5.5 A changed registry re-extracts what it affects

`prepare_run`, inside the `detect` wrapper, compares the new registry with the
previous run's `drupal-discovery.json` (read before it overwrites the file, and
kept in process state). Per `yaml_name` and per manager class it compares the
emitted facts; for every changed family, the files `*.<yaml_name>.yml` in
extension roots, and for every changed manager its class file, are:
- forced to miss the AST cache — a wrapper on `cache.load_cached` returns
  `None` for them this run (core's cache directory is namespaced by
  `_EXTRACTOR_VERSION` and `_cleanup_stale_ast_entries` deletes other
  namespaces, so putting the registry hash into the version would re-parse the
  whole project on any manager change);
- added to `paths` on incremental runs by the `extract()` wrapper (the same
  widening as P1b's collision group).

A newly learned family needs neither: its files had no nodes, and core
re-queues zero-node files.

The forced set is kept in `drupal-discovery.json` (`force_miss`) and carried
into the next run's set until an `extract()` completes: an interrupted run, or a
`detect` with nothing to extract, does not lose it. The widening lives in
`extract()`, and core skips `extract()` when a batch is empty, so a registry
change with no change to any graph file waits for the next extraction; the
carried `force_miss` set is what keeps it from being lost.

On `extract --out`, the incremental path calls `detect_incremental(root,
<out>/graphify-out/manifest.json)`, which calls `detect` without a `cache_root`.
The seam also wraps `detect_incremental`: an explicit `manifest_path` names the
out dir (its parent), and the nested `detect` uses it for the previous
registry, the new one and the inventory, so nothing is written into the
scanned tree and the previous registry is found. The report wrapper finds the
inventory beside the graph a caller names through
`report.load_learning_for_report(<out>/graph.json)` (e.g. `cluster-only <site>
--graph <out>/graphify-out/graph.json`), else under `<root>/graphify-out`.

### 5.6 Watch

A wrapper on `watch._batch_triggers_rebuild` returns true when the batch
contains a Drupal YAML file (`families.is_drupal_file`); a wrapper on
`watch._has_non_code` stops counting those files as non-code, so they no longer
raise the LLM `needs_update` flag.

### 5.7 The inventory

`inventory.build_inventory(registry, detected)` writes
`graphify-out/drupal-inventory.json`:

- `unrecognised_yaml`: files `<ext>.<name>.yml` directly in an extension
  directory (beside `<ext>.info.yml`, or `core/` for `core.*`), which detect
  returned (in any category), that no P1/P1b family, learned family or deferred
  family claims — grouped by `<name>` with file count, owners, up to 3 paths.
  Dot-files and YAML outside an extension root are not listed; their count is
  `summary.filtered`.
- `deferred`: family → phase, file count.
- `managers_unresolved`: `{class, file, reason}` with reason one of
  `service_provider_alter` (a `*ServiceProvider.php` whose `alter()` sets a
  class on a service), `not_a_service`, `psr4_unresolved` (a `Drupal\…` service
  class whose file PSR-4 cannot find), `dynamic_discovery`, `parse_error`.
- `summary`: types, registered types, YAML plugins, deferred files,
  unrecognised families and files, filtered.

A wrapper on `report.generate` appends a "Drupal coverage" section rendered
from the inventory (from process state, else `<out>/drupal-inventory.json`
beside the report). A non-Drupal run removes `drupal-inventory.json` from the
out dir, so a stale one cannot resurrect the section.

### 5.8 Errors

Nothing in the registry or the inventory raises: an unreadable or unparsable
file (a pathologically nested one included) becomes a `parse_error` entry and
the run continues. An out dir that cannot be written (a read-only checkout)
does not fail `detect`: the registry and the inventory stay in process, and
`GRAPHIFY_DRUPAL_DISCOVERY` points at a private temp copy of the registry
(`graphify-drupal-discovery-*.json`, replaced on the next run) so a
spawn/forkserver worker still reads it; if that too fails the variable is
removed and one warning is logged. The seam raises `DrupalSeamError` if
`detect.detect`, `detect.detect_incremental`, `detect.ignored_predicate`,
`paths.GRAPHIFY_OUT`, `cache.load_cached`, `report.generate`,
`report.load_learning_for_report`, `watch._batch_triggers_rebuild` or
`watch._has_non_code` is missing.

## 6. Acceptance criteria (corpus)

1. **No silent manager.** Every concrete manager class a full-tree scan finds is
   a `drupal_plugin_type` or a `managers_unresolved` entry.
2. **YAML partition.** Every `<ext>.<name>.yml` in an extension root falls into
   exactly one of: P1/P1b family, learned family, deferred, unrecognised.
3. Counts match §2 (types, learned files, YAML plugins) or the difference is
   explained in the plan's measurement task.
4. Every P1 menu link, local task, local action, contextual link and breakpoint
   has exactly one `plugin_of_type` edge.
5. No `drupal_plugin` carries an attribute outside §4.1.
6. Two consecutive runs with no change produce identical graphs; changing a
   manager's `YamlDiscovery` name moves the family's nodes on the next
   incremental run; `watch` rebuilds on a `*.services.yml`-only batch.
7. `GRAPH_REPORT.md` has the Drupal coverage section.
8. Registry build under 5 s.

## 7. Risks

- **Static discovery is incomplete by construction**: derivatives,
  `ServiceProvider::alter()`, managers built at runtime. The inventory makes each
  gap visible; P3 closes what the container knows.
- **Registry hash granularity.** Comparing per family and per manager keeps an
  unrelated manager's change from re-extracting everything; a bug here shows up
  as stale plugin nodes, which acceptance criterion 6 tests.
- **A registry change with no graph-file change is applied late.** Widening
  happens in `extract()`, which core skips for an empty batch; `force_miss`
  carries the set to the next extraction (§5.5).
- **tree-sitter PHP grammar drift** upstream: pinned by core's lockfile, and the
  seam's PHP reads are covered by unit tests on real manager shapes.

## 8. Measured

On `/home/user/Projects/FormsRemote`, 2026-09-23 (plan Tasks 2 and 9; the corpus
acceptance tests are `tests/test_drupal_corpus.py::test_p2a_*`), on a default
run: the corpus's `.gitignore` lists `/web/core` and `/web/modules/contrib`,
which the registry does not honour (§5.1 Scope).

| Measured | Value |
|---|---|
| plugin types | 143: 140 registered, 3 second-net |
| distinct manager classes behind them | 121: 118 registered, 3 second-net (5 classes back several services: `ViewsPluginManager`/`ViewsHandlerManager` 23 `views.*`, `MigratePluginManager` 2, `KeyPluginManager` 3, `BetterExposedFiltersWidgetManager` 3) |
| `discovery` | annotation 76, mixed 35, yaml 20, attribute 9, dynamic 3 |
| deferred types | `sdc` → P5; `migration` and `class:Drupal\migrate_drupal\MigrationPluginManager` → P6 |
| `by_yaml_name` families | 19: the 14 learned families of §2 (the three `modeler_api.*` need rule 6's `__construct()` reading) and P1's 5; not `routing`, `permissions` or `migrate_drupal` |
| concrete manager classes, full-tree scan (criterion 1) | 122: the 121 type classes and `symfony_mailer`'s `MailManagerReplacement` — every one a type or a `managers_unresolved` entry |
| `managers_unresolved` | 22: `service_provider_alter` 14, `dynamic_discovery` 3, `not_a_service` 3, `parse_error` 1, `psr4_unresolved` 1 |
| extension-root `<ext>.<name>.yml` files (criterion 2) | 1,081: P1/P1b 1,005, learned 72, deferred 0, unrecognised 4 — each in exactly one bucket |
| learned-family files / YAML plugins (criterion 3) | 72 / 260, as §2 |
| P1 plugin nodes with one `plugin_of_type` target (criterion 4) | 924 of 924 |
| `drupal_plugin` attributes outside §4.1 (criterion 5) | none |
| full `detect` inventory summary | unrecognised 4 families / 4 files (`permission`, `plugin_type`, `service`, `starterkit`), deferred 285 files (`component` 33, `migrations` 252), filtered 897 |
| `build_registry` | 2.0–3.0 s (load ≈ 3.5 on 14 cores); 2.4–2.5 s in Task 2 at lower load |
| `prepare_run`, what `detect` pays (criterion 8) | 2.6–3.7 s at the same load. Before the Task 9 fixes: ≈ 5 s, from core's ignore predicate on every walked file (now only directories and collected files) and the pure-Python YAML loader on the services files (now libyaml, falling back to P1's loader) |

Differences from §2, and why:

- **117 → 122 concrete managers, 114 → 118 registered classes.** §2's figures
  were a hand count. The extra registered classes are managers a manual count
  can leave out: `EntityTypeManager`, `TypedDataManager` and
  `TypedConfigManager` (all extend `DefaultPluginManager`), and webprofiler's
  `EntityTypeManagerWrapper` and `MailManagerWrapper`, which decorate core
  managers (types `webprofiler.debug.entity_type.manager`,
  `webprofiler.debug.mail_manager`). 143 types, not 117 plus the second net,
  because a type is per service.
- **`backup_migrate\Core\Plugin\PluginManager` is not a Drupal manager.** It
  implements backup_migrate's own `PluginManagerInterface`, which extends
  nothing.
- **`mailsystem\MailsystemManager` is a second-net manager** §2 does not list
  (extends `MailManager`, swapped in by `MailsystemServiceProvider::alter()`).
- **`symfony_mailer\MailManagerReplacement`** is swapped in by
  `SymfonyMailerServiceProvider::alter()` and is neither a service nor
  `*Manager.php`; the full-tree scan found it silent, so §5.1 rule 8 now names
  a manager a provider sets.
- **`Drupal\ckeditor5_font\FontColorsManager` is absent from the corpus** — the
  module ships only `src/Plugin/CKEditor5Plugin/*`, so it is a dead service
  (`psr4_unresolved`), not a PSR-4-by-machine-name problem.
- The `parse_error` is `azure_oauth_sso`'s service class `BaseOAuth`, a trait.
- `dynamic_discovery`: `TypedConfigManager` (schema discovery built outside a
  `new`), `ConstraintManager` (`parent::getDiscovery()` wrapped in a static
  decorator), `MigrateSourcePluginManager`
  (`AttributeDiscoveryWithAnnotationsAutomatedProviders`, whose short name is
  not in the table).
- Four P1 plugin nodes are declared by two files each (collapsed to one node,
  `declared_in` of 2) and so carry two identical `plugin_of_type` edges; the
  reader folds parallel edges of one relation, so the graph has one.

