# Drupal P1 Module-owned YAML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the nine module-owned Drupal YAML families — services, routing, permissions, libraries, breakpoints and the four link kinds — into the graph, emitting only edges whose endpoints this phase itself declares.

**Architecture:** P0's seam stays exactly as built; only its predicate changes. A family table maps a filename suffix to an extractor, and both classification and dispatch read that one table, so a family is switched on by adding one entry. All parsing goes through a tolerant YAML loader because Drupal service files use Symfony's custom tags, which `yaml.safe_load` refuses.

**Tech Stack:** Python 3.10+, PyYAML, pytest, uv.

## Global Constraints

- Python floor `>=3.10`; ruff `line-length = 100`, `target-version = "py310"`.
- Tests run as `uv run --frozen pytest tests/ -q --tb=short`. A dependency change needs a regenerated `uv.lock`.
- Test files are flat: `tests/test_drupal_*.py`. No files under `tests/fixtures/`; build corpora in `tmp_path`.
- Allow-list is `graphify/drupal/allowlist.txt`, enforced by `tests/test_drupal_allowlist.py` and `.github/workflows/drupal-guard.yml`. P1 adds no path to it.
- Commits touching a file that exists upstream are prefixed `core:`. **P1 should need none** — every change is inside `graphify/drupal/` or `tests/test_drupal_*.py`.
- `file_type` is `"code"`, or `"concept"` with `external: true`. The Drupal taxonomy goes in `type`.
- At most one relation per ordered node pair.
- Node ids come from `graphify.ids.make_id`.
- **Never parse with `yaml.safe_load`.** Use `load_drupal_yaml` from Task 1; `safe_load` silently loses 692 services including most of `core.services.yml`.
- **`*.info.yml` is the only family that emits `drupal:extension:*` nodes.** Every other family references the owner and creates nothing; graphify splits an id declared by two files.
- **Only emit an edge when this phase declares both endpoints.** A PHP FQN or a CSS/JS path is stored as a node attribute, not as an edge.

Reference corpus for every measured criterion: `/home/user/Projects/FormsRemote`.

---

### Task 1: Tolerant loader and shared helpers

**Files:**
- Create: `graphify/drupal/yaml_common.py`
- Test: `tests/test_drupal_yaml_common.py`

**Interfaces:**
- Consumes: `resolve_realm` from `graphify/drupal/paths.py`.
- Produces:
  - `load_drupal_yaml(path: Path) -> tuple[dict | None, str | None]` — `(data, error)`; exactly one is not `None`
  - `key_lines(text: str) -> dict[str, int]` — top-level and one-level-indented keys to 1-based line numbers
  - `node(nid, label, *, type, layer, path, line=1, **extra) -> dict`
  - `edge(source, target, relation, *, path, line=1, confidence="EXTRACTED", **extra) -> dict`
  - `service_id`, `route_id`, `permission_id`, `library_id`, `tag_id`, `parameter_id`, `menu_id`, `link_id`, `breakpoint_id`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_yaml_common.py
"""The tolerant loader, the line map, and the shared node/edge shapes."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import (
    edge,
    key_lines,
    load_drupal_yaml,
    node,
    service_id,
)

TAGGED_ITERATOR = """\
services:
  cache_contexts_manager:
    class: Drupal\\Core\\Cache\\CacheContextsManager
    arguments: ['@service_container', !tagged_iterator cache.context]
"""

SERVICE_CLOSURE = """\
services:
  modeler_api.owner:
    class: Drupal\\modeler_api\\Owner
    arguments: [!service_closure '@entity_type.manager']
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_tagged_iterator_parses(tmp_path):
    """core.services.yml uses this; safe_load raises ConstructorError on it."""
    data, error = load_drupal_yaml(_write(tmp_path, "core.services.yml", TAGGED_ITERATOR))
    assert error is None
    assert "cache_contexts_manager" in data["services"]
    assert data["services"]["cache_contexts_manager"]["arguments"][1] == "cache.context"


def test_service_closure_parses(tmp_path):
    data, error = load_drupal_yaml(_write(tmp_path, "m.services.yml", SERVICE_CLOSURE))
    assert error is None
    assert data["services"]["modeler_api.owner"]["arguments"] == ["@entity_type.manager"]


def test_structural_error_is_reported_not_raised(tmp_path):
    data, error = load_drupal_yaml(_write(tmp_path, "x.libraries.yml", "a:\n b: 1\n c\n"))
    assert data is None
    assert "parse error" in error


def test_non_mapping_document_is_not_an_error(tmp_path):
    data, error = load_drupal_yaml(_write(tmp_path, "x.libraries.yml", "- one\n- two\n"))
    assert data is None and error is None


def test_oversized_file_is_refused(tmp_path):
    path = _write(tmp_path, "big.services.yml", "a: 1\n")
    path.write_text("#" * (3 * 1024 * 1024), encoding="utf-8")
    data, error = load_drupal_yaml(path)
    assert data is None and "too large" in error


def test_key_lines_separates_the_two_indents():
    text = "services:\n  alpha:\n    class: A\n  beta:\n    class: B\n"
    lines = key_lines(text)
    assert lines[0] == {"services": 1}
    assert lines[2] == {"alpha": 2, "beta": 4}


def test_key_lines_ignores_list_items_and_comments():
    text = "# note\nroutes:\n  - not_a_key\nfoo.bar:\n  path: /x\n"
    lines = key_lines(text)
    assert lines[0] == {"routes": 2, "foo.bar": 4}
    assert "not_a_key" not in lines[2]


def test_key_lines_keeps_keys_containing_spaces():
    """A Drupal permission is `administer foo`; only the colon delimits a key."""
    assert key_lines("administer foo:\n  title: X\n")[0] == {"administer foo": 1}


def test_key_lines_does_not_let_a_property_shadow_an_entity():
    """A route named `path` must not take the line of some route's `path:`."""
    text = "path:\n  path: /a\nother:\n  path: /b\n"
    assert key_lines(text)[0] == {"path": 1, "other": 3}


def test_node_carries_the_universal_attributes(tmp_path):
    path = tmp_path / "web/modules/custom/foo/foo.services.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("services: {}\n", encoding="utf-8")
    n = node(service_id("foo.bar"), "foo.bar", type="drupal_service",
             layer="di", path=path, line=7, class_name="Drupal\\foo\\Bar")
    assert n["file_type"] == "code"
    assert n["type"] == "drupal_service"
    assert n["layer"] == "di"
    assert n["realm"] == "custom"
    assert n["_origin"] == "static_yaml"
    assert n["source_file"] == str(path)
    assert n["source_location"] == "L7"
    assert n["class_name"] == "Drupal\\foo\\Bar"


def test_edge_carries_the_universal_attributes(tmp_path):
    path = tmp_path / "foo.services.yml"
    path.write_text("services: {}\n", encoding="utf-8")
    e = edge(service_id("a"), service_id("b"), "injects_service", path=path, line=3)
    assert e["confidence"] == "EXTRACTED"
    assert e["_origin"] == "static_yaml"
    assert e["source_location"] == "L3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_yaml_common.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.yaml_common'`

- [ ] **Step 3: Implement**

