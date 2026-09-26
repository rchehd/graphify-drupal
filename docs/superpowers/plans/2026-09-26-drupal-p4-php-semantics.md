# P4 PHP Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The graph learns what the site's own PHP says. Plugins and entity types come from attributes and annotations, and forms and their alters are bound. `hook_ENTITY_TYPE_*` implementations are bound. `uses_service` covers every way custom code reaches a service, with `calls` resolved into the service class through direct receivers, locals and injected properties. `subscribes_to_event` comes from `getSubscribedEvents()`. P3's deferred minors land too.

**Architecture:**
- A per-file extractor, `graphify/drupal/php_semantics.py`, is composed onto core's PHP handler. It emits nodes, `uses_service`, and `pending` raw facts.
- The registry (`discovery.py`) learns several maps: the `\Drupal::` shortcut map, constructor/`create()` facts for custom classes, and boundary entity types, forms and event constants.
- The existing `drupal` resolver turns pending facts into `calls`, `alters_form` and `hooks_entity_type`, or into inventory candidates.
- The P3 overlay confirms service classes.

**Tech Stack:** Python 3, tree-sitter PHP via `graphify/drupal/php_classes.py` (extend it, never duplicate it), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-26-drupal-p4-php-semantics-design.md`. Read the sections each task names.

## Global Constraints

- graphify core (anything outside `graphify/drupal/`) is never edited. Drupal behaviour enters only through wrappers in `graphify/drupal/register.py`. Every wrapped or mutated core symbol is asserted, and a missing one raises `DrupalSeamError`.
- Commits touch only `graphify/drupal/`, `tests/test_drupal_*.py` and `docs/superpowers/`.
  - Messages are conventional: `feat(drupal): …`, `fix(drupal): …`, `test(drupal): …`, `docs(drupal): …`.
  - Every message ends exactly with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Never stage `docs/superpowers/plans/2026-09-23-drupal-p2a-plugin-discovery.md` (the user's uncommitted edit).
- Tests:
  - They live in flat `tests/test_drupal_*.py` files. Synthetic sites are built in `tmp_path` inside the test, with no fixture directories.
  - The reference corpus is `/home/user/Projects/FormsRemote` (`DRUPAL_CORPUS`). Corpus tests skip when it is absent.
  - Container-dependent corpus tests read `DRUPAL_CONTAINER_ARTIFACT` and skip without it.
  - Tests restore process state and env (`_isolated_discovery_state` in `tests/test_drupal_discovery_seam.py`).
- Never write into FormsRemote and never run drush/ddev/docker. The P3 artifact is at `/tmp/claude-1000/-home-user-Projects-graphify-drupal/e842a215-9b3c-4a15-b423-e0e6f858b0e0/scratchpad/p3/drupal-container.json`.
- Ids:
  - `make_id("drupal", "form", form_id)`;
  - entity form `make_id("drupal", "form", "entity", entity_type, op)`;
  - `make_id("drupal", "entity_type", id)`;
  - `make_id("drupal", "event", name)`;
  - the existing helpers: `yaml_common.service_id`, `plugin_id`, `permission_id`; `discovery.type_id`; `hooks.hook_id`, `hook_impl_id`; `yaml_extract.extension_id`.
- Nodes and edges are built through `yaml_common.node` / `edge`.
- Relations come only from the vocabulary, plus the two P4 adds (`form_implemented_by`, `hooks_entity_type`, spec §4.1). There is one relation per ordered node pair.
- No guessing:
  - an edge only for a literal or a proven type (spec §7.3/§7.4);
  - everything else is an inventory candidate of the spec §10 kinds;
  - there is never a `calls` edge into the boundary;
  - no `pending` edge ever reaches graph.json.
- Values never reach the graph: no labels, no settings, no evaluated expressions (argument source text at most).
- Nothing in the registry, the extractors or the resolver raises on bad input.
- `prepare_run(FormsRemote)` stays under 5 s. Measure it in every task that widens the registry walk.
- Bind PHP class and method nodes by `source_file` plus short name (`label` `Name` / `.method()`), as P2b/P3 do.
- Measured numbers that differ from the plan are investigated and reported, never forced.
- Commands:
  - `uv run --frozen pytest …` and `uv run --frozen ruff check graphify/drupal tests/test_drupal_*.py`;
  - the `rtk` hook reformats `ls`/`grep`/`find`/`sed` output, so use `rtk proxy <cmd>` or python for exact results;
  - four `tests/test_ollama_retry_cap.py` failures (missing `openai`) are pre-existing.

## File Structure

| File | Responsibility |
|---|---|
| `graphify/drupal/php_classes.py` | extended readers: docblock annotations (§5.1 subset), `getFormId`/`getBaseFormId` literal returns, `getSubscribedEvents` array, constructor params/promotion/assignments, `create()` `new static(…)` args, class constants, `\Drupal::`/`$container->get` call sites with receivers |
| `graphify/drupal/php_semantics.py` (new) | per-file extraction: plugin / entity type / form nodes, `uses_service`, pending `calls`, `subscribes_to_event`, candidates |
| `graphify/drupal/discovery.py` | registry: `shortcuts`, `class_facts` (custom constructors/`create()`), boundary `entity_types`, `forms`, `event_constants`; `affected_files` §9 |
| `graphify/drupal/resolvers.py` | bind pending facts; `alters_form`, `hooks_entity_type`; boundary stubs for entity types / forms / events |
| `graphify/drupal/inventory.py` | new candidate kinds and counts |
| `graphify/drupal/register.py` | compose `php_semantics` onto core's PHP handler |
| `graphify/drupal/container_overlay.py`, `divergence.py`, `yaml_links.py` | P3 minors (Task 7) |

---

### Task 1: The registry learns the P4 maps

**Files:** Modify `graphify/drupal/php_classes.py` and `graphify/drupal/discovery.py`. Test `tests/test_drupal_p4_registry.py`.

**Interfaces, produces:**
```python
# php_classes.py
@dataclass(frozen=True)
class Annotation:
    name: str                 # resolved FQCN of @Name via `use`
    line: int
    values: dict              # top-level id/deriver/handlers/... : str | {"class": fqcn} | nested dict; others omitted
