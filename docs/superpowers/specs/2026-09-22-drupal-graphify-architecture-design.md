# Drupal Graphify: Fork Architecture and Phase Plan

Status: proposed, awaiting review
Date: 2026-09-22
Related: `2026-09-22-drupal-graph-vocabulary.md`, `docs/drupal-graphify.md`

This document covers **how** the Drupal capability lives inside this fork and
**in what order** it gets built. The node and edge contract is in the vocabulary
document; this one does not restate it.

---

## 1. Goal

Two goals, in tension, and the architecture exists to resolve the tension:

1. Drupal architectural relationships — modules, routes, plugins, hooks,
   services, config — appear in the graph.
2. The upstream graphify core stays updatable. A new upstream release must be
   absorbable by rebase, not by manual reconciliation.

---

## 2. Approach: subpackage with a single runtime seam

Three options were considered.

| | Approach | Verdict |
|---|---|---|
| A | Side-car producers entirely outside the package | Rejected. Zero upstream conflict, but forfeits `cache.py` (1,782 lines of incremental logic), `detect_incremental`, and `watch`. Every one of those would be rebuilt by hand. |
| **B** | **Subpackage `graphify/drupal/` plus one seam** | **Chosen.** |
| C | Full fork with `extract.py` rewritten | Rejected. 8,322 lines that upstream is actively refactoring, one language per PR, per `extractors/MIGRATION.md`. Every update becomes a merge conflict. |

### 2.1 Layout

```
graphify/drupal/
├── __init__.py
├── paths.py          # filename predicates, realm rules, .graphifyrc reading
├── discovery.py      # the learned registry — plugin types, hooks (pre-pass)
├── yaml_extract.py   # Drupal YAML families -> nodes/edges
├── php_extract.py    # .module/.install/.theme/.profile/.inc, annotations, attributes
├── container.py      # drush producer and its artifact
├── resolvers.py      # cross-file binding, registered with resolver_registry
├── merge.py          # merge with divergence log
└── register.py       # the ONLY module that touches core
```

### 2.2 The seam

`register.py` edits nothing in core. `install()` places a finder on
`sys.meta_path` that wraps the loaders for `graphify.detect` and
`graphify.extract`, patching each the moment it finishes executing:

```python
def install() -> None:
    if not any(isinstance(f, _DrupalFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _DrupalFinder())
    for name, patch in _PATCHERS.items():        # already-imported modules
        module = sys.modules.get(name)
        if module is not None:
            patch(module)
```

A hook rather than eager registration because `graphify/__init__.py` is
deliberately lazy. Measured: `import graphify` costs 1 ms, `import
graphify.extract` costs 809 ms, and `graphify install` must work before heavy
dependencies exist. `install()` imports only `sys` and `importlib`.

As built, the patchers wrap `detect.classify_file`,
`detect._is_graphable_source` and `extract._get_extractor`, and register one
cross-file pass through `resolver_registry`. Mutating `CODE_EXTENSIONS` is not
needed until P4 adds `.module`/`.install`/`.theme`/`.profile`.

Three verified facts make this work:

| Fact | Evidence |
|---|---|
| `_DISPATCH` is read per call, not at import | `_get_extractor()` ends in `return _DISPATCH.get(suffix)` |
| `watch._CODE_EXTENSIONS` is an alias of the same set object, so in-place `\|=` propagates | `_CODE_EXTENSIONS = CODE_EXTENSIONS` in `watch.py` |
| `resolver_registry.register()` is a public, documented extension point | its module docstring describes exactly this use |

`watch._WATCHED_EXTENSIONS` is a *new* set built with `|` at import time, so it
does not see the mutation and must be updated explicitly in `register()`.

The total diff against upstream is two lines in `graphify/__init__.py`, plus one
line in `pyproject.toml` adding `graphify.drupal` to the explicit `packages`
list.

### 2.3 Known risk of the seam

`_DISPATCH` is private, and `extractors/MIGRATION.md` states that routing
dispatch through the public registry is planned upstream work. This fork must
not discover that change by producing a silently Drupal-free graph.

Mitigation: `register()` asserts the presence and shape of every core structure
it touches and raises a named error if one is missing. A test exercises those
assertions. Failure must be loud and at import, not a quiet loss of edges.

### 2.4 Two core behaviours to route around, not patch

Both are documented in the vocabulary (§1.3, §1.4); repeated here because they
are architecture-level:

- `build.py` rewrites unknown `file_type` values to `"concept"`. The Drupal
  taxonomy therefore lives in a separate `type` field.
