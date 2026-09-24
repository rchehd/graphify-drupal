# P2b — Hooks, the core boundary, and P2a's carried fixes

Status: approved in brainstorming, 2026-09-24
Date: 2026-09-24
Related: `2026-09-22-drupal-graph-vocabulary.md` (§3.5, §4.5, §5.4–5.6),
`2026-09-22-drupal-graphify-architecture-design.md` (§5),
`2026-09-23-drupal-p2a-plugin-discovery-design.md` (§9 — carried fixes)

All figures were measured on `/home/user/Projects/FormsRemote` (Drupal 11.2),
excluding `tests/` trees.

---

## 1. Goal

A site's graph is **its own code** plus a **boundary**: whatever core, contrib
and vendor code that code touches, represented as named, typed nodes, never as
core's internals. Onto that graph P2b adds hooks: which the site declares,
invokes and implements.

## 2. What the corpus says

**Full graph vs own code**, measured with the real CLI:

| | own code (`.gitignore` honoured today) | everything (`--no-gitignore`) |
|---|---|---|
| time | 6.7 s | 10 min 43 s |
| nodes / edges | 4,563 / 9,968 | 286,749 / 657,737 |
| `graph.json` | 6.7 MB | 430 MB |
| custom share | all | about 1 % of nodes |

The own-code graph already holds 290 stubs of boundary things (194 extensions,
43 services, 19 routes, 15 libraries, 7 permissions, 4 plugin types, 4 config,
3 menu links) with 1,299 of its 9,968 edges pointing at them, all
`realm: unknown` and named only.

**Hooks:**

| | core | contrib | custom |
|---|---|---|---|
| `function hook_*()` stubs in `*.api.php` | 298 | 137 | 0 |
| distinct hook names | — | — | 435 total, 51 with an UPPERCASE variable segment |
| invocation sites | 304 | 170 | 0 |
| `#[Hook(...)]` | 846 | 280 | 31 (19 name a declared hook literally) |
| procedural `<ext>_<declared hook>()` | 80 | 734 | 23 |

**Core facts** this design depends on:
- `.module`, `.install`, `.theme`, `.inc`, `.profile` are not PHP to core: not in
  `detect.CODE_EXTENSIONS`, not in `extract._DISPATCH`. Procedural hooks are
  invisible until they are.
- Core's PHP ids: class `make_id(<file stem>, <class>)`, method
  `make_id(<class id>, <method>)`; the PHP namespace is not part of the id.
- The zero-node re-queue is `cli._zero_node_stamped_code_sources(graph_path,
  scan_root, unchanged_code)`, reading only node `source_file`; it is called
  through the module global.
- `detect()`'s walk prunes through `detect._is_noise_dir(part, parent)`, a
  module global also used by `detect.ignored_predicate`.

## 3. Scope

### In
- The boundary (§4): realm from composer, boundary trees pruned from `detect`,
  `drupal.include` opt-in, boundary nodes enriched from the registries.
- Hooks (§5): the hook registry, `drupal_hook`, `drupal_hook_impl`,
  `declares_hook`, `implements_hook`, `hook_implemented_by`, `invokes_hook`.
- `.module`/`.install`/`.theme`/`.inc`/`.profile` become PHP for extraction.
- Carried fixes (§6): the `config/install` re-queue; menu-link stubs.
- Closing real CLI run (§8).

### Out
- Variable-segment hook binding (`form_FORM_ID_alter`, `preprocess_HOOK`,
  `ENTITY_TYPE_*`, `field_widget_WIDGET_TYPE_form_alter`) → inventory
  candidates now, edges in P4–P6 once form, theme-hook and entity inventories
  exist.
