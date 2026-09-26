"""The container artifact: its path, its shape, and its staleness (spec S6).

This is the Python model of the artifact `container_collect.php` produces
(`collect`, written by `main`, the `graphify drupal container` command): the
path resolution rule, the JSON shape `validate` enforces,
and the host-computable half of the stamp (`compute_host_stamp`) a build uses
to decide whether a committed artifact is still `fresh` for the working tree
it is laid over.

`container_sources` decides which files shape that stamp: the custom-realm
files (spec S4.1's `custom`, via `boundary.realm_of`) that a container build
actually depends on -- an extension's `*.info.yml`, `*.services.yml`,
`*.routing.yml`, its procedural files, and the PHP under `src/` Drupal's own
collectors read from (`*ServiceProvider`, `src/EventSubscriber/**`,
`src/Plugin/**`, `src/Hook/**`). When this process already built a plugin
registry (`discovery.current_registry()`), its `extensions` map names every
extension directory without a second walk, and its `types`/`services` name
manager and service class files a plain glob would miss (a manager or a
service class that lives directly under `src/` matches none of the four
glob patterns). Without a registry -- a bare `graphify drupal container` run,
or a process that never called `discovery.prepare_run` -- the same custom
extension directories are found by walking for `*.info.yml`.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphify.drupal.boundary import install_map, realm_of
from graphify.drupal.hooks import is_procedural_file
from graphify.drupal.paths import is_drupal_info_yaml
from graphify.drupal.rc import read_rc_value
from graphify.drupal.runners import detect_runner, run_php

SCHEMA_VERSION = 1
SOURCES = ("services", "aliases", "routes", "extensions", "hooks", "plugins", "subscribers")
ENV_ARTIFACT = "GRAPHIFY_DRUPAL_CONTAINER"
RC_ARTIFACT = "drupal.container.artifact"

#: SOURCES keys whose JSON type is a list; the rest are dicts (spec S6.2's shape).
_LIST_SOURCES = frozenset({"services", "routes", "extensions"})

_PRUNED_DIRS = frozenset({"vendor", "node_modules", ".git"})
_DEFAULT_ARTIFACT_NAME = "drupal-container.json"


class ArtifactError(ValueError):
    """The container artifact is missing, malformed, or fails validation.

    The message names what is wrong (which key, which type)."""


@dataclass(frozen=True)
class Artifact:
    data: dict            # the whole validated JSON
    path: Path

    @property
    def stamp(self) -> dict:
        return self.data.get("stamp", {})


def artifact_path(root: Path) -> Path:
    """Where the container artifact lives: `ENV_ARTIFACT` > `.graphifyrc`'s
    `RC_ARTIFACT` (resolved relative to `root`) > `root/drupal-container.json`."""
    root = Path(root)
    env = os.environ.get(ENV_ARTIFACT)
    if env:
        return Path(env)

    rel = read_rc_value(root, RC_ARTIFACT)
    if rel:
        return root / rel

    return root / _DEFAULT_ARTIFACT_NAME


def validate(data: object) -> dict:
    """`data` is a JSON object with `schema_version == SCHEMA_VERSION`, every
    `SOURCES` key, `"stamp"` and `"errors"`, and the right JSON type for each
    source. Raises `ArtifactError` naming the offending key otherwise."""
    if not isinstance(data, dict):
        raise ArtifactError(f"the container artifact must be a JSON object, got {type(data).__name__}")

    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ArtifactError(
            f"schema_version must be {SCHEMA_VERSION}, got {schema_version!r}")

    for key in (*SOURCES, "stamp", "errors"):
        if key not in data:
            raise ArtifactError(f"the container artifact is missing key {key!r}")

    if not isinstance(data["stamp"], dict):
        raise ArtifactError("stamp must be a JSON object")
    if not isinstance(data["errors"], list):
        raise ArtifactError("errors must be a JSON list")

    for key in SOURCES:
        expected = list if key in _LIST_SOURCES else dict
        if not isinstance(data[key], expected):
            raise ArtifactError(f"{key} must be a JSON {expected.__name__}, got {type(data[key]).__name__}")

    return data


def load_artifact(root: Path) -> Artifact | None:
    """The artifact at `artifact_path(root)`, or `None` when absent.

    Raises `ArtifactError` when the file exists but is not valid JSON or
    fails `validate`."""
    path = artifact_path(root)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactError(f"{path}: unreadable: {exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ArtifactError(f"{path}: not valid JSON: {exc}") from exc
    data = validate(raw)
    return Artifact(data=data, path=path)


# -- container_sources --------------------------------------------------------

_TOP_LEVEL_GLOBS = ("*.info.yml", "*.services.yml", "*.routing.yml")
_SRC_GLOBS = (
    "src/**/*ServiceProvider.php",
    "src/EventSubscriber/**/*.php",
    "src/Plugin/**/*.php",
    "src/Hook/**/*.php",
)


def _custom_extension_dirs_from_registry(registry: Any) -> set[Path]:
    return {Path(d) for d in registry.extensions.values() if d and realm_of(Path(d)) == "custom"}


def _custom_extension_dirs_by_walk(root: Path) -> set[Path]:
    """Every directory holding a `*.info.yml` whose realm is `custom`, found
    by walking `root` (used when no registry is available for this process)."""
    found: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        dirnames[:] = [d for d in sorted(dirnames) if d not in _PRUNED_DIRS and not d.startswith(".")]
        for name in filenames:
            if is_drupal_info_yaml(Path(name)) and realm_of(directory) == "custom":
                found.add(directory)
    return found


def _extension_source_files(ext_dir: Path) -> set[Path]:
    """The files `container_sources` cares about inside one extension
    directory: its own info/services/routing YAML, its procedural files, and
    the PHP under `src/` a P3 collector reads from."""
    files: set[Path] = set()
    for pattern in _TOP_LEVEL_GLOBS:
        files.update(p for p in ext_dir.glob(pattern) if p.is_file())
    for entry in ext_dir.iterdir():
        if entry.is_file() and is_procedural_file(entry):
            files.add(entry)
    for pattern in _SRC_GLOBS:
        files.update(p for p in ext_dir.glob(pattern) if p.is_file())
    return files


def _registry_extra_files(registry: Any, extensions: dict[str, Path],
                          custom_dirs: set[Path] | None = None) -> set[Path]:
    """Manager and service class files the four `src/` globs can miss: a
    manager or a plain service class sitting directly under `src/`, named by
    the registry rather than by one of those directory conventions. With
    `custom_dirs`, only services of an extension in it are looked at: a
    core or contrib service's class file is never custom, and asking
    `realm_of` for each of a site's thousand boundary services was most of
    the staleness check's cost (P3 final review, finding 4)."""
    files: set[Path] = set()
    for plugin_type in registry.types.values():
        class_file = Path(plugin_type.class_file)
        if class_file.is_file() and realm_of(class_file) == "custom":
            files.add(class_file)

    for _sid, (fqcn, provider) in registry.services.items():
        ext_dir = extensions.get(provider)
        if ext_dir is None or "\\" not in fqcn:
            continue
        if custom_dirs is not None and ext_dir not in custom_dirs:
            continue
        prefix = f"Drupal\\{provider}\\"
        if not fqcn.startswith(prefix):
            continue
        rest = fqcn[len(prefix):]
        class_file = ext_dir / "src" / (rest.replace("\\", "/") + ".php")
        if class_file.is_file() and realm_of(class_file) == "custom":
            files.add(class_file)
    return files


