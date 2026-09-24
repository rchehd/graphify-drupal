"""Boundary trees are not walked (spec S4.2).

`detect()` never descends a core, contrib or vendor install dir -- committed or
gitignored -- while the plugin registry, which is knowledge about the site
rather than graph content, still reads them. `.graphifyrc` `drupal.include`
opts realms back into the graph.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal.boundary import clear_caches
from graphify.drupal.register import DrupalSeamError, _patch_detect, install
from tests.test_drupal_discovery import D11_MANAGER, FOO_SERVICES, _module, _site
from tests.test_drupal_discovery_seam import CORE_BLOCK_MANAGER, _isolated_discovery_state  # noqa: F401

_INSTALLER_PATHS = {
    "web/core": ["type:drupal-core"],
    "web/modules/contrib/{$name}": ["type:drupal-module"],
    "web/modules/custom/{$name}": ["type:drupal-custom-module"],
}

_PACKAGES = [
    {"name": "drupal/core", "type": "drupal-core"},
    {"name": "drupal/token", "type": "drupal-module"},
    {"name": "symfony/yaml", "type": "library"},
]

TOKEN_SERVICES = (
    "services:\n"
    "  plugin.manager.token:\n"
    "    class: Drupal\\token\\TokenManager\n"
    "    parent: default_plugin_manager\n"
)
TOKEN_MANAGER = D11_MANAGER.replace("foo", "token").replace("Foo", "Token")

BOUNDARY_PARTS = ("/web/core/", "/web/modules/contrib/", "/vendor/")


def _composer_site(root: Path, *, gitignore: bool = False, include: str | None = None) -> Path:
    files = {
        **_module("foo", FOO_SERVICES, {"src/FooManager.php": D11_MANAGER}),
        "web/core/core.services.yml":
            "services:\n  plugin.manager.block:\n    class: Drupal\\Core\\Block\\BlockManager\n",
        "web/core/lib/Drupal/Core/Block/BlockManager.php": CORE_BLOCK_MANAGER,
        "web/modules/contrib/token/token.info.yml": "name: Token\ntype: module\n",
        "web/modules/contrib/token/token.services.yml": TOKEN_SERVICES,
        "web/modules/contrib/token/src/TokenManager.php": TOKEN_MANAGER,
        "vendor/symfony/yaml/Yaml.php": "<?php\nnamespace Symfony\\Component\\Yaml;\nclass Yaml {}\n",
        "composer.json": json.dumps({"extra": {"installer-paths": _INSTALLER_PATHS}}),
        "composer.lock": json.dumps({"packages": _PACKAGES, "packages-dev": []}),
    }
    if gitignore:
        files[".gitignore"] = "/web/core\n/web/modules/contrib\n/vendor\n"
    if include is not None:
        files[".graphifyrc"] = f"drupal.include = {include}\n"
    return _site(root, files)


@pytest.fixture(autouse=True)
def _fresh_boundary_caches():
    clear_caches()
    yield
    clear_caches()


def _scanned(result: dict) -> list[str]:
    return [Path(f).as_posix() for files in result["files"].values() for f in files]


def _in_boundary(path: str) -> bool:
    return any(part in path for part in BOUNDARY_PARTS)


@pytest.mark.parametrize("gitignore_file", [False, True], ids=["committed", "gitignored"])
@pytest.mark.parametrize("honour_gitignore", [True, False], ids=["gitignore", "no-gitignore"])
def test_detect_never_lists_a_boundary_file(tmp_path, _isolated_discovery_state,
                                            gitignore_file, honour_gitignore):
    install()
    import graphify.detect as detect
    from graphify.drupal.discovery import current_registry

    root = _composer_site(tmp_path, gitignore=gitignore_file)
    result = detect.detect(root, gitignore=honour_gitignore)

    scanned = _scanned(result)
    assert not [f for f in scanned if _in_boundary(f)]
    assert any(f.endswith("/web/modules/custom/foo/src/FooManager.php") for f in scanned)
    assert any(f.endswith("/web/modules/custom/foo/foo.info.yml") for f in scanned)
    # The registry still reads core and contrib.
    assert {"block", "token", "foo"} <= set(current_registry().types)


def test_drupal_include_puts_a_realm_back_in_the_graph(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect

    root = _composer_site(tmp_path, include="contrib")
    scanned = _scanned(detect.detect(root))

    assert any(f.endswith("/web/modules/contrib/token/token.info.yml") for f in scanned)
    assert any(f.endswith("/web/modules/contrib/token/src/TokenManager.php") for f in scanned)
    assert not [f for f in scanned if "/web/core/" in f or "/vendor/" in f]


def test_include_core_and_vendor(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect

    root = _composer_site(tmp_path, include="core, vendor")
    scanned = _scanned(detect.detect(root))

    assert any(f.endswith("/web/core/lib/Drupal/Core/Block/BlockManager.php") for f in scanned)
    assert any(f.endswith("/vendor/symfony/yaml/Yaml.php") for f in scanned)
    assert not [f for f in scanned if "/web/modules/contrib/" in f]


def test_graphifyignore_still_applies_on_top(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect

    root = _composer_site(tmp_path, include="contrib")
    (root / ".graphifyignore").write_text("web/modules/contrib/token/src\n", encoding="utf-8")
    scanned = _scanned(detect.detect(root))

    assert any(f.endswith("/token/token.info.yml") for f in scanned)
    assert not [f for f in scanned if "/token/src/" in f]


def test_a_scan_of_the_web_root_prunes_too(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect

    root = _composer_site(tmp_path)
    scanned = _scanned(detect.detect(root / "web"))

    assert not [f for f in scanned if _in_boundary(f)]
    assert any(f.endswith("/foo/foo.info.yml") for f in scanned)


def test_a_non_drupal_tree_keeps_its_core_named_dirs(tmp_path, _isolated_discovery_state):
    """Outside a Drupal run the wrapper answers with core's original only: P0's
    path rules (`*/core/lib/*`, `*/modules/contrib/*`) must not prune a
    directory of an unrelated repository that happens to match them."""
    install()
    import graphify.detect as detect

    for rel in ("app/core/lib/engine.py", "app/modules/contrib/plugin.py", "vendor/lib.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")

    scanned = _scanned(detect.detect(tmp_path))
    for rel in ("app/core/lib/engine.py", "app/modules/contrib/plugin.py"):
        assert any(f.endswith(rel) for f in scanned), rel


def test_walking_registry_is_set_only_during_the_registry_walk(tmp_path, monkeypatch,
                                                               _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal import discovery

    seen: list[bool] = []
    real_walk = discovery._walk

    def spying_walk(*args, **kwargs):
        seen.append(discovery.walking_registry())
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(discovery, "_walk", spying_walk)
    assert discovery.walking_registry() is False
    detect.detect(_composer_site(tmp_path))
    assert seen == [True]
    assert discovery.walking_registry() is False


def test_walking_registry_is_cleared_when_the_walk_raises(tmp_path, monkeypatch):
    from graphify.drupal import discovery

    def exploding_walk(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(discovery, "_walk", exploding_walk)
    with pytest.raises(RuntimeError):
        discovery.build_registry(_composer_site(tmp_path))
    assert discovery.walking_registry() is False


def test_patch_detect_fails_loudly_without_is_noise_dir(monkeypatch):
    install()
    import graphify.detect as detect

    monkeypatch.delattr(detect, "_is_noise_dir", raising=True)
    with pytest.raises(DrupalSeamError, match=r"graphify\.detect\._is_noise_dir"):
        _patch_detect(detect)


def test_inventory_counts_the_pruned_install_dirs(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.inventory import current_inventory

    detect.detect(_composer_site(tmp_path))
    inventory = current_inventory()

    assert inventory["summary"]["boundary"] == {"core": 1, "contrib": 1, "vendor": 1}
    assert inventory["summary"]["boundary_reasons"] == {"composer": 2, "vendor_dir": 1}
    assert "composer_unreadable" not in inventory


def test_inventory_boundary_counts_honour_include(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.inventory import current_inventory

    detect.detect(_composer_site(tmp_path, include="contrib"))

    assert current_inventory()["summary"]["boundary"] == {"core": 1, "contrib": 0, "vendor": 1}


def test_inventory_without_composer_counts_path_rule_dirs(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.inventory import current_inventory

    _site(tmp_path, {
        **_module("foo", FOO_SERVICES),
        "web/modules/contrib/token/token.info.yml": "name: Token\ntype: module\n",
        "web/core/modules/node/node.info.yml": "name: Node\ntype: module\n",
    })
    result = detect.detect(tmp_path)
    summary = current_inventory()["summary"]

    assert not [f for f in _scanned(result) if "/modules/contrib/" in f or "/core/modules/" in f]
    assert summary["boundary"] == {"core": 1, "contrib": 1, "vendor": 0}
    assert summary["boundary_reasons"] == {"path_rule": 2}


def test_inventory_reports_an_unreadable_composer_lock(tmp_path, _isolated_discovery_state):
    install()
    import graphify.detect as detect
    from graphify.drupal.inventory import current_inventory

    root = _composer_site(tmp_path)
    (root / "composer.lock").write_text("{not json", encoding="utf-8")
    detect.detect(root)

    assert current_inventory()["composer_unreadable"].startswith("composer.lock unreadable")


def test_the_cli_graph_has_no_boundary_source_file(tmp_path):
    root = _composer_site(tmp_path / "site")
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--out", str(out)],
        capture_output=True, text=True, cwd=root,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    graph = json.loads(next(out.rglob("graph.json")).read_text(encoding="utf-8"))

    sources = {str(n.get("source_file") or "") for n in graph["nodes"]}
    assert not [s for s in sources if _in_boundary("/" + s.lstrip("/"))]
    assert any("web/modules/custom/foo/" in s for s in sources)
