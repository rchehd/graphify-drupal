# Drupal Graph Vocabulary

Status: proposed, awaiting review
Date: 2026-09-22
Related: `docs/drupal-graphify.md` (origin), `2026-09-22-drupal-graphify-architecture-design.md`

This is the reference contract for every node and edge the Drupal producers emit.
It spans all phases; individual phase specs cite it rather than redefining terms.

---

## 1. Design rules

These four rules are why the vocabulary looks the way it does. Breaking any of
them breaks something downstream that is not obvious at the call site.

### 1.1 `realm` and `layer` are attributes, not node types

`custom` / `contrib` / `core` must never become separate node types. Three module
types plus three library types plus three service types is a vocabulary that
triples for no analytic gain. They are one attribute on every node:

| Attribute | Values | Derivation |
|---|---|---|
| `realm` | `custom` \| `contrib` \| `core` \| `vendor` | from composer, else from path via configurable rules (§1.2) |
| `layer` | `extension` \| `di` \| `routing` \| `plugin` \| `hook` \| `model` \| `block` \| `presentation` \| `config` \| `infra` | from node type, fixed |

`realm` is **inherited**: a service declared by a contrib module is `contrib`.
Inheritance is computed at merge time from the declaring extension, not guessed
per node.

Without these two attributes the filtered views in §6 are impossible, and the
full graph is too large to read. They are load-bearing, not metadata.

### 1.2 `realm` derivation is configured, never hardcoded

Path globs cannot be baked in. Real projects vary on all three axes:

- docroot is `web/`, `docroot/`, or the repository root;
- the custom directory is `custom/`, `local/`, or a client name;
- **install profiles contain their own modules and themes**
  (`profiles/<name>/modules/...`), so a "custom" module can live under a profile.

Defaults ship with the package; a project overrides them in `.graphifyrc`:

```
drupal.realm.core     = */core/modules/*, */core/themes/*, */core/profiles/*, */core/lib/*
drupal.realm.contrib  = */modules/contrib/*, */themes/contrib/*, */profiles/contrib/*
drupal.realm.custom   = */modules/custom/*, */themes/custom/*, */profiles/*/modules/*, */profiles/*/themes/*
```

First match wins, in the order core → contrib → custom.

**From P2b, composer decides first** (P2b spec §4.1, `boundary.realm_of`), in
this order:

1. `.graphifyrc` `drupal.realm.<realm>` patterns, when the project sets any,
   still win.