```python
# graphify/drupal/yaml_common.py
"""Shared parsing and node/edge construction for the Drupal YAML families.

The loader is the reason this module exists. Drupal service files use Symfony's
custom YAML tags, and `yaml.safe_load` refuses them:

    web/core/core.services.yml                    !tagged_iterator
    web/modules/contrib/modeler_api/...           !service_closure

The first is Drupal core's main service file. Measured on a real 1,140-extension
tree, `safe_load` parses 1,628 services and this loader parses 2,320 — the
difference is most of core's container. Every family parses through here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from graphify.drupal.paths import resolve_realm
from graphify.ids import make_id

#: A Drupal YAML file is configuration, not data. Anything past this is not one,
#: and parsing a corpus-supplied file without a cap invites a decompression bomb.
_MAX_YAML_BYTES = 2 * 1024 * 1024


class DrupalYamlLoader(yaml.SafeLoader):
    """SafeLoader that tolerates Symfony's custom service tags."""


def _any_tag(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> Any:
    """Return a custom-tagged node's payload instead of refusing to build it.

    Scoped to `!`-prefixed tags, so structural errors still raise and are
    reported — core ships a deliberately malformed libraries fixture that must
    keep failing.
    """
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


yaml.add_multi_constructor("!", _any_tag, Loader=DrupalYamlLoader)


def load_drupal_yaml(path: Path) -> tuple[dict | None, str | None]:
    """Parse a Drupal YAML file. Returns (mapping, None) or (None, error)."""
    try:
        if path.stat().st_size > _MAX_YAML_BYTES:
            return None, "yaml too large to index"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"yaml read error: {exc}"
    try:
        data = yaml.load(text, Loader=DrupalYamlLoader)
    except yaml.YAMLError as exc:
        return None, f"yaml parse error: {exc}"
    if not isinstance(data, dict):
        return None, None          # empty or a sequence: nothing to extract
    return data, None


def key_lines(text: str) -> dict[int, dict[str, int]]:
    """Map keys to 1-based line numbers, grouped by indent.

    Indent-scoped on purpose. Entity ids sit at indent 0 for routing,
    permissions, libraries, links and breakpoints, and at indent 2 for services
    (under `services:`) and parameters. A flat map would let a route's `path:`
    property at indent 2 overwrite the line of a route actually named `path`.

    Keys may contain spaces — a Drupal permission is `administer foo` — so only
    the colon delimits them. Last occurrence wins, as it does in the parser.
    """
    lines: dict[int, dict[str, int]] = {0: {}, 2: {}}
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.lstrip(" ")
        if not stripped or stripped.startswith(("#", "-")):
            continue
        indent = len(raw) - len(stripped)
        if indent not in lines:
            continue
        key, sep, _rest = stripped.partition(":")
        if not sep:
            continue
        key = key.strip().strip("'\"")
        if key:
            lines[indent][key] = number
    return lines


def node(
    nid: str,
    label: str,
    *,
    type: str,
    layer: str,
    path: Path,
    line: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """A node with every universal attribute filled in the same way."""
    payload: dict[str, Any] = {
        "id": nid,
        "label": label,
        "file_type": "code",
        "type": type,
        "layer": layer,
        "realm": resolve_realm(path),
        "_origin": "static_yaml",
        "source_file": str(path),
        "source_location": f"L{line}",
    }
    payload.update(extra)
    return payload


def edge(
    source: str,
    target: str,
    relation: str,
    *,
    path: Path,
    line: int = 1,
    confidence: str = "EXTRACTED",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": confidence,
        "_origin": "static_yaml",
        "source_file": str(path),
        "source_location": f"L{line}",
    }
    payload.update(extra)
    return payload


def service_id(sid: str) -> str:
    return make_id("drupal", "service", sid)


def route_id(name: str) -> str:
    return make_id("drupal", "route", name)


def permission_id(permission: str) -> str:
    return make_id("drupal", "permission", permission)


def library_id(owner: str, name: str) -> str:
    return make_id("drupal", "library", owner, name)


def tag_id(name: str) -> str:
    return make_id("drupal", "tag", name)


def parameter_id(name: str) -> str:
    return make_id("drupal", "parameter", name)


def menu_id(name: str) -> str:
    return make_id("drupal", "menu", name)


def link_id(kind: str, plugin_id: str) -> str:
    return make_id("drupal", kind, plugin_id)


def breakpoint_id(owner: str, name: str) -> str:
    return make_id("drupal", "breakpoint", owner, name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_drupal_yaml_common.py -q`
Expected: PASS, 10 tests. If `key_lines` fails a case, simplify it — the two
assertions in the test are the whole contract; do not add cases it must satisfy.

- [ ] **Step 5: Verify against the two real files that motivated the loader**

```bash
uv run python -c "
from pathlib import Path
from graphify.drupal.yaml_common import load_drupal_yaml
root = Path('/home/user/Projects/FormsRemote')
total = 0
bad = []
for p in root.glob('web/**/*.services.yml'):
    if 'node_modules' in p.parts: continue
    data, err = load_drupal_yaml(p)
    if err: bad.append((p, err)); continue
    if data: total += len(data.get('services', data) or {})
print('services:', total, 'errors:', len(bad))
"
```

Expected: `services: 2320 errors: 0`. Anything near 1,628 means the loader is not
being used.

- [ ] **Step 6: Commit**

```bash
git add graphify/drupal/yaml_common.py tests/test_drupal_yaml_common.py
git commit -m "feat(drupal): add a Symfony-tolerant YAML loader and shared node helpers"
```

---

### Task 2: Family table and seam widening

Behaviour-neutral by design: the table starts with `*.info.yml` alone, so after
this task the graph is byte-identical to P0's. Each later task switches one
family on by adding one entry.

**Files:**
- Create: `graphify/drupal/families.py`
- Modify: `graphify/drupal/register.py` (the two `is_drupal_info_yaml` imports and their uses)
- Test: `tests/test_drupal_families.py`

**Interfaces:**
- Consumes: `extract_drupal_info` from `graphify/drupal/yaml_extract.py`.
- Produces:
  - `FAMILY_EXTRACTORS: dict[str, Callable[[Path], dict]]` — suffix to handler
  - `is_drupal_yaml(path) -> bool`
  - `family_extractor(path) -> Callable | None`
  - `extension_owner(path) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_families.py
"""The family table: one source for classification and dispatch."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.drupal.families import (
    FAMILY_EXTRACTORS,
    extension_owner,
    family_extractor,
    is_drupal_yaml,
)


def test_info_family_is_registered():
    from graphify.drupal.yaml_extract import extract_drupal_info

    assert FAMILY_EXTRACTORS[".info.yml"] is extract_drupal_info


@pytest.mark.parametrize("name, owner", [
    ("foo.info.yml", "foo"),
    ("foo.services.yml", "foo"),
    ("foo.links.menu.yml", "foo"),
    ("views_ui.links.task.yml", "views_ui"),
])
def test_owner_is_the_name_before_the_longest_matching_family(name, owner):
    assert extension_owner(Path("/p") / name) == owner


def test_links_menu_is_not_read_as_a_menu_family():
    """Longest-suffix matching: `.links.menu.yml` must win over any `.menu.yml`."""
    assert extension_owner(Path("/p/foo.links.menu.yml")) == "foo"


@pytest.mark.parametrize("name", [
    "docker-compose.yml", "ci.yml", "token.yml", "credentials.yaml",
    "system.menu.main.yml", ".info.yml",
])
def test_non_family_yaml_is_not_drupal_yaml(name):
    assert is_drupal_yaml(Path("/p") / name) is False
    assert family_extractor(Path("/p") / name) is None


def test_classification_and_dispatch_cannot_disagree():
    """Every suffix is_drupal_yaml accepts must also have a handler."""
    for suffix in FAMILY_EXTRACTORS:
        probe = Path("/p/owner" + suffix)
        assert is_drupal_yaml(probe) is True
        assert family_extractor(probe) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_families.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.families'`

- [ ] **Step 3: Implement the table**

```python
# graphify/drupal/families.py
"""The Drupal YAML family table — one source for classification and dispatch.

`graphify.detect.classify_file` promotes a file to CODE and
`graphify.extract._get_extractor` hands it to a handler. If those two ever
disagree, a file is counted as code and then yields nothing, which core reports
only as a warning. Both read this table, so disagreement is not expressible.

A family is switched on by adding one entry here, together with its handler.
Until a handler exists the suffix is absent, and the file keeps whatever
behaviour core gives it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from graphify.drupal.yaml_extract import extract_drupal_info

FAMILY_EXTRACTORS: dict[str, Callable[[Path], dict]] = {
    ".info.yml": extract_drupal_info,
}

#: Longest first, so `*.links.menu.yml` is never matched as some `*.menu.yml`.
def _suffixes() -> tuple[str, ...]:
    return tuple(sorted(FAMILY_EXTRACTORS, key=len, reverse=True))


def _match(path: Path) -> str | None:
    name = path.name
    for suffix in _suffixes():
        # `len(name) > len(suffix)` rejects a bare `.info.yml`: no machine name.
        if name.endswith(suffix) and len(name) > len(suffix):
            return suffix
    return None


def is_drupal_yaml(path: Path) -> bool:
    return _match(path) is not None


def family_extractor(path: Path) -> Callable[[Path], dict] | None:
    suffix = _match(path)
    return FAMILY_EXTRACTORS[suffix] if suffix else None


def extension_owner(path: Path) -> str:
    """Machine name of the extension that owns this file, or ''."""
    suffix = _match(path)
    return path.name[: -len(suffix)] if suffix else ""
```