- The graph reader collapses parallel edges. Producers emit at most one relation
  per ordered node pair.

Neither requires a core change.

---

## 3. Upstream tracking

### 3.1 Current state (verified 2026-09-22)

`upstream` was added and fetched. `git rev-list --left-right --count
v8...upstream/v8` returns `0 0` — the local `v8` is identical to upstream's. The
base is clean and needs no reconciliation.

### 3.2 Branch model

| Branch | Role |
|---|---|
| `v8` | mirror of `upstream/v8`, zero local commits. Updated with `git fetch upstream && git merge --ff-only upstream/v8` |
| `drupal-graph` | all fork work. Updated with `git rebase v8` |

### 3.3 Two rules

1. **Commits that touch core files are separate and prefixed `core:`.** Then
   `git log v8..drupal-graph --grep '^core:'` is the exhaustive list of what to review
   after an update. Without the prefix, that list is reconstructed by hand every
   time.
2. **CI enforces the allow-list.** The patterns live in
   `graphify/drupal/allowlist.txt` — read by both `tests/test_drupal_allowlist.py`
   and `.github/workflows/drupal-guard.yml`, so the rule has one source. Every
   path in `git diff upstream/v8 --name-only` must match one. This catches an
   accidental edit to `extract.py` at commit time rather than three months later
   during a rebase; verified by appending a line to `extract.py` and watching the
   test name it.

---

## 4. The container producer does not require a running site

The most common objection to a `drush`-based producer is that the project may
not be running locally, or may run under DDEV, Lando, or docker compose. The
design answer is to decouple production from consumption.

### 4.1 The output is a committable artifact

```
[a site running anywhere] --drush ev--> drupal-container.json --committed-->
                                                  |
[any machine, no site needed] ------------------ merge --> graph.json
```

A site is needed by **one** participant, **once per change in module
composition** — not by everyone on every build. The artifact is stamped with the
repository commit, a `drush status` hash, and a timestamp.

