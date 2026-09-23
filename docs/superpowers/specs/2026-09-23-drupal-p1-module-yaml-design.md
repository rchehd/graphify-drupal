# P1 — Module-owned YAML: services, routes, permissions, libraries, links, breakpoints

Status: proposed, awaiting review
Date: 2026-09-23
Related: `2026-09-22-drupal-graph-vocabulary.md`,
`2026-09-22-drupal-graphify-architecture-design.md`,
`2026-09-22-drupal-p0-foundation-design.md`

All figures below were measured on `/home/user/Projects/FormsRemote`, a real
Drupal 11 project: 1,140 extensions (694 core, 423 contrib, 23 custom), 6,038
`.yml` files under `web/`, 621 under `config/`.

---

## 1. Goal

P0 proved one extractor can be registered at runtime and behave like a built-in
one. P1 uses that seam for the families that carry most of Drupal's declared
architecture: which services exist and what they inject, which routes exist and
what they require, which permissions and libraries each extension declares, and
how menu entries reach routes.

Everything P1 emits is **YAML declaring YAML**. That boundary is deliberate and
is the main scoping decision of this phase — see §4.

---

## 2. P1 splits in two

The architecture document folds configuration into P1. The measured volumes say
these are two phases, not one:

| Half | Files | Scanning model |
|---:|---:|---|
| **P1** — module-owned YAML | 1,383 | files named `<extension>.<family>.yml`, living beside the extension's `*.info.yml` |
| **P1b** — configuration | 629 | config entities in a sync directory, plus split folders outside it, owned by no extension |

They differ in more than size. Module-owned YAML is discovered by filename
family and attributes itself to an extension by prefix. Configuration is
discovered by directory, names its owner inside the file (`dependencies:`), and
its real location is not fixed — `config_split` moves parts of it outside
`config/sync` entirely.

P1b keeps its own spec. The remaining phases are not renumbered.

---

## 3. Three findings from the corpus that shape the design

### 3.1 `yaml.safe_load` loses 42% of the services

Drupal service files use Symfony's custom YAML tags. `safe_load` refuses them:

| File | Tag |
|---|---|
| `web/core/core.services.yml` | `!tagged_iterator` |
| `web/modules/contrib/modeler_api/modeler_api.services.yml` | `!service_closure` |

The first is **Drupal core's main service file**. Measured:

| Loader | Services parsed |
|---|---:|
| `yaml.safe_load` | 1,628 |
| tolerant loader | **2,205** |

P1 therefore parses with a `SafeLoader` subclass carrying a multi-constructor
for `!`, so any custom tag yields its underlying scalar, sequence or mapping
instead of raising. With it, zero files in the corpus fail to parse — including
the one deliberately malformed core test fixture,
`core/tests/.../invalid_file.libraries.yml`, which still errors and must be
reported rather than crash the run.

This loader is P1's single highest-value line of code: without it the graph
silently omits 692 services, most of Drupal core's container.

### 3.2 The secret screen must widen to exactly the family set

P0 exempted `*.info.yml` from core's credential screen and pinned the exemption
at that width with a test. Measured now over all 1,383 module-owned YAML files,
three are still dropped:

```
web/modules/contrib/token/token.services.yml
web/modules/contrib/token/token.routing.yml
web/modules/contrib/token/token.libraries.yml
```

Exactly the files P0's closing note predicted. The exemption widens from one
family to the family table in §5, and the existing test is extended: `token.yml`,
`token.json`, `credentials.yaml` and `secrets.yml` must still be caught, because
none of them matches a family suffix.

### 3.3 Exactly one family may declare an extension node

P0 found that graphify splits a node id when two files declare the same id with
different `source_file` values, prefixing each with its file stem. The same run
showed that merely *referencing* an id from another file is safe: `token` was
referenced by 16 edges and stayed canonical.

So the invariant P1 must hold is narrow and checkable: **`*.info.yml` is the only
family that emits `drupal:extension:*` nodes.** Every other family references the
owning extension from its edges and creates nothing. Where an owner has no
`*.info.yml` in the corpus, the existing cross-file resolver materialises it, as
it already does for undeclared dependency targets.

### 3.4 Every Drupal id is global

§3.3 is the special case of a wider fact, found while building the link
families: core's id-remap pass (`_disambiguate_colliding_node_ids`) salts apart
*any* id that two files declare. That is right for two same-named functions and
wrong for Drupal, whose container, routing table and tag vocabulary are global.
On the reference corpus it turned 57 service tags into over 300 nodes, split the
14 services and 4 routes that a test module overrides, and stranded every edge
written against the bare id, which core then drops without a word.

So the seam wraps that pass: nodes with `_origin: static_yaml` are collapsed to
one per id first — the declaration with the lowest path survives, and every
declaring file is kept in `declared_in` — and core receives a list with no
Drupal collision in it. Other producers' nodes are untouched. §3.3 still holds,
because an extension node created outside `*.info.yml` would carry the wrong
attributes, but splitting is no longer the reason.

