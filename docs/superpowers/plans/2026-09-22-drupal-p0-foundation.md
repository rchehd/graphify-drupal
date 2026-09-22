# Drupal P0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that a Drupal extractor registered at runtime behaves, through graphify's entire pipeline, exactly like one written into core — and leave the package scaffolding behind.

**Architecture:** A new subpackage `graphify/drupal/` holds everything Drupal. It reaches core through one seam: `graphify/__init__.py` installs a `sys.meta_path` hook that wraps `graphify.detect.classify_file` and `graphify.extract._get_extractor` the moment those modules finish importing. Nothing in core is edited. One extractor ships — `*.info.yml` — because it is the smallest Drupal file that still produces a real edge.

**Tech Stack:** Python 3.10+, PyYAML, pytest, uv.

## Global Constraints

- Python floor is `>=3.10`; ruff `line-length = 100`, `target-version = "py310"`.
- Tests run as `uv run --frozen pytest tests/ -q --tb=short`. `--frozen` means a dependency change **must** be accompanied by a regenerated `uv.lock` or CI fails.
- Test files are flat and named `tests/test_drupal_*.py` — the repo has 295 flat test files and no per-feature subdirectories.
- Tests must not write outside pytest's `tmp_path`. Build fixtures in test code; do not add files under `tests/fixtures/`.
- Allow-list — the diff against `upstream/v8` may only touch: `graphify/drupal/*`, `graphify/__init__.py`, `pyproject.toml`, `uv.lock`, `tests/test_drupal_*.py`, `docs/*`, `.github/workflows/drupal-guard.yml`.
- Any commit touching a file that exists in upstream is prefixed `core:` — currently `graphify/__init__.py`, `pyproject.toml`, `uv.lock`.
- `file_type` on every node must be one of `code, document, paper, image, rationale, concept`. The Drupal taxonomy goes in a separate `type` field. `build.py` silently rewrites anything else to `"concept"`.
- At most **one** relation per ordered node pair. graphify's reader collapses parallel edges and drops the loser silently.
- Node ids are built with `graphify.ids.make_id`, never hand-formatted.

---

### Task 1: Path predicates and realm resolution

**Files:**
- Create: `graphify/drupal/__init__.py`
- Create: `graphify/drupal/paths.py`
- Modify: `pyproject.toml:141` (the `packages` list)
- Test: `tests/test_drupal_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `is_drupal_info_yaml(path: Path) -> bool`
  - `extension_machine_name(path: Path) -> str`
  - `resolve_realm(path: Path, rules: dict[str, tuple[str, ...]] | None = None) -> str`
  - `load_realm_rules(root: Path) -> dict[str, tuple[str, ...]]`
  - `DEFAULT_REALM_RULES: dict[str, tuple[str, ...]]`

`resolve_realm` takes no `root`. Core calls extractors as `extractor(path)` with no
extra arguments, so every pattern is written to match an absolute POSIX path via a
leading `*`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_paths.py
"""Realm resolution and the *.info.yml predicate."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.drupal.paths import (
    DEFAULT_REALM_RULES,
    extension_machine_name,
    is_drupal_info_yaml,
    load_realm_rules,
    resolve_realm,
)


@pytest.mark.parametrize("name, expected", [
    ("foo.info.yml", True),
    ("mytheme.info.yml", True),
    (".info.yml", False),
    ("foo.services.yml", False),
    ("foo.yml", False),
    ("info.yml", True),
    ("docker-compose.yml", False),
])
def test_info_yaml_predicate(name, expected):
    assert is_drupal_info_yaml(Path("/p") / name) is expected


def test_machine_name_is_the_stem_before_info():
    assert extension_machine_name(Path("/p/web/modules/custom/foo/foo.info.yml")) == "foo"


@pytest.mark.parametrize("path, expected", [
    ("/p/core/modules/node/node.info.yml", "core"),
    ("/p/web/core/modules/node/node.info.yml", "core"),
    ("/p/web/modules/contrib/token/token.info.yml", "contrib"),
    ("/p/docroot/themes/contrib/olivero/olivero.info.yml", "contrib"),
    ("/p/web/modules/custom/foo/foo.info.yml", "custom"),
    ("/p/modules/custom/foo/foo.info.yml", "custom"),
    ("/p/web/profiles/myprofile/modules/bar/bar.info.yml", "custom"),
    ("/p/web/modules/contrib/core_flag/core_flag.info.yml", "contrib"),
    ("/p/somewhere/odd/foo.info.yml", "unknown"),
])
def test_realm_resolution(path, expected):
    assert resolve_realm(Path(path)) == expected


def test_realm_rules_are_overridable_from_graphifyrc(tmp_path):
    (tmp_path / ".graphifyrc").write_text(
        "viz_node_limit=0\n"
        "drupal.realm.custom = */modules/acme/*\n",
        encoding="utf-8",
    )
    rules = load_realm_rules(tmp_path)
    assert rules["custom"] == ("*/modules/acme/*",)
    assert rules["core"] == DEFAULT_REALM_RULES["core"]
    assert resolve_realm(Path("/p/web/modules/acme/foo/foo.info.yml"), rules) == "custom"


def test_missing_graphifyrc_yields_defaults(tmp_path):
    assert load_realm_rules(tmp_path) == DEFAULT_REALM_RULES


def test_core_graphifyrc_reader_tolerates_our_keys(tmp_path):
    """Our keys must not break graphify's own .graphifyrc parser."""
    from graphify.hooks import _load_graphifyrc

    (tmp_path / ".graphifyrc").write_text(
        "drupal.realm.custom = */modules/acme/*\nviz_node_limit=0\n", encoding="utf-8"
    )
    assert _load_graphifyrc(tmp_path) == {"viz_node_limit": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_paths.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal'`

