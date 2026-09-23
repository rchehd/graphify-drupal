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
| concrete plugin-manager classes (full-tree scan) | — | — | — | 117 |
| … reached from `*.services.yml` by class → PSR-4 → `extends` chain | — | — | — | 117 − 3 |
| managers overriding `getDiscovery()` | 14 | 4 | 0 | 18 |
| YAML plugin families read by a manager, not P1's | — | — | — | 14 families, 72 files, 260 plugins |
| module-root `*.yml` no P1/P1b family claims | — | — | — | 243 (80 are `.gitlab-ci.yml`) |

The three managers the services chain misses, and why:

| Manager | Why the chain misses it |
|---|---|
| `migrate_drupal`'s `MigrationPluginManager` | swapped in by `ServiceProvider::alter()` in PHP |
| `MetatagViewsCachePluginManager`, `backup_migrate\Core\Plugin\PluginManager` | not services; constructed with `new` |

Plus two shapes a naive chain would miss, present on the corpus:
`MenuLinkManager` is a manager only through `implements MenuLinkManagerInterface`
(which extends `PluginManagerInterface`), and `Drupal\ckeditor5_font\…` lives in
module `ckeditor5_plugin_pack_font`, so PSR-4 by machine name cannot find it.

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

1. **Web root.** The scan root if it holds `core/lib/Drupal.php`; else its
   `web/` or `docroot/` child if that does; else the nearest ancestor that
   does; else none — the scan root is used alone and the inventory records
   `no_drupal_core`.
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
   parent manager class in the chain counts.
7. **Deferred families** are a fixed table: `component` (SDC) → P5, the
   migrations manager and `migrate_drupal` → P6.

Performance budget: under 5 seconds on the corpus; measured in the plan.

### 5.2 Classification and dispatch

`families.is_drupal_file` and `families.drupal_extractor` gain one more case,
after P1b's configuration and P1's table: `<ext>.<yaml_name>.yml` in an
extension root, where `<yaml_name>` is a learned, non-deferred family and not a
P1 family → `yaml_plugins.extract_drupal_yaml_plugins`. The longest matching
`yaml_name` wins (`modeler_api.contexts` over any `contexts`).

### 5.3 Worker processes

Core extracts uncached files in a `ProcessPoolExecutor`; a worker does not see
the parent's in-process registry. The `detect` wrapper writes
`drupal-discovery.json` and sets `GRAPHIFY_DRUPAL_DISCOVERY` to its absolute
path; `discovery.current_registry()` returns the in-process registry, else loads
that file once per process. Both `fork` and `spawn` inherit the environment.

### 5.4 Plugin-type nodes come from a synthetic file

Plugin-type nodes must have an owning `source_file`, or an incremental run
cannot keep or evict them. Each type node is emitted by the extraction of its
**manager class file**: the `_get_extractor` wrapper composes core's PHP handler
with `discovery.extract_plugin_types(path)` for a PHP file that is some type's
manager (exactly as P1b composes `settings.php`). The type's edges
(`defines_plugin_type`, `plugin_manager_for`) are emitted there too.

### 5.5 A changed registry re-extracts what it affects

The `extract()` wrapper compares the new registry with the previous run's
`drupal-discovery.json` (read before the `detect` wrapper overwrites it and kept
in process state). Per `yaml_name` and per manager class it compares the
emitted facts; for every changed family, the files `*.<yaml_name>.yml` in
extension roots, and for every changed manager its class file, are:
- forced to miss the AST cache — a wrapper on `cache.load_cached` returns
  `None` for them this run (core's cache directory is namespaced by
  `_EXTRACTOR_VERSION` and `_cleanup_stale_ast_entries` deletes other
  namespaces, so putting the registry hash into the version would re-parse the
  whole project on any manager change);
- added to `paths` on incremental runs (the same widening as P1b's collision
  group).

A newly learned family needs neither: its files had no nodes, and core
re-queues zero-node files.

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
beside the report).

### 5.8 Errors

Nothing in the registry or the inventory raises: an unreadable or unparsable
file becomes a `parse_error` entry and the run continues. The seam raises
`DrupalSeamError` if `detect.detect`, `cache.load_cached`,
`report.generate`, `watch._batch_triggers_rebuild` or `watch._has_non_code` is
missing.

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
- **tree-sitter PHP grammar drift** upstream: pinned by core's lockfile, and the
  seam's PHP reads are covered by unit tests on real manager shapes.