def container_sources(root: Path) -> list[Path]:
    """The custom-realm files a container build's stamp depends on (spec S6.3),
    sorted. Uses `discovery.current_registry()` when this process has one
    (its `extensions` map, plus `types`/`services` for manager and service
    class files a glob alone would miss); walks the tree for `*.info.yml`
    otherwise."""
    from graphify.drupal.discovery import current_registry

    root = Path(root)
    registry = current_registry()

    if registry is not None:
        ext_dirs = _custom_extension_dirs_from_registry(registry)
        extensions = {name: Path(d) for name, d in registry.extensions.items()}
    else:
        ext_dirs = _custom_extension_dirs_by_walk(root)
        extensions = {}

    files: set[Path] = set()
    for ext_dir in ext_dirs:
        files.update(_extension_source_files(ext_dir))
    if registry is not None:
        files.update(_registry_extra_files(registry, extensions, ext_dirs))

    files = {p for p in files if realm_of(p) == "custom"}
    return sorted(files)


# -- the host-computable half of the stamp -------------------------------------

def _composer_root(root: Path) -> Path:
    """The composer root `root` belongs to (`install_map`), else `root`: what
    `sources` keys are relative to, at collection and at build time alike,
    so a stamp taken at the composer root holds for a scan of `web/`."""
    imap = install_map(Path(root))
    return Path(imap.project_root) if imap is not None else Path(root)