- Routes, libraries and permissions of the boundary beyond `boundary`/`realm`
  (needs reading core's YAML; later).
- Container (`drush ev`) → P3.

## 4. The boundary

### 4.1 Realm

`boundary.realm_of(path) -> "core" | "contrib" | "vendor" | "custom"`, in order:

1. `.graphifyrc` `drupal.realm.<realm> = patterns` (P0) wins.
2. With a `composer.json` at or above the path (nearest one; cached per
   directory) and its `composer.lock`: every package in `packages` and
   `packages-dev` gets an install path — the first `extra.installer-paths`
   pattern whose selector list contains `type:<package type>` or the package
   name, with `{$name}` (name after `/`) and `{$vendor}` substituted; a package
   no pattern selects installs to `config.vendor-dir` (default `vendor`) /
   `<vendor>/<name>`. Package type → realm: `drupal-core` → core;
   `drupal-module`, `drupal-theme`, `drupal-profile`, `drupal-recipe`,
   `drupal-drush`, `drupal-library`, `npm-asset`, `bower-asset` → contrib;
   `drupal-custom-*` → custom; anything else → vendor. The vendor dir itself is
   vendor. A path inside an install path takes that realm (longest match).
3. Without composer: P0's path rules (`*/core/*` core, `*/contrib/*` contrib);
   `vendor/` beside the web root is vendor.
4. Everything else is custom — modules, themes, profiles, recipes a package did
   not install, `sites/`, `config/`, `drush/` outside contrib.

`yaml_common.node()` sets `realm` through `realm_of`, so P1–P2a nodes get the
same answer (today's `realm: unknown` nodes mostly disappear).

### 4.2 Boundary trees are not walked

A wrapper on `detect._is_noise_dir(part, parent)` returns true for a directory
that is an install path of a core, contrib or vendor realm (or the vendor dir),
so `detect()` never descends it, whether or not it is committed or gitignored.
While the registry walks (`discovery`), the wrapper answers with core's
original only (a process flag set in `try/finally`), so registries still read
the boundary. `.graphifyignore` and `--exclude` apply as before, on top.

`.graphifyrc` `drupal.include = contrib` (or `core, contrib`, `vendor`) keeps
the named realms in the graph. `--no-gitignore` no longer pulls core in by
itself.

The inventory's summary gains `boundary: {core, contrib, vendor}` directory
counts and the reason (`composer`, `path_rule`, `vendor_dir`).

### 4.3 Boundary nodes

The resolver already materialises a node for an edge target (or owner) no
scanned file declares. It now also sets `boundary: true`, `realm` from
§4.1 when a path is known, and registry facts:

| stub | facts |
|---|---|
| `drupal_extension` | `extension_type` (module/theme/profile), `path` (relative to the scan root when inside it, else absolute), `realm` |
| `drupal_service` | `class_name`, `provider` (from every `*.services.yml` the registry reads) |
| `drupal_plugin_type` | every §4.1 attribute of P2a |
| `drupal_hook` | `provider`, `declared_file`, `line`, `pattern` |
| route, library, permission, config | `boundary: true` only |

A materialised `drupal_menu_link` whose family is a learned type gets
`plugin_of_type` (P2a §9 item 2).

Boundary facts come from the registry, which already reads the boundary: P2a's
walk gains the services index (id → class, provider) and the extension index
(name → type, dir).

## 5. Hooks

### 5.1 The registry

The P2a walk also collects every `*.api.php` (boundary included). Each
`function hook_<name>(` stub gives a declaration `{name, provider, file, line,
pattern}` where `provider` is the extension owning the file and `pattern` is
the name with each UPPERCASE segment run marked (`form_FORM_ID_alter` →
`form_*_alter`), present only for variable-segment hooks. `Registry.hooks`
maps name → declaration; the JSON round-trips it.

### 5.2 Procedural PHP files

A seam change makes `<ext>.module`, `.install`, `.theme`, `.profile` and
`<ext>.*.inc` in an extension directory PHP for detection and extraction: they
classify as code and dispatch to core's PHP handler, composed with the hook
extractor (as P1b composes `settings.php`). `watch` sees them through
`CODE_EXTENSIONS` (mutated in place, as the architecture §2.2 planned).

### 5.3 Nodes and edges

| | id | emitted by |
|---|---|---|
| `drupal_hook` | `make_id("drupal", "hook", <name>)` | an in-graph `*.api.php` per stub; otherwise a boundary stub (§4.3) |
| `drupal_hook_impl` | `make_id("drupal", "hook_impl", <module>, <hook>)` | the implementing file |

`drupal_hook` attributes: `hook_name`, `provider`, `pattern` (when variable).
`drupal_hook_impl`: `module`, `hook_name`, `via` (`attribute`/`procedural`),
`function` (procedural) or `class_name` + `method`, `order` (from
`#[Hook(order: …)]`, verbatim source text of the argument).

| relation | source → target | notes |
|---|---|---|
| `declares_hook` | extension → hook | in-graph stubs |
| `implements_hook` | extension → hook | the owner is `#[Hook(module: 'x')]` when given, else the file's extension |
| `hook_implemented_by` | hook_impl → PHP function/method node | source id computed with core's own id helpers; the edge is emitted only when the id equals a node core emits for that file (tested), else the impl keeps `function`/`method` attributes only |
| `invokes_hook` | PHP function/method → hook | `invokeAll`, `invoke`, `invokeAllWith`, `alter`, `hasImplementations`, and their `*Deprecated` forms, with a string-literal hook name; `alter('x')` targets `x_alter`; `alter(['a','b'])` targets each |
| `invokes_hook` | plugin_type → hook | P2a `alter_hook` → `<alter_hook>_alter`, emitted with the type node |

### 5.4 Recognising implementations

- `#[Hook('name')]` (also `#[\Drupal\Core\Hook\Attribute\Hook(...)]`) on a
  method; on a class with `method: 'm'`; on a class with `__invoke`. Other
  arguments: `module:`, `order:`.
- Procedural `function <ext>_<rest>()` in §5.2 files, `<ext>` the owning
  extension: `<rest>` equal to a declared hook name → implementation.
- **No guessing** (vocabulary §5.6). These go to the inventory's new
  `hook_candidates` list, not to edges:
  - `<rest>` matches a variable pattern (`form_user_login_form_alter` ~
    `form_*_alter`), with the pattern it matched;
  - `#[Hook('x')]` or a procedural `<rest>` naming no declared hook
    (`undeclared`);
  - an invocation whose hook name is not a literal (`non_literal`, with file
    and line).

### 5.5 A changed hook set re-extracts implementers

`affected_files` (P2a §5.5) also compares the hook sets: when a declaration is
added, removed or changes pattern, every in-graph §5.2 file and every
`src/Hook/**/*.php` of an in-graph extension is forced to miss and widened.

## 6. Carried fixes

1. **Unchanged reruns re-extract shadowed config copies.** A wrapper on
   `cli._zero_node_stamped_code_sources` removes from its result every path
   that appears in some node's `declared_in` in the same `graph.json` (paths
   relative to the scan root, normalised as the function normalises
   `source_file`). No node is added. `watch` never re-queues this way, so it
   needs nothing. Assert the symbol (DrupalSeamError).
2. **Menu-link stubs** get `plugin_of_type` (§4.3).

## 7. Errors

Nothing in `boundary`, the registry, the hook extractor or the inventory
raises: a broken `composer.json`/`composer.lock` falls back to path rules and
is recorded in the inventory (`composer_unreadable`); an unparsable PHP file
yields core's result alone. The seam raises `DrupalSeamError` for a missing
`detect._is_noise_dir`, `detect.CODE_EXTENSIONS`, `cli._zero_node_stamped_code_sources`.

## 8. Acceptance

Corpus tests (FormsRemote):
1. `realm_of` gives core for `web/core/**`, contrib for every composer-installed
   module/theme/profile/recipe path, vendor for `vendor/**`, custom for
   `web/modules/custom/**`, `web/themes/custom/**`, `config/**`, `web/sites/**`.
2. The hook registry holds 435 declarations (or the measured number,
   explained).
3. Every custom `#[Hook]` (31) and every custom procedural `<ext>_<declared
   hook>` (23) is an implementation or a `hook_candidates` entry — none silent.
4. Every `hook_implemented_by` target exists in the graph.
5. Registry + boundary build (`prepare_run`) under 5 s.

**Closing real run** (every phase from P2b on): `graphify extract
/home/user/Projects/FormsRemote --code-only --out <scratch>`, twice.
- nothing written into the project;
- node count within 20 % of 4,563 plus the new hook nodes, no core/contrib/vendor
  `source_file` in the graph;
- `realm: unknown` count reported (target: 0 outside materialised route,
  library and permission stubs);
- hook nodes, implementations and boundary nodes counted in the report;
- the unchanged rerun re-extracts only files core itself re-queues for other
  reasons — the ~50 shadowed config copies are gone;
- the same run with `drupal.include = contrib` in a scratch copy of
  `.graphifyrc` is not needed; the opt-in is covered by tests.

## 9. Risks

- **Composer layouts vary** (custom `installer-paths`, path repositories,
  `vendor-dir` outside the project, merge plugins). Unknown shapes fall back to
  path rules and are named in the inventory.
- **Id agreement with core's PHP ids** may break upstream; the edge is emitted
  only when the id is proven equal in the same extraction (§5.3), and a test
  pins it.
- **`_is_noise_dir` wrapping** changes `watch`'s ignore view too: boundary
  events are ignored there, which is the intended behaviour.