- [ ] **Step 4: Point the seam at the table**

In `graphify/drupal/register.py`, replace the import inside `_patch_detect`:

```python
    from graphify.drupal.families import is_drupal_yaml
```

and both uses of `is_drupal_info_yaml(path)` in that function with
`is_drupal_yaml(path)`.

In `_patch_extract`, replace its import and body:

```python
def _patch_extract(extract: ModuleType) -> None:
    from graphify.drupal.families import family_extractor
    ...
    def _dispatch(original):
        def _get_extractor(path: Path):
            handler = family_extractor(path)
            if handler is not None:
                return handler
            return original(path)
        return _get_extractor
```

The `from graphify.drupal.yaml_extract import extract_drupal_info` import in
`_patch_extract` is no longer needed; remove it.

- [ ] **Step 5: Run the Drupal suite — nothing may change yet**

Run: `uv run pytest tests/test_drupal_*.py -q`
Expected: PASS. The seam tests still assert `token.services.yml` IS dropped and
`.info.yml` is not, because no new family is registered.

- [ ] **Step 6: Commit**

```bash
git add graphify/drupal/families.py graphify/drupal/register.py tests/test_drupal_families.py
git commit -m "refactor(drupal): route the seam through a family table"
```

---

### Task 3: Services

**Files:**
- Create: `graphify/drupal/yaml_services.py`
- Modify: `graphify/drupal/families.py` (one entry)
- Test: `tests/test_drupal_services.py`

**Interfaces:**
- Consumes: `load_drupal_yaml`, `key_lines`, `node`, `edge`, `service_id`, `tag_id`, `parameter_id` (Task 1); `extension_owner` (Task 2); `extension_id` from `yaml_extract`.
- Produces: `extract_drupal_services(path) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_services.py
"""*.services.yml: the container as declared."""
from __future__ import annotations

from graphify.drupal.yaml_extract import extension_id
from graphify.drupal.yaml_common import parameter_id, service_id, tag_id
from graphify.drupal.yaml_services import extract_drupal_services

SERVICES = """\
parameters:
  foo.setting: true
services:
  foo.locator:
    class: Drupal\\foo\\Locator
    arguments: ['@database', '@?optional.thing', '%foo.setting%']
    tags:
      - {name: event_subscriber}
      - {name: access_check, applies_to: _foo_access}
  foo.decorated:
    class: Drupal\\foo\\Decorated
    decorates: foo.locator
  foo.child:
    parent: foo.base
    abstract: true
"""


def _write(tmp_path, text=SERVICES):
    path = tmp_path / "web/modules/custom/foo/foo.services.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_service_node_carries_class_as_an_attribute_not_an_edge(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    svc = next(n for n in result["nodes"] if n["id"] == service_id("foo.locator"))
    assert svc["type"] == "drupal_service"
    assert svc["layer"] == "di"
    assert svc["realm"] == "custom"
    assert svc["class_name"] == "Drupal\\foo\\Locator"
    # The PHP class is another layer's node; no edge until P4.
    assert not any(e["relation"] == "service_implemented_by" for e in result["edges"])


def test_owner_declares_every_service(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    declared = {e["target"] for e in result["edges"] if e["relation"] == "declares_service"}
    assert declared == {
        service_id("foo.locator"), service_id("foo.decorated"), service_id("foo.child")
    }
    assert all(
        e["source"] == extension_id("foo")
        for e in result["edges"] if e["relation"] == "declares_service"
    )


def test_argument_injection(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    rel = _rel(result)
    assert (service_id("foo.locator"), "injects_service", service_id("database")) in rel
    assert (service_id("foo.locator"), "injects_parameter", parameter_id("foo.setting")) in rel


def test_optional_injection_is_inferred_not_extracted(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    optional = next(
        e for e in result["edges"]
        if e["relation"] == "injects_service" and e["target"] == service_id("optional.thing")
    )
    assert optional["confidence"] == "INFERRED"


def test_tags_decoration_and_parent(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    rel = _rel(result)
    assert (service_id("foo.locator"), "tagged_as", tag_id("event_subscriber")) in rel
    assert (service_id("foo.locator"), "tagged_as", tag_id("access_check")) in rel
    assert (service_id("foo.decorated"), "decorates", service_id("foo.locator")) in rel
    assert (service_id("foo.child"), "parent_service", service_id("foo.base")) in rel


def test_parameters_are_declared_by_the_owner(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    assert any(
        e["relation"] == "declares_parameter" and e["target"] == parameter_id("foo.setting")
        for e in result["edges"]
    )


def test_no_extension_node_is_emitted(tmp_path):
    """*.info.yml is the only family that may declare one."""
    result = extract_drupal_services(_write(tmp_path))
    assert not any(n["id"] == extension_id("foo") for n in result["nodes"])


def test_line_numbers_point_at_the_service(tmp_path):
    result = extract_drupal_services(_write(tmp_path))
    svc = next(n for n in result["nodes"] if n["id"] == service_id("foo.locator"))
    assert svc["source_location"] == "L4"


def test_symfony_tagged_argument_does_not_become_a_service(tmp_path):
    text = (
        "services:\n"
        "  foo.manager:\n"
        "    class: Drupal\\foo\\Manager\n"
        "    arguments: [!tagged_iterator foo.plugin]\n"
    )
    result = extract_drupal_services(_write(tmp_path, text))
    assert not any(e["relation"] == "injects_service" for e in result["edges"])


def test_malformed_file_reports_instead_of_raising(tmp_path):
    result = extract_drupal_services(_write(tmp_path, "services:\n a\n  b\n"))
    assert result["nodes"] == [] and result["edges"] == []
    assert "parse error" in result["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_services.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.yaml_services'`

- [ ] **Step 3: Implement**

```python
# graphify/drupal/yaml_services.py
"""*.services.yml — the container as the codebase declares it.

Edges stay inside this phase: a service injects a service, carries a tag, or
extends another service, and every one of those endpoints is declared by a
`*.services.yml` somewhere in the corpus. The `class:` value is a PHP FQN, which
belongs to a layer that does not exist yet, so it is recorded as an attribute
and becomes an edge in P4.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.families import extension_owner
from graphify.drupal.yaml_common import (
    edge,
    key_lines,
    load_drupal_yaml,
    node,
    parameter_id,
    service_id,
    tag_id,
)
from graphify.drupal.yaml_extract import extension_id


def _service_reference(value: Any) -> tuple[str, bool] | None:
    """('database', required) for '@database', ('x', optional) for '@?x'."""
    if not isinstance(value, str) or not value.startswith("@"):
        return None
    ref = value[1:]
    optional = ref.startswith("?")
    ref = ref.lstrip("?")
    # '@@' is an escaped literal, and a closure reference is not an injection.
    if not ref or ref.startswith("@"):
        return None
    return ref, optional


def _parameter_reference(value: Any) -> str | None:
    if isinstance(value, str) and len(value) > 2 and value.startswith("%") and value.endswith("%"):
        return value[1:-1]
    return None


def extract_drupal_services(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    owner = extension_owner(path)
    owner_id = extension_id(owner)
    # Services and parameters are entity ids at indent 2, under their section key.
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[2]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    emitted: set[str] = set()

    def add_edge(source: str, target: str, relation: str, line: int, **extra: Any) -> None:
        if source == target or (source, target) in seen_pairs:
            return
        seen_pairs.add((source, target))
        edges.append(edge(source, target, relation, path=path, line=line, **extra))

    def add_node(nid: str, label: str, type_: str, line: int, **extra: Any) -> None:
        if nid in emitted:
            return
        emitted.add(nid)
        nodes.append(node(nid, label, type=type_, layer="di", path=path, line=line, **extra))

    for name in (data.get("parameters") or {}):
        line = lines.get(str(name), 1)
        pid = parameter_id(str(name))
        add_node(pid, str(name), "drupal_parameter", line)
        add_edge(owner_id, pid, "declares_parameter", line)

    services = data.get("services")
    if not isinstance(services, dict):
        return {"nodes": nodes, "edges": edges}

    for sid, definition in services.items():
        sid = str(sid)
        line = lines.get(sid, 1)
        own = service_id(sid)
        extra: dict[str, Any] = {}
        if isinstance(definition, dict):
            for key, attr in (("class", "class_name"), ("abstract", "abstract"),
                              ("deprecated", "deprecated"), ("autowire", "autowire")):
                if key in definition:
                    extra[attr] = definition[key]
        add_node(own, sid, "drupal_service", line, **extra)
        add_edge(owner_id, own, "declares_service", line)

        if not isinstance(definition, dict):
            continue

        for argument in definition.get("arguments") or []:
            reference = _service_reference(argument)
            if reference is not None:
                ref, optional = reference
                add_edge(own, service_id(ref), "injects_service", line,
                         confidence="INFERRED" if optional else "EXTRACTED")
                continue
            parameter = _parameter_reference(argument)
            if parameter is not None:
                add_edge(own, parameter_id(parameter), "injects_parameter", line)

        for tag in definition.get("tags") or []:
            name = tag.get("name") if isinstance(tag, dict) else tag
            if not name:
                continue
            tid = tag_id(str(name))
            add_node(tid, str(name), "drupal_service_tag", line)
            add_edge(own, tid, "tagged_as", line)

        if isinstance(definition.get("decorates"), str):
            add_edge(own, service_id(definition["decorates"]), "decorates", line)
        if isinstance(definition.get("parent"), str):
            add_edge(own, service_id(definition["parent"]), "parent_service", line)

    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 4: Register the family**

In `graphify/drupal/families.py`, add the import and the entry:

```python
from graphify.drupal.yaml_services import extract_drupal_services