def _sources_sha(anchor: Path, sources: list[Path]) -> tuple[str, dict[str, str]]:
    """(sha256 over the pairs, `{relpath: content sha256}`), each path relative
    to `anchor` (the composer root); one outside it is `../`-relative, never
    absolute (spec S5.3)."""
    hashes: dict[str, str] = {}
    try:
        base = Path(anchor).resolve()
    except (OSError, RuntimeError):
        base = Path(anchor).absolute()
    for path in sources:
        try:
            content = path.read_bytes()
        except OSError:
            continue
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            resolved = path.absolute()
        try:
            relpath = resolved.relative_to(base).as_posix()
        except ValueError:
            relpath = Path(os.path.relpath(resolved, base)).as_posix()
        hashes[relpath] = hashlib.sha256(content).hexdigest()

    digest = hashlib.sha256()
    for relpath in sorted(hashes):
        digest.update(f"{relpath}\0{hashes[relpath]}\n".encode("utf-8"))
    return digest.hexdigest(), hashes


def _run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _git_commit(root: Path) -> str | None:
    out = _run_git(root, "rev-parse", "HEAD")
    return out.strip() if out else None


def _git_dirty(root: Path) -> bool:
    out = _run_git(root, "status", "--porcelain")
    return bool(out and out.strip())


def _composer_lock_sha(root: Path) -> str | None:
    imap = install_map(root)
    if imap is None or imap.error:
        return None
    lock_path = Path(imap.project_root) / "composer.lock"
    try:
        return hashlib.sha256(lock_path.read_bytes()).hexdigest()
    except OSError:
        return None


def _host_sources(root: Path) -> dict:
    """The stamp fields a build recomputes: `composer_lock_sha`, `sources_sha`
    and `sources`. No git (a build uses neither `git_*` field)."""
    root = Path(root)
    sources_sha, hashes = _sources_sha(_composer_root(root), container_sources(root))
    return {
        "composer_lock_sha": _composer_lock_sha(root),
        "sources_sha": sources_sha,
        "sources": hashes,
    }


def compute_host_stamp(root: Path) -> dict:
    """The stamp fields Python can compute on the host: `git_commit`,
    `git_dirty`, `composer_lock_sha`, `sources_sha`, and `sources` (the
    `{relpath: content sha256}` map, relative to the composer root, that
    `staleness` diffs to name changed files). Never carries an absolute path
    (spec S5.3). Collection only: the git fields are informational."""
    root = Path(root)
    return {
        "git_commit": _git_commit(root),
        "git_dirty": _git_dirty(root),
        **_host_sources(root),
    }


#: How many changed paths a stale reason names (spec S6.3); the full list is
#: `check_staleness`'s third value.
_REASON_PATHS = 10


def check_staleness(artifact: Artifact, root: Path) -> tuple[str, list[str], list[str]]:
    """`staleness`, plus every changed container source file (relative to the
    composer root, sorted): the reason names only the first ten, and
    `divergence` marks `possibly_stale` from the full list. Runs no git."""
    host = _host_sources(root)
    old = artifact.stamp
    reasons: list[str] = []
    changed: list[str] = []

    if old.get("composer_lock_sha") != host["composer_lock_sha"]:
        reasons.append("composer.lock changed")

    if old.get("sources_sha") != host["sources_sha"]:
        old_sources = old.get("sources") or {}
        new_sources = host["sources"]
        changed = sorted(
            p for p in (old_sources.keys() | new_sources.keys())
            if old_sources.get(p) != new_sources.get(p)
        )
        names = ", ".join(changed[:_REASON_PATHS])
        reasons.append(f"{len(changed)} container source files changed: {names}")

    return ("stale" if reasons else "fresh", reasons, changed)