- [ ] **Step 3: Create the package and implement**

```python
# graphify/drupal/__init__.py
"""Drupal-specific producers for graphify.

Everything Drupal lives under this package. Core is reached through exactly one
seam, in `graphify.drupal.register`; no module in graphify/ outside that seam is
edited by this fork.
"""
```

```python
# graphify/drupal/paths.py
"""Filename predicates and realm resolution for Drupal extensions.

`resolve_realm` deliberately takes no project root: core invokes extractors as
`extractor(path)` with no further arguments, so every pattern is written to match
an absolute POSIX path and begins with `*`.
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

_INFO_SUFFIX = ".info.yml"

#: Checked in the order of `_REALM_ORDER`; first match wins. `fnmatch`'s `*`
#: matches path separators, so a leading `*` absorbs any docroot layout.
DEFAULT_REALM_RULES: dict[str, tuple[str, ...]] = {
    "core": ("*/core/modules/*", "*/core/themes/*", "*/core/profiles/*", "*/core/lib/*"),
    "contrib": ("*/modules/contrib/*", "*/themes/contrib/*", "*/profiles/contrib/*"),
    "custom": (
        "*/modules/custom/*",
        "*/themes/custom/*",
        "*/profiles/*/modules/*",
        "*/profiles/*/themes/*",
    ),
}

_REALM_ORDER = ("core", "contrib", "custom")

_RC_PREFIX = "drupal.realm."


def is_drupal_info_yaml(path: Path) -> bool:
    """True for `<machine_name>.info.yml`. A bare `.info.yml` has no name."""
    return path.name.endswith(_INFO_SUFFIX) and len(path.name) > len(_INFO_SUFFIX)


def extension_machine_name(path: Path) -> str:
    return path.name[: -len(_INFO_SUFFIX)]


def resolve_realm(path: Path, rules: dict[str, tuple[str, ...]] | None = None) -> str:
    """Return `core`, `contrib`, `custom`, or `unknown`.

    `unknown` is a reportable outcome, not a default to hide: an unmatched
    extension means the project's layout is not covered by the rules.
    """
    active = DEFAULT_REALM_RULES if rules is None else rules
    target = path.as_posix()
    for realm in _REALM_ORDER:
        for pattern in active.get(realm, ()):
            if fnmatch(target, pattern):
                return realm
    return "unknown"


def load_realm_rules(root: Path) -> dict[str, tuple[str, ...]]:
    """Read `drupal.realm.<realm> = a, b` lines from `<root>/.graphifyrc`.

    Only the realms named in the file are replaced; the rest keep their defaults.
    Lines this parser does not recognise are ignored, exactly as graphify's own
    `.graphifyrc` reader ignores ours.
    """
    rc_path = Path(root) / ".graphifyrc"
    if not rc_path.is_file():
        return DEFAULT_REALM_RULES
    rules = dict(DEFAULT_REALM_RULES)
    for raw in rc_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key.startswith(_RC_PREFIX):
            continue
        realm = key[len(_RC_PREFIX):].strip()
        if realm not in _REALM_ORDER:
            continue
        rules[realm] = tuple(p.strip() for p in val.split(",") if p.strip())
    return rules
```

- [ ] **Step 4: Register the package**

In `pyproject.toml:141`, change:

```toml
packages = ["graphify", "graphify.extractors", "graphify.exporters"]
```

to:

```toml
packages = ["graphify", "graphify.drupal", "graphify.extractors", "graphify.exporters"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_drupal_paths.py -q`
Expected: PASS, 15 tests

- [ ] **Step 6: Commit**

Two commits, because `pyproject.toml` is an upstream file:

```bash
git add graphify/drupal/__init__.py graphify/drupal/paths.py tests/test_drupal_paths.py
git commit -m "feat(drupal): add path predicates and configurable realm resolution"
git add pyproject.toml
git commit -m "core: ship graphify.drupal in the package list"
```

---

### Task 2: The `*.info.yml` extractor

**Files:**
- Create: `graphify/drupal/yaml_extract.py`
- Modify: `pyproject.toml:14` (the `dependencies` list)
- Modify: `uv.lock`
- Test: `tests/test_drupal_info_extract.py`