2. The nearest *project root* — a directory holding both `composer.json` and
   `composer.lock`. A `composer.json` with no lock beside it is a package's
   own manifest (85 of 94 contrib modules on the reference corpus ship one)
   and is walked past. Every package in the lock gets an install dir from
   `extra.installer-paths` (`type:<type>` or the package name; `{$name}`,
   `{$vendor}` and the package's own `extra.installer-name` substituted), or
   `<vendor-dir>/<vendor>/<name>`. `drupal-core` → core; `drupal-module`,
   `-theme`, `-profile`, `-recipe`, `-drush`, `-library`, `npm-asset`,
   `bower-asset` → contrib; `drupal-custom-*` → custom; any other type, and the
   vendor dir itself → vendor. Longest install dir wins; a path inside the
   project but under no install dir is custom.
3. No project root (or an unreadable `composer.json`/`composer.lock`, recorded
   in the inventory as `composer_unreadable`): the web root's `core/` as a
   whole is core, then the path rules above, then a `vendor/` beside the web
   root is vendor.
4. Everything else is custom.

`realm_of` never answers `unknown`. A node still carries `realm: unknown` when
it has no path to ask about: a boundary stub (§2.1) the registry knows nothing
of — a route, library or permission named only by an edge, or a hook no
`*.api.php` declares.

### 1.3 `file_type` must stay inside the core schema

`graphify/build.py` silently rewrites any `file_type` outside
`{code, document, paper, image, rationale, concept}` to `"concept"`. Emitting
`drupal_file`, `twig`, or `service` as `file_type` therefore *appears* to work
until the graph passes through `build()` — which `cluster-only` and
`merge-graphs` both do.

Rule:

- every node parsed from a file in the repository → `file_type: "code"`;
- the Drupal taxonomy lives in a separate `type` field (the pattern
  `graphify/manifest_ingest.py` already uses for `type: "package"`);
- a node with no file in the repository (e.g. `drupal:extension:views` in a project
  that does not vendor core) → `file_type: "concept"` plus `external: true`,
  matching what `build.py` already does for external nodes.

The last point resolves roadmap item 9 in `docs/drupal-graphify.md`: the 226
`source_file`-less nodes are not a schema violation to paper over with a
synthetic path, they are external nodes that were never tagged as such.

### 1.4 One relation per node pair

graphify's reader collapses parallel edges: two relations between the same pair
become one, and the loser is dropped silently. The measured loss in the current
artifact is 78 edges, caused by emitting both `contains_file` and
`declares_extension` between a module and its `.info.yml`.

Since every node already carries `source_file`, containment is derivable and
`contains_file` is redundant. It is not emitted. Producers must not emit two
relations between the same ordered pair; where two are genuinely meaningful,
emit the more specific one and record the other as an edge attribute.

---

## 2. Universal attributes

Every node carries:

| Field | Notes |
|---|---|
| `id`, `label`, `source_file`, `source_location` | graphify core schema |
| `file_type` | `code`, or `concept` for external — see §1.3 |
| `type` | the Drupal taxonomy, e.g. `drupal_service` |
| `realm` | `custom` \| `contrib` \| `core` \| `vendor` \| `unknown` (§1.2) |
| `layer` | see §1.1 |
| `_origin` | `static_yaml` \| `static_php` \| `learned` \| `container` |
| `external` | `true` when the entity has no file in the repository |
| `boundary` | `true` on a node materialised for something the site's code references but no scanned file declares (§2.1); absent otherwise |

Whether an extension is installed is not a node attribute: it is the
`installs_extension` edge from the `core.extension` config node (P1b deviation 4
— an attribute on an unchanged `*.info.yml`'s node would go stale on an
incremental run, while the edge is re-emitted with `core.extension.yml`).

Every edge carries `source`, `target`, `relation`, `confidence`
(`EXTRACTED`/`INFERRED`/`AMBIGUOUS`), `source_file`, `source_location`, and
`_origin`. Relation-specific attributes are named per entry below.

### 2.1 The boundary

From P2b a site's graph is its own code plus a **boundary** (P2b spec §4):
core, contrib and vendor code appear only as the named things the site's code
touches, never as their internals.

- **Boundary trees are not walked.** In a Drupal run, `detect()` never descends
  an install dir of a core, contrib or vendor realm, nor the vendor dir, whether
  committed or gitignored; `--no-gitignore` no longer pulls core in. Each site's
  public files directory (`sites/<site>/files`) is pruned too (reason
  `site_files`; it is not a realm, and no opt-in brings it back). The registry
  walks (plugin types, hooks, services, extensions) still read the boundary.
  Non-Drupal runs are not affected.
- **Opt-in:** `.graphifyrc` `drupal.include = contrib` (or `core, contrib`,
  `vendor`) keeps the named realms in the graph.
- **Boundary nodes** are the resolver's materialised stubs, with
  `boundary: true`, `external: true`, and what the registry knows:

| Stub type | Facts |
|---|---|
| `drupal_extension` | `extension_type` (`module`/`theme`/`profile`), `extension_path` (relative to the scan root when inside it, else absolute — not `path`, which core folds into `source_file`), `realm` |
| `drupal_service` | `class_name`, `provider` (the `*.services.yml` stem), `realm` of the provider |
| `drupal_plugin_type` | every §3.4 attribute, `realm` of the manager class file |
| `drupal_hook` | `provider`, `declared_file`, `line`, `pattern` (variable hooks only), `realm` of the `*.api.php` |
| `drupal_menu_link` and the other P1 link types | `plugin_of_type` when the family is a learned type's `yaml_name` |
| route, library, permission, config | `boundary: true` only (reading core's YAML for them is later) |

A stub the registry does not know keeps `realm: unknown`. Facts are refreshed
only when a file referencing the stub is re-extracted: a composer update alone
leaves the old facts until then (P2b spec §9).

The inventory's summary counts the pruned directories —
`boundary: {core, contrib, vendor, files}` — and `boundary_reasons`
(`composer`, `path_rule`, `vendor_dir`, `site_files`).

---

## 3. Node types

### 3.1 Extensions — `layer: extension`

Modules, themes and profiles share **one id namespace**:
`drupal:extension:<machine_name>`. Drupal itself treats these names as one
namespace, and a `dependencies:` entry names an extension without saying which
kind it is — the kind is only knowable after reading that extension's own
`*.info.yml`. Separate `drupal:module:` and `drupal:theme:` namespaces would
force the producer to guess the target's kind at edge-creation time, and every
wrong guess is a dangling edge.

The kind lives in `type`, not in the id.

| Type | ID | Source |
|---|---|---|
| `drupal_module` | `drupal:extension:<name>` | `*.info.yml`, `type: module` |
| `drupal_theme` | `drupal:extension:<name>` | `*.info.yml`, `type: theme` |
| `drupal_profile` | `drupal:extension:<name>` | `*.info.yml`, `type: profile` |
| `drupal_recipe` | `drupal:recipe:<dir>` | `recipe.yml` |

Ids in this document are written in readable form. Producers build them with
`graphify.ids.make_id`, so `drupal:extension:foo` is stored as
`drupal_extension_foo`. Emitting the readable form raw lets `build.py`'s own
normalisation rewrite it, after which the producer and the builder disagree
about the same node.

### 3.2 Container — `layer: di`

| Type | ID | Source |
|---|---|---|
| `drupal_service` | `drupal:service:<id>` | `*.services.yml` |
| `drupal_service_tag` | `drupal:tag:<tag>` | `tags:` in service definitions |
| `drupal_parameter` | `drupal:parameter:<name>` | `parameters:` |
| `drupal_event` | `drupal:event:<event_name>` | `getSubscribedEvents()` |

Service providers are not their own node type — the `*ServiceProvider` PHP class
node from the AST layer carries an `alters_container` edge to its module.

### 3.3 Routing and access — `layer: routing`

| Type | ID | Source |
|---|---|---|
| `drupal_route` | `drupal:route:<route_name>` | `*.routing.yml` |
| `drupal_menu` | `drupal:menu:<id>` | `system.menu.*.yml` |
| `drupal_menu_link` | `drupal:menu_link:<plugin_id>` | `*.links.menu.yml` |
| `drupal_local_task` | `drupal:local_task:<plugin_id>` | `*.links.task.yml` |
| `drupal_local_action` | `drupal:local_action:<plugin_id>` | `*.links.action.yml` |
| `drupal_permission` | `drupal:permission:<string>` | `*.permissions.yml` |
| `drupal_role` | `drupal:role:<id>` | `user.role.*.yml` |

### 3.4 Plugins — `layer: plugin`

| Type | ID | Source |
|---|---|---|
| `drupal_plugin_type` | `drupal:plugin_type:<id>` | **learned** — §5 |
| `drupal_plugin` | `drupal:plugin:<type>:<id>` | per the learned type's discovery |

`drupal_plugin_type` carries `discovery: annotation | attribute | yaml | mixed |
dynamic`. The value `dynamic` is an explicit statement that static analysis
cannot enumerate this type's instances and the container is authoritative.

`<id>` is the manager's service id without a leading `plugin.manager.`
(`block`, `menu.link`); a service id without that prefix is used whole; a
manager that is not a service uses `class:<FQCN>`. Label `<id>`. The node is
emitted by the extraction of the manager's class file, so `source_file`/
`source_location` point at the class declaration (P2a spec §5.4).

| Attribute | Value |
|---|---|
| `plugin_type` | `<id>` |
| `discovery` | `annotation`, `attribute`, `yaml`, `mixed` (more than one), or `dynamic` (a discovery built in `getDiscovery()` or `__construct()` that is not statically readable) |
| `manager_class` | FQCN |
| `registered` | `true` when a `*.services.yml` service reaches the class, else `false` |
| `manager_service` | the service id; absent when not registered |
| `subdir` | `Plugin/Block`, from `parent::__construct`'s first argument when a literal |
| `interface`, `annotation_class`, `attribute_class` | FQCNs from `parent::__construct`, when literal or `::class` |
| `yaml_name` | the `YamlDiscovery`/`YamlDiscoveryDecorator` name, when any |
| `alter_hook` | the `alterInfo('x')` literal, when any (P2b: an `invokes_hook` edge from the type to `<x>_alter`, §4.5) |
| `deferred_to` | `P5` for SDC, `P6` for migrations; a deferred type is never `dynamic` |

Empty attributes are omitted.

`drupal_plugin` (P2a: YAML-discovered plugins of a learned type whose family no
other phase owns — one per top-level key of `<ext>.<yaml_name>.yml`) carries,
beyond the universal attributes, exactly `plugin_id`, `plugin_type`,
`provider` (the owning extension), and `class_name`/`deriver` when the
definition has `class:`/`deriver:`. Labels, weights and settings are values and
never reach the graph. P1's menu links, local tasks, local actions, contextual
links and breakpoints are YAML-discovered plugins too; they keep their own node
types and gain a `plugin_of_type` edge instead.

### 3.5 Hooks — `layer: hook`

| Type | ID | Source |
|---|---|---|
| `drupal_hook` | `drupal:hook:<hook_name>` | `*.api.php` stubs, invocation sites |
| `drupal_hook_impl` | `drupal:hook_impl:<module>:<hook>` | procedural functions, `#[Hook]` |
| `drupal_form` | `drupal:form:<form_id>` | `getFormId()`, `_form:` in routes |
| `drupal_theme_hook` | `drupal:theme_hook:<hook>` | `hook_theme()` |

**Emitted from P2b** (P2b spec §5): `drupal_hook` and `drupal_hook_impl`.
`drupal_hook` carries `hook_name`, `provider`, `pattern` (each UPPERCASE
segment run as `*`, e.g. `form_*_alter`, only for variable hooks); it comes
from an in-graph `*.api.php` stub, else it is a boundary stub (§2.1).
`drupal_hook_impl` carries `module`, `hook_name`, `via`
(`attribute`/`procedural`), `function` or `class_name` + `method`, and
`order` (the verbatim source text of `#[Hook(order: …)]`, never evaluated).
There is one `drupal_hook_impl` node per (module, hook), however many
functions or methods implement it; each implementation has its own
`hook_implemented_by` edge. `drupal_form` and `drupal_theme_hook` stay for
P4–P5.

`drupal_form` and `drupal_theme_hook` sit in this layer because their reason for
existing is to give `hook_form_FORM_ID_alter` and `hook_preprocess_HOOK` a
resolvable target. Without them those hook chains have no endpoint.

### 3.6 Data model — `layer: model`

| Type | ID | Source |
|---|---|---|
| `drupal_entity_type` | `drupal:entity_type:<id>` | `#[ContentEntityType]` / `@ContentEntityType` and config variants |
| `drupal_bundle` | `drupal:bundle:<entity_type>:<bundle>` | `node.type.*`, `block_content.type.*`, … |
| `drupal_field_storage` | `drupal:field_storage:<entity_type>:<field>` | `field.storage.*.yml` |
| `drupal_field` | `drupal:field:<entity_type>:<bundle>:<field>` | `field.field.*.yml` |
| `drupal_view_mode` | `drupal:view_mode:<entity_type>:<mode>` | `core.entity_view_mode.*` |
| `drupal_display` | `drupal:display:<entity_type>:<bundle>:<mode>:<view\|form>` | `core.entity_{view,form}_display.*` |

### 3.7 Blocks — `layer: block`

| Type | ID | Source |
|---|---|---|
| `drupal_block_placement` | `drupal:block_placement:<config_id>` | `block.block.*.yml` |
| `drupal_region` | `drupal:region:<theme>:<region>` | `regions:` in a theme `*.info.yml` |

Block is deliberately split from the plugin that implements it and the content
entity it may render — see §4.7.

### 3.8 Presentation — `layer: presentation`

| Type | ID | Source |
|---|---|---|
| `drupal_template` | `drupal:template:<path>` | `*.html.twig` |
| `drupal_sdc` | `drupal:sdc:<owner>:<name>` | `*.component.yml` |
| `drupal_library` | `drupal:library:<owner>/<name>` | `*.libraries.yml` |
| `drupal_asset` | `drupal:asset:<path>` | `css:` / `js:` targets |
| `drupal_breakpoint` | `drupal:breakpoint:<owner>:<name>` | `*.breakpoints.yml` |

### 3.9 Configuration — `layer: config`

| Type | ID | Source |
|---|---|---|
| `drupal_config` | `drupal:config:<config_name>` | `config/install`, `config/optional`, sync dir |
| `drupal_config_schema` | `drupal:config_schema:<type>` | `config/schema/*.schema.yml` |
| `drupal_config_split` | `drupal:config_split:<id>` | `config_split.config_split.*.yml` |
| `drupal_domain` | `drupal:domain:<id>` | `domain.record.*.yml` |
| `drupal_config_patch` | `drupal:config_patch:<split>:<target>` | `config_split.patch.<target>.yml` in a split folder; one node per file, keyed by split + target so two splits patching the same config never collide |
| `drupal_config_translation` | `drupal:config_translation:<language>:<store kind>:<split>:<name>` | `language/<langcode>/<config>.yml` in a sync or split config directory; one node per file |

### 3.10 Infrastructure — `layer: infra`

| Type | ID | Source |
|---|---|---|
| `drupal_env` | `drupal:env:<tool>` | `.ddev/config.yaml`, `.lando.yml`, `compose.yaml` |

Composer packages reuse graphify's existing `pkg:` nodes rather than inventing a
parallel taxonomy.

---

## 4. Edge types

### 4.1 Extensions

| Relation | Source → target | Attributes |
|---|---|---|
| `depends_on_module` | module → module | from `dependencies:`; normalise `drupal:node`, `views:views_ui`, bare `node` to one id |
| `requires_package` | module → `pkg:` | from `composer.json`; deliberately distinct from `depends_on_module` — divergence between the two is diagnostic |
| `installs_module` | profile/recipe → module | `install:` |
| `installs_theme` | profile/recipe → theme | `install:` |
| `applies_recipe` | recipe → recipe | `recipes:` — recipes compose |
| `imports_config` | recipe → config | `config.import` |
| `config_action` | recipe → config | `config.actions`; `AMBIGUOUS` when the action uses an `${input}` token |
| `creates_content` | recipe → bundle | recipe `content/` directory |
| `base_theme` | theme → theme | `base theme:` |
| `declares_extension` | module/theme → `*.info.yml` file node | the only module↔info.yml relation — see §1.4 |

### 4.2 Container

| Relation | Source → target | Attributes |
|---|---|---|
| `declares_service` | module → service | |
| `service_implemented_by` | service → PHP class | |
| `injects_service` | service → service | from `arguments: ['@x']` **and** from constructor types when autowiring leaves `arguments:` absent |
| `injects_parameter` | service → parameter | `%name%` |
| `tagged_as` | service → tag | tag nodes make `event_subscriber`, `access_check`, `paramconverter`, `path_processor_*`, `theme_negotiator`, `cache.context` visible as groups without a node type each |
| `decorates` | service → service | `decorates:` |
| `parent_service` | service → service | `parent:` |
| `uses_service` | PHP class/function → service | `\Drupal::service('id')` |
| `alters_container` | PHP class → module | `*ServiceProvider` |
| `subscribes_to_event` | PHP class → event | `getSubscribedEvents()`; `priority` attribute |

### 4.3 Routing and access

| Relation | Source → target | Attributes |
|---|---|---|
| `declares_route` | module → route | |
| `routes_to` | route → PHP class::method | `_controller` |
| `routes_to_form` | route → form | `_form` — targets the form node, not the class |
| `requires_permission` | route → permission | `_permission` |
| `access_checked_by` | route → class::method or service | `_custom_access`, `_entity_access` |
| `references_route` | class / template / menu_link → route | `Url::fromRoute()`, Twig `path()`/`url()`, `route_name:` |
| `alters_routes` | PHP class → route | `RouteSubscriber`; always `AMBIGUOUS` |
| `links_to_route` | menu_link / local_task / local_action → route | |
| `in_menu` | menu_link → menu | |
| `base_route` | local_task → route | |
| `grants_permission` | role → permission | `user.role.*.yml` |

### 4.4 Plugins

| Relation | Source → target | Attributes |
|---|---|---|
| `defines_plugin_type` | module → plugin_type | `owner`; the manager service and class are node attributes |
| `plugin_manager_for` | service → plugin_type | |
| `provides_plugin` | module → plugin | |
| `plugin_of_type` | plugin → plugin_type | |
| `plugin_implemented_by` | plugin → PHP class | |
| `derives_plugins` | plugin → deriver class | `deriver:` in annotation/attribute |
| `configures_plugin` | config → plugin | e.g. `block.block.*` → block plugin, `field.field.*` → formatter |

P2a emits the first four. `defines_plugin_type` comes from the manager's owner
(the extension of its services file, or of its class file when it is not a
service; `core` for `core/lib` and `core.services.yml`); `plugin_manager_for`
only for registered managers; `plugin_of_type` from every `drupal_plugin` and
from every P1 menu link, local task, local action, contextual link and
breakpoint whose family is some learned type's `yaml_name`. The plugin's class
and deriver are recorded as `class_name`/`deriver` attributes until PHP class
nodes can be bound: `plugin_implemented_by` and `derives_plugins` come in P4
(with PHP-discovered plugins), `configures_plugin` in P6.

### 4.5 Hooks

| Relation | Source → target | Attributes |
|---|---|---|
| `declares_hook` | module → hook | from `*.api.php` stubs |
| `invokes_hook` | PHP class/function → hook | `invokeAll`, `invoke`, `invokeAllWith`, `alter`, `hasImplementations`; `AMBIGUOUS` when the first argument is not a literal |
| `implements_hook` | module → hook | `order` attribute from `#[Hook(order:)]`; the owning module is the `module:` parameter when present, not the file's module |
| `hook_implemented_by` | hook_impl → PHP function/method | |
| `alters_form` | hook_impl → form | `hook_form_FORM_ID_alter` |
| `declares_theme_hook` | module → theme_hook | `hook_theme()` |
| `uses_template` | theme_hook → template | `template:` key |
| `renders_theme_hook` | PHP class → theme_hook | `'#theme' => 'x'` in render arrays — the consuming half of `declares_theme_hook` |
| `preprocesses` | hook_impl → theme_hook | `hook_preprocess_HOOK` |
| `suggests_template` | hook_impl → theme_hook | `hook_theme_suggestions_*_alter`; always `AMBIGUOUS` |
| `overrides_template` | template → template | filename specificity chain, e.g. `node--article--teaser` over `node` |

P2b emits the first four:

- `declares_hook` from the extension owning an in-graph `*.api.php` stub.
- `implements_hook` (attributes `owner`, `target_name`) only for a literal,
  declared hook name (§5.6): `#[Hook('x')]` (on a method, or on a class with
  `method:` or `__invoke`) in the extension's `src/Hook/**/*.php` — where
  Drupal's `HookCollectorPass` looks — with `module:` as the owner when given;
  or a procedural `<ext>_<hook>()` in `<ext>.module`, `.install`, `.theme`,
  `.profile` or `<ext>.<group>.inc` beside `<ext>.info.yml`. `order` is a
  `drupal_hook_impl` attribute, not an edge attribute. An extension that
  implements a hook it declares itself gets no `implements_hook`: that pair
  already carries `declares_hook` (§1.4). `<ext>_update_N` and
  `<ext>_post_update_*` are update functions, never hooks.
- `hook_implemented_by` to the function/method node id core's PHP extractor
  emits for that file, computed with core's id helpers and emitted only when
  core emitted exactly that id.
- `invokes_hook` from the enclosing function/method to the hook named by a
  string literal of `invokeAll`, `invoke`, `invokeAllWith`, `alter`,
  `hasImplementations` or a `*Deprecated` form; `alter('x')` targets
  `x_alter`, `alter(['a', 'b'])` each. `invoke` and `alter`, names other APIs
  share (`ReflectionMethod::invoke`), count only on an explicit module- or
  theme-handler receiver (`\Drupal::moduleHandler()`,
  `\Drupal::service('module_handler')`, `\Drupal::service('theme.manager')`,
  `\Drupal::theme()`, or a variable/property named `moduleHandler` or
  `themeManager`); on any other receiver the call is an `unknown_receiver`
  candidate. The other names count on any receiver. A hook no `*.api.php`
  declares is still the target (a boundary stub) and the edge carries
  `undeclared: true`. From a `drupal_plugin_type` with `alter_hook` to
  `<alter_hook>_alter`. A non-literal name is not `AMBIGUOUS`: it is no edge,
  and a `non_literal` inventory candidate.

Everything that looks like a hook but is not a literal, declared name goes to
the inventory's `hook_candidates` (kinds `variable` with the `pattern` it
matched, `undeclared`, `misplaced` — `#[Hook]` outside `src/Hook/` —,
`non_literal` and `unknown_receiver`), never to an edge. A procedural
`<ext>_<rest>()` naming no declared hook is a candidate only when it claims to be one (an
`Implements hook_…` docblock, or `<rest>` starting with `<group>_` in
`<ext>.<group>.inc`); any other `<ext>_*` is a helper. Variable-segment
binding (`alters_form`, `preprocesses`, `suggests_template`, `ENTITY_TYPE_*`)
waits for the form, theme-hook and entity inventories (P4–P6, §5.5).

### 4.6 Data model

| Relation | Source → target | Attributes |
|---|---|---|
| `defines_entity_type` | module → entity_type | |
| `entity_handler` | entity_type → PHP class | `handler` attribute: `storage`, `list_builder`, `form`, `access`, `views_data` |
| `has_bundle` | entity_type → bundle | |
| `field_storage_of` | field_storage → entity_type | |
| `field_instance_of` | field → field_storage | |
| `bundle_has_field` | bundle → field | |
| **`references_bundle`** | bundle → bundle | from `field.storage.*.settings.target_type` plus `field.field.*.handler_settings.target_bundles`. **This is the content model graph** and is the single highest-value diagram for a Drupal project |
| `field_uses_formatter` | display → plugin | view display |
| `field_uses_widget` | display → plugin | form display |
| `display_of` | display → bundle | `view_mode` attribute |

### 4.7 Blocks

| Relation | Source → target |
|---|---|
| `places_in_theme` | block_placement → theme |
| `places_in_region` | block_placement → region |
| `configures_plugin` | block_placement → plugin (the block plugin) |
| `visibility_condition` | block_placement → plugin (a Condition plugin) |
| `renders_entity` | block_placement → bundle (for `block_content`) |

Blocks consume the plugin layer rather than duplicating it. This is why the
learned plugin registry (§5) must exist before block edges mean anything —
without it `configures_plugin` and `visibility_condition` point at nodes that
were never created.

### 4.8 Presentation

| Relation | Source → target |
|---|---|
| `declares_library` | module/theme → library |
| `library_depends_on` | library → library |
| `library_has_asset` | library → asset |
| `attaches_library` | template / PHP class / theme → library |
| `overrides_library` / `extends_library` | theme → library |
| `defines_component` | module/theme → sdc |
| `component_template` | sdc → template |
| `component_asset` | sdc → asset |
| `uses_component` | template / PHP class → sdc |
| `selects_component` | config → sdc |
| `includes_twig` | template → template |

SDC `props` and `slots` are node **attributes**, not nodes. As nodes they would
add thousands of leaves that connect to exactly one parent each.

### 4.9 Configuration

| Relation | Source → target | Attributes |
|---|---|---|
| `config_depends_on` | config → config/module/theme | `dependency_kind: config\|module\|theme\|content` |
| `enforced_dependency` | config → module | separate relation: its semantics differ — uninstalling the module deletes the config |
| `defines_config` | module → config | `install_mode: install\|optional` |
| `schema_for` | config_schema → config | |
| `splits_module` / `splits_config` | config_split → module/config | |
| `storage_folder` | config_split → path | tells the scanner where else configuration lives |
| **`overrides_config`** | override source → config | `override_source: split \| domain \| language \| settings_php` |
| `contains` | split/language entity → `drupal_config_patch`/`drupal_config_translation` | the patch or translation file's own node, so it is never an island; the same source `overrides_config` already uses for that file |

`overrides_config` unifies four mechanisms that all answer the same question —
*the value in the file is not the value in production*:

| Source | Location |
|---|---|
| Config Split | the split's folder, outside `config/sync` |
| Domain | `domain.config.<domain>.<config>.yml` |
| Language | `language/<langcode>/<config>.yml` |
| settings.php | `$config['system.site']['name'] = …` |

The first three are fully static. The fourth is a PHP assignment and yields an
inventory entry, not a confident edge.

`storage_folder` is load-bearing and must land early: configuration inside a
split folder is not in `config/sync`, so a scanner that does not know about
splits reports it as absent. That is the exact failure class this project exists
to avoid.

---

## 5. The discovery model

Two Drupal mechanisms are open-ended by design: any module may invent a plugin
type, and any module may invent a hook. A fixed list of filename patterns cannot
cover either. Both are therefore **learned from the project's own source**, and
both use the same three-bucket model.

### 5.1 Three buckets

| Bucket | Meaning |
|---|---|
| **known** | Drupal's own families, themselves discovered rather than hardcoded (core's `*.api.php`, core's plugin managers) |
| **learned** | discovered from this project's code |
| **unrecognised** | matched nothing — emitted as an inventory entry, never dropped silently and never guessed into an edge |

The third bucket is the point. The dominant failure in the benchmark behind this
project was not "did not find" but "declared absent". An unrecognised-family
inventory turns the coverage declaration from prose someone maintains by hand
into output generated on every run.

### 5.2 Learning plugin types

A plugin manager is a service, usually with `parent: default_plugin_manager`,
whose class extends `DefaultPluginManager`. The type's definition sits in string
literals in that class. Two places must be read, not one:

1. `parent::__construct($subdir, $namespaces, $module_handler, $interface,
   $annotation_or_attribute_class)` — yields the subdirectory (`Plugin/Block`),
   the annotation class (D8–D10) or attribute class (D10.2+), and the interface;
2. an **overridden `getDiscovery()`** — custom managers frequently build their
   own discovery chain there: `YamlDiscovery('name', …)`,
   `YamlDirectoryDiscovery(…)` (which is how SDC works),
   `YamlDiscoveryDecorator` (annotation *and* YAML for one type), or
   `ContainerDerivativeDiscoveryDecorator`.

Reading only the constructor misses every manager that overrides `getDiscovery()`.

**Static discovery is incomplete by construction.** Derivatives generate plugin
definitions at runtime; no parser enumerates them. This is not a defect in the
plan — it is the reason the container producer is the second half of the answer,
and why divergence between the two is recorded rather than resolved.

### 5.3 Ordering constraint: classification runs before PHP

`detect.classify_file()` runs before any PHP is parsed, so a learned YAML pattern
must exist before the first file is classified. The registry is therefore
**built at the start of every `detect`** (a wrapper on `detect.detect`), from
`*.services.yml` and plugin-manager classes only — about 3 s on the reference
corpus (P2a spec §8).

`graphify-out/drupal-discovery.json` is an **output**, not an input. It is never
trusted as a cache of the registry: an input cache would need invalidating on
every services file, every manager class and every `.info.yml`, and a stale
entry would silently misclassify YAML. It exists for two readers:

- **extraction workers** — core extracts in a `ProcessPoolExecutor`, and a
  worker never runs `detect`; the wrapper sets `GRAPHIFY_DRUPAL_DISCOVERY` to the
  file's absolute path and `current_registry()` loads it once per process. The
  file also carries `force_miss`, the files a changed registry forces out of the
  AST cache, so spawn/forkserver workers see it too; the set is carried across
  runs until an `extract()` completes;
- **the next run** — read before it is overwritten, it is the previous registry
  the change detection compares against (P2a spec §5.5).

Scope rules:

- a registry is built only when the tree has a Drupal marker — a web root
  (`core/lib/Drupal.php`) at, below (`web/`, `docroot/`) or above the scan
  root, or a `*.info.yml` directly in the scan root; any other tree is not
  walked, gets no registry file and no inventory;
- the registry is knowledge about the site, not graph content: the walk
  honours explicit user intent — `.graphifyignore` and `--exclude` — and core's
  noise-dir pruning (`graphify.detect.ignored_predicate` with `gitignore=False`),
  so an ignored module defines no type, but it does **not** honour
  `.gitignore`, by design: a composer-managed site gitignores `web/core` and
  `web/modules/contrib`, which define almost every plugin type. The graph
  still honours `.gitignore`; an edge from a custom file to a type defined in
  an ignored tree points at a materialised (`missing: true`) type node. The
  predicate is asked only about directories and the files the registry
  collects;
- when the web root is an **ancestor** of the scan root, the registry walks the
  web root, outside the scan root, and those paths are not subject to the
  scan's ignore rules (core's predicate is anchored at the scan root).

