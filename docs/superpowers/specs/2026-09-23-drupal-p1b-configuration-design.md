# P1b — Configuration: config objects, splits, overrides, schemas, recipes

Status: proposed, awaiting review
Date: 2026-09-23
Related: `2026-09-22-drupal-graph-vocabulary.md` (§3.9, §4.1, §4.9),
`2026-09-22-drupal-graphify-architecture-design.md` (§5),
`2026-09-23-drupal-p1-module-yaml-design.md`

All figures were measured on `/home/user/Projects/FormsRemote`, the same Drupal 11
tree P1 used. Every count below is a count of **nodes after the id collapse** or
of files, never of top-level YAML keys — P1 overstated four of its reference
numbers by counting keys.

---

## 1. Goal

P1 read what extensions declare about themselves. P1b reads what the **site**
is: which configuration exists, which of it is active, what it depends on, what
overrides it and where, and which modules and themes the site installs.

It emits one node per configuration object and the edges that configuration
states about itself. What a field formats, what a view queries, which plugin a
block places — the *meaning* of heavy configuration — is P6's, attached later to
the same node ids.

---

## 2. What the corpus says

| Source | Files | Note |
|---|---:|---|
| `config/sync` | 613 | `core.extension.yml` lists 207 modules (the profile `standard` among them) and 8 themes |
| `config/splits/{dev,test,prod}` | 8 | config_split 2.x **patches**, not copies (§5.3) |
| `config/install`, `config/optional` of non-test extensions | 858 | 772 distinct names; **432 are also in sync**, 340 are shipped but not active |
| `config/install`, `config/optional` of test modules | 959 | out of scope (§4) |
| `config/schema/*.schema.yml`, non-test | 303 | 1,806 top-level keys: 1,805 schema types (`condition.plugin.entity_bundle:*` is declared twice), 150 of them wildcard patterns |
| `recipe.yml` | 58 | 28 in `core/recipes`, 30 test fixtures; 49 sit at `recipes/<name>/recipe.yml` (4 Composer-unpack fixtures and 5 grouped fixtures do not); 3 directory names occur twice, so **46 recipe ids** |
| recipe `config/*.yml` | 103 | 23 in test fixtures |
| `settings*.php` visible to git | 1 | `settings.php`: 8 `$config[…]` lines, 6 of them set a split's `status` |
| `domain.config.*.yml` in sync | 2 | per-domain `system.site` overrides |

Three facts shape the design:

1. **Configuration is found by location, not by name.** `system.site.yml` carries
   no family suffix. P1's filename table cannot see it.
2. **Which split is active is decided in `settings.php`**, by environment
   (`$config['config_split.config_split.prod']['status'] = TRUE`). All three
   splits have `status: false` in sync. Without reading `settings.php` the graph
   cannot say which environment changes what.
3. **Configuration holds secrets.** `smtp.settings`, `key.key.*` and the split
   patches carry hostnames, passwords and timeouts. The graph must carry
   structure and never values.

---

## 3. Discovery

### 3.1 Config stores are recognised by local markers

`graphify/drupal/config_stores.py` answers, for a path, "is this configuration,
and of which store?" It looks only at the path and its directory, and caches per
directory.

| `store` | Recognised by |
|---|---|
| `sync` | the directory contains `core.extension.yml` — every full site export does |
| `split` | the directory is named by the `folder:` of a `config_split.config_split.*` in a recognised sync store, resolved relative to the web root as Drupal resolves it |
| `install` / `optional` | `*/config/install/*.yml`, `*/config/optional/*.yml` below an extension |
| `schema` | `*/config/schema/*.schema.yml` below an extension |
| `recipe` | `recipe.yml`, and `<recipe>/config/*.yml` beside it |

A `language/<langcode>/` directory inside any store belongs to that store; it
carries no marker of its own. Split **patch** files (`config_split.patch.*`) and
language-override files each read for their `overrides_config` edge (§5.3) and
additionally emit one node of their own — `drupal_config_patch` /
`drupal_config_translation` — so core's incremental cache has something to stamp
the file against (Task 11: a file whose extraction returns zero nodes is treated
as un-extracted and re-queued on every run).

For a sync directory without the marker, `.graphifyrc` accepts
`drupal.config.sync = <path>`, parsed like the existing realm rules.