**Interfaces:**
- Consumes: `extension_machine_name`, `resolve_realm` from Task 1.
- Produces:
  - `extract_drupal_info(path: Path) -> dict[str, Any]` — the graphify extractor contract, `{"nodes": [...], "edges": [...]}`
  - `extension_id(machine_name: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_info_extract.py
"""The *.info.yml extractor: nodes, edges, and failure modes."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_extract import extension_id, extract_drupal_info

MODULE_INFO = """\
name: Foo
type: module
core_version_requirement: ^10 || ^11
dependencies:
  - drupal:node
  - views:views_ui
  - token
"""

THEME_INFO = """\
name: My Theme
type: theme
base theme: olivero
"""


def _write(tmp_path, rel, text):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_module_node_carries_the_drupal_taxonomy(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml", MODULE_INFO)
    result = extract_drupal_info(path)
    own = next(n for n in result["nodes"] if n["id"] == extension_id("foo"))
    assert own["label"] == "Foo"
    assert own["file_type"] == "code"       # schema value, never "drupal_module"
    assert own["type"] == "drupal_module"   # taxonomy lives here
    assert own["layer"] == "extension"
    assert own["realm"] == "custom"
    assert own["_origin"] == "static_yaml"
    assert own["source_file"] == str(path)


def test_dependency_spellings_all_normalise_to_one_id(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml", MODULE_INFO)
    result = extract_drupal_info(path)
    deps = {e["target"] for e in result["edges"] if e["relation"] == "depends_on_module"}
    assert deps == {extension_id("node"), extension_id("views_ui"), extension_id("token")}


def test_dependency_targets_are_external_stubs(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml", MODULE_INFO)
    result = extract_drupal_info(path)
    stub = next(n for n in result["nodes"] if n["id"] == extension_id("node"))
    assert stub["external"] is True
    assert stub["file_type"] == "concept"


def test_theme_emits_base_theme_edge(tmp_path):
    path = _write(tmp_path, "web/themes/custom/mytheme/mytheme.info.yml", THEME_INFO)
    result = extract_drupal_info(path)
    own = next(n for n in result["nodes"] if n["id"] == extension_id("mytheme"))
    assert own["type"] == "drupal_theme"
    assert [(e["source"], e["relation"], e["target"]) for e in result["edges"]] == [
        (extension_id("mytheme"), "base_theme", extension_id("olivero")),
    ]


def test_one_relation_per_ordered_pair(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml",
                  "name: Foo\ntype: module\ndependencies:\n  - node\n  - drupal:node\n")
    result = extract_drupal_info(path)
    pairs = [(e["source"], e["target"]) for e in result["edges"]]
    assert len(pairs) == len(set(pairs)) == 1


def test_self_dependency_is_dropped(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml",
                  "name: Foo\ntype: module\ndependencies:\n  - foo\n")
    assert extract_drupal_info(path)["edges"] == []


def test_malformed_yaml_returns_an_error_not_an_exception(tmp_path):
    path = _write(tmp_path, "web/modules/custom/foo/foo.info.yml", "name: [unclosed\n")
    result = extract_drupal_info(path)
    assert result["nodes"] == [] and result["edges"] == []
    assert "parse error" in result["error"]


def test_node_ids_survive_builder_normalisation(tmp_path):
    """make_id output must be a fixed point of the builder's own normaliser."""
    from graphify.ids import normalize_id

    path = _write(tmp_path, "web/modules/custom/foo_bar/foo_bar.info.yml",
                  "name: Foo Bar\ntype: module\n")
    for node in extract_drupal_info(path)["nodes"]:
        assert normalize_id(node["id"]) == node["id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_info_extract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.yaml_extract'`

- [ ] **Step 3: Declare the PyYAML dependency**

PyYAML is currently present only transitively in the dev environment; it is not a
declared dependency of the package. Add it to the `dependencies` list in
`pyproject.toml` (which starts at line 14), keeping the existing entries:

```toml
dependencies = [
    "networkx>=3.4",
    "numpy>=1.21",
    "pyyaml>=6",
    "rapidfuzz>=3.0",
    ...
]
```

Then regenerate the lock, because CI runs `uv run --frozen`:

```bash
uv lock
```

- [ ] **Step 4: Implement the extractor**