---

## 4. Scope

### In

The nine families in §5, their nodes, and edges **between things this phase
declares**.

### Out, and why

Two edge classes are deliberately deferred rather than guessed at:

| Deferred | Reason | Lands in |
|---|---|---|
| `service_implemented_by`, `routes_to`, `routes_to_form`, `access_checked_by` | The target is a PHP class named by FQN. Mapping an FQN to graphify's PHP node id is the YAML↔PHP stitching problem, and the PHP layer does not exist yet. Emitting a guessed id would either dangle or, worse, materialise an external node that shadows the real class. | P4 |
| `library_has_asset` | The target is a `.css`/`.js` path. graphify has no CSS extractor and its own JS file-node id convention; inventing one here would collide. | P5 |

Both are recorded as **node attributes** in P1 — the FQN on the service and
route nodes, the asset lists on the library node — so the data is captured now
and the edges are drawn when the other endpoint genuinely exists.

This keeps every edge P1 emits verifiable inside P1: both endpoints are declared
by YAML this phase reads.

Also out: configuration (P1b), plugin discovery (P2), `.module`/`.install` PHP
(P4), templates and SDC (P5).

---

## 5. Families

Owner resolution is uniform: the file is `<owner>.<family>.yml`, and `<owner>` is
the extension machine name. Matching tries the **longest** family suffix first,
so `x.links.menu.yml` is never read as family `menu`.

| Family | Files | Entries | Primary node |
|---|---:|---:|---|
| `*.services.yml` | 375 | **2,205** | `drupal:service:<id>` |
| `*.routing.yml` | 293 | **1,533** | `drupal:route:<name>` |
| `*.libraries.yml` | 241 | **1,066** | `drupal:library:<owner>/<name>` |
| `*.permissions.yml` | 133 | **355** | `drupal:permission:<string>` |
| `*.links.task.yml` | 117 | **437** | `drupal:local_task:<id>` |
| `*.links.menu.yml` | 131 | **312** | `drupal:menu_link:<id>` |
| `*.links.action.yml` | 65 | **110** | `drupal:local_action:<id>` |
| `*.breakpoints.yml` | 9 | **36** | `drupal:breakpoint:<owner>:<name>` |
| `*.links.contextual.yml` | 19 | **34** | `drupal:contextual_link:<id>` |
| **total** | **1,383** | **6,088** | |

Entries count the nodes an extractor emits, not top-level YAML keys. The
first draft counted keys, which scored 15 parameters-only service files,
11 `route_callbacks` and 27 `permission_callbacks` sections as entries.
Every row is now confirmed by its extractor. Local actions have 112 keys; 2
belong to a config object whose file name ends in `.links.action.yml` (see §9).

### 5.1 Services

Nodes carry `class` (FQN), `abstract`, `deprecated` and `autowire` as attributes.

| Edge | From `*.services.yml` |
|---|---|
| `declares_service` | extension → service |
| `injects_service` | `arguments: ['@other']` — both endpoints are declared here |
| `tagged_as` | `tags: [{name: event_subscriber}]` → `drupal:tag:<name>` |
| `decorates` | `decorates:` |
| `parent_service` | `parent:` |

A `@?optional` argument keeps the edge with `confidence: INFERRED`; the service
may legitimately be absent. `%parameter%` arguments become `injects_parameter`
edges to `drupal:parameter:<name>`, which `parameters:` in the same family
declares.

### 5.2 Routes

Nodes carry `route_path` (not `path`, which core reads as a legacy alias of
`source_file`), and the `_controller` / `_form` / `_entity_form` value as an
attribute pending P4.

| Edge | From |
|---|---|
| `declares_route` | extension → route |
| `requires_permission` | `requirements._permission`; `+` and `,` separated lists become one edge each |

A `_permission` naming a permission that no `*.permissions.yml` declares is
normal — core's `access content` comes from `user`, which may or may not be in
the corpus. The existing resolver materialises it as external.

### 5.3 Permissions

`*.permissions.yml` declares permission nodes, including entries under
`permission_callbacks`, which are recorded as an attribute (the callback is PHP,
so the edge waits for P4).

### 5.4 Libraries

Nodes carry `version`, `license`, and the `css`/`js` lists as attributes.

| Edge | From |
|---|---|
| `declares_library` | extension → library |
| `library_depends_on` | `dependencies: ['core/once']` → `drupal:library:core/once` |

### 5.5 Links

All four link families are YAML-discovered plugins. They are read as data in P1;
recognising them *as plugins* belongs to P2's discovery registry.

| Edge | From |
|---|---|
| `declares_menu_link` / `declares_local_task` / `declares_local_action` / `declares_contextual_link` | extension → link |
| `links_to_route` | `route_name:` → route |
| `in_menu` | `menu_name:` → `drupal:menu:<id>` |
| `base_route` | local task → route |
| `parent_link` | `parent:` (menu link) / `parent_id:` (local task) → link |

