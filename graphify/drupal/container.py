"""The container artifact: its path, its shape, and its staleness (spec S6).

This is the Python model of the artifact `container_collect.php` will write
(a later task): the path resolution rule, the JSON shape `validate` enforces,
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

    rc_path = root / ".graphifyrc"
    if rc_path.is_file():
        try:
            text = rc_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if key.strip() == RC_ARTIFACT:
                rel = val.strip()
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


def _registry_extra_files(registry: Any, extensions: dict[str, Path]) -> set[Path]:
    """Manager and service class files the four `src/` globs can miss: a
    manager or a plain service class sitting directly under `src/`, named by
    the registry rather than by one of those directory conventions."""
    files: set[Path] = set()
    for plugin_type in registry.types.values():
        class_file = Path(plugin_type.class_file)
        if class_file.is_file() and realm_of(class_file) == "custom":
            files.add(class_file)

    for _sid, (fqcn, provider) in registry.services.items():
        ext_dir = extensions.get(provider)
        if ext_dir is None or "\\" not in fqcn:
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
        files.update(_registry_extra_files(registry, extensions))

    files = {p for p in files if realm_of(p) == "custom"}
    return sorted(files)


# -- the host-computable half of the stamp -------------------------------------

def _sources_sha(root: Path, sources: list[Path]) -> tuple[str, dict[str, str]]:
    hashes: dict[str, str] = {}
    for path in sources:
        try:
            content = path.read_bytes()
        except OSError:
            continue
        try:
            relpath = path.relative_to(root).as_posix()
        except ValueError:
            relpath = path.as_posix()
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


def compute_host_stamp(root: Path) -> dict:
    """The stamp fields Python can compute on the host: `git_commit`,
    `git_dirty`, `composer_lock_sha`, `sources_sha`, and `sources` (the
    `{relpath: content sha256}` map `staleness` diffs to name changed files).
    Never carries an absolute path (spec S5.3)."""
    root = Path(root)
    sources = container_sources(root)
    sources_sha, hashes = _sources_sha(root, sources)
    return {
        "git_commit": _git_commit(root),
        "git_dirty": _git_dirty(root),
        "composer_lock_sha": _composer_lock_sha(root),
        "sources_sha": sources_sha,
        "sources": hashes,
    }


def staleness(artifact: Artifact, root: Path) -> tuple[str, list[str]]:
    """`("fresh", [])` when `artifact`'s stamp still matches the working tree
    at `root`; else `("stale", reasons)`. Reasons are `"composer.lock
    changed"` and `"N container source files changed: a, b, …"` (up to 10
    paths), per spec S6.3."""
    host = compute_host_stamp(root)
    old = artifact.stamp
    reasons: list[str] = []

    if old.get("composer_lock_sha") != host["composer_lock_sha"]:
        reasons.append("composer.lock changed")

    if old.get("sources_sha") != host["sources_sha"]:
        old_sources = old.get("sources") or {}
        new_sources = host["sources"]
        changed = sorted(
            p for p in (old_sources.keys() | new_sources.keys())
            if old_sources.get(p) != new_sources.get(p)
        )
        names = ", ".join(changed[:10])
        reasons.append(f"{len(changed)} container source files changed: {names}")

    return ("stale" if reasons else "fresh", reasons)