FAMILY_EXTRACTORS: dict[str, Callable[[Path], dict]] = {
    ".info.yml": extract_drupal_info,
    ".services.yml": extract_drupal_services,
}
```

- [ ] **Step 5: Update the seam test — `token.services.yml` is now exempt**

In `tests/test_drupal_seam.py`, move `"token.services.yml"` out of
`test_real_secret_stores_are_still_caught` and assert the opposite in
`test_drupal_info_yaml_is_not_treated_as_a_secret_store`:

```python
    assert detect._is_sensitive(Path("/p/web/modules/contrib/token/token.services.yml")) is False
```

`token.yml`, `token.json`, `credentials.yaml`, `secrets.yml` and `api_token.txt`
stay in the still-caught list: none of them matches a family suffix.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_drupal_services.py tests/test_drupal_seam.py tests/test_drupal_families.py -q`
Expected: PASS

- [ ] **Step 7: Verify on the reference corpus**

```bash
uv run python -c "
from pathlib import Path
from graphify.drupal.yaml_services import extract_drupal_services
root = Path('/home/user/Projects/FormsRemote')
n = e = errs = 0
for p in root.glob('web/**/*.services.yml'):
    if 'node_modules' in p.parts: continue
    r = extract_drupal_services(p)
    if r.get('error'): errs += 1; continue
    n += sum(1 for x in r['nodes'] if x['type'] == 'drupal_service')
    e += len(r['edges'])
print('service nodes:', n, 'edges:', e, 'errors:', errs)
"
```

Expected: `service nodes: 2320`, `errors: 0`.

- [ ] **Step 8: Commit**

```bash
git add graphify/drupal/yaml_services.py graphify/drupal/families.py \
        tests/test_drupal_services.py tests/test_drupal_seam.py
git commit -m "feat(drupal): extract services, injections, tags and decoration"
```

---

### Task 4: Permissions and routing

Together because `requires_permission` joins them and is only verifiable with
both present.

**Files:**
- Create: `graphify/drupal/yaml_access.py`
- Modify: `graphify/drupal/families.py` (two entries)
- Test: `tests/test_drupal_access.py`

**Interfaces:**
- Consumes: Task 1 helpers, `extension_owner`, `extension_id`.
- Produces: `extract_drupal_permissions(path) -> dict`, `extract_drupal_routing(path) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_access.py
"""*.permissions.yml and *.routing.yml, and the edge that joins them."""
from __future__ import annotations

from graphify.drupal.yaml_access import extract_drupal_permissions, extract_drupal_routing
from graphify.drupal.yaml_common import permission_id, route_id
from graphify.drupal.yaml_extract import extension_id

PERMISSIONS = """\
administer foo:
  title: 'Administer foo'
  restrict access: true
view foo:
  title: 'View foo'
permission_callbacks:
  - Drupal\\foo\\Permissions::dynamic
"""

ROUTING = """\
foo.settings:
  path: '/admin/config/foo'
  defaults:
    _form: '\\Drupal\\foo\\Form\\SettingsForm'
    _title: 'Foo settings'
  requirements:
    _permission: 'administer foo+view foo'
foo.page:
  path: '/foo/{node}'
  defaults:
    _controller: '\\Drupal\\foo\\Controller\\Page::view'
  requirements:
    _custom_access: '\\Drupal\\foo\\Access::check'
"""


def _write(tmp_path, name, text):
    path = tmp_path / "web/modules/custom/foo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_permissions_are_declared_by_the_owner(tmp_path):
    result = extract_drupal_permissions(_write(tmp_path, "foo.permissions.yml", PERMISSIONS))
    ids = {n["id"] for n in result["nodes"]}
    assert permission_id("administer foo") in ids
    assert permission_id("view foo") in ids
    assert (extension_id("foo"), "declares_permission", permission_id("view foo")) in _rel(result)


def test_permission_callbacks_are_an_attribute_not_a_permission(tmp_path):
    result = extract_drupal_permissions(_write(tmp_path, "foo.permissions.yml", PERMISSIONS))
    assert permission_id("permission_callbacks") not in {n["id"] for n in result["nodes"]}
    assert any(n.get("permission_callbacks") for n in result["nodes"] if n["type"] == "drupal_permission") \
        or all("permission_callbacks" not in n for n in result["nodes"])


def test_restrict_access_is_carried(tmp_path):
    result = extract_drupal_permissions(_write(tmp_path, "foo.permissions.yml", PERMISSIONS))
    admin = next(n for n in result["nodes"] if n["id"] == permission_id("administer foo"))
    assert admin["restrict_access"] is True
    assert admin["label"] == "Administer foo"


def test_routes_are_declared_with_path_and_handler_attribute(tmp_path):
    result = extract_drupal_routing(_write(tmp_path, "foo.routing.yml", ROUTING))
    settings = next(n for n in result["nodes"] if n["id"] == route_id("foo.settings"))
    assert settings["type"] == "drupal_route"
    assert settings["layer"] == "routing"
    assert settings["path"] == "/admin/config/foo"
    assert settings["form"] == "\\Drupal\\foo\\Form\\SettingsForm"
    # The PHP handler is another layer's node; no edge until P4.
    assert not any(e["relation"] in ("routes_to", "routes_to_form") for e in result["edges"])


def test_permission_requirement_splits_on_plus_and_comma(tmp_path):
    result = extract_drupal_routing(_write(tmp_path, "foo.routing.yml", ROUTING))
    rel = _rel(result)
    assert (route_id("foo.settings"), "requires_permission", permission_id("administer foo")) in rel
    assert (route_id("foo.settings"), "requires_permission", permission_id("view foo")) in rel


def test_custom_access_is_an_attribute(tmp_path):
    result = extract_drupal_routing(_write(tmp_path, "foo.routing.yml", ROUTING))
    page = next(n for n in result["nodes"] if n["id"] == route_id("foo.page"))
    assert page["custom_access"] == "\\Drupal\\foo\\Access::check"


def test_neither_family_emits_an_extension_node(tmp_path):
    for name, text, fn in (
        ("foo.permissions.yml", PERMISSIONS, extract_drupal_permissions),
        ("foo.routing.yml", ROUTING, extract_drupal_routing),
    ):
        result = fn(_write(tmp_path, name, text))
        assert not any(n["id"] == extension_id("foo") for n in result["nodes"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_access.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.yaml_access'`

- [ ] **Step 3: Implement**

