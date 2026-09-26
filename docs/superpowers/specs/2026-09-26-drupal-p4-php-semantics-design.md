# P4 — PHP semantics: plugins, entity types, forms, services, events

Status: implemented (branch `drupal-graph`); measured and closed in §15
Date: 2026-09-26
Related: `2026-09-22-drupal-graph-vocabulary.md` §3.4–3.6, §4.2–4.6, §5.4–5.6;
`2026-09-22-drupal-graphify-architecture-design.md` §5;
`2026-09-24-drupal-p2b-hooks-boundary-design.md` §5;
`2026-09-25-drupal-p3-container-design.md` §7, §15

---

## 1. Goal

P4 adds what the site's own PHP says, and what no YAML file says:

1. **Plugins from PHP.** Classes carrying a learned type's attribute or
   annotation become `drupal_plugin` nodes. Entity types become
   `drupal_entity_type` nodes with their handlers.
2. **Forms.** `drupal_form` nodes, routes bound to them, and
   `form_FORM_ID_alter` implementations bound to the form they alter.
3. **Entity-type hooks.** `hook_ENTITY_TYPE_*` implementations are bound to
   the entity type they fire for.
4. **Service use.** A `uses_service` edge for every way custom code reaches a
   service. Calls on the service are resolved to the service class's method
   (`calls`), including through properties injected in the constructor.
5. **Events from PHP.** `getSubscribedEvents()` gives `subscribes_to_event`.
6. **P3's deferred minors** (§11).

Nothing is guessed. An edge appears for a literal or a proven type. Anything
else is an inventory candidate.

## 2. What the corpus says

Measured on FormsRemote custom code (265 PHP-family files under
`web/modules/custom`, `web/themes/custom` and `web/profiles`, `tests/`
excluded), 2026-09-26:

| Construct | Count | Notes |
|---|---|---|
| plugin attributes `#[X(` (not `Hook`) | 32 | `ContentEntityType` 7, `WebformIntegrationType` 6, `Action` 4, `RestResource` 3, `EcaAction` 3, `AdvancedQueueJobType` 2, `SystemType` 2, `ConfigEntityType` 1, `FieldFormatter` 1, `EcaEvent` 1, `EcaCondition` 1, `Mail` 1 |
| plugin annotations `@X(` | 15 | `WebformElement` 6, `WebformHandler` 3, `RenderElement` 3, `ViewsField` 2, `FieldFormatter` 1 |
| `getFormId()` | 34 | |
| entity types (attribute or annotation) | 8 | |
| `\Drupal::service('x')` | 35 | |
| `\Drupal::<shortcut>()` | 130 | `entityTypeManager()`, `config()`, `currentUser()`, … |
| `$container->get('x')` | 194 | almost all in `create()` |
| `form_*_alter` implementations | 4 | |
| `ENTITY_TYPE_*` hook implementations (`#[Hook]`) | 6 | |
| `getSubscribedEvents()` | 0 | custom subscribers exist only in the P3 container (193 site-wide) |

Before P4, the graph has no `service_implemented_by` or `routes_to` edges
from static extraction (P3 adds them from the container), no `drupal_form`,
no `drupal_entity_type`, and no edge from PHP to a service.

## 3. Scope

### In

The six parts of §1. The registry learns:

- the `\Drupal::` shortcut map;
- entity type ids, forms and event-name constants across the boundary;
- each custom class's constructor signature and `create()` arguments.

The P2b variable-hook candidates of the two patterns P4 covers get bound.
The closing real run.

### Out

- `preprocess_*` and `theme_suggestions_*` (P5).
- Bundles, fields, displays; the real ids of entity forms (P6).
- Type inference beyond §7.3's four rules and §7.4. A local variable holding a
  service (`$s = \Drupal::…(); $s->x()`) is resolved only in the single
  assignment form of §7.4.
- `configures_plugin` (P6).

## 4. Architecture

P2b's pattern, extended:

- **A per-file extractor** (`graphify/drupal/php_semantics.py`) is composed
  onto core's PHP handler wherever P2b composes hook extraction, and onto
  every in-graph custom `.php`. It emits what one file shows: plugin, entity
  type and form nodes, `uses_service`, and raw facts for cross-file
  resolution.
- **The registry** (`discovery.py`) learns the maps of §3 in the walk it
  already makes (target: `prepare_run(FormsRemote)` stays under 5 s).
- **A resolver pass**, in the existing `drupal` resolver, turns raw facts
  into `calls` edges and binds variable hooks.
- **The P3 overlay** confirms or adds service classes (`confirmed_by`).

The per-file extractor emits raw facts as edges with `target_name` and a
`pending` attribute. The resolver either replaces each one with a bound
edge or removes it and records a candidate. No `pending` edge ever reaches
graph.json. This is the same mechanism P2b uses for hook stubs.

Incremental: `affected_files` gains the files that use a service whose class
or constructor arguments changed in the registry (§9).

### 4.1 New vocabulary

P4 adds two relations to the vocabulary:

- `form_implemented_by` (form → PHP class), the form's counterpart of
  `plugin_implemented_by`;
- `hooks_entity_type` (hook_impl → entity type), the `ENTITY_TYPE_*`
  counterpart of `alters_form`.

It also adds the node attributes named below. The vocabulary document is
updated in the closing task.

## 5. Plugins and entity types

### 5.1 Recognition

For each learned plugin type with a `subdir` and an `attribute_class` or
`annotation_class` (`Registry.types`), a class in `<ext>/src/<subdir>/**`
of a custom extension is a plugin of that type when it carries:

- the attribute: `#[<attribute_class>(…)]`, name resolved through `use`; or
- the annotation: a docblock `@<Name>(…)` directly above the class, whose
  short name equals the short name of `annotation_class`. As in Drupal's own
  annotation reader, no `use` is required (none of the corpus's 62 annotated
  entity classes imports its annotation class). When a `use` (or a leading
  `\`) does fix the name and it resolves to a different FQCN, the annotation
  does not match.

The id is the attribute's first positional string or `id:` argument, or the
annotation's `id = "…"`. The deriver is `deriver: X::class`, or an annotation
`deriver = "Fqcn"`, resolved to a FQCN. Anything non-literal is dropped.

The annotation is parsed with a small reader for Doctrine-style `@Name(key =
value, …)`. Only top-level `id`, `deriver`, and for entity types `handlers`
and the keys of §5.3, as strings, `::class` or nested `{…}` maps. Every
other value is skipped, never evaluated.

A plugin-looking attribute or annotation in `src/Plugin/**` whose class is no
learned type's becomes an `unknown_plugin_type` candidate (`module`,
`class`, `attribute`, `file`, `line`).

### 5.2 Plugin nodes and edges

The node is `drupal_plugin`, `plugin_id(type, id)`, `layer: plugin`, with
`plugin_type`, `class_name`, `provider`, and `deriver` when present. Edges:

- `provides_plugin` (extension → plugin);
- `plugin_of_type` (→ `type_id(type)`);
- `plugin_implemented_by` (→ the class node core emitted for this file);
- `derives_plugins` (→ the deriver's class node, when it is in the graph).

A plugin of a links family type stays P1's link node (P3 ruling).

### 5.3 Entity types

A class carrying `ContentEntityType` or `ConfigEntityType` (attribute or
annotation) becomes `drupal_entity_type` (`make_id("drupal", "entity_type",
id)`, `layer: model`), not `drupal_plugin`. It carries:

- `entity_kind` (`content` | `config`);
- `class_name`;
- `bundle_entity_type`, `base_table`, `admin_permission`, when they are
  literals.

Edges:

- `defines_entity_type` (extension → entity type);
- `entity_handler` (entity type → handler class node), attribute `handler`:
  `storage`, `list_builder`, `view_builder`, `access`, `views_data`,
  `form.<op>`, `route_provider.<name>`;
- `requires_permission` for `admin_permission`.

A handler class outside the graph gives no edge. Its FQCN goes on the node
as `handlers: {name: fqcn}`.

### 5.4 The boundary

The registry learns, per extension, the entity type ids declared by
core/contrib classes, with the same reader, over the boundary it already
walks. They become boundary stubs only as targets of custom facts (the P2b
rule).

## 6. Forms and variable-hook binding

### 6.1 Form nodes

A class whose `getFormId()` body is `return '<literal>';` becomes
`drupal_form` (`make_id("drupal", "form", form_id)`, `layer: hook`). It
carries `form_id` and `class_name`, plus `base_form_id` when
`getBaseFormId()` is a literal return. Its edge is `form_implemented_by`
(form → class node).

A route's `_form: Fqcn` (P1) targets the form node whose class is that FQCN:
`routes_to_form`, re-pointed from the class node. The class node keeps
`routes_to` only for `_controller`.

An entity type's `form.<op>` handler is an **entity form**. Its id is built
at runtime (`<entity>_<bundle>_<op>_form`). It gets a node
`make_id("drupal", "form", "entity", entity_type, op)` with `entity_form:
true` and `pattern: "<entity>_*_<op>_form"`, plus `form_implemented_by` to
the handler class.

The registry learns boundary forms (`getFormId()` literals in core/contrib
classes) as a map form_id → class, and base form ids (`getBaseFormId()`
literals) as a separate map base id → the forms of that base. A form becomes a
stub only as the target of a custom fact; a base id is never a form node of
its own.

An entity form's `pattern` is a label, never a matcher. `EntityForm::getFormId()`
builds `<entity>[_<bundle>][_<op>]_form` (the bundle only for an entity type
with a `bundle` entity key, the op left out for `default`), and
`getBaseFormId()` is `<entity>_form` for every operation. The registry learns,
per entity type, its `form.<op>` operations and, for an entity type with a
`bundle` key, the bundle ids its bundle entity type's config files prove:
`<provider>.<config_prefix>.<bundle>.yml` in a sync store or a
`config/install|optional` directory, read by file name only (amended in P4
Task 5 fix round 1).

### 6.2 `form_FORM_ID_alter`

For an implementation of hook `form_<x>_alter` (P2b `variable` candidate,
`pattern: form_*_alter`), resolve `<x>`, first match wins:

1. a custom form whose `form_id` is `x`;
2. every custom form whose `base_form_id` is `x`;
3. the boundary form whose literal id is `x`;
4. every boundary form whose base id is `x` (none known: no match);
5. `x == <t>_form`: every entity form of entity type `t` (its base form id);
6. every entity form whose id `EntityForm::getFormId()` builds equals `x`
   exactly: `<t>[_<op>]_form` for a `t` without a bundle key,
   `<t>_<bundle>[_<op>]_form` only for a bundle the registry proves (§6.1).
   No bundle is guessed, and nothing here is `INFERRED`.

- a match gives `alters_form` (hook_impl → form), `EXTRACTED`, with `bundle`
  when a proven bundle was read;
- the `implements_hook` target is `form_BASE_FORM_ID_alter` when the match
  came through a base form id (2, 4, 5) and the registry declares it, else
  `form_FORM_ID_alter`, so the implementation is no longer a candidate;
- no match gives an `unbound_form` candidate (`module`, `name`, `form_id`,
  `file`, `line`). It still implements `form_FORM_ID_alter` for the P3
  overlay, which never makes a hook node of the concrete name.

The `drupal_hook_impl` node is created as P2b creates it for a declared hook.

### 6.3 `hook_ENTITY_TYPE_*`

For `<t>_<op>`, with `op` in the operations core declares for the
`ENTITY_TYPE` pattern (read from the registry: every declared hook whose
name contains `ENTITY_TYPE`):

- if `<t>` is a known entity type id (custom or boundary), the result is
  `hooks_entity_type` (hook_impl → entity type), and `implements_hook`
  targets the declared pattern hook (`hook_id("ENTITY_TYPE_<op>")`);
- otherwise nothing: it stays the P2b candidate.

A procedural `<ext>_<t>_<op>()` is split by vocabulary §5.5. The module list
and the entity type list must both agree on one split, or it is a candidate.

## 7. Services

### 7.1 `uses_service`

| Form | Source node | `via` |
|---|---|---|
| `\Drupal::service('x')` | enclosing method or function | `service` |
| `\Drupal::<m>()` in the shortcut map | enclosing method or function | `shortcut` |
| `$container->get('x')` inside `create()` | the class | `create` |
| `$this->p` read or called, `p` resolved by §7.3 other than the class's own `create()` (rules 2, 3, 3b, 4, or 1/1b through an ancestor) | the class | `injected` |

An `injected` edge is one per (class, service) pair, with `properties: [p, …]`.
It appears only when the property is used, so injection alone adds no edge.
A pair that already has a `create` edge (the class's own `create()`) keeps
that edge. The `injected` edge is the carrier of `methods` / `_pending_calls`
(§7.4) for calls on such a property, as the other rows are for their calls.

The shortcut map is learned from core's `core/lib/Drupal.php`: every public
static method whose return expression is exactly
`static::getContainer()->get('<literal>')` or
`static::$container->get('<literal>')`. `\Drupal::request()` (which returns
`->get('request_stack')->getCurrentRequest()`) is not a shortcut, and
neither is `\Drupal::service()`. On FormsRemote's core that file has 40
`->get(` calls.

The target is `service_id(resolve_alias(x))`: the registry's aliases
(`x: '@y'`, `x: {alias: y}`) are followed, and the literal as written is kept
in an `alias` attribute when it differs. It is a boundary stub if needed (the
P2b rule). A non-literal id gives a `non_literal_service` candidate.

### 7.2 Constructor facts

The registry records, for every custom class:

- `__construct` parameters in order: name and declared type, resolved FQCN;
- promoted properties: visibility modifier present, so `$this->name` is the
  parameter;
- assignments `$this->p = $param;` inside `__construct`;
- `create()`'s `new static(…)` / `new self(…)` / `new <Class>(…)` arguments,
  positionally: a `$container->get('x')` gives service `x`, anything else
  gives unknown;
- `create()`'s setter injection: `$v->p = $container->get('x');` where `$v`
  is the local variable `create()` returns (typically `$instance =
  parent::create($container, …); … return $instance;`) gives property `p` →
  service `x` (`create_props`). Only a direct property assignment with a
  literal id counts: not a setter call (`$v->setFoo(…)`), and not a chained
  `$container->get('x')->get(…)`. On FormsRemote: 48 properties in 14
  classes.

### 7.3 Property → service

A property `$this->p` of class `C` names service `x` when one of these holds,
first match wins:

1. `create()` passes `$container->get('x')` at the position of the
   parameter assigned to `p`. The `create()` is the class's own, or an
   ancestor's, but an ancestor's only when it builds with `new static(…)`.
   An ancestor's `new self(…)` / `new <Ancestor>(…)` builds the ancestor, not
   `C`, so it says nothing about `C`'s properties;

   1b. `create()` assigns `$container->get('x')` to `$<returned>->p`
   (setter injection, §7.2);
2. `C` is a service in `*.services.yml` whose `arguments:` has `@x` at that
   position;
3. `C` is an autowired service (`autowire: true`, or `_defaults`) and the
   parameter's type is a service id or alias the registry knows, i.e. an
   interface/class used as a service id;

   3b. the same when `C` is a custom class under its extension's `src/Hook/`
   with a `#[Hook]` attribute (on the class or a method) and no
   `*.services.yml` defines it. Core's
   `core/lib/Drupal/Core/Hook/HookCollectorPass.php`
   (`registerHookServices`: `if (!$container->hasDefinition($class))
   $container->register($class, $class)->setAutowired(TRUE)`) registers
   every such class as an autowired service whose id is its FQCN. The
   registry records them as `hook_services`;
4. a parent class's constructor facts give it, when `C` calls
   `parent::__construct($a, …)` with the parameter passed through.

Otherwise `p` is unresolved, and calls on it become
`unresolved_receiver` candidates only when the parameter's declared type is
an interface or class some known service implements. This keeps the
inventory from filling with every value object.

### 7.4 Calls on a service

A method call `R->m(…)` is resolved when its receiver `R` is one of:

- `\Drupal::service('x')`;
- `\Drupal::<shortcut>()`;
- `$this->p`, with `p` resolved by §7.3;
- a local `$v` assigned exactly once in the same function from one of the
  above, and not reassigned.

The service `x` has a class `K`: the registry's `services` map (static), or
the P3 container's class (the overlay confirms or adds). The edge is:

- `calls` from the enclosing method or function node to the method node
  `K::m`, when `K` is a class node in the graph (custom) and has method `m`
  (walking `extends` within the graph);
- otherwise only `uses_service` (already emitted), carrying `methods: [m, …]`
  as an attribute. There is no `calls` edge into the boundary.

A chain beyond one call (`\Drupal::entityTypeManager()->getStorage('node')->load(…)`)
resolves only its first call. The rest need return types, which is out of
scope.

## 8. Events

`getSubscribedEvents()`'s returned array: for each key that is a string
literal or a class constant `X::NAME`, the event name. The constant is
resolved through the registry, which records `const NAME = '<literal>'` of
classes named `*Events` in its walk (custom, core, contrib, and vendor when
the registry walks it). The value is a method name, `[method, priority]`, or
a list of those. Each gives `subscribes_to_event` (class → `drupal_event`,
`make_id("drupal", "event", name)`) with `method` and `priority`. An
unresolvable key gives an `unresolved_event` candidate. The P3 overlay's
subscriber edges meet these through `confirmed_by`.

## 9. Incremental

`affected_files(previous, current)` adds:

- a service whose class or `arguments:` changed → every in-graph file with a
  `uses_service` edge to it, read from the previous graph.json (the P3
  `_invokes_hook_files` pattern);
- a changed constructor or `create()` in class `C` → `C`'s file. It is in the
  batch anyway, and its subclasses in the graph;
- a changed boundary form, entity type or event-constant map → files with
  `alters_form`, `hooks_entity_type`, `subscribes_to_event` or the matching
  candidates.

## 10. Inventory and report

The inventory gains candidate kinds:

- `unknown_plugin_type`;
- `unbound_form`;
- `non_literal_service`;
- `unresolved_receiver`;
- `unresolved_event`.

It also gains counts for plugins, entity types, forms, `uses_service` by
`via`, and `calls` from resolved receivers. GRAPH_REPORT's "Drupal
coverage" shows them.

## 11. Carried minors (P3)

1. `lay_on_records` builds a throw-away graph on every Drupal `--no-cluster`
   write. Skip it when there is no artifact and no boundary stub.
2. The raw `--no-cluster` overlay ignores `directed`. Pass the graph's
   directedness.
3. `divergence._subject_file` compares a resolved path with an unresolved
   composer root. Resolve both.
4. `refresh_after_prune` does not recount `result.edges`. Recount.
5. `container_overlay._LINK_FAMILIES` duplicates `yaml_links`' table. Share
   one constant.
6. The realm memo has no unit test. Add one that counts `realm_of` calls.

Each gets a test.

## 12. Acceptance

On synthetic sites:

- each §5–§8 construct produces exactly its nodes and edges;
- each candidate kind appears for its negative case;
- attributes and annotations give identical nodes for the same plugin;
- the four §7.3 rules each resolve a property, and a fifth case stays
  unresolved;
- `calls` never targets a boundary class;
- incremental: changing a service's class in `*.services.yml` re-extracts
  its users, and their `calls` follow;
- `prepare_run` time on FormsRemote stays under 5 s.

On FormsRemote (§13):

- plugins about 47, entity types 8, forms about 34;
- `uses_service` from all three forms;
- `calls` from resolved receivers above 0;
- `alters_form` for the 4 `form_*_alter`s, or each explained as a
  candidate;
- `hooks_entity_type` for the 6 `ENTITY_TYPE_*` implementations, or each
  explained;
- an unchanged rerun re-extracts no more than P3's two files;
- FormsRemote untouched.

## 13. The closing real run

No drush. The P3 artifact collected on 2026-09-25 (scratchpad
`p3/drupal-container.json`) is reused through `GRAPHIFY_DRUPAL_CONTAINER`:

1. `extract --code-only --out <scratch>/p4` twice with the artifact;
2. once without it, into `<scratch>/p4-static`;
3. counts of every §12 item, candidates by kind, timings, and nodes/edges
   against P3's 4,946 / 10,724 (static 4,825 / 10,395);
4. FormsRemote `git status --porcelain` and its `graphify-out/` mtimes
   unchanged.

The results go in §15.

## 14. Risks

- **Annotation parsing** is a docblock grammar. The reader is limited to
  §5.1's keys, and a parse failure gives a candidate, never an exception.
- **Registry cost.** Constructor facts for every custom class, plus boundary
  forms, entity types and event constants, widen the walk. The 5 s budget is
  measured in the task that adds each map, not assumed.
- **Id agreement** with core's PHP class and method node ids: bind by
  `source_file` and short name, as P2b and P3 do. What does not bind becomes
  an attribute.
- **Property inference is narrow by design** (§7.3, §7.4). An unresolved
  receiver is a candidate only when its declared type is a known service's,
  so the inventory stays readable.

## 15. Measured and the closing real run

Run on 2026-09-27 against FormsRemote at `60b010db` (Drupal 11.4.7), with no
drush: the P3 artifact collected on 2026-09-25 (same commit, status `fresh`)
was reused through `GRAPHIFY_DRUPAL_CONTAINER`.

1. `extract --code-only --out <scratch>/p4` with the artifact, twice;
2. once without it into `<scratch>/p4-static`;
3. `cluster-only --no-viz --no-label` on a copy of the first, for the report.

`tests/test_drupal_corpus.py` (P4 section) asserts every number below that is
a §12 criterion; the container half runs with `DRUPAL_CONTAINER_ARTIFACT`.

### 15.1 Size and time

| | nodes | edges | wall |
|---|---|---|---|
| P3, with the artifact | 4,946 | 10,724 | 10.8 s |
| P3, static | 4,825 | 10,395 | |
| **P4, with the artifact** (full, cold cache) | **5,051** | **11,523** | 10.9 s |
| P4, with the artifact, unchanged rerun | 5,051 | 11,523 | 8.3 s |
| **P4, static** (full, cold cache) | **5,003** | **11,342** | 12.0 s |

- `prepare_run(FormsRemote)`: 2.85, 2.94 and 2.99 s cold (fresh out dir,
  boundary caches cleared), under the 5 s budget. The corpus timing tests keep
  that budget but take the best of up to three cold runs. A single run once
  hit 5.37 s under two concurrent pytest processes (Task 7), which was CPU
  contention, not the build.
- The rerun's incremental line: `1142 files cached/unchanged, 2 re-extracted,
  0 deleted`, the same as P3. The two are the files core never records in its
  manifest: `docker/mssql/seed.sql` (tree_sitter_sql is not installed) and
  `webform_integrations_logs.links.action.yml`, which is empty and so has zero
  nodes. The rerun's graph has the same node ids and edges, with the same
  attributes, except that 24 core `external` concept nodes (`fieldconfig`, `url`, …) gain
  `_origin: semantic` from core's incremental merge. That is core behaviour,
  not a Drupal one.
- Static, P4 adds 178 nodes to P3: 39 plugins; 62 forms (34 custom and 28
  entity forms); 8 entity types; 15 `drupal_hook_impl`s (the bound variable
  hooks); and boundary stubs (27 services, 12 plugin types, 7 hooks, 4 entity
  types, 3 entity forms), plus 1 core external. It adds 947 edges:
  `uses_service` 487, `calls` 183, `form_implemented_by` 50, the plugin
  triple 39 × 3, `entity_handler` 25, `routes_to_form` 25, `hook_implemented_by`
  15, `implements_hook` 13, `hooks_entity_type` 12, `defines_entity_type` 8,
  `requires_permission` 8, `alters_form` 3 and `derives_plugins` 1.
- With the artifact, P4 adds 105 nodes and 799 edges to P3. That is less than
  statically because P3's container had already added some of them (plugins,
  and service stubs). The 11 concrete-name hook nodes P3's overlay made
  (`drupal_hook_node_access`, `drupal_hook_form_webform_edit_form_alter`, …)
  are gone: those implementations now implement the declared pattern hook
  statically (§15.2).
- `pending` edges in graph.json: 0 in all three runs.

### 15.2 The §12 criteria on FormsRemote

| Criterion | Expected | Measured |
|---|---|---|
| plugins | about 47 | **39** plugin nodes + **8** entity types (below) |
| entity types | 8 | **8**: `system`, `task`, `task_workflow`, `webform_integration`, `webform_integration_lim`, `webform_integration_result`, `webform_integrations_log`, `webform_integrations_token` |
| forms | about 34 | **34** custom forms (= the 34 `getFormId()`s) + **28** entity forms; `routes_to_form` 25, all to form nodes, all `confirmed_by: container` |
| `uses_service` from all three forms | yes | `service` 35, `shortcut` 96, `create` 193, and `injected` 163: 487 edges |
| `calls` from resolved receivers | above 0 | **183** static, all to custom method nodes, 0 into the boundary; 0 bound by the container |
| `alters_form` for the 4 `form_*_alter`s | 4 or explained | **3**, the 4th explained (below) |
| `hooks_entity_type` for the 6 `ENTITY_TYPE_*`s | 6 or explained | **12**: the 6 `#[Hook]` ones plus 6 procedural ones (below) |
| unchanged rerun re-extracts no more than P3's two | ≤ 2 | **2** |
| FormsRemote untouched | yes | yes (§15.5) |

**Plugins: 39 nodes, not about 47.** §2's 47 counts constructs (32
attributes + 15 annotations), not plugins:

- 8 of the 32 attributes are the entity types, and 3 are `#[EcaAction]`
  companions of an `#[Action]`, which carry no id and are not plugins. That
  leaves 21 plugin attributes.
- 2 of the 15 annotations are `@ViewsField` (`IntegrationUsageCount`,
  `JsonPretty`). The `views.*` managers build `"Plugin/views/$type"` at
  runtime, so the registry has no `subdir` or `annotation_class` for them, and
  both are `unknown_plugin_type` candidates, as §5.1 says. The container knows
  them: they are two of its eight container-only plugins. That leaves 13
  plugin annotations.
- 21 + 13 = 34 classes. Shared subdirs add 5 nodes: `action` and `eca.action`
  both read `Plugin/Action` + `#[Action]` (×4), and core's `mail` and
  mailsystem's replacement manager (`class:Drupal\mailsystem\MailsystemManager`)
  both read `Plugin/Mail` (×1). 34 + 5 = 39.

By type: `webform.element` 6, `webform_integration_type` 6, `action` 4,
`eca.action` 4, `rest` 3, `element_info` 3, `webform.handler` 3,
`field.formatter` 2 (one attribute, one annotation), `advancedqueue_job_type`
2, `system_type` 2, and 1 each of `eca.event`, `eca.condition`, `mail` and
`class:Drupal\mailsystem\MailsystemManager`. `derives_plugins`: 1 static
(`eca_custom_webform_submission` → `WebformSubmissionEventDeriver`).

With the artifact, 40 of the 48 own plugin nodes are `runtime: present` and
8 are `runtime: absent`. The absent ones are:

- eca_custom's 3 actions (`redirect_to_webform`, `task_create_action`,
  `task_update_action`), each under both `action` and `eca.action`: 6 nodes.
  The artifact's `action` and `eca.action` lists (88 definitions each) do
  not hold them. It lists them only under `views_bulk_operations_action`,
  with `provider: core`: eca_custom's `PluginInfoHooks::actionInfoAlter()` (a
  container-only hook implementation in the divergence log) relabels them and
  sets their provider to `core`. The static graph cannot tell why the two
  lists lack them, so `absent` is recorded as the container says, never
  corrected. The three reach the graph through the container as
  `views_bulk_operations_action` plugins.
- the deriver's base id `eca_custom_webform_submission`. The container lists
  only the derivative `…:insert`, which the overlay adds as its own node.
- `webform_integrations_email_smtp` under the mailsystem type. The artifact
  has no plugin list for that class-keyed type and lists the plugin under
  `mail`, where it is confirmed.

31 static plugins have `provides_plugin` and `plugin_implemented_by`
`confirmed_by: container`. The container adds 8 plugins no static reader can
see: `help_topic` 2, `views.field` 2 and `views_bulk_operations_action` 4.

**Entity types.** `defines_entity_type` 8, `requires_permission` 8 (one
literal `admin_permission` each), and `entity_handler` 25. Handlers whose
class is core's or contrib's (24, e.g. `views_data` → `EntityViewsData`,
`form.delete` → `ContentEntityDeleteForm`) are entries of the node's
`handlers` map. On 7 entity types one form class serves both `form.add` and
`form.edit`. That is one edge (one relation per pair) with `handler:
"form.add,form.edit"`, a vocabulary decision recorded in vocabulary §4.6.

**`alters_form`: 3, not 4.** §2's regex count of `form_*_alter` includes
govuk_forms' `theme_suggestions_form_element_alter`. That is a
`theme_suggestions_*_alter` hook, which stays a `variable` candidate for P5.
The three bound, all `EXTRACTED`, none `INFERRED`:

| implementation | form | how |
|---|---|---|
| `eca_custom_form_node_case_viewer_edit_form_alter` | entity form `node`/`edit` | rule 6, `bundle: case_viewer`, proven by `node.type.case_viewer.yml` |
| `eca_custom_form_node_case_viewer_form_alter` | entity form `node`/`default` | rule 6 (op left out for `default`), `bundle: case_viewer` |
| `webform_integrations_form_webform_edit_form_alter` | entity form `webform`/`edit` (boundary) | rule 6, `webform` has no bundle key |

Each implements `form_FORM_ID_alter`, and the container confirms it. There
are 0 `unbound_form` candidates.

**`hooks_entity_type`: 12, not 6.** §2 counted the `#[Hook]` implementations
only. The six procedural `<ext>_<t>_<op>()` implementations P2b also left as
`variable` candidates bind by the same rule, once module list and entity-type
list agree on the split:

- attribute (6): eca_custom `node_access`, `node_view`,
  `webform_submission_insert`, `webform_submission_presave`,
  `webform_submission_update`; webform_integrations
  `webform_submission_insert`;
- procedural (6): custom_forms `user_access`, `user_presave`; webform_domain
  `node_access`; webform_integrations_logs `user_predelete`;
  webform_integrations `webform_presave`, `webform_submission_presave`.

All targets are boundary entity types (`node`, `user`, `webform`,
`webform_submission`). All 13 `implements_hook` edges to the pattern hooks
(`entity_type_<op>` and `form_form_id_alter`, one per module and hook) are
`confirmed_by: container`.

**Events.** Custom code has no `getSubscribedEvents()`, so `subscribes_to_event`
is 0 statically and there are 0 `unresolved_event` candidates. With the
artifact, 2 edges are `origin: container`: the two custom route subscribers
(`DomainRouteSubscriber`, `RouteSubscriber`) → `routing.route_alter`, method
`onAlterRoutes`.

### 15.3 Service use, calls and the registry

- The registry: 28 shortcuts (below); 211 custom classes with constructor
  facts; 48 setter-injected properties in 14 classes (`create_props`); 16
  autowired `src/Hook` classes (`hook_services`); 104 entity types; 374
  forms and 8 base form ids across core, contrib and custom; `form.<op>`
  operations for 75 entity types (278 in all); bundle lists for the 8 entity
  types with a `bundle` key; 168 `*Events` constants; 388
  aliases.
- **Shortcuts: 28, not the plan's ~35–40.** The plan counted `->get(` lines
  in `Drupal.php` (40). The exact-return rule (§7.1) admits only a method
  whose sole statement is `return static::getContainer()->get('<literal>')`.
  The other 12 are `service($id)`, `hasRequest()`, `request()`,
  `cache($bin)`, `classResolver()`'s two (a guarded early return), and the
  chained `keyValueExpirable`, `config`, `queue`, `keyValue`,
  `isConfigSyncing` and `logger`. So `\Drupal::config()` and
  `\Drupal::logger()` give no edge, which is why `shortcut` is 96 edges for
  §2's 130 `\Drupal::<m>()` sites. Those counts are also per (source, service)
  pair, not per call site.
- `service` 35 is the 35 literal `\Drupal::service('x')` sites, one pair each.
  The other 2 sites use `::class` ids and are `non_literal_service`
  candidates. `create` 193 is 194 `$container->get(` sites less one `::class`
  id, also a candidate. No custom code uses an alias, so no edge carries
  `alias`.
- 315 of the 487 `uses_service` edges carry `methods`: calls not bound to a
  custom method, because the class is core's or contrib's. 23 of their
  `_pending_calls` entries were offered to the container. None was bound: the
  container gives no service a different class inside the graph. (The 5
  classes that differ, such as `plugin.manager.mail` → `MailsystemManager`,
  are all contrib or core.)
- Every `calls` edge with `service` targets a custom method node; there is
  never a `calls` edge into the boundary.

**Candidates by kind:**

| Section | Kind | Count | Examples |
|---|---|---|---|
| PHP | `unresolved_receiver` | 24 | `DeploymentStatusCommands::$deploymentStatus` and `::$dateFormatter` (12 calls; a Drush command class built by Drush's `AutowireTrait`, which no §7.3 rule reads), `IntegrationImportForm::$logger` (10; `LoggerInterface`, passed `$container->get('logger.factory')->get(…)`, a chain, so unknown), `TaskListFilterForm::$stageDirectory` (2; its `create()` passes a `::class` id) |
| PHP | `non_literal_service` | 3 | `$container->get(WorkflowStageDirectory::class)` in `TaskListFilterForm::create`; `\Drupal::service(WorkflowStageDirectory::class)` in `TaskListAllowedValues`; `\Drupal::service(TaskSummaryUpdater::class)` in `TaskSummaryBatch` |
| PHP | `unknown_plugin_type` | 2 | `@ViewsField` on `IntegrationUsageCount`, `JsonPretty` |
| PHP | `unresolved_event` | 0 | |
| hook | `variable` | 9 | `preprocess_*` 7, `theme_suggestions_*_alter` 2 (P5) |
| hook | `undeclared` | 3 | eca_custom's `action_info_alter`, `eca_condition_info_alter`, `eca_event_info_alter` |
| hook | `unknown_receiver` | 14 | all in custom `tests/` (`ReflectionMethod::invoke`, a unit test's `alter()` helper) |
| hook | `unbound_form` | 0 | |

GRAPH_REPORT's "PHP semantics" table, with the artifact: plugins 48 (the
static 39, plus the container's 8, plus the eca.event derivative), entity
types 8, forms 34, entity forms 28, `uses_service` `create 193, injected 163,
service 35, shortcut 96`, calls bound 183 static and 0 by the container,
`alters_form` 3, `hooks_entity_type` 12, `subscribes_to_event` 2.

### 15.4 Rulings and deviations made during implementation

These are recorded where the spec states the rule. They are listed here so
the phase can be reviewed in one place.

- Setter injection (`$instance->p = $container->get('x')` in `create()`) is
  in scope, as §7.3 rule 1b (`create_props`, §7.2). Only a direct property
  assignment with a literal id counts.
- Annotations match by short name, as Drupal's reader does, unless a `use`
  or a leading `\` fixes a different FQCN (§5.1).
- Rule 3b: a custom `src/Hook/` class with a `#[Hook]` attribute, defined by
  no `*.services.yml`, is autowired, as `HookCollectorPass` does (§7.3).
- `uses_service` has the `injected` carrier for rules 2–4 and for properties
  resolved through an ancestor (§7.1). Rule 1 through an ancestor applies
  only to `new static(…)` or the exact class. The target is
  `service_id(resolve_alias(x))`.
- Binding of `form_*_alter` and `ENTITY_TYPE_*` happens per file, in
  `hooks.py`, against the registry's maps. `unbound_form` replaces the
  `variable` candidate for a `form_*_alter`. An entity-form alter needs an
  exact `EntityForm::getFormId()` match: with a bundle key, only for a bundle
  a config file name proves (sync, `config/install` or `config/optional`:
  what the site ships, as for all static facts). There are no `INFERRED`
  edges. A base-form match targets `form_BASE_FORM_ID_alter` when that hook
  is declared (§6.1–§6.2, amended in Task 5).
- One class serving several entity handlers gives one `entity_handler` edge
  with a comma-joined `handler` (vocabulary §4.6).
- The incremental rules live in `graphify/drupal/staleness.py` (`discovery.py`
  was too large). `affected_files` forces:
  - the class file of every service whose wiring changed;
  - the users of a changed alias;
  - the classes whose `hook_services` membership changed;
  - the hook files, when forms, base forms, entity types, entity-form
    handlers, bundles, extension info or event constants change;
  - every custom file, when the shortcut map changes (§9, widened).
- The inventory's PHP candidates are cached in
  `<out>/drupal-php-candidates.json`, keyed by file stat and a digest of the
  registry and the extractor's code.

### 15.5 FormsRemote untouched

Before and after all three runs, and again after the final test suite:

- `git -C FormsRemote status --porcelain` was `?? docs/` (the user's own
  untracked directory, which was already there);
- `FormsRemote/graphify-out/` held one file, `cache/stat-index.json`, whose
  mtime (2026-09-24) and size were identical.

Nothing was written into the corpus, and no drush, ddev or docker command was
run. `test_p4_an_unchanged_rerun_re_extracts_two_files_and_the_corpus_is_untouched`
checks both in every corpus run.

### 15.6 Defects

The closing run found no P4 defect. Every number that differs from §2 or §12
is explained above by what the corpus holds, not by a fix. Two test issues
were fixed:

- the `<5 s` timing flake, now best of three cold runs with the budget kept;
- an unused import in `tests/test_drupal_container_overlay.py`.

Final suite: 6,501 passed, 100 skipped, and the 4 known
`test_ollama_retry_cap` failures (`openai` is not installed). `-k drupal`
with `DRUPAL_CONTAINER_ARTIFACT` set: 774 passed, and 4 skipped for missing
optional dependencies (`falkordb`, `docx`, tree-sitter-sql, `mcp`).

### 15.7 Left for later phases

- `preprocess_*` and `theme_suggestions_*` (9 candidates) are P5.
- `views.*` plugins from PHP need the dynamic `Plugin/views/$type` subdirs
  (2 candidates).
- Chains beyond the first call, and receivers outside §7.3/§7.4 (the 24
  `unresolved_receiver`s), need return and parameter types. Drush's
  `AutowireTrait` (12 of them) would be a rule of its own.
- The deferred minors in the P4 ledger, all reviewed as non-blocking:
  - `php_classes.py` is large enough to split, and its Doctrine reader could
    be its own module;
  - `<ext>.post_update.php` is not read;
  - a `RemoveHook`-emptied class is still treated as autowired;
  - the default form of a bundle-less type is labelled
    `form_BASE_FORM_ID_alter`;
  - `_sync_config_names` does not consult `is_ignored`.