A local task whose `base_route` equals its `route_name` is the tab its base route
shows by default: it keeps `links_to_route` and carries `default_tab: true`, since
both edges would share one pair. A menu link emits the `drupal:menu:<id>` node it
names; menus are declared by configuration (P1b), and the copies collapse (§3.4).

`links_to_route` and `base_route` are the edges that make this family worth the
phase: they connect the UI surface to the routing table, and both endpoints are
declared by YAML P1 reads.

### 5.6 Breakpoints

`declares_breakpoint` only; `mediaQuery` and `multipliers` are attributes.

---

## 6. Mechanism changes from P0

P0 wrapped three core functions against one predicate. P1 keeps the same three
wrappers and replaces the predicate with a family table:

- `is_drupal_yaml(path)` — true for any family suffix, used by `classify_file`
  and `_is_graphable_source`;
- `drupal_yaml_extractor(path)` — returns the family handler or `None`, used by
  `_get_extractor`.

Both read one table in `graphify/drupal/paths.py`, so classification and dispatch
cannot disagree. No new core function is touched, and the allow-list does not
change.

One new module per family group keeps files focused:
`yaml_services.py`, `yaml_routing.py`, `yaml_assets.py` (libraries +
breakpoints), `yaml_access.py` (permissions), `yaml_links.py`. The existing
`yaml_extract.py` keeps `*.info.yml` and gains the shared tolerant loader.

---

## 7. Volume

| | Nodes |
|---|---:|
| after P0 | 1,169 |
| P1 primary entities | +6,088 |
| tags, menus, parameters | ~+150 |
| **after P1** | **≈7,400** |

Already past graphify's ~5,000-node aggregation threshold, which the vocabulary
anticipated. P1 does not need to solve that — but it must not make it worse, so
`realm` and `layer` are mandatory on every node P1 emits, and an acceptance
criterion checks that a `realm: custom` slice is small enough to render
un-aggregated.

---

## 8. Acceptance criteria

Measured against the reference corpus, not a fixture.

1. **Parsing.** All 1,383 module-owned YAML files are read. Exactly one fails —
   `core/tests/.../invalid_file.libraries.yml`, which is deliberately malformed —
   and it returns an error dict rather than raising. The two Symfony-tag files
   parse.
2. **Services.** 2,191 service nodes — 2,205 declarations, 14 of them overrides
   of a service declared elsewhere — not 1,628. The gap is the whole point of
   the tolerant loader. `_defaults` (100 files) is Symfony file configuration,
   not a service.
3. **Secret screen.** Zero of the 1,383 files are dropped, including the three
   `token.*` ones. `token.yml`, `token.json`, `credentials.yaml`, `secrets.yml`
   are still dropped.
4. **One declarer.** Exactly 1,137 `drupal:extension:*` nodes carry a
   `source_file` ending in `.info.yml`; no other family declares one. The 1,140
   files include three pairs that are core's own name-collision fixtures
   (`evil`, `name_collision_test`, `drupal_system_listing_compatible_test`); each
   pair is one node with both files in `declared_in`. No Drupal id is salted.
5. **No dangling.** Every edge P1 emits has both endpoints present after the
   cross-file resolver runs.
6. **Ownership.** Every service, route, permission, library and link has at
   least one `declares_*` edge from an extension, and more than one only when its
   `declared_in` names that many files.
7. **Realm.** Every P1 node carries `realm` and `layer`; zero `realm: unknown`
   for files under a resolved extension.
8. **Incremental.** Editing one `*.services.yml` changes only edges sourced from
   that file — the same CLI-level check P0 used, which caught nothing but proves
   the incremental path still holds at 40× the file count.
9. **Upstream.** Full suite green apart from the four pre-existing `openai`
   failures; allow-list unchanged.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| The tolerant loader masks a real parse error | The multi-constructor applies only to `!`-prefixed tags. Structural errors still raise and are reported, as the malformed core fixture demonstrates. |
| A family suffix collides with a config filename (`system.menu.main.yml` vs a `*.menu.yml` family) | Family keys are full compound suffixes and the table is matched longest-first. `*.links.menu.yml` is a key; `*.menu.yml` is not. Criterion 4 catches a regression. One real case remains: `menu_test/config/install/menu_test.links.action.yml` is a config object; it yields no nodes. Excluding `config/install|optional` from the family table belongs to P1b. |
| Service ids collide across extensions | Drupal service ids are globally unique by definition — the container is flat. Criterion 6 catches a violation. |
| Node count pushes visualisation over the edge | Anticipated, not solved here. Criterion 7 keeps the filter attributes intact so P7 can generate slices. |

---

## 10. What this sets up

`links_to_route` plus `requires_permission` already answer "what does a user need
to reach this page" without any PHP. `injects_service` plus `tagged_as` give the
container's shape. Both become far more useful in P3, when the container producer
can be diffed against them — statics says what is shipped, the container says
what is wired.
