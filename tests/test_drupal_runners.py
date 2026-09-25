"""Runner detection and `php:eval` invocation (spec S4)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graphify.drupal.runners import (
    AmbiguousComposeService,
    BootstrapFailed,
    Runner,
    RunnerError,
    RunnerNotFound,
    RunnerUnavailable,
    detect_runner,
    run_php,
)


def _touch(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- detect_runner: markers ---------------------------------------------------


def test_ddev_marker_yields_ddev_drush_prefix(tmp_path):
    _touch(tmp_path, ".ddev/config.yaml", "name: site\n")
    runner = detect_runner(tmp_path)
    assert runner.name == "ddev"
    assert runner.prefix == ("ddev", "drush")
    assert runner.cwd == tmp_path


def test_lando_marker_yields_lando_drush_prefix(tmp_path):
    _touch(tmp_path, ".lando.yml", "name: site\n")
    runner = detect_runner(tmp_path)
    assert runner.name == "lando"
    assert runner.prefix == ("lando", "drush")


def test_vendor_bin_drush_marker(tmp_path):
    vendor_drush = _touch(tmp_path, "vendor/bin/drush", "#!/bin/sh\n")
    runner = detect_runner(tmp_path)
    assert runner.name == "vendor"
    assert runner.prefix == (str(vendor_drush.resolve()),)


def test_vendor_bin_drush_resolves_under_composer_root(tmp_path):
    _touch(tmp_path, "composer.json", "{}\n")
    _touch(tmp_path, "composer.lock", "{}\n")
    vendor_drush = _touch(tmp_path, "vendor/bin/drush", "#!/bin/sh\n")
    sub = tmp_path / "web"
    sub.mkdir(exist_ok=True)
    runner = detect_runner(sub)
    assert runner.name == "vendor"
    assert runner.prefix == (str(vendor_drush.resolve()),)


def test_no_marker_raises_runner_not_found(tmp_path):
    with pytest.raises(RunnerNotFound) as excinfo:
        detect_runner(tmp_path)
    message = str(excinfo.value)
    assert ".graphifyrc" in message
    assert "drupal.container.command" in message
    assert "--print-script" in message


def test_runner_not_found_is_a_runner_error(tmp_path):
    with pytest.raises(RunnerError):
        detect_runner(tmp_path)


# -- override and rc command win ---------------------------------------------


def test_override_wins_over_ddev(tmp_path):
    _touch(tmp_path, ".ddev/config.yaml", "name: site\n")
    runner = detect_runner(tmp_path, override="my-custom-drush-cmd")
    assert runner.name == "config"
    assert runner.prefix == ("my-custom-drush-cmd",)


def test_rc_command_wins_over_ddev(tmp_path):
    _touch(tmp_path, ".ddev/config.yaml", "name: site\n")
    _touch(tmp_path, ".graphifyrc", "drupal.container.command = ssh host drush\n")
    runner = detect_runner(tmp_path)
    assert runner.name == "config"
    assert runner.prefix == ("ssh", "host", "drush")


# -- compose -------------------------------------------------------------------


def _compose_with_services(root: Path, services: dict) -> None:
    import yaml

    _touch(root, "docker-compose.yml", yaml.safe_dump({"services": services}))


def test_compose_single_candidate_service(tmp_path):
    _compose_with_services(tmp_path, {"php": {"image": "php"}, "db": {"image": "mysql"}})
    runner = detect_runner(tmp_path)
    assert runner.name == "compose"
    assert runner.prefix == ("docker", "compose", "exec", "-T", "php", "drush")


def test_compose_two_candidates_raises_ambiguous(tmp_path):
    _compose_with_services(tmp_path, {"web": {"image": "a"}, "php": {"image": "b"}})
    with pytest.raises(AmbiguousComposeService) as excinfo:
        detect_runner(tmp_path)
    message = str(excinfo.value)
    assert "web" in message
    assert "php" in message


def test_compose_rc_service_resolves_ambiguity(tmp_path):
    _compose_with_services(tmp_path, {"web": {"image": "a"}, "php": {"image": "b"}})
    _touch(tmp_path, ".graphifyrc", "drupal.container.service = php\n")
    runner = detect_runner(tmp_path)
    assert runner.name == "compose"
    assert runner.prefix == ("docker", "compose", "exec", "-T", "php", "drush")


def test_compose_no_candidates_raises_ambiguous(tmp_path):
    _compose_with_services(tmp_path, {"db": {"image": "mysql"}})
    with pytest.raises(AmbiguousComposeService):
        detect_runner(tmp_path)


# -- alias ---------------------------------------------------------------------


def test_alias_single_file_single_env(tmp_path):
    _touch(tmp_path, "drush/sites/mysite.site.yml", "prod:\n  uri: 'https://example.com'\n")
    runner = detect_runner(tmp_path)
    assert runner.name == "alias"
    assert runner.prefix == ("drush", "@mysite.prod")


def test_alias_prefers_vendor_bin_drush_when_present(tmp_path):
    vendor_drush = _touch(tmp_path, "vendor/bin/drush", "#!/bin/sh\n")
    _touch(tmp_path, "drush/sites/mysite.site.yml", "prod:\n  uri: 'https://example.com'\n")
    runner = detect_runner(tmp_path)
    assert runner.name == "alias"
    assert runner.prefix == (str(vendor_drush.resolve()), "@mysite.prod")


def test_alias_rc_key_names_it(tmp_path):
    _touch(tmp_path, "drush/sites/mysite.site.yml", "prod:\n  uri: 'a'\ndev:\n  uri: 'b'\n")
    _touch(tmp_path, ".graphifyrc", "drupal.container.alias = mysite.dev\n")
    runner = detect_runner(tmp_path)
    assert runner.name == "alias"
    assert runner.prefix == ("drush", "@mysite.dev")


def test_alias_ambiguous_multiple_envs_falls_through(tmp_path):
    _touch(tmp_path, "drush/sites/mysite.site.yml", "prod:\n  uri: 'a'\ndev:\n  uri: 'b'\n")
    with pytest.raises(RunnerNotFound):
        detect_runner(tmp_path)


# -- run_php ---------------------------------------------------------------


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_php_argv_and_returns_stdout(tmp_path, monkeypatch):
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["cwd"] = kwargs.get("cwd")
        return _Result(returncode=0, stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(name="ddev", prefix=("ddev", "drush"), cwd=tmp_path)
    code = "<?php echo 1;"
    out = run_php(runner, code)
    assert out == '{"ok": true}'
    assert recorded["argv"][-2:] == ["php:eval", code]
    assert recorded["argv"][:2] == ["ddev", "drush"]
    assert recorded["cwd"] == str(tmp_path)


def test_run_php_creates_no_file(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        return _Result(returncode=0, stdout="ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(name="ddev", prefix=("ddev", "drush"), cwd=tmp_path)
    run_php(runner, "<?php echo 1;")
    assert list(tmp_path.iterdir()) == []


def test_run_php_file_not_found_raises_unavailable(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("ddev")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(name="ddev", prefix=("ddev", "drush"), cwd=tmp_path)
    with pytest.raises(RunnerUnavailable) as excinfo:
        run_php(runner, "<?php echo 1;")
    assert "not installed" in str(excinfo.value)


def test_run_php_ddev_not_running_raises_unavailable(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        return _Result(returncode=1, stderr="Error: ddev project is not currently running")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(name="ddev", prefix=("ddev", "drush"), cwd=tmp_path)
    with pytest.raises(RunnerUnavailable) as excinfo:
        run_php(runner, "<?php echo 1;")
    assert "ddev start" in str(excinfo.value)


def test_run_php_docker_daemon_not_running_raises_unavailable(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        return _Result(returncode=1, stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(name="compose", prefix=("docker", "compose", "exec", "-T", "php", "drush"), cwd=tmp_path)
    with pytest.raises(RunnerUnavailable) as excinfo:
        run_php(runner, "<?php echo 1;")
    assert "docker compose up -d" in str(excinfo.value)


def test_run_php_other_nonzero_exit_raises_bootstrap_failed(tmp_path, monkeypatch):
    stderr = "PHP Fatal error:  Uncaught Error: Call to undefined function foo()"

    def fake_run(argv, **kwargs):
        return _Result(returncode=1, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(name="ddev", prefix=("ddev", "drush"), cwd=tmp_path)
    with pytest.raises(BootstrapFailed) as excinfo:
        run_php(runner, "<?php echo 1;")
    assert "Fatal error" in str(excinfo.value)


def test_run_php_bootstrap_failed_carries_last_40_stderr_lines(tmp_path, monkeypatch):
    lines = [f"line {i}" for i in range(100)]
    stderr = "\n".join(lines)

    def fake_run(argv, **kwargs):
        return _Result(returncode=1, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(name="ddev", prefix=("ddev", "drush"), cwd=tmp_path)
    with pytest.raises(BootstrapFailed) as excinfo:
        run_php(runner, "<?php echo 1;")
    message = str(excinfo.value)
    assert "line 60" in message
    assert "line 99" in message
    assert "line 0" not in message


def test_run_php_timeout_raises_bootstrap_failed(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 600.0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = Runner(name="ddev", prefix=("ddev", "drush"), cwd=tmp_path)
    with pytest.raises(BootstrapFailed) as excinfo:
        run_php(runner, "<?php echo 1;", timeout=5.0)
    assert "timed out" in str(excinfo.value)