```python
# graphify/drupal/yaml_extract.py
"""Drupal `*.info.yml` extractor.

Modules, themes and profiles share one id namespace: a `dependencies:` entry
names an extension without saying which kind it is, and the kind is only knowable
after reading that extension's own `*.info.yml`. Separate namespaces would force
a guess at edge-creation time, and every wrong guess is a dangling edge.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.drupal.paths import extension_machine_name, resolve_realm
from graphify.ids import make_id

_TYPE_TO_NODE_TYPE = {
    "module": "drupal_module",
    "theme": "drupal_theme",
    "profile": "drupal_profile",
}

#: An info.yml is a handful of lines. Anything larger is not one, and parsing a
#: corpus-supplied file without a cap is how a zip-bomb equivalent gets in.
_MAX_INFO_BYTES = 512 * 1024


def extension_id(machine_name: str) -> str:
    return make_id("drupal", "extension", machine_name)


def _normalise_dependency(raw: Any) -> str:
    """`drupal:node`, `views:views_ui`, `node (>=8.x)` and `node` name one thing."""
    dep = str(raw).strip()
    if ":" in dep:
        dep = dep.split(":", 1)[1]
    return dep.split("(", 1)[0].strip()


def extract_drupal_info(path: Path) -> dict[str, Any]:
    # Function-local so `import graphify` stays at 1 ms.
    import yaml

    try:
        if path.stat().st_size > _MAX_INFO_BYTES:
            return {"nodes": [], "edges": [], "error": "info.yml too large to index"}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": f"info.yml read error: {exc}"}

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {"nodes": [], "edges": [], "error": f"info.yml parse error: {exc}"}

    if not isinstance(data, dict):
        return {"nodes": [], "edges": []}

    machine_name = extension_machine_name(path)
    own_id = extension_id(machine_name)
    str_path = str(path)
    node_type = _TYPE_TO_NODE_TYPE.get(str(data.get("type", "")).strip(), "drupal_module")

    nodes: list[dict[str, Any]] = [{
        "id": own_id,
        "label": str(data.get("name") or machine_name),
        "file_type": "code",
        "type": node_type,
        "layer": "extension",
        "realm": resolve_realm(path),
        "_origin": "static_yaml",
        "source_file": str_path,
        "source_location": "L1",
    }]
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(target_name: str, relation: str) -> None:
        if not target_name or target_name == machine_name:
            return
        target_id = extension_id(target_name)
        if target_id in seen:
            return  # one relation per ordered pair — the reader drops the rest
        seen.add(target_id)
        nodes.append({
            "id": target_id,
            "label": target_name,
            "file_type": "concept",
            "type": "drupal_extension",
            "layer": "extension",
            "realm": "unknown",
            "external": True,
            "_origin": "static_yaml",
            "source_file": str_path,
            "source_location": "L1",
        })
        edges.append({
            "source": own_id,
            "target": target_id,
            "relation": relation,
            "confidence": "EXTRACTED",
            "_origin": "static_yaml",
            "source_file": str_path,
            "source_location": "L1",
        })

    raw_deps = data.get("dependencies") or []
    if isinstance(raw_deps, list):
        for raw in raw_deps:
            add(_normalise_dependency(raw), "depends_on_module")

    base = data.get("base theme")
    if isinstance(base, str) and base.strip():
        add(base.strip(), "base_theme")

    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_drupal_info_extract.py -q`
Expected: PASS, 8 tests

- [ ] **Step 6: Commit**

```bash
git add graphify/drupal/yaml_extract.py tests/test_drupal_info_extract.py
git commit -m "feat(drupal): extract extensions and their dependencies from *.info.yml"
git add pyproject.toml uv.lock
git commit -m "core: declare the pyyaml dependency the Drupal producers need"
```

---

### Task 3: The seam

**Files:**
- Create: `graphify/drupal/register.py`
- Test: `tests/test_drupal_seam.py`

**Interfaces:**
- Consumes: `is_drupal_info_yaml` (Task 1), `extract_drupal_info` (Task 2).
- Produces:
  - `install() -> None` — idempotent; imports only `sys` and `importlib`
  - `DrupalSeamError` — raised when a core structure the seam needs is missing

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_seam.py
"""The one place graphify.drupal touches core, and its guard rails."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from graphify.drupal.register import DrupalSeamError, _patch_detect, _patch_extract, install


def test_install_is_idempotent():
    install()
    install()
    from graphify.drupal.register import _DrupalFinder

    assert sum(isinstance(f, _DrupalFinder) for f in sys.meta_path) == 1


def test_classify_file_promotes_info_yaml_to_code():
    install()
    import graphify.detect as detect

    assert detect.classify_file(Path("/p/web/modules/custom/foo/foo.info.yml")) is detect.FileType.CODE


def test_classify_file_leaves_other_yaml_alone():
    install()
    import graphify.detect as detect

    assert detect.classify_file(Path("/p/docker-compose.yml")) is detect.FileType.DOCUMENT
    assert detect.classify_file(Path("/p/.github/workflows/ci.yml")) is detect.FileType.DOCUMENT


def test_get_extractor_routes_info_yaml_and_nothing_else():
    install()
    import graphify.extract as extract
    from graphify.drupal.yaml_extract import extract_drupal_info

    assert extract._get_extractor(Path("/p/foo.info.yml")) is extract_drupal_info
    assert extract._get_extractor(Path("/p/docker-compose.yml")) is None
    assert extract._get_extractor(Path("/p/a.py")) is extract.extract_python


def test_patching_twice_does_not_stack_wrappers():
    install()
    import graphify.detect as detect

    first = detect.classify_file
    _patch_detect(detect)
    assert detect.classify_file is first


def test_seam_fails_loudly_when_core_moves(monkeypatch):
    """An upstream rename must crash, not silently drop every Drupal edge."""
    import graphify.detect as detect

    monkeypatch.delattr(detect, "classify_file", raising=True)
    with pytest.raises(DrupalSeamError, match="classify_file"):
        _patch_detect(detect)


def test_seam_fails_loudly_when_dispatch_is_replaced(monkeypatch):
    import graphify.extract as extract

    monkeypatch.setattr(extract, "_DISPATCH", None)
    monkeypatch.setattr(extract._get_extractor, "_drupal_patched", False, raising=False)
    with pytest.raises(DrupalSeamError, match="_DISPATCH"):
        _patch_extract(extract)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_seam.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphify.drupal.register'`

- [ ] **Step 3: Implement the seam**

```python
# graphify/drupal/register.py
"""The single point where graphify.drupal touches graphify core.

Nothing in core is edited. `install()` places a finder on `sys.meta_path` that
wraps the loader for `graphify.detect` and `graphify.extract`, applying the patch
immediately after each module finishes executing — and patches either directly if
it is already imported.

