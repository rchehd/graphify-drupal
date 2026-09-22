# P0 — Foundation: subpackage, seam, and one extractor end to end

Status: proposed, awaiting review
Date: 2026-09-22
Related: `2026-09-22-drupal-graphify-architecture-design.md`,
`2026-09-22-drupal-graph-vocabulary.md`

---

## 1. Goal

P0 is deliberately small and deliberately first. Its purpose is not Drupal
coverage — it is to **prove the architecture before anything is built on it**.

The central bet of approach B is that a Drupal extractor registered at runtime
behaves, through the entire graphify pipeline, exactly like one written into
core. That bet has never been tested. P0 tests it on roughly 200 lines, so that
if it fails, it fails in week one rather than after five phases of work.

Secondary goal: put the scaffolding in place — package, configuration, CI guard
— so later phases add producers and nothing else.

---

## 2. Scope

### In

1. `graphify/drupal/` package, registered in `pyproject.toml`.
2. `register()` with guarded, asserted mutation of core structures.
3. Two lines in `graphify/__init__.py`.
4. Configurable `realm` resolution with shipped defaults.
5. One extractor: `*.info.yml` only.
6. CI check that the diff against `upstream/v8` stays inside the allow-list.
7. Tests, including the two pipeline-survival tests in §6.

### Out

Everything else. No services, routing, permissions, libraries, plugins, hooks,
templates, container producer, or merge. `*.info.yml` is chosen because it is
the simplest Drupal YAML family that still produces a real edge
(`depends_on_module`), which makes it a genuine end-to-end test rather than a
smoke test.

---

## 3. The seam

Three distinct mechanisms, with the evidence for each.

### 3.1 Dictionary mutation — extractor dispatch

`_get_extractor()` in `extract.py` ends with `return _DISPATCH.get(suffix)`. The
lookup happens per call, so keys added after import are honoured.

### 3.2 In-place set mutation — deferred to P4

`CODE_EXTENSIONS |= {".module", ".install", ".theme", ".profile"}` is how the
PHP-adjacent Drupal extensions get picked up. `watch.py` binds
`_CODE_EXTENSIONS = CODE_EXTENSIONS` — the same set object, not a copy — so an
in-place `|=` propagates to the watcher; `_WATCHED_EXTENSIONS` is a *new* set
built with `|` at import time and must be updated explicitly.

None of this is needed in P0. `*.info.yml` already has a suffix core knows
(`.yml`); it is routed by the filename predicate in §3.3, not by a new extension.
Recorded here so the mechanism is not rediscovered in P4.

### 3.3 Function wrapping — routing `.info.yml` without capturing all YAML

This is the part that needs care, and it is the main reason P0 exists.

`.yml` is in `DOC_EXTENSIONS`, not `CODE_EXTENSIONS`, so YAML goes to the LLM
document pass. Adding `.yml` to `CODE_EXTENSIONS` wholesale would divert every
YAML file in the repository — CI configs, `docker-compose.yml`, unrelated data
— away from that pass. That is an unacceptable behaviour change for non-Drupal
files.

Core already solves exactly this problem for package manifests: `classify_file()`
promotes a `.yml` to `CODE` **by filename predicate** before the suffix lookup,
and `_get_extractor()` mirrors the predicate on the dispatch side.

P0 uses the same shape, applied by wrapping rather than editing:

| Target | Why it works |
|---|---|
| `graphify.detect.classify_file` | its two internal callers (`detect.py:280`, `detect.py:1959`) reference the module global, which resolves at call time |
| `graphify.extract._get_extractor` | called by global name from within `extract()`, same resolution |

Each wrapper delegates to the original for anything that is not a recognised
Drupal file. Non-Drupal YAML keeps its current behaviour exactly.

**Rejected alternative:** monkeypatching
`manifest_ingest.is_package_manifest_path`. It would work — `detect.py` imports
it *inside* the function, so the patch is picked up — but it makes a Drupal YAML
file claim to be a package manifest everywhere else that predicate is consulted.
Wrong semantics, and it would bind incorrectly in `extract.py`, which imports the
name at module level.

---

## 4. Components

### 4.1–4.2 `graphify/drupal/paths.py`

One module owns the filename predicates and realm resolution together, so
classification and dispatch cannot disagree — a divergence there produces a file
counted as code that yields zero nodes, which core reports only as a warning.