Chosen over an explicit `.graphifyrc`-only model, which loses all configuration
silently when unset, and over parsing `config_sync_directory` from `settings.php`,
which is frequently computed from `getenv()` and relative to a web root the
scanner must guess.

### 3.2 Ordering and exclusions

- **The config predicate is checked before P1's family table.** A file inside a
  config store is configuration whatever its name ends in. This closes P1's one
  known collision: `menu_test/config/install/menu_test.links.action.yml` is a
  config object, not a set of local actions.
- **Test modules are excluded**: `install`/`optional`/`schema` stores under
  `*/tests/*` are not configuration for this graph. The exception is
  `core/tests/fixtures/recipes/*`, kept because it is the only corpus exercising
  `config.actions` and `${input}`.
- **`settings*.php` is not rerouted.** Core's PHP extractor keeps it; P1b reads
  its `$config[…]` lines in a resolver pass (§5.4), which receives the path list.

Promotion to CODE, dispatch and the secret-screen exemption all consult one
predicate — family file *or* config file — exactly as P1's three wrappers
consult the family table today. `simple_oauth.oauth2_token.settings.yml` and
`key.key.*.yml` would otherwise hit the `token`/`key` keyword rule, the failure
P0 found on `token.info.yml`.

---

## 4. Scope

### In

Every configuration object in the stores of §3.1 as a node, its declared
dependencies, ownership of shipped configuration, schemas, splits, the four
override mechanisms of vocabulary §4.9, `core.extension.yml`, and recipes.

### Out, and why

| Deferred | Reason | Lands in |
|---|---|---|
| Field, display, view, block, Layout Builder semantics | the value of heavy configuration needs bundle, plugin and entity-type nodes that do not exist yet | P6 |
| Conditions around `$config[…]` in `settings.php` | a PHP control-flow question | P4 |
| `creates_content` (recipe `content/`) | its target is a bundle node (P6); recorded as `has_content: true` | P6 |
| Files excluded by `.gitignore` | core never passes them to extractors (`settings.local.php`, `settings.ddev.php` on the corpus); P1b cannot see what core does not collect | — |
| Configuration of test modules | 959 fixture files that describe no site | — |

**Configuration values never enter the graph**, with exceptions that are
structural metadata rather than site settings: a config entity's boolean
`status`, an extension's `weight` in `core.extension.yml`, a split's `folder`,
and a recipe's `name` (its label) and `type`. Keys are recorded by *path* (`hostname`,
`smtp_host`), never with the value assigned to them.

---

## 5. Vocabulary

### 5.1 Nodes — `layer: config` unless noted

| Type | ID | From |
|---|---|---|
| `drupal_config` | `drupal:config:<name>` | any config file; attributes `active`, `install_mode`, `status` |
| `drupal_config_split` | `drupal:config:<name>` | `config_split.config_split.*` — a specialised `type` on the config node; attribute `folder` |
| `drupal_domain` | `drupal:config:<name>` | `domain.record.*` — likewise |
| `drupal_config_schema` | `drupal:config_schema:<type>` | each top-level key of a `*.schema.yml`; `pattern: true` for wildcards |
| `drupal_config_patch` | `drupal:config_patch:<split>:<target>` | `config_split.patch.<target>.yml` in a split folder; keyed by split + target, never `config_id(name)`, so two splits patching the same target never collide; attributes `config_name`, `store: split`, `split`, `target_name`, `realm: custom`; no `_rank` |
| `drupal_config_translation` | `drupal:config_translation:<language>:<store kind>:<split>:<name>` | `language/<langcode>/<config>.yml` in a sync or split store; attributes `config_name`, `language`, `store`, `realm: custom` for sync/split stores; no `_rank` |
| `drupal_recipe` | `drupal:recipe:<dir>` | `recipe.yml`; `layer: extension`; the id is the directory name because `recipes:` refers to recipes by it |
| `drupal_settings` | `drupal:settings:<site>/<file>` | a `settings*.php` with at least one `$config[…]` line |

**Deviations from the vocabulary.**

1. A split and a domain keep the config id. The vocabulary's
   `drupal:config_split:<id>` would give one entity two nodes; P1 showed what
   that costs.
2. `installs_module` and `installs_theme` become one `installs_extension`. A
   recipe's `install: [node, claro]` does not say which is a theme, for the reason
   extensions already share one id namespace (vocabulary §3.1).
