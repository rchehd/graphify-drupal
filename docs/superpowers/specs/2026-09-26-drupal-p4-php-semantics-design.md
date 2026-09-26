# P4 — PHP semantics: plugins, entity types, forms, services, events

Status: approved in brainstorming, awaiting spec review
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