It exposes `is_drupal_info_yaml(path)`, `extension_machine_name(path)`,
`resolve_realm(path, rules=None)` and `load_realm_rules(root)`.

`resolve_realm` takes no project root: core invokes extractors as
`extractor(path)` with no further arguments, so every rule is written to match an
absolute POSIX path.

Realm rules are read from `.graphifyrc`, whose core reader ignores keys it does
not recognise, so `drupal.realm.*` lines coexist with graphify's own settings:

```
drupal.realm.core     = */core/modules/*, */core/themes/*, */core/profiles/*, */core/lib/*
drupal.realm.contrib  = */modules/contrib/*, */themes/contrib/*, */profiles/contrib/*
drupal.realm.custom   = */modules/custom/*, */themes/custom/*, */profiles/*/modules/*, */profiles/*/themes/*
```

Patterns are `fnmatch`, whose `*` crosses path separators, so a leading `*`
absorbs any docroot layout. First match wins in the order core → contrib → custom. An unmatched extension is
`realm: unknown` and is reported, never silently defaulted. Defaults ship with
the package; a project overrides any line.

The generality matters: docroot may be `web/`, `docroot/` or the repository root;
the custom directory may be named anything; and **install profiles contain their
own modules**, so a custom module can live under `profiles/`.

### 4.3 `graphify/drupal/yaml_extract.py`

`extract_drupal_info(path) -> {"nodes": [...], "edges": [...]}` producing, per
the vocabulary:

- one node per extension: `drupal_module` / `drupal_theme` / `drupal_profile`,
  with `file_type: "code"`, `type`, `realm`, `layer: "extension"`,
  `_origin: "static_yaml"`;
- `depends_on_module` edges from `dependencies:`, normalising the three accepted
  spellings (`drupal:node`, `views:views_ui`, bare `node`) to one id;
- `base_theme` edges from `base theme:`;
- unresolved dependency targets as `external: true`,
  `file_type: "concept"` nodes.

Node ids are built with `graphify.ids.make_id`, so the readable form
`drupal:extension:foo` in the vocabulary is stored as `drupal_extension_foo`. Emitting
the readable form raw would let `build.py`'s own normalisation rewrite it, and
the extractor and the builder would disagree about the same node.

`declares_extension` is **deferred to P1**. It needs a file node that nothing
else in P0 produces, and it exercises no mechanism that `depends_on_module` does
not already exercise. P0 is a test of the seam, not a coverage exercise.

PyYAML is required to parse the file and is **not currently a declared
dependency** of the package — it is only present transitively in the dev
environment. It is added to `[project] dependencies` in this phase, and `uv.lock`
is regenerated, because CI runs `uv run --frozen` and will fail on a stale lock.
The import stays function-local so `import graphify` remains 1 ms.

### 4.4 `graphify/drupal/register.py`

Applies §3 and asserts every structure it touches exists and has the expected
shape. A missing or renamed structure raises a named error **at import**. Silent
loss of Drupal edges after an upstream update is the failure mode this guards
against, and it is strictly worse than a crash.

### 4.5 `graphify/__init__.py` — a post-import hook, not eager registration

An earlier draft of this spec called for `register()` to run eagerly here.
Measurement rules that out:

| Import | Cost |
|---|---|
| `graphify` | **1 ms** |
| `graphify.detect` | 45 ms |
| `graphify.extract` | **809 ms** |

`graphify/__init__.py` is deliberately lazy — its `__getattr__` exists so
`graphify install` works before heavy dependencies are present. Importing
`graphify.extract` from it would turn every invocation of the CLI, including
`install`, from 1 ms into roughly 850 ms. That is an unacceptable regression and
a direct violation of the module's stated design intent.

The two lines therefore install a **post-import hook** rather than doing the
work:

```python
from graphify.drupal.register import install as _install_drupal
_install_drupal()
```

`install()` imports nothing beyond `sys` and `importlib`, so the 1 ms stays 1 ms.
It places a `MetaPathFinder` on `sys.meta_path` that wraps the loader for
`graphify.detect` and `graphify.extract` only, applying the patch immediately
after each module finishes executing — and patches either one directly if it is
already in `sys.modules` when `install()` runs.