This is the same discipline `docs/drupal-graphify.md` §6 already prescribes
("the repository commit and the database state are recorded alongside the
graph"). The `platform_tests` incident recorded there — a module enabled between
two batches of runs, invalidating the baseline — becomes a visible stamp
mismatch rather than a silently wrong reference.

### 4.2 Environment detection

| Marker in the repository | Command |
|---|---|
| `.ddev/config.yaml` | `ddev drush ev '…'` |
| `.lando.yml` | `lando drush ev '…'` |
| `docker-compose.yml` / `compose.yaml` | `docker compose exec <service> drush ev '…'` |
| `drush/sites/*.site.yml`, `drush.yml` | `drush @alias ev '…'` — also covers remote |
| `vendor/bin/drush` with a working `settings.php` | `vendor/bin/drush ev '…'` |
| none | static-only mode |

Overridable in `.graphifyrc`. Lagoon, Platform.sh, Pantheon and bespoke wrappers
will never fit a fixed table, and the design should not pretend otherwise.

Side benefit: `.ddev/config.yaml` as a node closes roadmap item 6 in
`docs/drupal-graphify.md` — `docroot` and `composer_root` are declared there.

### 4.3 Degradation is loud

With no runner and no artifact, the graph builds from static producers only,
`graph.audit.json` records `container: unavailable`, and the coverage
declaration says so. An agent holding a static-only graph must know it is
holding half. Silent degradation reproduces the exact failure this project
exists to prevent.

---

## 5. Phases

Each phase is a separate spec, plan and execution cycle, and each ends with a
working graph.

| # | Phase | Contents |
|---|---|---|
| **P0** | Foundation | subpackage, `register()` seam and its assertions, configurable `realm`, CI allow-list check, one extractor end to end |
| **P1** | Module-owned YAML | services, routing, permissions, libraries, links, breakpoints — 1,383 files on the reference corpus |
| **P1b** | Configuration | config entities, **config_split**, domains, profiles, recipes, config `dependencies:`, `core.extension.yml` — 629 files |
| **P2a** | Plugin discovery | plugin-type registry learned from the site's managers at the start of every `detect`, YAML-discovered plugins of learned types, `plugin_of_type` for P1's links and breakpoints, the unrecognised-family inventory and its `GRAPH_REPORT.md` section, `watch` rebuilds on Drupal YAML — see the P2a spec |
| **P2b** | Hooks and the boundary | the boundary: `realm` from composer, core/contrib/vendor trees not walked (`drupal.include` opt-in), boundary nodes with registry facts; the hook registry from every `*.api.php`; `drupal_hook`, `drupal_hook_impl`, `declares_hook`, `implements_hook`, `hook_implemented_by`, `invokes_hook` (incl. `alter_hook`); `.module`/`.install`/`.theme`/`.profile`/`<ext>.<group>.inc` become PHP; P2a's two carried fixes — see the P2b spec |
| **P3** | Container producer | runner detection, `drush ev`, the artifact, merge as a distinct step with a divergence log |
| **P4** | PHP semantics | annotations and attributes, plugin instances against learned types, forms (and `form_FORM_ID_alter` binding), events, entity-type handlers (and `ENTITY_TYPE_*` hooks), `\Drupal::service()` |
| **P5** | Presentation | theme hooks, templates, preprocess, override chain, SDC, library attachment |
| **P6** | Heavy configuration | fields and bundles, `references_bundle`, displays, blocks, Views (trimmed), Layout Builder defaults, migrations |
| **P7** | Operations | watcher by file type, frozen artifact and `graph.audit.json`, generated `realm`/`layer` slices |

### 5.1 Why this order

- **P0 first and small.** It tests the riskiest assumption cheaply: whether a
  runtime-registered extractor survives the whole pipeline, `cache.py` included.
  If it does not, approach B is in question, and that should surface in week one
  on 200 lines rather than on 5,000.
- **P1 split in two.** Measured on the reference corpus, module-owned YAML and
  configuration are 1,383 and 629 files with different scanning models: the first
  is discovered by filename family and names its owner by prefix, the second by
  directory, names its owner inside the file, and moves part of itself outside
  `config/sync` via `config_split`. See the P1 spec §2.
- **P2 before P4.** Classification of custom YAML depends on the learned
  registry, so the registry is built at the start of every `detect`, before
  any file is classified (vocabulary §5.3). P2 is split: P2a (plugins) needs
  only YAML and manager classes; P2b (hooks) reads `*.api.php` and invocation
  sites.
- **The `watch` YAML gap moved from P7 to P2a.** `.yml` is not in core's
  `_CODE_EXTENSIONS`, so a YAML-only batch never rebuilt the graph, only set
  the LLM `needs_update` flag — a P1/P1b gap. Once a changed services file can
  change the plugin registry it could no longer wait for P7; P2a wraps
  `watch._batch_triggers_rebuild` and `watch._has_non_code` (P2a spec §5.6).
- **Procedural and `#[Hook]` implementations moved from P4 to P2b.** Hook
  implementations are literal names checked against a registry, not PHP
  semantics: `<ext>_<hook>()` needs only the extension list and the hook list
  (vocabulary §5.5), and `#[Hook('x')]` is an attribute read. What stays in
  P4–P6 is the binding of variable-segment hooks, which needs the form,
  theme-hook and entity inventories; until then those are inventory
  candidates. Making `.module` and its family PHP (core treats them as
  unknown files) is part of the same move.
- **The boundary model (P2b).** Measured with `--no-gitignore`, core and
  contrib are about 99 % of a site's nodes (286,749 against 4,563) and 10 min
  of extraction. The graph is now the site's own code plus a boundary: the
  core/contrib/vendor things it references, as typed stub nodes carrying
  registry facts, never their internals. Realm comes from `composer.lock`
  first; the registries still read the boundary, so knowledge of core is not
  lost, only its bulk.
- **P3 before P4.** The container is the largest measured gain — 4 of 5
  `shortest_path` misses in the benchmark's G02 — and it is independent of the
  PHP work, because container edges reference service ids and PHP class nodes
  that stock extraction already produces. It only requires that ids agree, which
  P1 settles.
- **P5 after P4.** Theme-hook chains need hook binding, which needs the
  inventories.
- **P6 last of the producers.** Highest risk of node-count inflation; run it when
  the vocabulary has stopped moving.

### 5.2 Effect on the original roadmap

`docs/drupal-graphify.md` §7 changes as follows:

| Item | Change |
|---|---|
| 1 — container producer | unchanged, still first among producers (P3) |
| 4 — `git_excluded_by` node | mostly moot: `--no-gitignore` already exists in the CLI and is persisted in the build config |
| 8 — collapsing parallel edges | not a merge-time fix; `contains_file` is simply not emitted (vocabulary §1.4) |
| 9 — `source_file` for external nodes | not a synthetic path; tag them `external: true` with `file_type: "concept"`, which is what `build.py` already does |
| 11 — replace the AST half with `php-parser` | the stated justification (graphify's gitignore blindness) no longer holds. The real justification is different and narrower: annotations are PHP comments, invisible to tree-sitter |