`graphify-out/drupal-inventory.json` is written beside it on every Drupal run —
what discovery did not recognise (§5.1's third bucket), the deferred families
and the unresolved managers — and rendered as the "Drupal coverage" section of
`GRAPH_REPORT.md`. A non-Drupal run removes a stale one.

### 5.4 Learning hooks

Drupal has a canonical declaration mechanism for hooks that plugins lack: every
extension ships `<name>.api.php` containing non-executing `function hook_*()`
stubs. Scanning `core/**/*.api.php` and `modules/custom/**/*.api.php` with the
same code gives core's hundreds of hooks and the project's custom ones with no
hardcoded list.

Four sources, in order of authority:

1. `*.api.php` stubs — the declaration;
2. invocation sites — `invokeAll`, `invoke`, `invokeAllWith`, `alter`,
   `hasImplementations`; the first argument is a literal in the common case, and
   this is the ground truth that a hook actually fires;
3. implementations — procedural `<module>_<hook>()` and `#[Hook]` (Drupal 11.2,
   in three forms: on a method, on a class with `method:`, on a class with
   `__invoke`);
4. `hook_hook_info()` for group files.

Divergence between 1 and 2 is informative: documented but never invoked means
dead; invoked but undocumented means an undeclared API.

### 5.5 The `foo_bar_baz` problem

Given a function `foo_bar_baz()`, there is no way to tell from the name alone
whether it is module `foo` implementing hook `bar_baz` or module `foo_bar`
implementing hook `baz`. It resolves only with **both** inventories present: the
module list from `*.info.yml` and the hook list from §5.4.

Hooks with a variable segment resolve by cross-reference to inventories built
elsewhere in this vocabulary:

| Hook pattern | Resolved by |
|---|---|
| `hook_form_FORM_ID_alter` | form inventory (§3.5) |
| `hook_preprocess_HOOK` | theme-hook inventory (§3.5) |
| `hook_ENTITY_TYPE_insert` | entity-type inventory (§3.6) |
| `hook_field_widget_WIDGET_TYPE_form_alter` | plugin inventory (§3.4) |

This makes the vocabulary self-reinforcing, and imposes a hard ordering: hook
**discovery** is early and cheap, hook **binding** runs only after the form,
theme-hook, entity-type and plugin inventories exist.

### 5.6 No guessing

Most functions named `<module>_<something>` are ordinary helpers, not hook
implementations.

- match against a **known** hook → `implements_hook`, `EXTRACTED`;
- no match → **no edge**. The function goes to the inventory as a candidate and
  is never presented as a fact.

---

## 6. Volume and filtered views

The current artifact is 5,952 nodes and 9,819 edges. graphify switches to
aggregated visualisation above roughly 5,000 nodes — the existing graph already
renders as 358 communities rather than nodes.

The layers in this document add, in round terms: entity types, bundles and
fields (hundreds), permissions and roles (hundreds), theme hooks (hundreds),
forms, events, service tags (tens each). A realistic total is **9,000–12,000
nodes**.

The consequence is not to cut the vocabulary but to accept a two-artifact model:

- **one full graph** as the source of truth for queries;
- **generated slices** for visual reading, filtered by `realm` and `layer`.

A `realm: custom` slice of the reference project is roughly 1,500–2,500 nodes,
which renders in detail without aggregation.

---

## 7. What is not statically knowable

This list is the graph's declared competence boundary and ships beside the
artifact. Everything here requires the container producer.

| Not static | Why |
|---|---|
| plugin derivatives | definitions are generated by code at runtime |
| container changes from `*ServiceProvider` and compiler passes | the container is assembled programmatically |
| routes added by `RouteSubscriber` | same |
| actual hook execution order | depends on weights and alters |
| `$config[…]` overrides in `settings.php` | PHP assignment, not declaration |

Two things commonly assumed to need runtime **do not**:

- **which modules are enabled** — `core.extension.yml` in the sync directory is a
  static list of installed modules and themes with weights. The divergence
  recorded in `docs/drupal-graphify.md` §4 (23 container edges vs 29 file edges,
  the difference being six shipped-but-not-enabled sub-modules) is resolvable
  statically from the `installs_extension` edges of `core.extension` (an
  extension without one is shipped but not enabled);
- **block placement and Layout Builder defaults** — `block.block.*.yml` is a
  config entity, and Layout Builder defaults live in
  `core.entity_view_display.*` under
  `third_party_settings.layout_builder.sections`. Only per-entity Layout Builder
  *overrides* are content. `docs/drupal-graphify.md` §8 currently understates
  this.