This also satisfies §6.2 for free. A spawned worker unpickles
`graphify.extract._extract_single_file`, which imports `graphify.extract`, which
imports the parent package `graphify` first, which installs the hook before
`extract` executes.

### 4.6 CI guard

A job asserting `git diff upstream/v8 --name-only` stays within
`graphify/drupal/*`, `graphify/__init__.py`, `pyproject.toml`, `tests/test_drupal_*.py`,
`docs/*`.

---

## 5. Out-of-scope core behaviours P0 must respect

- `file_type` stays inside the core vocabulary; the Drupal taxonomy goes in
  `type` (vocabulary §1.3).
- At most one relation per ordered node pair (vocabulary §1.4).

---

## 6. Acceptance criteria

Items 1 and 2 are the point of the phase. The rest is scaffolding.

### 6.1 The cache preserves runtime-registered extractions

`cache.py` is 1,782 lines of incremental logic keyed on file hashes. Whether it
round-trips nodes produced by an extractor that core does not know about is
**unverified**.

> **Criterion.** Run extraction twice over a fixture with no changes between
> runs. The second run must serve `*.info.yml` results from cache, and the
> resulting graph must be identical to the first — same node count, same edge
> count, same ids.
>
> Then modify one `dependencies:` entry and re-run. Exactly the expected edge
> must change, and nothing else.

### 6.2 The patch survives subprocess workers

Extraction runs through `ProcessPoolExecutor`. Under the `fork` start method the
patched state is inherited; under `spawn` (Windows, and the default on some
platforms) worker processes **re-import** modules and would start unpatched.
`extract.py` already carries a comment acknowledging the Windows spawn re-import
path, so this is a known-live concern, not a hypothetical.

Calling `register()` from `graphify/__init__.py` is the mitigation: any process
that imports `graphify` at all applies the patch, including a re-importing
worker.

> **Criterion.** Force a spawn-context process pool over a fixture large enough
> to engage parallel extraction. Drupal nodes and edges must be present and
> identical to the sequential result.

### 6.3 Full pipeline

`detect → extract → build → cluster → export` completes on a fixture. In the
exported `graph.json`:

- extension nodes are present with `type`, `realm` and `layer` intact after
  `build()`;
- `file_type` is `"code"` (or `"concept"` for external), i.e. `build.py` has not
  rewritten anything;
- `depends_on_module` edges are present with the expected count.

### 6.4 Non-Drupal YAML is unaffected

A fixture containing `docker-compose.yml`, `.github/workflows/ci.yml` and a
Drupal `*.info.yml`. Only the last is classified `CODE`. The other two classify
exactly as they do on unpatched core.

### 6.5 The seam fails loudly

A test that removes or renames a core structure `register()` depends on and
asserts a named error is raised, rather than a graph that silently lacks Drupal
edges.

### 6.6 `realm` resolution

Table-driven tests over: `web/` docroot, `docroot/` docroot, repository-root
docroot, a module under `profiles/myprofile/modules/custom/`, a non-standard
custom directory name overridden via `.graphifyrc`, and an unmatched path
resolving to `unknown` **and being reported**.

### 6.7 CI guard

The allow-list job fails on a deliberate edit to `graphify/extract.py`.

---

## 7. Risks

| Risk | Signal | Mitigation |
|---|---|---|
| Cache does not round-trip unknown-extractor nodes | 6.1 fails | Approach B is in question. Fall back to A (side-car producers) — the reason P0 is 200 lines and not 5,000 |
| Spawn workers run unpatched | 6.2 fails | `register()` in `graphify/__init__.py`; if insufficient, an explicit worker initialiser |
| `_DISPATCH` is private and upstream plans to replace dispatch | import-time assertion fires after a rebase | 6.5; the failure is loud and the fix is localised to `register.py` |
| Wrapping `classify_file` misses a caller that took a local reference | Drupal files classified inconsistently | 6.3 and 6.4 cover both known call sites; the wrapper delegates by default so a miss degrades to current behaviour rather than corrupting it |

---

## 8. Definition of done

Every criterion in §6 passes; `uv run pytest -q` is green with no edits to tests
outside `tests/test_drupal_*.py`; the diff against `upstream/v8` is inside the allow-list;
and a run over a real Drupal fixture yields extension nodes with correct `realm`
values in `graph.json`.