```python
# graphify/drupal/yaml_access.py
"""*.permissions.yml and *.routing.yml.

`requires_permission` is the payoff: both endpoints are declared by YAML this
phase reads, so "what does a user need to reach this page" is answerable with no
PHP at all. The `_controller` / `_form` / `_custom_access` values are PHP FQNs
and stay attributes until P4.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graphify.drupal.families import extension_owner
from graphify.drupal.yaml_common import (
    edge,
    key_lines,
    load_drupal_yaml,
    node,
    permission_id,
    route_id,
)
from graphify.drupal.yaml_extract import extension_id

#: Drupal reads `_permission` as an OR list on `,` and an AND list on `+`. The
#: distinction is about evaluation, not about which permissions are referenced,
#: so both separators produce one edge per permission.
_PERMISSION_SPLIT = re.compile(r"[+,]")


def extract_drupal_permissions(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    owner_id = extension_id(extension_owner(path))
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for name, definition in data.items():
        name = str(name)
        # A reserved key, not a permission: the callback is PHP and waits for P4.
        if name == "permission_callbacks":
            continue
        line = lines.get(name, 1)
        pid = permission_id(name)
        extra: dict[str, Any] = {}
        label = name
        if isinstance(definition, dict):
            label = str(definition.get("title") or name)
            if "restrict access" in definition:
                extra["restrict_access"] = definition["restrict access"]
        nodes.append(node(pid, label, type="drupal_permission", layer="routing",
                          path=path, line=line, **extra))
        edges.append(edge(owner_id, pid, "declares_permission", path=path, line=line))

    return {"nodes": nodes, "edges": edges}


def extract_drupal_routing(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    owner_id = extension_id(extension_owner(path))
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def add_edge(source: str, target: str, relation: str, line: int) -> None:
        if (source, target) in seen_pairs:
            return
        seen_pairs.add((source, target))
        edges.append(edge(source, target, relation, path=path, line=line))

    for name, definition in data.items():
        name = str(name)
        if not isinstance(definition, dict):
            continue
        line = lines.get(name, 1)
        rid = route_id(name)
        defaults = definition.get("defaults") or {}
        requirements = definition.get("requirements") or {}
        extra: dict[str, Any] = {"path": definition.get("path", "")}
        for key, attr in (("_controller", "controller"), ("_form", "form"),
                          ("_entity_form", "entity_form"), ("_title", "title")):
            if isinstance(defaults, dict) and key in defaults:
                extra[attr] = defaults[key]
        if isinstance(requirements, dict) and "_custom_access" in requirements:
            extra["custom_access"] = requirements["_custom_access"]

        nodes.append(node(rid, name, type="drupal_route", layer="routing",
                          path=path, line=line, **extra))
        add_edge(owner_id, rid, "declares_route", line)

        permission = requirements.get("_permission") if isinstance(requirements, dict) else None
        if isinstance(permission, str):
            for part in _PERMISSION_SPLIT.split(permission):
                part = part.strip()
                if part:
                    add_edge(rid, permission_id(part), "requires_permission", line)

    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 4: Register both families**

```python
from graphify.drupal.yaml_access import extract_drupal_permissions, extract_drupal_routing

FAMILY_EXTRACTORS = {
    ".info.yml": extract_drupal_info,
    ".services.yml": extract_drupal_services,
    ".permissions.yml": extract_drupal_permissions,
    ".routing.yml": extract_drupal_routing,
}
```

- [ ] **Step 5: Update the seam test**

`token.routing.yml` moves from the still-caught list to the exempt assertions,
alongside `token.services.yml`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_drupal_access.py tests/test_drupal_seam.py -q`
Expected: PASS

- [ ] **Step 7: Verify on the reference corpus**

```bash
uv run python -c "
from pathlib import Path
from graphify.drupal.yaml_access import extract_drupal_permissions, extract_drupal_routing
root = Path('/home/user/Projects/FormsRemote')
for glob, fn, label in (('web/**/*.routing.yml', extract_drupal_routing, 'routes'),
                        ('web/**/*.permissions.yml', extract_drupal_permissions, 'permissions')):
    n = errs = 0
    for p in root.glob(glob):
        if 'node_modules' in p.parts: continue
        r = fn(p)
        if r.get('error'): errs += 1; continue
        n += len(r['nodes'])
    print(label, n, 'errors:', errs)
"
```

Expected: `routes 1544 errors: 0`, `permissions 382 errors: 0`.

- [ ] **Step 8: Commit**

```bash
git add graphify/drupal/yaml_access.py graphify/drupal/families.py \
        tests/test_drupal_access.py tests/test_drupal_seam.py
git commit -m "feat(drupal): extract routes, permissions and the requirement between them"
```

---

### Task 5: Libraries and breakpoints

**Files:**
- Create: `graphify/drupal/yaml_assets.py`
- Modify: `graphify/drupal/families.py` (two entries)
- Test: `tests/test_drupal_assets.py`

**Interfaces:**
- Consumes: Task 1 helpers, `extension_owner`, `extension_id`.
- Produces: `extract_drupal_libraries(path) -> dict`, `extract_drupal_breakpoints(path) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_assets.py
"""*.libraries.yml and *.breakpoints.yml."""
from __future__ import annotations

from graphify.drupal.yaml_assets import extract_drupal_breakpoints, extract_drupal_libraries
from graphify.drupal.yaml_common import breakpoint_id, library_id
from graphify.drupal.yaml_extract import extension_id

LIBRARIES = """\
main:
  version: 1.x
  css:
    theme:
      css/main.css: {}
  js:
    js/main.js: {}
  dependencies:
    - core/once
    - foo/helper
helper:
  js:
    js/helper.js: {}
"""

BREAKPOINTS = """\
foo.narrow:
  label: Narrow
  mediaQuery: 'all and (min-width: 560px)'
  weight: 1
  multipliers: ['1x', '2x']
"""


def _write(tmp_path, name, text):
    path = tmp_path / "web/themes/custom/foo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_library_is_declared_by_its_owner(tmp_path):
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", LIBRARIES))
    assert (extension_id("foo"), "declares_library", library_id("foo", "main")) in _rel(result)


def test_library_dependencies_resolve_to_owner_scoped_ids(tmp_path):
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", LIBRARIES))
    rel = _rel(result)
    assert (library_id("foo", "main"), "library_depends_on", library_id("core", "once")) in rel
    assert (library_id("foo", "main"), "library_depends_on", library_id("foo", "helper")) in rel


def test_assets_are_attributes_not_edges(tmp_path):
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", LIBRARIES))
    main = next(n for n in result["nodes"] if n["id"] == library_id("foo", "main"))
    assert main["css"] == ["css/main.css"]
    assert main["js"] == ["js/main.js"]
    assert main["version"] == "1.x"
    # A .css/.js node belongs to P5; no edge yet.
    assert not any(e["relation"] == "library_has_asset" for e in result["edges"])


def test_malformed_dependency_is_skipped_not_guessed(tmp_path):
    text = "main:\n  dependencies:\n    - no_slash_here\n"
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", text))
    assert not any(e["relation"] == "library_depends_on" for e in result["edges"])


def test_breakpoints(tmp_path):
    result = extract_drupal_breakpoints(_write(tmp_path, "foo.breakpoints.yml", BREAKPOINTS))
    bp = next(n for n in result["nodes"] if n["id"] == breakpoint_id("foo", "foo.narrow"))
    assert bp["label"] == "Narrow"
    assert bp["media_query"] == "all and (min-width: 560px)"
    assert (extension_id("foo"), "declares_breakpoint", bp["id"]) in _rel(result)


def test_neither_family_emits_an_extension_node(tmp_path):
    for name, text, fn in (
        ("foo.libraries.yml", LIBRARIES, extract_drupal_libraries),
        ("foo.breakpoints.yml", BREAKPOINTS, extract_drupal_breakpoints),
    ):
        result = fn(_write(tmp_path, name, text))
        assert not any(n["id"] == extension_id("foo") for n in result["nodes"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_assets.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.yaml_assets'`

- [ ] **Step 3: Implement**