def staleness(artifact: Artifact, root: Path) -> tuple[str, list[str]]:
    """`("fresh", [])` when `artifact`'s stamp still matches the working tree
    at `root`; else `("stale", reasons)`. Reasons are `"composer.lock
    changed"` and `"N container source files changed: a, b, …"` (up to 10
    paths), per spec S6.3."""
    status, reasons, _changed = check_staleness(artifact, root)
    return status, reasons


# -- the collector and the command (spec S5, S10) ------------------------------

_COLLECTOR = Path(__file__).with_name("container_collect.php")
_OPEN_TAG = "<?php"

#: Fields holding a path, per source (spec S5.1: made relative to the composer
#: root, or `None` outside it).
_PATH_FIELDS = {"services": "file", "extensions": "path"}
_NESTED_PATH_SOURCES = ("hooks", "plugins", "subscribers")

_USAGE = ("usage: graphify drupal container [PATH] [--out FILE] [--print-script] "
          "[--runner-command CMD]")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collector_code(hook_names: list[str] | None = None) -> str:
    """The collector's PHP as `php:eval` takes it: no opening tag, with a
    header line `$GRAPHIFY_HOOKS = [...];` (the pre-11.1 hook fallback's list)."""
    text = _COLLECTOR.read_text(encoding="utf-8")
    if text.startswith(_OPEN_TAG):
        text = text[len(_OPEN_TAG):].lstrip("\n")
    header = "$GRAPHIFY_HOOKS = " + json.dumps(list(hook_names or []), separators=(",", ":")) + ";"
    return header + "\n" + text


def _parse_stdout(stdout: str) -> dict:
    """The collector's JSON object. drush or PHP can print notices before it,
    so the object is looked for from the first line that opens one."""
    text = (stdout or "").strip()
    candidates = [text]
    for i, line in enumerate(text.splitlines()):
        if i and line.lstrip().startswith("{"):
            candidates.append("\n".join(text.splitlines()[i:]))
            break
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
        raise ArtifactError(f"the collector printed a JSON {type(data).__name__}, not an object")
    head = text[:200] or "(nothing)"
    raise ArtifactError(f"the collector's output is not JSON: {head}")


def _relativizer(composer_root: str | None):
    prefix = composer_root.rstrip("/") + "/" if composer_root else None

    def rel(path):
        if not isinstance(path, str) or prefix is None:
            return None
        if path.startswith(prefix):
            return path[len(prefix):] or None
        return None
    return rel


def _strip_roots(data: dict, composer_root: str | None) -> None:
    """Every path made relative to the composer root, in place (spec S5.1)."""
    rel = _relativizer(composer_root)
    for source, key in _PATH_FIELDS.items():
        for entry in data.get(source) or []:
            if isinstance(entry, dict) and key in entry:
                entry[key] = rel(entry[key])
    for source in _NESTED_PATH_SOURCES:
        value = data.get(source)
        for entries in value.values() if isinstance(value, dict) else []:
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and "file" in entry:
                    entry["file"] = rel(entry["file"])
    # An error message can quote a path; it never carries the machine's root.
    for error in data.get("errors") or []:
        if isinstance(error, dict) and composer_root and isinstance(error.get("message"), str):
            error["message"] = error["message"].replace(composer_root.rstrip("/") + "/", "")


def _stable(data: dict) -> dict:
    """A stable order for every list whose order carries no meaning. Hook
    lists (execution order) and subscriber lists (priority order) are kept."""
    def key(*fields):
        return lambda e: tuple(str(e.get(f) or "") for f in fields) if isinstance(e, dict) else ("",)

    data["services"] = sorted(data["services"], key=key("id"))
    data["routes"] = sorted(data["routes"], key=key("name"))
    data["extensions"] = sorted(data["extensions"], key=key("type", "name"))
    data["plugins"] = {t: sorted(v, key=key("id")) if isinstance(v, list) else v
                       for t, v in data["plugins"].items()}
    data["errors"] = sorted(data["errors"], key=key("source", "message"))
    return data


