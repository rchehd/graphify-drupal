# P4x — deferred fixes before P5

Status: approved in brainstorming (option 1), awaiting spec review
Date: 2026-09-28
Related: `2026-09-25-drupal-p3-container-design.md` §15;
`2026-09-26-drupal-p4-php-semantics-design.md` §7, §15

---

## 1. Goal

Close everything P3 and P4 deferred before P5 starts. There are no new
features. Section A fixes real gaps where the graph misses facts. Section B
fixes portability and cosmetics. Section C restructures code without
changing behaviour. Section D is one approved live collector run.

## 2. A — gaps in the graph

### A1. Autowired `create()` (rule 3c)

Core `Drupal\Core\DependencyInjection\AutowireTrait` (Drupal 10.2+) defines
`create()` as `static::createInstanceAutowired($container)`
(`AutowiredInstanceTrait`), and Drush has `Drush\Commands\AutowireTrait`. For
a class using either trait, each constructor parameter is resolved like this:

- a parameter attribute `#[Autowire(service: 'x')]` (or its positional first
  argument) names service `x`;
- otherwise, the parameter's declared type (a resolved FQCN) is the service id
  when the registry knows it as a service id or alias.

This becomes rule 3c in P4 spec §7.3, placed after 3b and before 4. It feeds
the same `injected` carrier and `calls` binding. The registry records trait
use (`ClassFacts.traits`, resolved FQCNs, own and inherited within the
graph) and the per-parameter `#[Autowire]` service. On FormsRemote,
`DeploymentStatusCommands` uses the Drush trait, which accounts for the 12
`unresolved_receiver` candidates.

### A2. Class-string service ids

`$container->get(Foo::class)` and `\Drupal::service(Foo::class)` use the
FQCN as the id. They resolve like a literal id when the registry knows that
FQCN as a service id or alias. Otherwise they stay a `non_literal_service`
candidate. There are 2 corpus cases.

### A3. `<ext>.post_update.php`

This file sits beside `<ext>.info.yml` and is PHP, but core's classification
never reaches it (the P2b procedural gate does not list it). It becomes a
procedural file for P4 semantics (`uses_service`, `calls`) only. Its
`<ext>_post_update_*` functions are update functions, never hooks
(vocabulary §4.5).

### A4. Unreadable container sources

When a file cannot be read, `container._sources_sha` currently skips it
silently. The stamp now records `unreadable: [relpath, …]`, and the report
shows it next to the stale reasons.

## 3. B — portability and cosmetics

- B1. `drupal-inventory.json` `managers_unresolved[].file` is written
  relative to the scan root, like every other inventory path.
- B2. The CLI incremental summary counts a file pulled in by registry
  widening once, as re-extracted, not also as unchanged. This is done through
  the seam only, without editing core. If no clean seam exists, the case is
  documented and left.
- B3. Style nits:
  - the missing space in `=[` in `resolvers.py`;
  - the unused `line` in `php_semantics._injected`;
  - a public name for `discovery._ENTITY_TYPE_*` sets;
  - `Registry.to_json` no longer normalises `class_facts` (only `from_json`
    does);
  - `_plugin` returns when `add_node` is False.

## 4. C — structure, no behaviour change

- C1. Split `graphify/drupal/php_classes.py` (~1,600 lines) by
  responsibility into:
  - `php_parse.py` (parser, `_parse`, name resolution, shared walkers);
  - `php_annotations.py` (Doctrine reader);
  - `php_facts.py` (`ClassFacts`, constructor/`create()` facts, form ids,
    constants, methods);
  - `php_calls.py` (call sites, receivers, locals).

  `php_classes.py` keeps the manager-class reader and re-exports every public
  name, so existing imports keep working. The proof is that every existing
  test passes unchanged, and the diff moves code without rewriting it.
- C2. One shared helper for the best-of-3 cold `prepare_run` timing, used by
  the three tests that duplicate it. Flat test layout: a
  `tests/test_drupal_timing.py` module holding only the helper and its own
  test, imported by the others.
- C3. `test_a_subscriber_known_only_as_an_unresolved_event_binds_once_the_constant_exists`
  runs through the four-mode harness (extract, watch, watch-nc, full).

## 5. D — the approved live run

The user asks once more, right before it. One run:
`graphify drupal container /home/user/Projects/FormsRemote --out <scratch>`.
It confirms the collector's class-file line for subscribers (P3 fix), which
has only run against a stub. It is compared with the 2026-09-25 artifact per
source. The new artifact replaces the scratch copy used by the corpus tests.

## 6. Acceptance

- A1: on synthetic sites, both traits resolve by type and by `#[Autowire]`.
  A class without a trait stays unresolved. On FormsRemote, `unresolved_receiver`
  goes from 24 to ≤ 12.
- A2, A3, A4: each has a synthetic test.
- B and C: all existing tests pass. The C1 diff is moves and re-exports only.
- The closing real run on FormsRemote: counts are compared with P4 §15, and
  every change is explained. The rerun re-extracts 2 files, `prepare_run`
  stays under 5 s, and FormsRemote is untouched.

The results go in P4 spec §16 "After the deferred fixes".