```python
# graphify/drupal/yaml_assets.py
"""*.libraries.yml and *.breakpoints.yml.

A library dependency is written `<owner>/<name>` and both sides are declared by
a `*.libraries.yml`, so `library_depends_on` stays inside this phase. The css and
js paths point at files whose node ids belong to P5 — graphify has no CSS
extractor and its own convention for JS file nodes — so they are attributes here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.families import extension_owner
from graphify.drupal.yaml_common import (
    breakpoint_id,
    edge,
    key_lines,
    library_id,
    load_drupal_yaml,
    node,
)
from graphify.drupal.yaml_extract import extension_id


def _asset_paths(section: Any) -> list[str]:
    """Flatten `css: {theme: {path: {}}}` and `js: {path: {}}` to a path list."""
    if not isinstance(section, dict):
        return []
    paths: list[str] = []
    for key, value in section.items():
        if isinstance(value, dict) and value and all(
            isinstance(inner, dict) for inner in value.values()
        ):
            paths.extend(str(inner_key) for inner_key in value)
        else:
            paths.append(str(key))
    return paths


def extract_drupal_libraries(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    owner = extension_owner(path)
    owner_id = extension_id(owner)
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for name, definition in data.items():
        name = str(name)
        line = lines.get(name, 1)
        lid = library_id(owner, name)
        extra: dict[str, Any] = {}
        if isinstance(definition, dict):
            css = _asset_paths(definition.get("css"))
            js = _asset_paths(definition.get("js"))
            if css:
                extra["css"] = css
            if js:
                extra["js"] = js
            for key in ("version", "license", "remote"):
                if key in definition:
                    extra[key] = definition[key]
        nodes.append(node(lid, f"{owner}/{name}", type="drupal_library",
                          layer="presentation", path=path, line=line, **extra))
        edges.append(edge(owner_id, lid, "declares_library", path=path, line=line))

        if not isinstance(definition, dict):
            continue
        for dependency in definition.get("dependencies") or []:
            # A dependency without a slash is malformed; guessing an owner for it
            # would fabricate an edge to a library that does not exist.
            if not isinstance(dependency, str) or "/" not in dependency:
                continue
            dep_owner, _, dep_name = dependency.partition("/")
            target = library_id(dep_owner, dep_name)
            if (lid, target) in seen_pairs or target == lid:
                continue
            seen_pairs.add((lid, target))
            edges.append(edge(lid, target, "library_depends_on", path=path, line=line))

    return {"nodes": nodes, "edges": edges}


def extract_drupal_breakpoints(path: Path) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    owner = extension_owner(path)
    owner_id = extension_id(owner)
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for name, definition in data.items():
        name = str(name)
        line = lines.get(name, 1)
        bid = breakpoint_id(owner, name)
        extra: dict[str, Any] = {}
        label = name
        if isinstance(definition, dict):
            label = str(definition.get("label") or name)
            if "mediaQuery" in definition:
                extra["media_query"] = definition["mediaQuery"]
            if "multipliers" in definition:
                extra["multipliers"] = definition["multipliers"]
        nodes.append(node(bid, label, type="drupal_breakpoint",
                          layer="presentation", path=path, line=line, **extra))
        edges.append(edge(owner_id, bid, "declares_breakpoint", path=path, line=line))

    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 4: Register both families, update the seam test**

Add `".libraries.yml"` and `".breakpoints.yml"` to `FAMILY_EXTRACTORS`, and move
`token.libraries.yml` to the exempt assertions in `tests/test_drupal_seam.py`.
That is the last of the three files P0 predicted; `test_real_secret_stores_are_still_caught`
now holds only non-family names.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_drupal_assets.py tests/test_drupal_seam.py -q`
Expected: PASS

- [ ] **Step 6: Verify on the reference corpus**

```bash
uv run python -c "
from pathlib import Path
from graphify.drupal.yaml_assets import extract_drupal_libraries, extract_drupal_breakpoints
root = Path('/home/user/Projects/FormsRemote')
for glob, fn, label in (('web/**/*.libraries.yml', extract_drupal_libraries, 'libraries'),
                        ('web/**/*.breakpoints.yml', extract_drupal_breakpoints, 'breakpoints')):
    n = errs = 0
    for p in root.glob(glob):
        if 'node_modules' in p.parts: continue
        r = fn(p)
        if r.get('error'): errs += 1; continue
        n += len(r['nodes'])
    print(label, n, 'errors:', errs)
"
```

Expected: `libraries 1066 errors: 1` — the one error is core's deliberately
malformed `core/tests/.../invalid_file.libraries.yml`. `breakpoints 36 errors: 0`.

- [ ] **Step 7: Commit**

```bash
git add graphify/drupal/yaml_assets.py graphify/drupal/families.py \
        tests/test_drupal_assets.py tests/test_drupal_seam.py
git commit -m "feat(drupal): extract asset libraries, their dependencies, and breakpoints"
```

---

### Task 6: Links

Four families, one module: they share a shape, and `links_to_route` is the same
edge in three of them.

**Files:**
- Create: `graphify/drupal/yaml_links.py`
- Modify: `graphify/drupal/families.py` (four entries)
- Test: `tests/test_drupal_links.py`

**Interfaces:**
- Consumes: Task 1 helpers, `extension_owner`, `extension_id`, `route_id`.
- Produces: `extract_drupal_menu_links`, `extract_drupal_local_tasks`, `extract_drupal_local_actions`, `extract_drupal_contextual_links` — each `(path) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_links.py
"""The four *.links.*.yml families: the UI surface over the routing table."""
from __future__ import annotations

from graphify.drupal.yaml_common import link_id, menu_id, route_id
from graphify.drupal.yaml_extract import extension_id
from graphify.drupal.yaml_links import (
    extract_drupal_contextual_links,
    extract_drupal_local_actions,
    extract_drupal_local_tasks,
    extract_drupal_menu_links,
)

MENU = """\
foo.admin:
  title: 'Foo'
  route_name: foo.settings
  menu_name: admin
  parent: system.admin_config
  weight: 10
"""

TASK = """\
foo.settings_tab:
  title: 'Settings'
  route_name: foo.settings
  base_route: foo.settings
"""

ACTION = """\
foo.add:
  title: 'Add foo'
  route_name: foo.add_form
  appears_on:
    - foo.collection
"""

CONTEXTUAL = """\
foo.edit:
  title: 'Edit'
  route_name: foo.edit_form
  group: foo
"""


def _write(tmp_path, name, text):
    path = tmp_path / "web/modules/custom/foo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_menu_link_reaches_its_route_and_menu(tmp_path):
    result = extract_drupal_menu_links(_write(tmp_path, "foo.links.menu.yml", MENU))
    lid = link_id("menu_link", "foo.admin")
    rel = _rel(result)
    assert (extension_id("foo"), "declares_menu_link", lid) in rel
    assert (lid, "links_to_route", route_id("foo.settings")) in rel
    assert (lid, "in_menu", menu_id("admin")) in rel
    assert (lid, "parent_link", link_id("menu_link", "system.admin_config")) in rel


def test_local_task_carries_base_route(tmp_path):
    result = extract_drupal_local_tasks(_write(tmp_path, "foo.links.task.yml", TASK))
    lid = link_id("local_task", "foo.settings_tab")
    rel = _rel(result)
    assert (lid, "links_to_route", route_id("foo.settings")) in rel
    assert (lid, "base_route", route_id("foo.settings")) in rel


def test_one_relation_per_ordered_pair_when_route_equals_base_route(tmp_path):
    """links_to_route and base_route share endpoints here; the reader keeps one."""
    result = extract_drupal_local_tasks(_write(tmp_path, "foo.links.task.yml", TASK))
    pairs = [(e["source"], e["target"]) for e in result["edges"]]
    assert len(pairs) == len(set(pairs))


def test_local_action_appears_on_routes(tmp_path):
    result = extract_drupal_local_actions(_write(tmp_path, "foo.links.action.yml", ACTION))
    lid = link_id("local_action", "foo.add")
    rel = _rel(result)
    assert (lid, "links_to_route", route_id("foo.add_form")) in rel
    assert (lid, "appears_on_route", route_id("foo.collection")) in rel


def test_contextual_link(tmp_path):
    result = extract_drupal_contextual_links(
        _write(tmp_path, "foo.links.contextual.yml", CONTEXTUAL))
    lid = link_id("contextual_link", "foo.edit")
    assert (lid, "links_to_route", route_id("foo.edit_form")) in _rel(result)


def test_no_family_emits_an_extension_or_route_node(tmp_path):
    """Routes belong to *.routing.yml; these families only reference them."""
    result = extract_drupal_menu_links(_write(tmp_path, "foo.links.menu.yml", MENU))
    ids = {n["id"] for n in result["nodes"]}
    assert extension_id("foo") not in ids
    assert route_id("foo.settings") not in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_links.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.yaml_links'`

- [ ] **Step 3: Implement**