def _registry_hook_names(root: Path) -> list[str]:
    """The static registry's hook names, building the registry when this
    process has none (`main` points `prepare_run`'s out dir at a temp dir)."""
    from graphify.drupal import discovery

    registry = discovery.current_registry()
    if registry is None:
        registry = discovery.prepare_run(root)
    return sorted(registry.hooks) if registry is not None else []


def collect(root: Path, runner_override: str | None = None) -> dict:
    """Run the collector for the site at `root` and return the artifact:
    paths relative to the composer root, validated, with the stamp merged.
    Raises `RunnerError` (no runner, drush failed) or `ArtifactError` (the
    output is not a valid collector object)."""
    root = Path(root)
    runner = detect_runner(root, runner_override)
    code = collector_code(_registry_hook_names(root))
    data = _parse_stdout(run_php(runner, code))

    site = data.pop("site", None)
    site = site if isinstance(site, dict) else {}
    _strip_roots(data, site.get("composer_root") or site.get("drupal_root"))

    data["stamp"] = {
        "created_at": _now(),
        "runner": runner.name,
        "drupal_version": site.get("drupal_version"),
        "enabled_extensions_sha": site.get("enabled_extensions_sha"),
        **compute_host_stamp(root),
    }
    validate(data)
    return _stable(data)


def write_artifact(data: dict, path: Path) -> None:
    """Write `data` to `path` atomically: sorted keys, one-space indent, a
    temp file beside it, then `os.replace`."""
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _summary(data: dict, path: Path) -> str:
    lines = []
    for source in SOURCES:
        value = data.get(source)
        if isinstance(value, dict) and source in _NESTED_PATH_SOURCES:
            total = sum(len(v) for v in value.values() if isinstance(v, list))
            lines.append(f"  {source}: {len(value)} ({total} entries)")
        else:
            lines.append(f"  {source}: {len(value) if value is not None else 0}")
    errors = data.get("errors") or []
    lines.append(f"  errors: {len(errors)}")
    for error in errors:
        lines.append(f"    {error.get('source')}: {error.get('class')}: {error.get('message')}")
    lines.append(f"wrote {path}")
    return "\n".join(lines)


@contextlib.contextmanager
def _restoring_discovery():
    """A registry `prepare_run` makes inside the block does not outlive it,
    unless one was already current (its env var pointed at a temp dir)."""
    from graphify.drupal import discovery

    before = discovery.current_registry()
    saved = os.environ.get(discovery.ENV_VAR)
    try:
        yield
    finally:
        if before is None:
            discovery.set_current(None)
            if saved is None:
                os.environ.pop(discovery.ENV_VAR, None)
            else:
                os.environ[discovery.ENV_VAR] = saved


def main(argv: list[str]) -> int:
    """`container [PATH] [--out FILE] [--print-script] [--runner-command CMD]`.

    Returns 0 on success, 1 when drush or the artifact fails (message on
    stderr, no traceback, an existing artifact untouched), 2 on bad usage."""
    import argparse
    import sys

    from graphify.drupal.runners import RunnerError

    if not argv or argv[0] != "container":
        print(_USAGE, file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(prog="graphify drupal container", add_help=False)
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--out")
    parser.add_argument("--print-script", action="store_true")
    parser.add_argument("--runner-command")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        print(_USAGE, file=sys.stderr)
        return 2

    if args.print_script:
        print(_OPEN_TAG + "\n" + collector_code(), end="")
        return 0

    import tempfile

    from graphify.drupal import discovery

    root = Path(args.path).resolve()
    out = Path(args.out).resolve() if args.out else artifact_path(root)
    try:
        # The registry `collect` builds (`prepare_run`, for the hook list and
        # the stamp) goes to a temp dir: never beside the artifact, and never
        # into the site's own out dir, where the next build would read it as
        # its previous registry.
        with tempfile.TemporaryDirectory(prefix="graphify-drupal-container-") as scratch, \
                discovery.using_out_dir(Path(scratch)), _restoring_discovery():
            data = collect(root, args.runner_command)
        write_artifact(data, out)
    except (RunnerError, ArtifactError, OSError) as exc:
        print(f"graphify drupal container: {exc}", file=sys.stderr)
        return 1

    print(_summary(data, out))
    return 0
