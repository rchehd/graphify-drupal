"""Finding how to run drush for a project, and running the collector through
it (spec S4).

`detect_runner` turns a project root into a `Runner`: an argv prefix that
runs drush, checked in the table's order (spec S4) -- the first match wins.
`run_php` hands that runner one PHP snippet through `php:eval`, never
touching the filesystem: the code is one argv element, never written to
disk. Both are exercised with `subprocess.run` monkeypatched; nothing here
calls a real drush.
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from graphify.drupal.boundary import install_map
from graphify.drupal.rc import read_rc_value

RC_COMMAND = "drupal.container.command"
RC_SERVICE = "drupal.container.service"
RC_ALIAS = "drupal.container.alias"

#: Candidate compose service names (spec S4's table), checked against a
#: compose file's `services:` keys.
_COMPOSE_CANDIDATES = ("php", "web", "app", "drupal", "cli", "php-fpm")

_COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")

#: substrings in drush's stderr that mean "the container/runtime isn't up",
#: mapped from the runner name that produced them to the command that starts
#: it (spec S4's `RunnerUnavailable`).
_NOT_RUNNING_MARKERS = ("not running", "cannot connect to the docker daemon", "is not currently running")

_START_COMMAND = {
    "ddev": "ddev start",
    "lando": "lando start",
    "compose": "docker compose up -d",
}


class RunnerError(RuntimeError):
    """Base for every error this module raises."""


class RunnerNotFound(RunnerError):
    """No marker (spec S4's table) matched `root`."""


class AmbiguousComposeService(RunnerError):
    """A compose file exists but the service to run drush in is not clear."""


class RunnerUnavailable(RunnerError):
    """The runner's tool is not installed, or its container/daemon is not up."""


class BootstrapFailed(RunnerError):
    """drush ran but exited non-zero, or timed out."""


@dataclass(frozen=True)
class Runner:
    name: str            # "config" | "ddev" | "lando" | "compose" | "alias" | "vendor"
    prefix: tuple[str, ...]   # argv that runs drush, e.g. ("ddev", "drush")
    cwd: Path


def _vendor_bin_drush(root: Path) -> Path | None:
    """`vendor/bin/drush` resolved under the composer root (falling back to
    `root` when there is none), or `None` when it does not exist."""
    imap = install_map(root)
    base = Path(imap.project_root) if imap is not None else root
    candidate = base / "vendor" / "bin" / "drush"
    return candidate.resolve() if candidate.is_file() else None


def _compose_file(root: Path) -> Path | None:
    for name in _COMPOSE_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _compose_service_names(compose_path: Path) -> list[str]:
    try:
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    services = data.get("services")
    if not isinstance(services, dict):
        return []
    return list(services.keys())


def _detect_compose(root: Path) -> Runner | None:
    compose_path = _compose_file(root)
    if compose_path is None:
        return None

    service_names = _compose_service_names(compose_path)
    rc_service = read_rc_value(root, RC_SERVICE)
    if rc_service:
        service = rc_service
    else:
        candidates = [name for name in service_names if name in _COMPOSE_CANDIDATES]
        if len(candidates) != 1:
            named = ", ".join(sorted(candidates)) if candidates else "none"
            raise AmbiguousComposeService(
                f"{compose_path.name} names more than one candidate service, or none: "
                f"{named} (checked {', '.join(_COMPOSE_CANDIDATES)}; set "
                f"{RC_SERVICE} in .graphifyrc to pick one)"
            )
        service = candidates[0]

    prefix = ("docker", "compose", "exec", "-T", service, "drush")
    return Runner(name="compose", prefix=prefix, cwd=root)


def _alias_files(root: Path) -> list[Path]:
    sites_dir = root / "drush" / "sites"
    if not sites_dir.is_dir():
        return []
    return sorted(sites_dir.glob("*.site.yml"))


def _alias_drush_binary(root: Path) -> str:
    """The fallback alias case prefers `vendor/bin/drush` when present."""
    vendor_drush = _vendor_bin_drush(root)
    return str(vendor_drush) if vendor_drush is not None else "drush"


def _detect_alias(root: Path) -> Runner | None:
    rc_alias = read_rc_value(root, RC_ALIAS)
    if rc_alias:
        return Runner(name="alias", prefix=("drush", f"@{rc_alias}"), cwd=root)

    files = _alias_files(root)
    if len(files) != 1:
        return None

    site = files[0].name[: -len(".site.yml")]
    try:
        data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or len(data) != 1:
        return None
    env = next(iter(data.keys()))
    if not isinstance(env, str):
        return None

    drush_bin = _alias_drush_binary(root)
    return Runner(name="alias", prefix=(drush_bin, f"@{site}.{env}"), cwd=root)


def detect_runner(root: Path, override: str | None = None) -> Runner:
    """A `Runner` for `root`, per spec S4's table -- the first match wins.
    `override` (`--runner-command`) wins over every marker, including
    `.graphifyrc`'s `drupal.container.command`."""
    root = Path(root)

    if override:
        return Runner(name="config", prefix=tuple(shlex.split(override)), cwd=root)

    rc_command = read_rc_value(root, RC_COMMAND)
    if rc_command:
        return Runner(name="config", prefix=tuple(shlex.split(rc_command)), cwd=root)

    if (root / ".ddev" / "config.yaml").is_file():
        return Runner(name="ddev", prefix=("ddev", "drush"), cwd=root)

    if (root / ".lando.yml").is_file():
        return Runner(name="lando", prefix=("lando", "drush"), cwd=root)

    compose_runner = _detect_compose(root)
    if compose_runner is not None:
        return compose_runner

    alias_runner = _detect_alias(root)
    if alias_runner is not None:
        return alias_runner

    vendor_drush = _vendor_bin_drush(root)
    if vendor_drush is not None:
        imap = install_map(root)
        cwd = Path(imap.project_root) if imap is not None else root
        return Runner(name="vendor", prefix=(str(vendor_drush),), cwd=cwd)

    raise RunnerNotFound(
        f"no way to run drush was found under {root}: no .ddev/config.yaml, .lando.yml, "
        f"compose file, drush/sites/*.site.yml alias, or vendor/bin/drush. Set "
        f"{RC_COMMAND} in .graphifyrc, or pass --runner-command, or use "
        f"--print-script to get the collector's PHP without running it."
    )


def _start_command_for(runner: Runner) -> str:
    return _START_COMMAND.get(runner.name, f"start {runner.name}")


def run_php(runner: Runner, code: str, timeout: float = 600.0) -> str:
    """Run `code` (one PHP snippet) through `runner` via drush's `php:eval`,
    and return stdout. Nothing is ever written to disk: `code` is passed as a
    single argv element."""
    argv = [*runner.prefix, "php:eval", code]

    try:
        result: Any = subprocess.run(
            argv, cwd=str(runner.cwd), capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RunnerUnavailable(f"{runner.prefix[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise BootstrapFailed(f"drush timed out after {timeout}s") from exc

    if result.returncode != 0:
        stderr = result.stderr or ""
        lower = stderr.lower()
        if any(marker in lower for marker in _NOT_RUNNING_MARKERS):
            raise RunnerUnavailable(
                f"{runner.name} is not running; start it with `{_start_command_for(runner)}`. "
                f"drush said: {stderr.strip()}"
            )
        last_lines = "\n".join(stderr.splitlines()[-40:])
        raise BootstrapFailed(last_lines or f"drush exited with status {result.returncode}")

    return result.stdout