```python
# graphify/drupal/yaml_links.py
"""The four *.links.*.yml families.

These are YAML-discovered plugins. Read here as data; recognising them AS
plugins belongs to P2's discovery registry, which learns plugin types from the
project's own plugin managers.

`links_to_route` is what makes the family worth a phase: it joins the UI surface
to the routing table, and both endpoints are declared by YAML P1 reads.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.families import extension_owner
from graphify.drupal.yaml_common import (
    edge,
    key_lines,
    link_id,
    load_drupal_yaml,
    menu_id,
    node,
    route_id,
)
from graphify.drupal.yaml_extract import extension_id


def _extract_links(path: Path, kind: str, node_type: str, declares: str) -> dict[str, Any]:
    data, error = load_drupal_yaml(path)
    if error:
        return {"nodes": [], "edges": [], "error": error}
    if not data:
        return {"nodes": [], "edges": []}

    owner_id = extension_id(extension_owner(path))
    lines = key_lines(path.read_text(encoding="utf-8", errors="replace"))[0]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def add_edge(source: str, target: str, relation: str, line: int) -> None:
        # One relation per ordered pair: a local task whose route_name equals its
        # base_route would otherwise emit two, and the reader silently keeps one.
        if source == target or (source, target) in seen_pairs:
            return
        seen_pairs.add((source, target))
        edges.append(edge(source, target, relation, path=path, line=line))

    for plugin_id, definition in data.items():
        plugin_id = str(plugin_id)
        if not isinstance(definition, dict):
            continue
        line = lines.get(plugin_id, 1)
        lid = link_id(kind, plugin_id)
        extra: dict[str, Any] = {}
        for key in ("weight", "group", "deriver"):
            if key in definition:
                extra[key] = definition[key]
        nodes.append(node(lid, str(definition.get("title") or plugin_id),
                          type=node_type, layer="routing", path=path, line=line, **extra))
        add_edge(owner_id, lid, declares, line)

        route = definition.get("route_name")
        if isinstance(route, str) and route:
            add_edge(lid, route_id(route), "links_to_route", line)

        base = definition.get("base_route")
        if isinstance(base, str) and base:
            add_edge(lid, route_id(base), "base_route", line)

        menu = definition.get("menu_name")
        if isinstance(menu, str) and menu:
            add_edge(lid, menu_id(menu), "in_menu", line)

        parent = definition.get("parent")
        if isinstance(parent, str) and parent:
            add_edge(lid, link_id(kind, parent), "parent_link", line)

        for appears in definition.get("appears_on") or []:
            if isinstance(appears, str) and appears:
                add_edge(lid, route_id(appears), "appears_on_route", line)

    return {"nodes": nodes, "edges": edges}


def extract_drupal_menu_links(path: Path) -> dict[str, Any]:
    return _extract_links(path, "menu_link", "drupal_menu_link", "declares_menu_link")


def extract_drupal_local_tasks(path: Path) -> dict[str, Any]:
    return _extract_links(path, "local_task", "drupal_local_task", "declares_local_task")


def extract_drupal_local_actions(path: Path) -> dict[str, Any]:
    return _extract_links(path, "local_action", "drupal_local_action", "declares_local_action")


def extract_drupal_contextual_links(path: Path) -> dict[str, Any]:
    return _extract_links(
        path, "contextual_link", "drupal_contextual_link", "declares_contextual_link")
```

- [ ] **Step 4: Register the four families**

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_drupal_links.py -q`
Expected: PASS, 6 tests

- [ ] **Step 6: Commit**

```bash
git add graphify/drupal/yaml_links.py graphify/drupal/families.py tests/test_drupal_links.py
git commit -m "feat(drupal): extract menu links, local tasks, actions and contextual links"
```

---

### Task 7: Widen the resolver past extensions

P0's resolver materialises an external node for an undeclared *extension*. P1
edges can name a service, permission, library, route or menu that nothing in the
corpus declares — a module depending on a contrib module the site does not
vendor, or `_permission: 'access content'` when `user` is absent. Those edges are
dropped on the way into the graph unless the endpoint exists.

**Files:**
- Modify: `graphify/drupal/resolvers.py`
- Test: `tests/test_drupal_info_extract.py` (extend the resolver tests already there)

**Interfaces:**
- Consumes: nothing new.
- Produces: `resolve_missing_targets(per_file, all_nodes, all_edges) -> None`, replacing `resolve_missing_extensions`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_drupal_info_extract.py`:

```python
def test_resolver_materialises_missing_targets_of_every_p1_relation():
    from graphify.drupal.resolvers import resolve_missing_targets
    from graphify.drupal.yaml_common import library_id, permission_id, route_id, service_id

    nodes = [{"id": service_id("database")}]
    edges = [
        {"relation": "injects_service", "target": service_id("database"),
         "source_file": "a/foo.services.yml"},
        {"relation": "injects_service", "target": service_id("absent.service"),
         "target_name": "absent.service", "source_file": "a/foo.services.yml"},
        {"relation": "requires_permission", "target": permission_id("access content"),
         "target_name": "access content", "source_file": "a/foo.routing.yml"},
        {"relation": "library_depends_on", "target": library_id("core", "once"),
         "target_name": "core/once", "source_file": "a/foo.libraries.yml"},
        {"relation": "links_to_route", "target": route_id("user.login"),
         "target_name": "user.login", "source_file": "a/foo.links.menu.yml"},
        {"relation": "calls", "target": "another_language_node", "source_file": "a/x.py"},
    ]
    resolve_missing_targets([], nodes, edges)

    created = {n["id"]: n for n in nodes if n.get("external")}
    assert service_id("absent.service") in created
    assert permission_id("access content") in created
    assert library_id("core", "once") in created
    assert route_id("user.login") in created
    assert service_id("database") not in created      # already declared
    assert "another_language_node" not in created     # not ours to invent
    assert created[route_id("user.login")]["type"] == "drupal_route"
    assert created[permission_id("access content")]["label"] == "access content"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_info_extract.py -q`
Expected: FAIL — `ImportError: cannot import name 'resolve_missing_targets'`

- [ ] **Step 3: Implement**

Replace the contents of `graphify/drupal/resolvers.py` below its docstring:

```python
#: relation -> (node type, layer) for the endpoint this pass may materialise.
#: A relation absent from this table belongs to another producer, and its
#: dangling endpoint is not ours to invent.
_RESOLVABLE: dict[str, tuple[str, str]] = {
    "depends_on_module": ("drupal_extension", "extension"),
    "base_theme": ("drupal_extension", "extension"),
    "injects_service": ("drupal_service", "di"),
    "decorates": ("drupal_service", "di"),
    "parent_service": ("drupal_service", "di"),
    "injects_parameter": ("drupal_parameter", "di"),
    "requires_permission": ("drupal_permission", "routing"),
    "library_depends_on": ("drupal_library", "presentation"),
    "links_to_route": ("drupal_route", "routing"),
    "base_route": ("drupal_route", "routing"),
    "appears_on_route": ("drupal_route", "routing"),
    "in_menu": ("drupal_menu", "routing"),
    "parent_link": ("drupal_menu_link", "routing"),
}


def resolve_missing_targets(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Materialise an external node for every target named but never declared."""
    known = {node.get("id") for node in all_nodes}
    created: dict[str, dict[str, Any]] = {}

    for edge in all_edges:
        spec = _RESOLVABLE.get(edge.get("relation"))
        if spec is None:
            continue
        target = edge.get("target")
        if not target or target in known or target in created:
            continue
        node_type, layer = spec
        created[target] = {
            "id": target,
            "label": edge.get("target_name") or target,
            "file_type": "concept",
            "type": node_type,
            "layer": layer,
            "realm": "unknown",
            "external": True,
            "_origin": "static_yaml",
            "source_file": edge.get("source_file", ""),
            "source_location": edge.get("source_location", "L1"),
        }

    all_nodes.extend(created.values())


#: Kept as the previous name so an older registration keeps working.
resolve_missing_extensions = resolve_missing_targets
```

Update the registration in `graphify/drupal/register.py` to use
`resolve_missing_targets`.

- [ ] **Step 4: Carry `target_name` on every resolvable edge**

An external node's label comes from `target_name`; without it the label is the
normalised id, so `access content` reads as `drupal_permission_access_content`.
Every edge whose relation appears in `_RESOLVABLE` must carry the raw name.

In `yaml_services.py`, the injection edges become:

```python
                add_edge(own, service_id(ref), "injects_service", line,
                         target_name=ref,
                         confidence="INFERRED" if optional else "EXTRACTED")
```

and likewise `injects_parameter` (`target_name=parameter`), `decorates` and
`parent_service` (`target_name=definition["decorates"]` / `["parent"]`).

`yaml_access.py`: `requires_permission` gets `target_name=part`.