def read_class_annotations(path: Path) -> list[tuple[str, Annotation]]: ...   # (class FQCN, annotation) for docblocks directly above a class
@dataclass(frozen=True)
class CtorParam:
    name: str; type: str; promoted: bool     # type = resolved FQCN or "" ; promoted = has visibility modifier
@dataclass(frozen=True)
class ClassFacts:
    fqcn: str; file: str; extends: str
    params: tuple[CtorParam, ...]
    assigns: dict[str, str]                  # property -> parameter name (`$this->p = $param;` in __construct)
    parent_args: tuple[str, ...]             # parameter names passed positionally to parent::__construct ("" when not a bare param)
    create_args: tuple[str, ...]             # service id per `new static|self|<C>(…)` position in create(); "" when not $container->get('<literal>')
    form_id: str; base_form_id: str          # literal returns, else ""
    constants: dict[str, str]                # const NAME = '<literal>'
def read_class_facts(path: Path) -> list[ClassFacts]: ...
def read_drupal_shortcuts(path: Path) -> dict[str, str]: ...   # core/lib/Drupal.php: method -> service id, spec §7.1 exact-return rule
# discovery.Registry gains (to_json/from_json round-trip):
#   shortcuts: dict[str, str]
#   class_facts: dict[str, ClassFacts-as-dict]      # custom classes only, by FQCN
#   entity_types: dict[str, tuple[str, str]]        # id -> (provider, class FQCN), custom + boundary
#   forms: dict[str, tuple[str, str]]               # form_id -> (provider, class FQCN), boundary + custom; base ids included
#   event_constants: dict[str, str]                 # "Fqcn::NAME" -> event name, classes named *Events
```

Behaviour (spec §5.1, §5.4, §6.1, §7.1, §7.2, §8):
- Scope of each map:
  - shortcuts come from `<web_root>/core/lib/Drupal.php` only;
  - `class_facts` covers custom extensions only;
  - `entity_types`, `forms` and `event_constants` cover every tree the registry already walks, boundary included.
- Walk cost:
  - to keep the walk cheap, read PHP only where a cheap text precheck matches (`getFormId`, `EntityType`, `class .*Events`, `__construct`/`create(`);
  - measure `prepare_run(FormsRemote)` before and after;
  - it must stay < 5 s. If it does not, report with a profile and do not ship slower.
- `affected_files`: a changed `class_facts[C]` forces `C`'s file (already in the batch) and in-graph subclasses. The service-user part is Task 6.
- The annotation reader handles Doctrine `@Name(key = value, …)`:
  - values may be strings, `Class::class`-free FQCN strings, `@Translation(...)` (skipped), nested `{…}` maps and `{…}` lists;
  - any parse trouble means that annotation is skipped;
  - the reader never raises.

- [ ] Step 1: failing tests.
  - `read_drupal_shortcuts` on a written `core/lib/Drupal.php` fixture with `entityTypeManager` → `entity_type.manager`, `request()` returning `…->get('request_stack')->getCurrentRequest()` (absent), and `service($id)` (absent).
  - An annotation fixture (`@WebformHandler(id = "x", label = @Translation("X"), …)`) gives `values == {"id": "x"}`. A `@ContentEntityType` with `handlers = {"storage" = "Drupal\foo\FooStorage", "form" = {"edit" = "…"}}` gives the nested maps.
  - `ClassFacts`:
    - a promoted constructor;
    - an assignment constructor;
    - `parent::__construct($a, $b)`;
    - `create()` with `new static($container->get('a'), $x, $container->get('b'))` → `("a", "", "b")`;
    - literal and non-literal `getFormId`;
    - class constants.
  - A registry over a synthetic site holds boundary entity types, forms and `*Events` constants from contrib, plus custom `class_facts`, and round-trips through JSON.
  - A corpus measurement: shortcut count (expect ~35–40), entity types, forms, `prepare_run` time.
- [ ] Step 2: run the tests and see them fail. Step 3: implement. Step 4: new tests, `-k drupal`, ruff. Step 5: commit `feat(drupal): the registry learns shortcuts, constructors, entity types, forms and event names`.

---

### Task 2: Plugins and entity types from PHP

**Files:** Create `graphify/drupal/php_semantics.py`. Modify `graphify/drupal/register.py` (compose it) and `graphify/drupal/inventory.py` (candidates). Test `tests/test_drupal_php_plugins.py`.

**Interfaces:**
- Consumes: Task 1's readers and registry maps; `Registry.types` (`PluginType.subdir`, `attribute_class`, `annotation_class`).
- Produces:
```python
def extract_php_semantics(path: Path, core_result: dict) -> dict: ...   # {"nodes", "edges", "php_candidates": [...]}; this task: plugins + entity types
```

Behaviour (spec §5):
- Composition:
  - `register.py` composes `extract_php_semantics` onto core's PHP handler for every in-graph `.php` under a custom extension's `src/`, the same way `hooks` is composed (`merge.compose_handlers`);
  - it is gated so non-Drupal runs pay nothing;
  - `php_candidates` feed the inventory like `hook_candidates`.
- Plugins:
  - `drupal_plugin` with `plugin_type`, `class_name`, `provider`, and `deriver` when present;
  - edges `provides_plugin`, `plugin_of_type`, `plugin_implemented_by` (to the class node in `core_result`) and `derives_plugins` (a pending edge with `target_name` = deriver FQCN, bound by the resolver to a class node anywhere in the graph, else dropped);
  - links-family types are skipped, because they stay P1's node.
- Entity types:
  - `drupal_entity_type` per spec §5.3;
  - `entity_handler` edges are pending (handler FQCN → class node), bound by the resolver;
  - unbound handlers stay in the `handlers` attr;
  - `requires_permission` for a literal `admin_permission`.
- `unknown_plugin_type` candidates.
- The P3 overlay's existing plugin edges now meet static ones: confirm that `confirmed_by` appears on a fixture with an artifact.

- [ ] Step 1: failing tests on a synthetic site.
  - It has a learned `Block` type (attribute and annotation classes), a custom `src/Plugin/Block/FooBlock.php` with `#[Block(id: "foo_block", deriver: FooDeriver::class)]`, and `BarBlock.php` with `@Block(id = "bar_block")`. Both give the same node shape. `derives_plugins` binds to `FooDeriver`'s class node.
  - A `#[ContentEntityType(id: "foo", handlers: [...], admin_permission: "administer foo")]` gives `drupal_entity_type` and bound handler edges, plus `requires_permission`.
  - An annotation `@ConfigEntityType` gives the same shape.
  - An attribute of an unknown type in `src/Plugin/X/` gives the candidate.
  - With an artifact containing `foo_block`, the edge is `confirmed_by: container`.
  - Corpus: plugins (~47 expected) and entity types (8) by type.
- [ ] Steps 2–5, then commit `feat(drupal): plugins and entity types from attributes and annotations`.

---

### Task 3: Forms

**Files:** Modify `graphify/drupal/php_semantics.py` and `graphify/drupal/resolvers.py` (re-point `routes_to_form`). Check P1's routing producer, `yaml_routing` or wherever `_form` is handled, and change it only through the resolver. Test `tests/test_drupal_php_forms.py`.

Behaviour (spec §6.1):
- A class with a literal `getFormId()` gives `drupal_form` (`form_id`, `class_name`, `base_form_id`) and `form_implemented_by`.
- Entity forms from Task 2's `form.<op>` handlers give a node with `entity_form: true`, `pattern`, and `form_implemented_by` (pending, bound to the handler class).
- A route with `_form: Fqcn` targets the form node whose `class_name` is that FQCN. This happens in the resolver, because the route and the form live in different files. The class-node target is used only when no form node exists.
  - P3's overlay also maps `_form` to the class node. Update it so it confirms the form node when one exists. Its `conflict` handling must not report a static/container disagreement for this re-pointing: add a test.

- [ ] Step 1: failing tests.
  - Three forms: plain, with a base form id, and non-literal (no node, no candidate: a form with a computed id is legitimate).
  - A route `_form` pointing at each.
  - An entity type with `form.edit`.
  - The P3 overlay run on the fixture shows no false conflict.
  - Corpus: form count (~34).
- [ ] Steps 2–5, then commit `feat(drupal): forms and routes bound to them`.

---

### Task 4: Services — `uses_service` and `calls`

**Files:** Modify `graphify/drupal/php_classes.py` (call-site reader with receivers and locals), `graphify/drupal/php_semantics.py` and `graphify/drupal/resolvers.py`. Test `tests/test_drupal_php_services.py`.

**Interfaces, produces:** pending edges `{"relation": "calls", "pending": "service_call", "service": id, "method": m}` from the enclosing method/function node, and `{"pending": "property_call", "class": fqcn, "property": p, "method": m}`. The resolver binds or drops them (spec §4: no pending edge reaches graph.json).

Behaviour (spec §7):
- `uses_service` for the three forms (§7.1 table), with `via`. A non-literal id gives a `non_literal_service` candidate.
- Receivers (§7.4):
  - `\Drupal::service('x')->m()`;
  - `\Drupal::<shortcut>()->m()`;
  - `$this->p->m()`;
  - a local assigned once from one of those.
- Property resolution (§7.3) uses the four rules in order:
  - rule 2/3 read `*.services.yml` facts: `Registry.services` gives the class, `arguments:` needs the registry to keep each custom service's argument list;
  - add that to the registry in this task if Task 1 did not, and measure the time.
- Service class: the registry's `services[id][0]`. The P3 overlay, when it runs, may add or confirm a class. Make sure the resolver's `calls` binding also runs from the overlay when the container supplies a class the static map lacked:
  - the simplest is for the resolver to record unbound service calls as pending facts on the stub (`_pending_calls`);
  - then the overlay binds them when it has `service_implemented_by`.
  - Pick a design, keep it undoable (P3 invariants), and describe it in the report.
- `calls` goes only to an in-graph class's method node (walk `extends` inside the graph). Otherwise `methods: [...]` goes on the `uses_service` edge.
- `unresolved_receiver` candidates only per §7.3's last paragraph.

- [ ] Step 1: failing tests on a synthetic site with:
  - a custom service `foo.helper` (class `FooHelper::run()`);
  - a core stub `entity_type.manager`;
  - a controller using `\Drupal::service('foo.helper')->run()`, `\Drupal::entityTypeManager()->getStorage('node')`, and `$h = \Drupal::service('foo.helper'); $h->run();`;
  - a form whose `create()` injects `foo.helper` into a promoted `$helper` used as `$this->helper->run()`;
  - a service class with `arguments: ['@foo.helper']` assigned to `$this->h`;
  - an autowired service typed `FooHelperInterface` aliased to `foo.helper`;
  - a subclass passing through `parent::__construct`;
  - a typed-but-unresolvable property (candidate);
  - a value-object property (no candidate).

  Assert exact `uses_service` (with `via`), `calls` to `FooHelper::run`'s method node, no `calls` into the boundary, `methods` attr for `getStorage`, and no `pending` key in graph.json after a real CLI run. Corpus: `uses_service` by `via`, `calls` count.
- [ ] Steps 2–5, then commit `feat(drupal): service use and calls resolved through receivers and injected properties`.

---

### Task 5: Variable hooks and events

**Files:** Modify `graphify/drupal/resolvers.py`, `graphify/drupal/hooks.py` (hand variable candidates to the resolver as pending impls), and `graphify/drupal/php_semantics.py` (events). Test `tests/test_drupal_php_hook_binding.py`.

Behaviour (spec §6.2, §6.3, §8):
- `form_<x>_alter`:
  - resolved by custom `drupal_form` (form_id or base_form_id), then the registry's `forms`, then entity-form patterns;
  - a match gives the `drupal_hook_impl` node (P2b shape), `implements_hook` → `hook_id("form_FORM_ID_alter")`, `hook_implemented_by`, and `alters_form` (to the form node, or a boundary form stub);
  - no match gives `unbound_form`.
- `<t>_<op>`:
  - `op` comes from the registry's declared `ENTITY_TYPE_*` hooks;
  - `t` must be a known entity type;
  - the `foo_bar_baz` split needs both lists to agree on one split;
  - a match gives the impl node, `implements_hook` → `hook_id("ENTITY_TYPE_<op>")`, `hook_implemented_by`, and `hooks_entity_type`;
  - otherwise the P2b candidate stays.
- Both attribute (`#[Hook('node_presave')]`) and procedural forms are covered. The P2b `variable` candidates bound here leave the inventory.
- Events:
  - `getSubscribedEvents()` keys: a literal, or `X::NAME` via `event_constants`;
  - values: `'m'`, `['m', prio]`, or a list of those;
  - `subscribes_to_event` (class → `drupal_event`) with `method` and `priority`;
  - `unresolved_event` candidates;
  - the P3 overlay's subscriber edges confirm them.

- [ ] Step 1: failing tests.
  - Alters for a custom form, a base form, a boundary form (contrib fixture) and an unknown form.
  - `node_presave` (boundary entity type `node`), `foo_insert` (custom entity type), and `foo_bar_insert` where both `foo` and `foo_bar` are modules and only `foo_bar` makes an entity type (bound correctly), plus the ambiguous case (candidate).
  - A subscriber with a literal key, a constant key from a contrib `*Events` class, an unknown constant, and a priority list.
  - Corpus: the 4 alters and 6 entity-type hooks, each bound or explained.
- [ ] Steps 2–5, then commit `feat(drupal): bind form alters and entity-type hooks; events from getSubscribedEvents`.

---

### Task 6: Incremental and the report

**Files:** Modify `graphify/drupal/discovery.py` (`affected_files`) and `graphify/drupal/inventory.py`. Test `tests/test_drupal_p4_incremental.py`.

Behaviour (spec §9, §10):
- `affected_files`, on a registry change:
  - service class or `arguments:` changed → the previous graph.json's `uses_service` sources to that service (reuse `_invokes_hook_files`' pattern and generalise it rather than copying);
  - constructor or `create()` facts changed → the class file and in-graph subclasses;
  - boundary forms, entity types or event constants changed → the sources of `alters_form`, `hooks_entity_type`, `subscribes_to_event`, and files with matching candidates.
