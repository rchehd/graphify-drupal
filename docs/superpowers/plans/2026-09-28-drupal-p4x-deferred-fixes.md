# P4x Deferred Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close what P3 and P4 deferred before P5 starts:
- the graph gaps: `AutowireTrait`, class-string service ids, `post_update.php`, and unreadable container sources;
- portability and cosmetics;
- the `php_classes.py` split and test-helper dedup;
- one approved live collector run.

**Architecture:** No new subsystems. Changes go into the existing P4 modules (`php_services.py`, `php_semantics.py`, `hooks.py`, `discovery.py`, `container.py`, `inventory.py`, `register.py`). The split in C1 is moves and re-exports only.

**Tech Stack:** Python 3, tree-sitter PHP, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-28-drupal-p4x-deferred-fixes-design.md`

## Global Constraints

- graphify core (anything outside `graphify/drupal/`) is never edited. Drupal behaviour enters only through wrappers in `graphify/drupal/register.py`, and every wrapped or mutated core symbol is asserted: a missing one raises `DrupalSeamError`.
- Commits touch only `graphify/drupal/`, `tests/test_drupal_*.py` and `docs/superpowers/`.
  - Messages are conventional (`feat(drupal): …`, `fix(drupal): …`, `refactor(drupal): …`, `test(drupal): …`, `docs(drupal): …`) and end exactly with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Never stage `docs/superpowers/plans/2026-09-23-drupal-p2a-plugin-discovery.md`, which is the user's uncommitted edit.
- Tests live in flat `tests/test_drupal_*.py` files. Synthetic sites are built in `tmp_path`. The corpus is `/home/user/Projects/FormsRemote`, and its tests skip when it is absent. Container tests read `DRUPAL_CONTAINER_ARTIFACT`. Tests restore process state (`_isolated_discovery_state`).
- Every staleness-relevant change also runs through the four-mode harness in `tests/test_drupal_p4_incremental.py` (extract, watch, watch-nc, full), with the equality-with-full check.
- Never write into FormsRemote. Never run drush/ddev/docker, except Task 4's single live run after the user confirms in chat.
- No guessing: an edge only for a literal or a proven type. Everything else is an inventory candidate. No `calls` into the boundary, and no `pending` key in graph.json.
- `prepare_run(FormsRemote)` stays under 5 s. Run one pytest process at a time.
- Commands: `uv run --frozen pytest …` and `uv run --frozen ruff check graphify/drupal tests/test_drupal_*.py`. Four `test_ollama_retry_cap` failures are pre-existing.
- P3 artifact: `/tmp/claude-1000/-home-user-Projects-graphify-drupal/e842a215-9b3c-4a15-b423-e0e6f858b0e0/scratchpad/p3/drupal-container.json`.

---

### Task 1: Graph gaps (spec §2: A1–A4)

**Files:** Modify `graphify/drupal/php_classes.py`, `php_services.py`, `php_semantics.py`, `hooks.py`, `discovery.py`, `container.py`, `inventory.py`. Test `tests/test_drupal_p4x_gaps.py`, and extend `tests/test_drupal_p4_incremental.py`.

Behaviour:
- **A1, rule 3c.** It sits after 3b and before 4.
  - Record `ClassFacts.traits`: resolved FQCNs of `use X;` inside the class body.
  - Record each constructor parameter's `#[Autowire(service: 'x')]`, or its positional first string. Add it as `CtorParam.autowire`.
  - A class whose own traits, or whose in-graph ancestors' traits, include `Drupal\Core\DependencyInjection\AutowireTrait` or `Drush\Commands\AutowireTrait` resolves each parameter as follows. First, the `#[Autowire]` service. Otherwise, the declared type, when the registry knows it as a service id or alias.
  - The result feeds the `injected` carrier and the `calls` binding.
  - `staleness`: a change in `traits` or `autowire` facts forces the class and its in-graph subclasses. This goes through `_class_facts_files`.
  - Amend P4 spec §7.3 with rule 3c.
- **A2.** `X::class` as the id of `$container->get(...)`, `\Drupal::service(...)`, or an argument in `create()`/`create_props`: resolve it to the FQCN, then treat that as a literal id only when the registry knows it. Otherwise it becomes `non_literal_service`.
- **A3.** `<ext>.post_update.php` beside `<ext>.info.yml` is composed for P4 semantics.
  - It is classified as code through the existing procedural gate, and gets no hook emission.
  - `<ext>_post_update_*` functions are never hooks.
  - Check how core classifies `.php` files, since this one already ends in `.php`. Find why it was not read before, and fix exactly that.
- **A4.** In `container._sources_sha`/`_host_sources`, record unreadable files as `stamp.unreadable` (relative paths). The report's Container block lists them.