The hook exists rather than an eager `import graphify.extract` because
`graphify/__init__.py` is deliberately lazy: importing it costs 1 ms, importing
`graphify.extract` costs 809 ms, and `graphify install` must keep working before
heavy dependencies are present.

A side effect worth naming: a spawned `ProcessPoolExecutor` worker unpickles
`graphify.extract._extract_single_file`, which imports `graphify.extract`, which
imports the parent package first — so the hook is installed in the worker before
`extract` executes.
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


class DrupalSeamError(RuntimeError):
    """A graphify core structure the seam depends on is missing or changed."""


def _patch_detect(detect: ModuleType) -> None:
    from graphify.drupal.paths import is_drupal_info_yaml

    for attr in ("classify_file", "FileType"):
        if not hasattr(detect, attr):
            raise DrupalSeamError(
                f"graphify.detect.{attr} is missing — graphify core changed shape; "
                "graphify/drupal/register.py must be updated"
            )
    original = detect.classify_file
    if getattr(original, "_drupal_patched", False):
        return

    def classify_file(path: Path):
        if is_drupal_info_yaml(path):
            return detect.FileType.CODE
        return original(path)

    classify_file._drupal_patched = True
    classify_file.__wrapped__ = original
    detect.classify_file = classify_file


def _patch_extract(extract: ModuleType) -> None:
    from graphify.drupal.paths import is_drupal_info_yaml
    from graphify.drupal.yaml_extract import extract_drupal_info

    if not hasattr(extract, "_get_extractor"):
        raise DrupalSeamError(
            "graphify.extract._get_extractor is missing — graphify core changed shape; "
            "graphify/drupal/register.py must be updated"
        )
    # Not mutated in P0, but asserted as a canary: upstream plans to route
    # dispatch through the public registry, and that change must fail here
    # rather than silently produce a Drupal-free graph.
    if not isinstance(getattr(extract, "_DISPATCH", None), dict):
        raise DrupalSeamError(
            "graphify.extract._DISPATCH is missing or no longer a dict — dispatch "
            "was restructured upstream; graphify/drupal/register.py must be updated"
        )
    original = extract._get_extractor
    if getattr(original, "_drupal_patched", False):
        return

    def _get_extractor(path: Path):
        if is_drupal_info_yaml(path):
            return extract_drupal_info
        return original(path)

    _get_extractor._drupal_patched = True
    _get_extractor.__wrapped__ = original
    extract._get_extractor = _get_extractor


_PATCHERS = {
    "graphify.detect": _patch_detect,
    "graphify.extract": _patch_extract,
}


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, inner, fullname: str) -> None:
        self._inner = inner
        self._fullname = fullname

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._inner.exec_module(module)
        _PATCHERS[self._fullname](module)


class _DrupalFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname not in _PATCHERS:
            return None
        # Step aside so find_spec resolves through the real finders.
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader, fullname)
        return spec