- Inventory:
  - the five new candidate kinds;
  - counts: plugins by type, entity types, forms (entity forms separately), `uses_service` by `via`, `calls` bound, `alters_form`, `hooks_entity_type`, `subscribes_to_event`;
  - GRAPH_REPORT's Drupal coverage renders them.

- [ ] Step 1: failing tests, each a real CLI run (`extract`, then `update`) on a synthetic site:
  - changing `foo.helper`'s class in `*.services.yml` re-extracts the controller, and its `calls` now target the new class;
  - changing a `create()` argument moves the property's service;
  - adding a contrib form binds a previously unbound alter;
  - an unchanged rerun re-extracts 0 files;
  - the report shows the new counts.
- [ ] Steps 2–5, then commit `feat(drupal): P4 facts follow registry changes; report counts`.

---

### Task 7: P3's deferred minors

**Files:** Modify `graphify/drupal/container_overlay.py`, `graphify/drupal/divergence.py`, `graphify/drupal/register.py` and `graphify/drupal/yaml_links.py`. Test the existing `tests/test_drupal_container_*.py` (extend).

Behaviour (spec §11), each with a test:
1. `lay_on_records` skips the throw-away build when there is no artifact and no boundary stub in the records.
2. The raw `--no-cluster` overlay honours the graph's `directed`.
3. `divergence._subject_file` resolves both sides.
4. `refresh_after_prune` recounts `result.edges`.
5. One shared links-family table in `yaml_links`, imported by the overlay.
6. A realm-memo unit test counting `realm_of` calls.

