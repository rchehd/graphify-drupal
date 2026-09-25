"""The collector, `collect`, and the `graphify drupal container` command (spec S5, S10).

Nothing here runs drush: `run_php` and `detect_runner` are monkeypatched to
return a canned collector output built in the test, with absolute paths under
a fake in-container root `/var/www/html`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.drupal import container
from graphify.drupal.container import ArtifactError, collect, collector_code, main, write_artifact
from graphify.drupal.runners import BootstrapFailed, Runner
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401

_REPO = Path(__file__).resolve().parent.parent
_ROOT = "/var/www/html"
_CUSTOM = f"{_ROOT}/web/modules/custom/foo"


def _canned() -> dict:
    return {
        "schema_version": 1,
        "site": {
            "drupal_version": "11.4.7",
            "drupal_root": f"{_ROOT}/web",
            "composer_root": _ROOT,
            "enabled_extensions_sha": "e" * 64,
        },
        "services": [
            {"id": "foo.bar", "class": "Drupal\\foo\\Bar", "file": f"{_CUSTOM}/src/Bar.php",
             "arguments": ["entity_type.manager", "%app.root%"],
             "tags": [{"name": "event_subscriber", "attributes": {"priority": 5}}],
             "decorates": None, "provider": "foo"},
            {"id": "a.first", "class": "Outside", "file": "/usr/share/php/Outside.php",
             "arguments": [], "tags": [], "decorates": None, "provider": None},
        ],
        "aliases": {"Drupal\\foo\\Bar": "foo.bar"},
        "routes": [
            {"name": "foo.page", "path": "/foo", "provider": "foo",
             "defaults": {"_controller": "Drupal\\foo\\Controller\\FooController::page"},
             "requirements": {"_permission": "access content"}},
        ],
        "extensions": [
            {"name": "foo", "type": "module", "path": _CUSTOM, "status": 1, "weight": 0,
             "dependencies": ["system"]},
            {"name": "system", "type": "module", "path": f"{_ROOT}/web/core/modules/system",
             "status": 1, "weight": 0, "dependencies": []},
        ],
        "hooks": {"cron": [{"module": "foo", "callable": "foo_cron", "file": f"{_CUSTOM}/foo.module"}]},
        "plugins": {"block": [{"id": "foo_block", "class": "Drupal\\foo\\Plugin\\Block\\FooBlock",
                               "file": f"{_CUSTOM}/src/Plugin/Block/FooBlock.php", "provider": "foo",
                               "deriver": None, "base_plugin_id": None}]},
        "subscribers": {"kernel.request": [{"callable": "Drupal\\foo\\Sub::onRequest",
                                            "file": f"{_CUSTOM}/src/Sub.php", "priority": 0}]},
        "closures": {},
        "errors": [],
    }


@pytest.fixture
def fake_drush(monkeypatch):
    """`collect` sees a ddev runner whose `php:eval` prints `state["stdout"]`."""
    state = {"stdout": json.dumps(_canned()), "calls": []}

    def _detect(root, override=None):
        return Runner(name="ddev", prefix=("ddev", "drush"), cwd=Path(root))

    def _run(runner, code, timeout=600.0):
        state["calls"].append(code)
        if isinstance(state["stdout"], Exception):
            raise state["stdout"]
        return state["stdout"]

    monkeypatch.setattr(container, "detect_runner", _detect)
    monkeypatch.setattr(container, "run_php", _run)
    monkeypatch.setattr(container, "_now", lambda: "2026-09-25T12:00:00Z")
    return state


def test_collector_code_carries_the_hook_list_and_no_open_tag():
    code = collector_code(["cron"])
    assert code.startswith('$GRAPHIFY_HOOKS = ["cron"];')
    assert "<?php" not in code
    assert len(code.encode("utf-8")) < 64 * 1024


def test_collector_code_without_hooks_is_an_empty_list():
    assert collector_code().startswith("$GRAPHIFY_HOOKS = [];")


@pytest.mark.skipif(shutil.which("php") is None, reason="php is not installed")
def test_the_collector_is_valid_php(tmp_path):
    script = tmp_path / "collector.php"
    script.write_text("<?php\n" + collector_code(["cron", "form_alter"]), encoding="utf-8")
    result = subprocess.run(["php", "-l", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("php") is None, reason="php is not installed")
def test_the_collector_outside_drupal_prints_json_with_every_source_failed(tmp_path):
    """Without a booted Drupal every source throws; each lands in `errors[]`
    and the output is still one JSON object."""
    script = tmp_path / "collector.php"
    script.write_text("<?php\ndefine('DRUPAL_ROOT', '/nowhere');\n" + collector_code(["cron"]),
                      encoding="utf-8")
    result = subprocess.run(["php", str(script)], capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert data["schema_version"] == 1
    failed = {e["source"] for e in data["errors"]}
    assert {"services", "routes", "extensions", "hooks", "plugins", "subscribers"} <= failed
    assert data["services"] == [] and data["hooks"] == {}


_STUB = r"""<?php
namespace Drupal\Component\Plugin {
  interface PluginManagerInterface { public function getDefinitions(); }
}
namespace {
require getenv('STUB_BASE');
}
namespace Drupal\foo {
  class Bar { public function page() {} }
  class Sub { public function onRequest() {} }
  // Its listener is inherited from a class in another file, like a custom
  // `RouteSubscriberBase` subclass.
  class RouteSub extends \GStubBaseSubscriber {}
}
namespace {
define('DRUPAL_ROOT', getenv('STUB_ROOT') . '/web');
function foo_cron() {}

class GStubExt {
  public $info; public $status; public $weight;
  public function __construct(private $t, private $p, $status, $deps) {
    $this->status = $status; $this->info = ['dependencies' => $deps, 'name' => 'Secret label'];
  }
  public function getType() { return $this->t; }
  public function getPathname() { return $this->p; }
}
class GStubRoute {
  public function getPath() { return '/foo'; }
  public function getDefaults() { return ['_controller' => '\Drupal\foo\Bar::page', '_title' => 'Secret title']; }
  public function getRequirements() { return ['_permission' => 'access content', 'node' => '\d+', '_access_foo' => 'TRUE']; }
}
class Drupal {
  const VERSION = '11.4.7';
  public static $s = [];
  public static function service($id) { return self::$s[$id]; }
  public static function moduleHandler() { return self::$s['module_handler']; }
  public static function keyValue($c) { return self::$s['kv']; }
  public static function getContainer() { return self::$s['container']; }
}

$ref = (object) ['type' => 'service', 'id' => 'entity_type.manager', 'invalidBehavior' => 1];
$param = (object) ['type' => 'parameter', 'name' => 'app.root'];
$inner = (object) ['type' => 'private_service', 'id' => 'foo.bar.inner', 'value' => [], 'shared' => TRUE];
$anon = (object) ['type' => 'private_service', 'id' => 'private__abc', 'value' => [], 'shared' => FALSE];
$def = [
  'aliases' => ['old.bar' => 'foo.bar'],
  'services' => [
    'foo.bar' => serialize([
      'class' => 'Drupal\foo\Bar',
      'arguments' => (object) ['type' => 'collection', 'value' => [$ref, $param, 'secret-scalar', $inner, $anon, $ref]],
      'arguments_count' => 6,
    ]),
    'plugin.manager.block' => serialize(['class' => 'Stub\Manager', 'arguments_count' => 0]),
  ],
];
Drupal::$s['kernel'] = new class($def) {
  public function __construct(private $d) {}
  public function getCachedContainerDefinition() { return $this->d; }
};
Drupal::$s['container'] = new class { public function getServiceIds() { return ['plugin.manager.block', 'kernel']; } };
Drupal::$s['plugin.manager.block'] = new class implements \Drupal\Component\Plugin\PluginManagerInterface {
  public function getDefinitions() {
    return [
      'foo_block' => ['id' => 'foo_block', 'class' => 'Drupal\foo\Bar', 'provider' => 'foo', 'label' => 'Secret label'],
      'foo_block:x' => ['id' => 'foo_block', 'class' => 'Drupal\foo\Bar', 'provider' => 'foo', 'deriver' => 'Drupal\foo\Sub'],
    ];
  }
};
Drupal::$s['event_dispatcher'] = new class {
  public function getListeners() {
    return ['kernel.request' => [[new \Drupal\foo\Sub(), 'onRequest'], function () {}],
            'routing.route_alter' => [[new \Drupal\foo\RouteSub(), 'onAlterRoutes']]];
  }
  public function getListenerPriority($event, $listener) { return 5; }
};
Drupal::$s['module_handler'] = new class {
  public function getModuleList() { return ['foo' => 1, 'system' => 1]; }
  public function invokeAllWith($hook, callable $callback) {
    if ($hook === 'cron') { $callback('foo_cron', 'foo'); $callback([new \Drupal\foo\Sub(), 'onRequest'], 'foo'); }
  }
};
Drupal::$s['theme_handler'] = new class { public function listInfo() { return []; } };
Drupal::$s['kv'] = new class {
  public function get($key) {
    return getenv('STUB_D10') ? NULL : ['cron' => ['foo_cron' => 'foo', 'Drupal\foo\Sub::onRequest' => 'foo']];
  }
};
Drupal::$s['extension.list.module'] = new class {
  public function getList() {
    return ['foo' => new GStubExt('module', 'modules/custom/foo/foo.info.yml', 1, ['drupal:system', 'project:bar (>=1.0)'])];
  }
};
Drupal::$s['extension.list.theme'] = new class { public function getList() { return []; } };
Drupal::$s['extension.list.profile'] = new class { public function getList() { return []; } };
Drupal::$s['router.route_provider'] = new class { public function getAllRoutes() { return ['foo.page' => new GStubRoute()]; } };
}
"""


def _run_stub(tmp_path: Path, d10: bool = False) -> tuple[dict, str]:
    """The collector run by plain `php` over a stub `\\Drupal` (no drush)."""
    import os

    root = tmp_path / "site"
    (root / "web").mkdir(parents=True)
    (root / "composer.json").write_text("{}", encoding="utf-8")
    script = tmp_path / "stub.php"
    base = tmp_path / "base.php"
    base.write_text("<?php\nclass GStubBaseSubscriber { public function onAlterRoutes() {} }\n",
                    encoding="utf-8")
    # The collector goes in the global namespace block, after the stubs.
    script.write_text(_STUB.rstrip()[:-1] + collector_code(["cron"]) + "\n}\n", encoding="utf-8")
    env = {**os.environ, "STUB_ROOT": str(root), "STUB_BASE": str(base), **({"STUB_D10": "1"} if d10 else {})}
    result = subprocess.run(["php", str(script)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout), str(script)


@pytest.mark.skipif(shutil.which("php") is None, reason="php is not installed")
def test_the_collector_reads_a_stub_drupal(tmp_path):
    data, script = _run_stub(tmp_path)
    assert data["errors"] == []
    assert "Secret" not in json.dumps(data) and "secret-scalar" not in json.dumps(data)

    (service,) = [s for s in data["services"] if s["id"] == "foo.bar"]
    assert service["arguments"] == ["entity_type.manager", "%app.root%", "foo.bar.inner"]
    assert service["decorates"] == "old.bar"
    assert service["provider"] == "foo" and service["file"] == script
    assert data["aliases"] == {"old.bar": "foo.bar"}

    assert data["hooks"] == {"cron": [
        {"module": "foo", "callable": "foo_cron", "file": script},
        {"module": "foo", "callable": "Drupal\\foo\\Sub::onRequest", "file": script}]}

    block = {p["id"]: p for p in data["plugins"]["block"]}
    assert block["foo_block:x"]["base_plugin_id"] == "foo_block"
    assert block["foo_block:x"]["deriver"] == "Drupal\\foo\\Sub"
    assert block["foo_block"]["base_plugin_id"] is None and block["foo_block"]["file"] == script

    # A listener's file is its object's class file, not the file declaring
    # an inherited method: the subscriber is the class.
    assert data["subscribers"] == {
        "kernel.request": [{"callable": "Drupal\\foo\\Sub::onRequest", "file": script, "priority": 5}],
        "routing.route_alter": [{"callable": "Drupal\\foo\\RouteSub::onAlterRoutes", "file": script,
                                 "priority": 5}]}
    assert data["closures"] == {"kernel.request": 1}

    (route,) = data["routes"]
    assert route == {"name": "foo.page", "path": "/foo", "provider": "foo",
                     "defaults": {"_controller": "\\Drupal\\foo\\Bar::page"},
                     "requirements": {"_permission": "access content", "_access_foo": "TRUE"}}

    (ext,) = data["extensions"]
    root = str(tmp_path / "site")
    assert ext == {"name": "foo", "type": "module", "path": f"{root}/web/modules/custom/foo",
                   "status": 1, "weight": 0, "dependencies": ["system", "bar"]}
    import hashlib

    assert data["site"] == {"drupal_version": "11.4.7", "drupal_root": f"{root}/web",
                            "composer_root": root,
                            "enabled_extensions_sha": hashlib.sha256(b"module:foo").hexdigest()}


@pytest.mark.skipif(shutil.which("php") is None, reason="php is not installed")
def test_the_collector_falls_back_to_invoke_all_with_before_11_1(tmp_path):
    data, script = _run_stub(tmp_path, d10=True)
    assert data["hooks"] == {"cron": [
        {"module": "foo", "callable": "foo_cron", "file": script},
        {"module": "foo", "callable": "Drupal\\foo\\Sub::onRequest", "file": script}]}


def test_collect_makes_paths_relative_and_merges_the_host_stamp(tmp_path, fake_drush,
                                                                _isolated_discovery_state):
    data = collect(tmp_path)

    by_id = {s["id"]: s for s in data["services"]}
    assert by_id["foo.bar"]["file"] == "web/modules/custom/foo/src/Bar.php"
    assert by_id["a.first"]["file"] is None
    assert data["hooks"]["cron"][0]["file"] == "web/modules/custom/foo/foo.module"
    assert data["plugins"]["block"][0]["file"] == "web/modules/custom/foo/src/Plugin/Block/FooBlock.php"
    assert data["subscribers"]["kernel.request"][0]["file"] == "web/modules/custom/foo/src/Sub.php"
    assert {e["name"]: e["path"] for e in data["extensions"]} == {
        "foo": "web/modules/custom/foo", "system": "web/core/modules/system"}

    stamp = data["stamp"]
    assert stamp["runner"] == "ddev"
    assert stamp["created_at"] == "2026-09-25T12:00:00Z"
    assert stamp["drupal_version"] == "11.4.7"
    assert stamp["enabled_extensions_sha"] == "e" * 64
    for key in ("git_commit", "git_dirty", "composer_lock_sha", "sources_sha", "sources"):
        assert key in stamp
    assert "site" not in data
    assert _ROOT not in json.dumps(data)


def test_collect_passes_the_registry_hooks_to_the_collector(tmp_path, fake_drush, monkeypatch,
                                                            _isolated_discovery_state):
    from graphify.drupal import discovery

    registry = discovery.Registry(web_root=None)
    registry.hooks = {"cron": None, "form_alter": None}
    monkeypatch.setattr(discovery, "current_registry", lambda: registry)
    collect(tmp_path)
    assert fake_drush["calls"][0].startswith('$GRAPHIFY_HOOKS = ["cron","form_alter"];')


@pytest.mark.parametrize("stdout", ["", "PHP Warning: boom", "[1, 2]", '{"schema_version": 7}'])
def test_collect_rejects_garbage(tmp_path, fake_drush, stdout, _isolated_discovery_state):
    fake_drush["stdout"] = stdout
    with pytest.raises(ArtifactError):
        collect(tmp_path)


def test_collect_tolerates_noise_before_the_json(tmp_path, fake_drush, _isolated_discovery_state):
    fake_drush["stdout"] = "Deprecated: something\n" + json.dumps(_canned()) + "\n"
    assert collect(tmp_path)["services"]


def test_write_artifact_is_sorted_and_atomic(tmp_path):
    out = tmp_path / "a.json"
    write_artifact({"b": 1, "a": [1, 2]}, out)
    assert out.read_text(encoding="utf-8") == '{\n "a": [\n  1,\n  2\n ],\n "b": 1\n}\n'
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_main_writes_the_same_bytes_twice(tmp_path, fake_drush, capsys, _isolated_discovery_state):
    root = tmp_path / "site"
    root.mkdir()
    out = tmp_path / "out" / "drupal-container.json"
    # Unordered collector output: the file must not depend on it.
    shuffled = _canned()
    shuffled["services"].reverse()
    shuffled["extensions"].reverse()
    fake_drush["stdout"] = json.dumps(shuffled)

    assert main(["container", str(root), "--out", str(out)]) == 0
    first = out.read_bytes()
    fake_drush["stdout"] = json.dumps(_canned())
    assert main(["container", str(root), "--out", str(out)]) == 0
    assert out.read_bytes() == first

    data = json.loads(first)
    assert [s["id"] for s in data["services"]] == ["a.first", "foo.bar"]
    printed = capsys.readouterr().out
    assert "services: 2" in printed and str(out) in printed
    # No graphify-out/ appears in the site when --out is given.
    assert not (root / "graphify-out").exists()


def test_main_on_bootstrap_failure_keeps_the_previous_artifact(tmp_path, fake_drush, capsys,
                                                               _isolated_discovery_state):
    out = tmp_path / "drupal-container.json"
    out.write_text("previous", encoding="utf-8")
    fake_drush["stdout"] = BootstrapFailed("drush said no")

    assert main(["container", str(tmp_path), "--out", str(out)]) == 1
    assert out.read_text(encoding="utf-8") == "previous"
    err = capsys.readouterr().err
    assert "drush said no" in err and "Traceback" not in err


def test_main_on_garbage_returns_1(tmp_path, fake_drush, capsys, _isolated_discovery_state):
    fake_drush["stdout"] = "not json"
    assert main(["container", str(tmp_path), "--out", str(tmp_path / "x.json")]) == 1
    assert not (tmp_path / "x.json").exists()


def test_main_defaults_to_the_artifact_path(tmp_path, fake_drush, monkeypatch,
                                            _isolated_discovery_state):
    target = tmp_path / "elsewhere.json"
    monkeypatch.setenv(container.ENV_ARTIFACT, str(target))
    assert main(["container", str(tmp_path)]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == 1


def test_print_script(capsys):
    assert main(["container", "--print-script"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("<?php\n$GRAPHIFY_HOOKS = [];")


def test_main_rejects_an_unknown_subcommand(capsys):
    assert main(["bogus"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_the_cli_seam_runs_the_command():
    result = subprocess.run([sys.executable, "-m", "graphify", "drupal", "container", "--print-script"],
                            capture_output=True, text=True, cwd=_REPO, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("<?php")


def test_the_cli_seam_rejects_other_drupal_commands():
    result = subprocess.run([sys.executable, "-m", "graphify", "drupal", "bogus"],
                            capture_output=True, text=True, cwd=_REPO, timeout=120)
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_the_cli_seam_needs_dispatch_command(monkeypatch):
    import graphify.cli as cli
    from graphify.drupal.register import DrupalSeamError, _patch_cli

    monkeypatch.delattr(cli, "dispatch_command")
    with pytest.raises(DrupalSeamError, match="dispatch_command"):
        _patch_cli(cli)


def test_the_cli_seam_leaves_other_commands_to_core():
    import graphify.cli as cli
    from graphify.drupal.register import _dispatch_wrapper

    assert getattr(cli.dispatch_command, "_drupal_patched", False)
    seen = []
    _dispatch_wrapper(seen.append)("provider")
    assert seen == ["provider"]