def install() -> None:
    """Arrange for core to be patched, without importing it."""
    if not any(isinstance(f, _DrupalFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _DrupalFinder())
    for name, patch in _PATCHERS.items():
        module = sys.modules.get(name)
        if module is not None:
            patch(module)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_drupal_seam.py -q`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add graphify/drupal/register.py tests/test_drupal_seam.py
git commit -m "feat(drupal): patch core through a post-import hook with loud guard rails"
```

---

### Task 4: Wire the seam into the package, without making imports heavy

**Files:**
- Modify: `graphify/__init__.py`
- Test: `tests/test_drupal_import_cost.py`

**Interfaces:**
- Consumes: `install()` from Task 3.
- Produces: the seam is active for any process that imports `graphify`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_import_cost.py
"""`import graphify` must stay lazy after the Drupal seam is wired in.

graphify/__init__.py exists to be cheap: `graphify install` has to work before
tree-sitter and friends are installed. Measured costs are 1 ms for `graphify`
against 809 ms for `graphify.extract`, so the seam may install a hook but must
never import the modules it patches.
"""
from __future__ import annotations

import subprocess
import sys


def _probe(expr: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", f"import sys, graphify; print({expr})"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_importing_graphify_does_not_import_extract():
    assert _probe("'graphify.extract' in sys.modules") == "False"


def test_importing_graphify_does_not_import_detect():
    assert _probe("'graphify.detect' in sys.modules") == "False"


def test_importing_graphify_does_not_import_yaml():
    assert _probe("'yaml' in sys.modules") == "False"


def test_seam_is_installed_by_importing_graphify():
    expr = (
        "any(type(f).__name__ == '_DrupalFinder' for f in sys.meta_path)"
    )
    assert _probe(expr) == "True"


def test_extract_is_patched_on_first_import_not_before():
    code = (
        "import sys, graphify;"
        "import graphify.extract as e;"
        "print(getattr(e._get_extractor, '_drupal_patched', False))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_import_cost.py -q`
Expected: FAIL — `test_seam_is_installed_by_importing_graphify` returns `False`

- [ ] **Step 3: Wire it in**

Append to `graphify/__init__.py`, after the existing `__getattr__` definition:

```python
# The Drupal seam. `install()` imports only sys and importlib, so the 1 ms cost
# of importing this package is unchanged; the modules it patches are wrapped when
# and if they are imported. See graphify/drupal/register.py.
from graphify.drupal.register import install as _install_drupal  # noqa: E402

_install_drupal()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_drupal_import_cost.py -q`
Expected: PASS, 5 tests

- [ ] **Step 5: Run the whole suite — nothing upstream may regress**

Run: `uv run pytest tests/ -q --tb=short`
Expected: PASS, with no test file modified outside `tests/test_drupal_*.py`

- [ ] **Step 6: Commit**

```bash
git add tests/test_drupal_import_cost.py
git commit -m "test(drupal): guard that the seam keeps `import graphify` lazy"
git add graphify/__init__.py
git commit -m "core: install the Drupal seam from the package __init__"
```

---

### Task 5: Behave like core through the whole pipeline

This is one of the two tasks P0 exists for. If it fails, approach B is in
question and the fallback is side-car producers outside the package.

**Files:**
- Test: `tests/test_drupal_pipeline.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: nothing importable — this task is evidence.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_pipeline.py
"""P0's central bet: a runtime-registered extractor behaves like a built-in one.

Covers detect -> extract -> build, cache round-tripping, and the requirement that
non-Drupal YAML is completely unaffected.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.drupal.yaml_extract import extension_id

MODULE_INFO = """\
name: Foo
type: module
dependencies:
  - drupal:node
  - token
"""

THEME_INFO = "name: My Theme\ntype: theme\nbase theme: olivero\n"
CONTRIB_INFO = "name: Token\ntype: module\n"


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    files = {
        "web/modules/custom/foo/foo.info.yml": MODULE_INFO,
        "web/modules/contrib/token/token.info.yml": CONTRIB_INFO,
        "web/themes/custom/mytheme/mytheme.info.yml": THEME_INFO,
        "docker-compose.yml": "services:\n  web:\n    image: php:8.3\n",
        ".github/workflows/ci.yml": "name: CI\non: [push]\n",
        "web/modules/custom/foo/src/Foo.php": "<?php\nclass Foo {}\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_detect_classifies_info_yaml_as_code_and_other_yaml_as_document(corpus):
    from graphify.detect import detect

    result = detect(corpus)
    code = {Path(p).name for p in result["files"].get("code", [])}
    docs = {Path(p).name for p in result["files"].get("document", [])}
    assert {"foo.info.yml", "token.info.yml", "mytheme.info.yml"} <= code
    assert {"docker-compose.yml", "ci.yml"} <= docs
    assert "docker-compose.yml" not in code


def test_extract_produces_drupal_nodes_and_edges(corpus):
    from graphify.extract import extract

    paths = list(corpus.rglob("*.info.yml"))
    result = extract(paths, root=corpus)
    ids = {n["id"] for n in result["nodes"]}
    assert extension_id("foo") in ids
    assert extension_id("mytheme") in ids
    relations = {(e["source"], e["relation"], e["target"]) for e in result["edges"]}
    assert (extension_id("foo"), "depends_on_module", extension_id("node")) in relations
    assert (extension_id("mytheme"), "base_theme", extension_id("olivero")) in relations


def test_collect_files_does_not_reach_info_yaml(corpus):
    """Documents the boundary: collect_files walks `_DISPATCH` suffixes.

    `.info.yml` has suffix `.yml`, which is not a dispatch key, so the library
    walker does not return it. Drupal files reach extraction through `detect()`,
    which the CLI drives — this is why Task 5 tests the CLI and not this walker.
    """
    from graphify.extract import collect_files

    found = {p.name for p in collect_files(corpus, root=corpus)}
    assert "foo.info.yml" not in found
    assert "Foo.php" in found


def test_build_preserves_the_drupal_taxonomy(corpus):
    from graphify.build import build
    from graphify.extract import extract

    paths = list(corpus.rglob("*.info.yml"))
    graph = build([extract(paths, root=corpus)])
    node = graph.nodes[extension_id("foo")]
    assert node["file_type"] == "code"       # not rewritten to "concept"
    assert node["type"] == "drupal_module"   # taxonomy survived
    assert node["realm"] == "custom"
    assert node["layer"] == "extension"


def test_build_keeps_the_real_node_when_a_stub_shares_its_id(corpus):
    """`token` is both a dependency stub and a real extension in this corpus.

    If build() lets the external stub win, the producer must stop emitting stubs
    for targets present in the corpus and resolve them in a later pass instead.
    """
    from graphify.build import build
    from graphify.extract import extract

    paths = list(corpus.rglob("*.info.yml"))
    graph = build([extract(paths, root=corpus)])
    token = graph.nodes[extension_id("token")]
    assert token["file_type"] == "code"
    assert token.get("external") is not True
    assert token["realm"] == "contrib"


def _run_cli(corpus: Path) -> dict:
    """Run the real CLI and return the produced graph.json.

    `--code-only` keeps the run deterministic and offline by skipping the LLM
    document pass; `--no-gitignore` keeps a stray ignore file from hiding the
    fixture. Going through the CLI rather than calling a cache function directly
    is deliberate: it tests the incremental path the user actually gets,
    whichever cache layer implements it.
    """
    import json
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(corpus),
         "--code-only", "--no-gitignore"],
        capture_output=True, text=True, cwd=corpus,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((corpus / "graphify-out" / "graph.json").read_text(encoding="utf-8"))


def _relations(graph: dict) -> set[tuple[str, str, str]]:
    links = graph.get("links", graph.get("edges", []))
    return {(e["source"], e["relation"], e["target"]) for e in links if "relation" in e}


def test_rerunning_with_no_changes_reproduces_the_same_graph(corpus):
    first = _run_cli(corpus)
    second = _run_cli(corpus)
    assert {n["id"] for n in second["nodes"]} == {n["id"] for n in first["nodes"]}
    assert _relations(second) == _relations(first)


def test_changing_one_dependency_changes_exactly_one_edge(corpus):
    before = _relations(_run_cli(corpus))
    (corpus / "web/modules/custom/foo/foo.info.yml").write_text(
        MODULE_INFO.replace("  - token\n", "  - path_alias\n"), encoding="utf-8"
    )
    after = _relations(_run_cli(corpus))

    assert before - after == {
        (extension_id("foo"), "depends_on_module", extension_id("token"))
    }
    assert after - before == {
        (extension_id("foo"), "depends_on_module", extension_id("path_alias"))
    }
```

- [ ] **Step 2: Run tests and read the failures carefully**

Run: `uv run pytest tests/test_drupal_pipeline.py -q --tb=short`

These tests exercise core, not new code, so a failure is information rather than
a bug to code around. Three specific outcomes and what each means:

- `test_build_keeps_the_real_node_when_a_stub_shares_its_id` fails → `build()`
  lets the external stub overwrite the real node. Fix in
  `graphify/drupal/yaml_extract.py`: stop emitting stub nodes and let `build.py`
  create external placeholders for dangling endpoints itself (it already does
  this, tagging them `external` with `file_type="concept"`). Re-run Task 2's
  tests and update `test_dependency_targets_are_external_stubs` accordingly.
- `_run_cli` fails on an unrecognised flag → read the usage string in
  `graphify/cli.py` (the `extract` branch lists the accepted flags) and use the
  ones that exist. The requirement is only that the run be offline and
  deterministic; the exact flags are not the point of the test.
- `test_rerunning_with_no_changes_reproduces_the_same_graph` fails → the
  incremental path does not round-trip runtime-registered extractions.
  **Stop and report.** This is the failure mode that puts approach B in question,
  and the decision to fall back to side-car producers is the user's, not the
  implementer's.

- [ ] **Step 3: Make the tests pass**

Apply whichever correction step 2 identified. No new production code should be
needed if the pipeline behaves as intended — that is precisely what this task
measures.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest tests/ -q --tb=short`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_drupal_pipeline.py
git commit -m "test(drupal): prove the seam behaves like core through detect, extract, build and cache"
```

---

### Task 6: Survive process boundaries

The second of P0's two load-bearing tasks. Extraction runs through
`ProcessPoolExecutor`; under the `spawn` start method workers re-import modules
and would start unpatched. `extract.py` already carries a comment about the
Windows spawn re-import path, so this is a live concern rather than a
hypothetical.

**Files:**
- Test: `tests/test_drupal_spawn_workers.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: nothing importable — this task is evidence.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_spawn_workers.py
"""The seam must exist inside spawned subprocesses, not just the parent.

A spawned worker re-imports modules from scratch. It unpickles
graphify.extract._extract_single_file, which imports graphify.extract, which
imports the parent package graphify first — installing the hook before extract
executes. This test is the proof of that chain.
"""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

MODULE_INFO = "name: Foo\ntype: module\ndependencies:\n  - drupal:node\n"


def _probe_in_worker(_ignored):
    """Runs in a spawned subprocess: report whether core is patched there."""
    import graphify.detect as detect
    import graphify.extract as extract

    return (
        getattr(extract._get_extractor, "_drupal_patched", False),
        getattr(detect.classify_file, "_drupal_patched", False),
    )


def test_seam_is_active_in_a_spawned_worker():
    ctx = mp.get_context("spawn")
    with ctx.Pool(1) as pool:
        extract_patched, detect_patched = pool.map(_probe_in_worker, [None])[0]
    assert extract_patched is True
    assert detect_patched is True


def test_parallel_extraction_matches_sequential(tmp_path: Path):
    """Whatever start method the pool uses, the graph must not depend on it."""
    from graphify.extract import extract

    paths = []
    for i in range(12):
        path = tmp_path / f"web/modules/custom/m{i}/m{i}.info.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MODULE_INFO, encoding="utf-8")
        paths.append(path)

    parallel = extract(paths, root=tmp_path, parallel=True)
    sequential = extract(paths, root=tmp_path, parallel=False)

    assert {n["id"] for n in parallel["nodes"]} == {n["id"] for n in sequential["nodes"]}
    assert len(parallel["edges"]) == len(sequential["edges"])
    assert len(parallel["edges"]) == 12
```

`extract()`'s signature is
`extract(paths, cache_root=None, *, root=None, parallel=True, max_workers=None, ...)`,
so `parallel=False` is the supported way to force the sequential path.

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_drupal_spawn_workers.py -q --tb=short`

- [ ] **Step 3: If the worker is unpatched, fix it in `register.py`**

The mitigation is an explicit pool initialiser rather than relying on the import
chain. Do **not** patch `graphify/extract.py` to add one — that widens the core
diff to the file most likely to change upstream. Instead have `install()` also
register the hook through `multiprocessing.util.register_after_fork` and, for
spawn, verify whether the parent-package import chain holds; report back if
neither suffices, because the answer changes the seam design.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_drupal_spawn_workers.py -q`
Expected: PASS, 2 tests

- [ ] **Step 5: Commit**

```bash
git add tests/test_drupal_spawn_workers.py
git commit -m "test(drupal): prove the seam survives spawned extraction workers"
```

---

### Task 7: CI guard for the core allow-list

**Files:**
- Create: `.github/workflows/drupal-guard.yml`
- Create: `graphify/drupal/allowlist.txt`
- Test: `tests/test_drupal_allowlist.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `graphify/drupal/allowlist.txt` — one fnmatch pattern per line, read by both the workflow and the test.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drupal_allowlist.py
"""Every file this fork changes against upstream must be on the allow-list.

The point is to catch an accidental edit to graphify/extract.py at commit time
rather than three months later during a rebase.
"""
from __future__ import annotations

import subprocess
from fnmatch import fnmatch
from pathlib import Path

import pytest

ALLOWLIST = Path("graphify/drupal/allowlist.txt")


def _patterns() -> list[str]:
    return [
        line.strip()
        for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _changed_against_upstream() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "upstream/v8", "--name-only"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip("upstream/v8 not available in this checkout")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_allowlist_file_is_readable():
    assert _patterns()


def test_every_change_against_upstream_is_allowed():
    offenders = [
        path for path in _changed_against_upstream()
        if not any(fnmatch(path, pattern) for pattern in _patterns())
    ]
    assert offenders == [], (
        "These files diverge from upstream/v8 but are not on the allow-list in "
        f"{ALLOWLIST}: {offenders}. Either revert them or extend the allow-list "
        "deliberately — each entry widens the surface that a rebase can conflict on."
    )


def test_core_files_are_not_silently_editable():
    assert not any(fnmatch("graphify/extract.py", p) for p in _patterns())
    assert not any(fnmatch("graphify/detect.py", p) for p in _patterns())
    assert not any(fnmatch("graphify/build.py", p) for p in _patterns())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drupal_allowlist.py -q`
Expected: FAIL — `FileNotFoundError: graphify/drupal/allowlist.txt`

- [ ] **Step 3: Write the allow-list**

```
# Paths this fork may diverge from upstream/v8 on.
# Every entry widens the surface a rebase can conflict on. Add deliberately.
graphify/drupal/*
graphify/__init__.py
pyproject.toml
uv.lock
tests/test_drupal_*.py
docs/*
.github/workflows/drupal-guard.yml
.gitignore
```

- [ ] **Step 4: Add the workflow**

```yaml
# .github/workflows/drupal-guard.yml
name: drupal-guard

on: [push, pull_request]

jobs:
  allowlist:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Fetch upstream
        run: |
          git remote add upstream https://github.com/Graphify-Labs/graphify.git
          git fetch --no-tags upstream v8
          git branch -f upstream/v8 FETCH_HEAD || true
      - name: Check the core allow-list
        run: |
          python - <<'PY'
          import subprocess, sys
          from fnmatch import fnmatch
          from pathlib import Path

          patterns = [
              line.strip()
              for line in Path("graphify/drupal/allowlist.txt").read_text().splitlines()
              if line.strip() and not line.startswith("#")
          ]
          changed = subprocess.run(
              ["git", "diff", "FETCH_HEAD", "--name-only"],
              capture_output=True, text=True, check=True,
          ).stdout.split()
          offenders = [p for p in changed if not any(fnmatch(p, q) for q in patterns)]
          if offenders:
              print("Files diverging from upstream but not on the allow-list:")
              for path in offenders:
                  print(f"  {path}")
              sys.exit(1)
          print(f"OK: {len(changed)} changed file(s), all on the allow-list.")
          PY
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_drupal_allowlist.py -q`
Expected: PASS, 3 tests

- [ ] **Step 6: Verify the guard actually catches a violation**

```bash
echo "# deliberate violation" >> graphify/extract.py
uv run pytest tests/test_drupal_allowlist.py::test_every_change_against_upstream_is_allowed -q
git checkout graphify/extract.py
```

Expected: FAIL naming `graphify/extract.py`, then a clean tree after the checkout.

- [ ] **Step 7: Commit**

```bash
git add graphify/drupal/allowlist.txt tests/test_drupal_allowlist.py
git commit -m "feat(drupal): pin the core allow-list and test it"
git add .github/workflows/drupal-guard.yml
git commit -m "core: add CI guard rejecting undeclared divergence from upstream"
```

---

## Definition of done

- `uv run --frozen pytest tests/ -q --tb=short` passes, with no test file changed outside `tests/test_drupal_*.py`.
- `git diff upstream/v8 --name-only` stays inside `graphify/drupal/allowlist.txt`.
- `git log v8..HEAD --grep '^core:'` lists exactly the commits that touched upstream files.
- The two load-bearing questions have answers on record: the cache round-trips runtime-registered extractions (Task 5), and the seam is live inside spawned workers (Task 6).