`yaml_assets.py`: `library_depends_on` gets `target_name=dependency` — the
`<owner>/<name>` form, which is how a Drupal developer writes it.

`yaml_links.py`: `add_edge` gains a `target_name` argument and passes it
through; call it with `route`, `base`, `menu`, `parent` and `appears`
respectively.

Extend one test per family with a label assertion, for example in
`tests/test_drupal_assets.py`:

```python
def test_dependency_edge_carries_a_readable_target_name(tmp_path):
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", LIBRARIES))
    dep = next(e for e in result["edges"] if e["relation"] == "library_depends_on")
    assert dep["target_name"] in ("core/once", "foo/helper")
```

- [ ] **Step 5: Run the Drupal suite**

Run: `uv run pytest tests/test_drupal_*.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add graphify/drupal/resolvers.py graphify/drupal/register.py graphify/drupal/yaml_*.py \
        tests/test_drupal_*.py
git commit -m "feat(drupal): resolve undeclared targets for every P1 relation"
```

---

### Task 8: Verify against the reference corpus

The spec's nine acceptance criteria, as a test that runs only where the corpus
exists.

**Files:**
- Create: `tests/test_drupal_corpus.py`
- Test: itself

**Interfaces:**
- Consumes: everything above.
- Produces: nothing importable — this task is evidence.

- [ ] **Step 1: Write the test**

```python
# tests/test_drupal_corpus.py
"""P1's acceptance criteria, measured on a real Drupal tree.

Skipped when the corpus is absent, so CI stays hermetic. The numbers are the
ones in the P1 spec; a change in them is either a regression or a corpus that
moved, and both deserve a look.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))

pytestmark = pytest.mark.skipif(
    not (CORPUS / "web" / "core").is_dir(),
    reason="reference Drupal corpus not present",
)


def _files(pattern: str) -> list[Path]:
    return [p for p in CORPUS.glob(pattern) if "node_modules" not in p.parts]


def _extract_all() -> dict:
    from graphify.drupal.families import family_extractor

    nodes, edges, errors = [], [], []
    for path in CORPUS.glob("web/**/*.yml"):
        if "node_modules" in path.parts:
            continue
        handler = family_extractor(path)
        if handler is None:
            continue
        result = handler(path)
        if result.get("error"):
            errors.append((path, result["error"]))
            continue
        nodes.extend(result["nodes"])
        edges.extend(result["edges"])
    return {"nodes": nodes, "edges": edges, "errors": errors}


@pytest.fixture(scope="module")
def corpus_extraction():
    return _extract_all()


def test_criterion_1_only_the_malformed_core_fixture_fails(corpus_extraction):
    errors = corpus_extraction["errors"]
    assert [p.name for p, _ in errors] == ["invalid_file.libraries.yml"]


def test_criterion_2_the_tolerant_loader_finds_every_service(corpus_extraction):
    services = [n for n in corpus_extraction["nodes"] if n["type"] == "drupal_service"]
    assert len(services) == 2320, "1628 means safe_load crept back in"


def test_criterion_3_no_family_file_is_dropped_as_a_secret():
    import graphify  # installs the seam
    from graphify.detect import _is_sensitive
    from graphify.drupal.families import is_drupal_yaml

    dropped = [
        p for p in CORPUS.glob("web/**/*.yml")
        if "node_modules" not in p.parts and is_drupal_yaml(p) and _is_sensitive(p)
    ]
    assert dropped == []


def test_criterion_4_only_info_yml_declares_extensions(corpus_extraction):
    declared = [
        n for n in corpus_extraction["nodes"]
        if n["type"] in ("drupal_module", "drupal_theme", "drupal_profile")
    ]
    assert len(declared) == 1140
    assert all(n["source_file"].endswith(".info.yml") for n in declared)


def test_criterion_5_nothing_dangles_after_the_resolver(corpus_extraction):
    from graphify.drupal.resolvers import resolve_missing_targets

    nodes = list(corpus_extraction["nodes"])
    edges = corpus_extraction["edges"]
    resolve_missing_targets([], nodes, edges)
    ids = {n["id"] for n in nodes}
    assert [e["relation"] for e in edges if e["target"] not in ids] == []


def test_criterion_6_every_entity_has_exactly_one_declaring_edge(corpus_extraction):
    import collections

    declares = collections.Counter(
        e["target"] for e in corpus_extraction["edges"] if e["relation"].startswith("declares_")
    )
    multiply_owned = {t: n for t, n in declares.items() if n > 1}
    assert multiply_owned == {}


def test_criterion_7_every_node_is_filterable(corpus_extraction):
    for node in corpus_extraction["nodes"]:
        assert node.get("realm") in ("core", "contrib", "custom", "unknown"), node["id"]
        assert node.get("layer"), node["id"]
    unknown = [n for n in corpus_extraction["nodes"] if n["realm"] == "unknown"]
    assert unknown == []


def test_criterion_7b_the_custom_slice_renders_without_aggregation(corpus_extraction):
    custom = [n for n in corpus_extraction["nodes"] if n["realm"] == "custom"]
    assert len(custom) < 5000, "the custom slice must stay visually readable"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_drupal_corpus.py -q --tb=short`

Read every failure as information about the corpus or the extractors, not as a
number to edit. Two in particular:

- criterion 2 off by roughly 692 → a `yaml.safe_load` crept into an extractor;
- criterion 6 non-empty → two files declare the same entity, which is either a
  real duplicate in the corpus or an owner-resolution bug in `extension_owner`.

- [ ] **Step 3: Add the incremental check (spec criterion 8)**

```python
def test_criterion_8_editing_one_file_changes_only_its_own_edges(tmp_path):
    """The CLI-level check P0 used, now at 40x the file count."""
    import json, shutil, subprocess, sys

    work = tmp_path / "proj"
    (work / "web/modules/custom/foo").mkdir(parents=True)
    (work / "web/modules/custom/foo/foo.info.yml").write_text(
        "name: Foo\ntype: module\n", encoding="utf-8")
    (work / "web/modules/custom/foo/foo.services.yml").write_text(
        "services:\n  foo.a:\n    class: A\n    arguments: ['@database']\n", encoding="utf-8")
    (work / "web/modules/custom/foo/foo.routing.yml").write_text(
        "foo.page:\n  path: /foo\n", encoding="utf-8")

    def run():
        proc = subprocess.run(
            [sys.executable, "-m", "graphify", "extract", str(work), "--code-only"],
            capture_output=True, text=True, cwd=work)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        graph = json.loads((work / "graphify-out" / "graph.json").read_text())
        links = graph.get("links", graph.get("edges", []))
        return {(e["source"], e["relation"], e["target"]) for e in links if "relation" in e}

    before = run()
    (work / "web/modules/custom/foo/foo.services.yml").write_text(
        "services:\n  foo.a:\n    class: A\n    arguments: ['@state']\n", encoding="utf-8")
    after = run()

    changed = (before - after) | (after - before)
    assert all(rel == "injects_service" for _s, rel, _t in changed), changed
    shutil.rmtree(work, ignore_errors=True)
```

This one does not need the corpus, so it lives outside the skip marker — move it
to `tests/test_drupal_pipeline.py` rather than `tests/test_drupal_corpus.py`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest tests/ -q --tb=short`
Expected: green apart from the four pre-existing `test_ollama_retry_cap.py`
failures, which are a missing optional `openai` extra and reproduce on upstream.

- [ ] **Step 5: Confirm the allow-list did not move**

Run: `uv run pytest tests/test_drupal_allowlist.py -q`
Expected: PASS. P1 adds no path outside `graphify/drupal/` and
`tests/test_drupal_*.py`, so there should be no `core:` commit in this phase.

- [ ] **Step 6: Commit**

```bash
git add tests/test_drupal_corpus.py tests/test_drupal_pipeline.py
git commit -m "test(drupal): assert P1's acceptance criteria against a real Drupal tree"
```

---

## Definition of done

- `uv run --frozen pytest tests/ -q` green apart from the four known `openai` failures.
- `tests/test_drupal_corpus.py` passes against the reference corpus: 2,320 services, 1,140 extensions declared only by `*.info.yml`, zero family files dropped as secrets, zero dangling edges, zero `realm: unknown`.
- `git log v8..HEAD --grep '^core:'` gained **no** new entries — P1 touches nothing upstream owns.
- The graph carries roughly 7,600 nodes, and the `realm: custom` slice is small enough to render un-aggregated.