- [ ] Steps 1–5 (RED → GREEN for each), then commit `fix(drupal): P3's deferred overlay minors`.

---

### Task 8: Acceptance, docs, the closing real run

**Files:** Modify `tests/test_drupal_corpus.py`, `docs/superpowers/specs/2026-09-22-drupal-graph-vocabulary.md`, `docs/superpowers/specs/2026-09-22-drupal-graphify-architecture-design.md` and `docs/superpowers/specs/2026-09-26-drupal-p4-php-semantics-design.md`.

- [ ] Step 1: corpus tests for spec §12's FormsRemote criteria. The static ones need no artifact; the container ones read `DRUPAL_CONTAINER_ARTIFACT`.
- [ ] Step 2: docs.
  - Vocabulary: `drupal_form`, `drupal_entity_type` and `drupal_event` as emitted; `form_implemented_by` and `hooks_entity_type`; `uses_service` `via` and `methods`; `calls` from resolved receivers; §4.4 static plugin edges; §5.5 binding as built; the new candidate kinds.
  - Architecture §5: the P4 row.
- [ ] Step 3: **closing real run** (spec §13), with no drush.
  - Run `GRAPHIFY_DRUPAL_CONTAINER=<P3 artifact> uv run --frozen graphify extract /home/user/Projects/FormsRemote --code-only --out <scratch>/p4` twice, then once without the variable into `<scratch>/p4-static`.
  - Record in spec §15, "Measured and the closing real run": every §12 count, candidates by kind with examples, timings (`prepare_run`, full, rerun), nodes/edges against P3's 4,946 / 10,724 (static 4,825 / 10,395), and the rerun's incremental line.
  - Record `git -C /home/user/Projects/FormsRemote status --porcelain` and the mtimes under its `graphify-out/` before and after; both must be unchanged by us.
  - Any defect gets a `fix(drupal):` commit with a test.
- [ ] Step 4: full suite once, `-k drupal` with `DRUPAL_CONTAINER_ARTIFACT`, and ruff. Step 5: commits `test(drupal): P4 acceptance on the reference corpus` and `docs(drupal): P4 vocabulary, architecture, measurements and real run`.