3. `storage_folder` is the attribute `folder`, not an edge to a path node; no
   other part of the graph uses path nodes.
4. `installed` is not an attribute: a resolver may not write to nodes of
   unchanged files, which incremental runs never return; the
   `installs_extension` edges from `core.extension` carry the fact.

A recipe whose `config.actions` keys or `config.import` lists name configuration
with `${…}` gets those names, sorted, in the attribute `templated_config` and no
edge for them: the target is unknowable before the recipe is applied.

### 5.2 Edges

| Relation | Source → target | From | Attributes |
|---|---|---|---|
| `config_depends_on` | config → config / extension | `dependencies.config`, `.module`, `.theme` | `dependency_kind`; `content:` entries are an attribute list, their targets are content |
| `enforced_dependency` | config → extension | `dependencies.enforced` | wins over `config_depends_on` for the same pair |
| `defines_config` | extension → config | owner of the `config/install|optional` directory | `install_mode` |
| `defines_schema` | extension → schema | owner of the `config/schema` directory | |
| `schema_for` | schema → config | exact type match `EXTRACTED`, wildcard match `INFERRED` | |
| `installs_extension` | `core.extension` config / recipe / profile → extension | `module:`, `theme:`, `install:` | `weight` |
| `splits_extension` | split → extension | `module`, `theme` | |
| `splits_config` | split → config | `complete_list`, `partial_list` | `split_kind: complete|partial` |
| `applies_recipe` | recipe → recipe | `recipes:` | |
| `imports_config` | recipe → config / extension | `config.import`; `'*'` targets the extension; a `${…}` name is `templated_config`, not an edge | `wildcard` |
| `config_action` | recipe → config | `config.actions`; a `${…}` name is `templated_config`, not an edge | `AMBIGUOUS` when an argument uses `${…}` |
| `overrides_config` | override source → config | §5.3–5.4 | `override_source`, `keys` |
| `contains` | override source → `drupal_config_patch` / `drupal_config_translation` | the patch/translation file's own node (Task 11) | |

At most one relation per ordered pair, as in P1.

### 5.3 Overrides

| `override_source` | Read from | Source node | Confidence |
|---|---|---|---|
| `split` (patch) | `config_split.patch.<config>.yml` in a split folder; key paths of `adding:` and `removing:` | the split | `EXTRACTED` |
| `split` (copy) | `<config>.yml` in a split folder; collapses with the sync copy, both in `declared_in` | the split, `whole: true` | `EXTRACTED` |
| `domain` | `domain.config.<domain>.[<langcode>.]<config>.yml` | `drupal:config:domain.record.<domain>` | `EXTRACTED`; `INFERRED` when the name parses two ways |
| `language` | `language/<langcode>/<config>.yml` in any store | `drupal:config:language.entity.<langcode>` | `EXTRACTED` |
| `settings_php` | §5.4 | the `drupal_settings` node | `AMBIGUOUS` |

A domain file name can be read two ways when a segment looks like a langcode.
The resolver picks the reading whose target config exists in the corpus; when
neither does, the edge is `INFERRED` and the target is materialised as external.

The language row has no instance on the corpus and is covered by synthetic tests
only.

The `split` (patch) and `language` rows each also emit one `drupal_config_patch`
/ `drupal_config_translation` node for the file itself, with a `contains` edge
from the same source node used for `overrides_config` — so the file is never an
island and, when that source is itself undeclared in the corpus, the `contains`
edge dangles on the source side exactly as `overrides_config`'s does today (the
resolver materialises neither; §6.3).

### 5.4 `settings.php`

A resolver pass reads every `settings*.php` in the path list and matches, per
line, `$config['<name>']` followed by any number of `['<key>']` and an `=`.
Lines whose first non-space characters are `#`, `//` or `*` are skipped. One
`overrides_config` edge per (file, config), with `keys` the list of key paths and
`source_location` the first matching line. The right-hand side is never read.

`AMBIGUOUS` because conditions are not parsed: on the corpus, `settings.php`
sets each split's `status` to `FALSE` and then, per environment, to `TRUE`. The
edges say *this file decides the split's status*; the split node's own `status`
says what sync holds. Together they answer "is prod's split active here?" as far
as static reading can.