- [ ] Step 1: failing tests.
  - A controller with `use AutowireTrait;` (core), whose constructor parameters are `EntityTypeManagerInterface $etm` (known alias) and `#[Autowire(service: 'foo.helper')] $helper`. `$this->helper->run()` gives `calls`. `$this->etm->getStorage()` gives `methods` on the injected carrier.
  - A Drush command class with Drush's trait gives the same result.
  - A class without the trait stays unresolved. A subclass inherits the trait.
  - `$container->get(FooHelper::class)`, where the FQCN is a service id, resolves. An unknown FQCN becomes a candidate.
  - A `foo.post_update.php` calling `\Drupal::service('foo.helper')->run()` gives `uses_service` and `calls`. Its functions are not hooks.
  - An unreadable source file (chmod 000 in tmp) appears in `stamp.unreadable`.
  - A four-mode staleness test: adding the trait to an unchanged caller's class moves its facts.
  - Corpus: `unresolved_receiver` falls from 24 to ≤ 12; report the exact number and the rest.
- [ ] Steps 2–5: run the tests and confirm they fail, implement, run focused tests, then `-k drupal` and ruff. Commit `feat(drupal): autowired create(), class-string service ids, post_update files, unreadable container sources`.

---

### Task 2: Portability and cosmetics (spec §3: B1–B3)

**Files:** `graphify/drupal/inventory.py`/`discovery.py` (B1), `register.py` (B2), and the files named in B3. Test: extend `tests/test_drupal_inventory.py`; B2 gets a CLI test in `tests/test_drupal_p4_incremental.py`.

- B1: `managers_unresolved[].file` is relative to the scan root. Test it.
- B2:
  - Find where core computes `unchanged_total` and the re-extracted count for the incremental summary (`cli.py` ~3521/~4627).
  - If a clean asserted seam exists (for example wrapping the function that returns the unchanged list, and removing the pulled files from it), use it, and test that the summary line adds up.
  - If not, document it in P4 spec §15.4 and leave the behaviour. Report which you did.
- B3: each style nit from spec §3. `_plugin` returning on a duplicate id needs a test: two classes in one file with the same type and id give one `plugin_implemented_by`.

- [ ] Steps 1–5 (RED where testable), then commit `fix(drupal): relative inventory paths, the incremental summary, small tidy-ups`.

---

### Task 3: Structure (spec §4: C1–C3)

**Files:**
- Create `graphify/drupal/php_parse.py`, `php_annotations.py`, `php_facts.py`, `php_calls.py`.
- Modify `graphify/drupal/php_classes.py` (keep the manager reader and re-exports).
- Create `tests/test_drupal_timing.py`.
- Modify the three tests that duplicate the best-of-3 loop (`tests/test_drupal_corpus.py`, `tests/test_drupal_hooks_registry.py`, `tests/test_drupal_p4_registry.py`) and `tests/test_drupal_p4_incremental.py` (C3).

- C1: move code only.
  - No renames of public names, and no logic edits.
  - `php_classes.py` re-exports every public name it has today. Test it: every name in the pre-split `php_classes.__all__`, or every public attribute if there is no `__all__`, still imports from `graphify.drupal.php_classes`.
  - Update internal imports in `graphify/drupal/` to the new modules where that is cleaner. The fingerprint (`drupal_fingerprint`) changes, which is expected: it forces one re-extraction.
- C2: one helper `best_prepare_run(root, runs=3) -> float` in `tests/test_drupal_timing.py`, imported by the three tests.
- C3: the unresolved-event test goes through the four-mode harness.

- [ ] Step 1: record the pre-split public names. Step 2: split, then run the full `-k drupal` suite unchanged (must be green) and ruff. Step 3: C2/C3. Step 4: commit `refactor(drupal): split php_classes by responsibility` (C1 alone), then `test(drupal): shared timing helper; the event case in every mode` (C2/C3).

---

### Task 4: The live run, the real run, docs (spec §5, §6)

**Files:** Modify `docs/superpowers/specs/2026-09-26-drupal-p4-php-semantics-design.md` (add §16) and `docs/superpowers/specs/2026-09-25-drupal-p3-container-design.md` (§15 note). Possibly modify `tests/test_drupal_corpus.py`.

- [ ] Step 1: **the controller asks the user in chat before this step**, and runs it only on a yes. Run `uv run --frozen graphify drupal container /home/user/Projects/FormsRemote --out <scratch>/p4x/drupal-container.json` once, timed. Compare it per source with the P3 artifact: subscribers' `file` must now be the class's own file.
  - If it is identical apart from that, point `DRUPAL_CONTAINER_ARTIFACT` at the new file for the rest of the task.
  - On any failure, do not retry. Report back.
- [ ] Step 2: the closing real run, with no further drush.
  - Run `GRAPHIFY_DRUPAL_CONTAINER=<new or P3 artifact> uv run --frozen graphify extract /home/user/Projects/FormsRemote --code-only --out <scratch>/p4x/run` twice, then once without the artifact into `<scratch>/p4x/static`.
  - Compare every P4 §15 count and explain each change (A1 is expected to raise `injected`/`calls` and lower `unresolved_receiver`).
  - Measure `prepare_run`.
  - Record FormsRemote's `git status --porcelain` and its `graphify-out/` mtimes before and after; they must be unchanged.
- [ ] Step 3: write P4 spec §16 "After the deferred fixes" with the numbers, and add a P3 §15 note on the confirmed collector line. Update corpus-test constants only where a change is explained.
- [ ] Step 4: the full suite once, `-k drupal` with the artifact, ruff, and `graphify update .`. Commit `docs(drupal): P4x measurements and the confirmed collector line`.