---

## 6. Mechanism

### 6.1 One configuration name, one node

`system.site` may be declared by sync, by `system/config/install`, by a split
copy and by a recipe. The seam's collapse (P1 §3.4) keeps one node. P1's
survivor rule — lowest path — is replaced for `drupal_config` nodes by an explicit
rank:

**sync > split > recipe > optional > install**, then path.

Attributes the survivor lacks are filled from the next-ranked copy, so a synced
config gains `install_mode` from its shipped default. `active` is `true` exactly
when one copy is in a sync store. P1 node types keep the path rule; nothing they
emit changes.

A non-surviving copy's outgoing `config_depends_on` / `enforced_dependency`
edges are kept on the one node and marked `shadowed: true`: they say what the
shipped default needs, not what the active object needs. When the survivor's own
edges already name the same target, the shadowed duplicate is dropped, so an
ordered pair still carries at most one relation. `defines_config` edges and the
unranked (P1) groups are untouched. An incremental run re-extracts every copy of
a colliding id together (the seam's `extract()` wrapper widens the batch to the
collision group), because a copy extracted alone would win its own collapse.

### 6.2 Ownership

Shipped configuration and schemas are owned by the extension whose `*.info.yml`
sits above their `config/` directory: `defines_config`, `defines_schema`. Synced
configuration is the site's and has no owning edge; P1's criterion 6
(`declares_*`) does not apply to it.

### 6.3 Resolver

`_RESOLVABLE` gains `config_depends_on`, `enforced_dependency`,
`installs_extension`, `splits_extension`, `splits_config`, `imports_config`,
`config_action`, `overrides_config` and `applies_recipe`. A missing target's type
comes from its id prefix (`drupal_config_…`, `drupal_extension_…`,
`drupal_recipe_…`).

An extension listed in `core.extension.yml` but absent from the code base is
materialised with `missing: true` — the site expects a module the repository
does not contain, which is a real finding, not noise.

Whether the site installs an extension is the `installs_extension` edge from
`core.extension` to it, not a node attribute (§5.1, deviation 4). An attribute
would be written onto extension nodes of unchanged `*.info.yml` files, and an
incremental run returns only the nodes of the files it re-extracts, so the
attribute would go stale; the edge is re-emitted with `core.extension.yml`.

### 6.4 Unchanged from P1

Every producer uses `load_drupal_yaml`, `node()` / `edge()` and the id helpers.
The AST cache namespace follows the package source (P1 Task 7b), so new modules
invalidate it without a manual bump.

### 6.5 Decided during planning

1. **`settings.php` is read per file, not by a resolver pass (spec §5.4).** The resolver receives no scan root, and core hands it `settings.php`'s `source_file` relative. The seam instead returns, for `sites/*/settings*.php`, a handler that runs core's PHP extractor and appends the Drupal settings result. Per-file also makes it cached and incremental like every other producer.
2. **Path rules precede the sync marker.** `web/core/config/install/core.extension.yml` exists; a marker-first rule would call core's default config a sync store.
3. **Guards against non-Drupal repositories.** `config/install|optional|schema` counts only when an extension (`*.info.yml`, or Drupal core's own directory) sits above `config/`; `recipe.yml` counts only at `recipes/<name>/recipe.yml`.
4. **Realm.** Sync and split stores and `settings.php` are the project's own: `realm: custom`. `*/core/config/*` and `*/core/recipes/*` join the `core` rules.
5. **Recipe-shipped configuration** is owned by its recipe: `defines_config` recipe → config, `install_mode: recipe`.

---

## 7. Volume

| | Nodes |
|---|---:|
| after P1 | 7,646 |
| active configuration (sync) | 613 |
| shipped, not active | ~340 |
| recipe configuration not already counted | ≤80 |
| recipes | 46 |
| schema nodes (1,805 types, all distinct ids) | 1,805 |
| split-patch and language-override nodes (Task 11) | 8 |
| settings, externals | ~50 |
| **after P1b** | **≈10,600** |

Schemas are the largest item and are kept whole, including those of core modules
the site does not enable: "which schema does this extension ship" is itself a
question the graph should answer, and pruning by match would need wildcard
matching at resolve time. The `realm: custom` slice grows by roughly 60 nodes (53
shipped configs, 4 schema files).

### 7.1 Measured (Task 10)

Through `graphify.extract.extract` with a fresh `cache_root`, over every
`config/**/*.yml` and `web/**/*.yml` the Drupal predicate accepts plus
`web/sites/*/settings*.php` — 4,459 files. That glob also picks up the three
gitignored `settings.local.php`, `settings.ddev.php`, `settings.ddev.redis.php`,
which the CLI would not collect; `settings.ddev.php` adds the second
`drupal_settings` node (3 `overrides_config` edges, to `smtp.settings` and the two
domain records). Parse errors: `invalid_file.libraries.yml` only.

10,478 nodes, 22,282 edges; **10,473 Drupal nodes**, 918 of them `realm: custom`.

After the final-review fixes (§6.1): 21,274 edges. The 1,008 removed were a
shadowed copy's dependency edges repeating a pair the survivor already has; the
set of ordered pairs is unchanged (21,172), and 56 edges carry `shadowed: true`.

After Task 11 (collision-free schema ids, a node for every split patch): **10,488
nodes, 21,282 edges; 10,483 Drupal nodes**, 926 of them `realm: custom`. The
8-node, 8-edge growth is exactly the 8 `drupal_config_patch` nodes and their
`contains` edges (§9); the two extra schema nodes are `views.field.user` /
`views_field_user` and `views.field.bulk_form` / `views_field_bulk_form`, no
longer collapsed into one id. `drupal_config_translation`: 0, as FormsRemote has
no `language/<langcode>/` override files (the row is covered by synthetic tests
only, §5.3).

| Type | Nodes |
|---|---:|
| `drupal_service` | 2,220 |
| `drupal_config_schema` | 1,805 |
| `drupal_route` | 1,634 |
| `drupal_library` | 1,114 |
| `drupal_module` | 1,018 |
| `drupal_config` | 964 |
| `drupal_local_task` | 436 |
| `drupal_permission` | 362 |
| `drupal_menu_link` | 311 |
| `drupal_parameter` | 136 |
| `drupal_local_action` | 108 |
| `drupal_theme` | 99 |
| `drupal_extension` | 59 |
| `drupal_service_tag` | 57 |
| `drupal_recipe` | 46 |
| `drupal_breakpoint` | 36 |
| `drupal_contextual_link` | 34 |
| `drupal_profile` | 20 |
| `drupal_menu` | 9 |
| `drupal_config_patch` | 8 |
| `drupal_config_split` | 3 |
| `drupal_domain` | 2 |
| `drupal_settings` | 2 |
| `drupal_config_translation` | 0 |

Configuration nodes (`config_name` set): 969 = 613 active (sync) + 340 shipped
and not active (305 install, 35 optional) + 9 recipe-only + 7 external. 215
`installs_extension` edges from `core.extension`, none to a missing extension.
45 `config_action` edges (1 `AMBIGUOUS`); `create_node_type` carries
`templated_config: [node.type.${node_type}]`. `schema_for`: 104 `EXTRACTED`,
835 `INFERRED`.

Schema types: 1,805 distinct (150 wildcards) give 1,805 nodes, all 150
wildcards with `pattern: true`, and no two distinct types share an id (Task
11): `schema_id` keeps the direct id for a type that already matches
`^[a-z0-9]+(\.[a-z0-9]+)*$`, and hashes the rest (§9).

---

## 8. Acceptance criteria

Measured through `graphify.extract.extract` with a fresh `cache_root`, in
`tests/test_drupal_corpus.py`. P1's criteria stay green.

1. **Parse.** Only deliberately malformed core fixtures fail; the list is
   established by measurement during implementation and asserted by name.
2. **Active configuration.** Exactly 613 `drupal_config` nodes carry
   `active: true`, one per file in `config/sync`.
3. **Secret screen.** Zero configuration files are dropped.
4. **Splits.** Three split nodes with `folder` resolved to
   `config/splits/{dev,test,prod}`; 8 patches produce `overrides_config` edges
   and one `drupal_config_patch` node each, with a `contains` edge from the
   owning split (Task 11); `settings.php` produces `AMBIGUOUS` edges to all
   three splits with `status` in `keys`.
5. **Installed.** Exactly 215 `installs_extension` edges from `core.extension`
   (207 modules including the profile, 8 themes), every target a node; no node
   carries `installed`; listed-but-absent extensions carry `missing: true`.
6. **Integrity.** No dangling edge, no salted Drupal id;
   `menu_test/config/install/menu_test.links.action.yml` belongs to a test
   module, so it yields neither a config node nor local actions (the module's own
   `menu_test.links.action.yml` still yields its local actions).
7. **No values.** No value of the split patches or `smtp.settings` reaches the
   graph: none of their 8+-character strings occurs anywhere in the output, and
   no node or edge attribute (other than P1's `weight`/`version`) equals any
   value of a patch's `adding:`/`removing:` block, however short — the
   `password_reset_timeout` and `password_reset` values included. The
   `settings.php` right-hand side is never read (§5.4).
8. **Recipes.** 46 recipe nodes from 49 recognised files — `article_content_type`,
   `article_tags` and `page_content_type` exist as a core recipe and as a test
   fixture, and collapse with both in `declared_in`; every `config_action` whose arguments contain
   `${…}` is `AMBIGUOUS`, and no edge carries a `${…}` name.
9. **Incremental.** Through the CLI: editing one split patch changes only that
   patch's `overrides_config` edge; removing a module from `core.extension.yml`
   changes exactly one `installs_extension` edge and nothing else.
10. **Volume.** Roughly 10,600 nodes; the `realm: custom` slice stays under 5,000.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| A sync directory without `core.extension.yml` (partial export) is not recognised | `.graphifyrc` override; criterion 2 fails loudly on the reference corpus if detection regresses |
| A split `folder:` outside the scanned tree | the split node keeps `folder`; its configuration is simply absent, and the spec says so rather than guessing |
| Collapse rank misapplied to P1 types | the rank applies to `drupal_config` only; P1's corpus criteria guard the rest |
| A value leaks through a new attribute | criterion 7 scans the whole `graph.json`, not named fields |
| `settings.php` regex matches inside a heredoc or string | accepted: `AMBIGUOUS` already says the edge is unverified |
| A test-fixture recipe shares a directory name with a core recipe (3 on the corpus) | collapsed like P1's name-collision fixtures, with both files in `declared_in`; recipes refer to each other by that name, so a separate id would dangle every `applies_recipe` |
| Wildcard schema matching is slow on 1,805 types × ~1,500 configs | patterns are grouped by their literal prefix before `fnmatch`; measured in the plan |
| The AST cache keys a configuration file by its own content, but its store (and so its node) also depends on neighbouring files — a `core.extension.yml` marker, a split entity's `folder:`, an `*.info.yml` above `config/` | accepted: such a change is rare and a `--force` rebuild (or a fresh cache) re-reads it; the per-run store caches are cleared, so only the persisted AST cache can hold a stale answer |
| A P1 collision (an overridden service) whose shadowing file also declares other ids, when only the survivor's file changes: the persisted graph hands `extract()` no node naming the shadowing file, so the collision group cannot be found | accepted residual: the survivor keeps its own attributes (it wins either way); only `declared_in` loses the unchanged shadowing file until that file is re-extracted or a full build runs. Configuration is not affected: a shadowed config copy owns no node, so core re-extracts it on every run and it pulls the survivor in |

**Resolved (Task 11), after the final whole-branch review:**

| Former risk | Resolution |
|---|---|
| Two literal schema pairs shared an id because `make_id` treats `.` and `_` alike: `views.field.user` / `views_field_user`, `views.field.bulk_form` / `views_field_bulk_form` | `schema_id` now hashes any type that is not plain lowercase-dotted (§5.1); no two distinct types share an id on the corpus (§7.1) |
| A split patch or a language override returned zero nodes, so core's zero-node heal left the file unstamped and re-extracted it on every run | each now emits one `drupal_config_patch` / `drupal_config_translation` node with a `contains` edge from its owning entity (§3.1, §5.1–5.3); the file is stamped like any other config file |

---

## 10. What this sets up

- **P2** learns plugin types; P6 then attaches `configures_plugin` to config
  nodes that already exist.
- **P3**'s container artifact can be checked against the `installs_extension`
  edges from `core.extension`: a service from an extension that is not installed
  is a divergence worth logging.
- **P6** adds field, display, view and block semantics to the `drupal_config`
  nodes P1b creates, without new ids.
