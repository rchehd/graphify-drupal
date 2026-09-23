"""Where Drupal configuration lives, recognised from the path and its directory.

Configuration carries no family suffix -- `system.site.yml` is named after the
object, not the kind -- so it is found by location (P1b spec §3.1). Every rule
here is local: the file's own path, its directory, and for splits the sync store
beside it. Results are cached per directory, because classification runs once per
file of the corpus.

Rule order matters. Test modules go first; then the fixed path rules
(`config/install|optional|schema`, recipes), because Drupal core's own
`core/config/install/` ships a `core.extension.yml`; then the sync marker; then
splits, which are only knowable from a sync store's split entities.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SPLIT_PREFIX = "config_split.config_split."
PATCH_PREFIX = "config_split.patch."

#: The one test tree kept: the only recipe corpus exercising config.actions/${input}.
_RECIPE_FIXTURES = ("core", "tests", "fixtures", "recipes")
_WEB_ROOT_NAMES = ("web", "docroot", "html", "public_html")


@dataclass(frozen=True)
class ConfigStore:
    kind: str
    directory: Path
    owner: str = ""
    split: str = ""
    language: str = ""


def clear_caches() -> None:
    """Forget every per-directory answer.

    The caches hold for one `extract()` run. `graphify watch` and the MCP server
    call it many times in one process, and a new `core.extension.yml`, split
    entity, `folder:` edit, `*.info.yml` or `.graphifyrc` must be seen by the next
    run; the seam calls this at the start of each.
    """
    for cached in (_owner, _rc_sync_dirs_from, _markers_under, _split_folders, _store_of_dir):
        cached.cache_clear()


def _in_tests(path: Path) -> bool:
    """True inside a Drupal test tree: a `tests` directory below an extension or core.

    Only a `tests` segment with an extension (`*.info.yml`) or a `core` directory
    above it counts, so a checkout that itself sits under some `…/tests/…`
    directory keeps its configuration.
    """
    parts = path.parts
    if "tests" not in parts:
        return False
    for i in range(len(parts) - len(_RECIPE_FIXTURES) + 1):
        if parts[i:i + len(_RECIPE_FIXTURES)] == _RECIPE_FIXTURES:
            return False
    # `_owner` is also true of a directory named `core`: Drupal core's own.
    return any(part == "tests" and any(_owner(Path(*parts[:j])) for j in range(2, i + 1))
               for i, part in enumerate(parts))


@lru_cache(maxsize=None)
def _owner(extension_dir: Path) -> str:
    """Machine name of the extension whose `config/` this is, or ''."""
    infos = sorted(extension_dir.glob("*.info.yml"))
    if infos:
        return infos[0].name[: -len(".info.yml")]
    # Drupal core's own config/ has no core.info.yml; P1 names that owner `core`.
    return "core" if extension_dir.name == "core" else ""


@lru_cache(maxsize=None)
def _rc_sync_dirs_from(rc: Path) -> frozenset[Path]:
    dirs: set[Path] = set()
    try:
        text = rc.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return frozenset()  # unreadable: no override, as if absent
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key.strip() != "drupal.config.sync":
            continue
        for part in val.split(","):
            if part.strip():
                dirs.add((rc.parent / part.strip()).resolve())
    return frozenset(dirs)


def _rc_sync_dirs(directory: Path) -> frozenset[Path]:
    for ancestor in (directory, *directory.parents):
        rc = ancestor / ".graphifyrc"
        if rc.is_file():
            return _rc_sync_dirs_from(rc)
    return frozenset()


def _is_sync_dir(directory: Path) -> bool:
    if (directory / "core.extension.yml").is_file():
        return True
    return directory.resolve() in _rc_sync_dirs(directory)


@lru_cache(maxsize=None)
def _markers_under(ancestor: Path) -> tuple[Path, ...]:
    """Sync stores one or two levels below `ancestor`."""
    hits = sorted(ancestor.glob("*/core.extension.yml")) + sorted(
        ancestor.glob("*/*/core.extension.yml"))
    return tuple(
        m.parent for m in hits
        if not _in_tests(m) and m.parent.name not in ("install", "optional")
    )


def _nearby_sync_dirs(directory: Path) -> list[Path]:
    found: list[Path] = []
    for ancestor in [directory, *directory.parents][:4]:
        if len(ancestor.parts) <= 2:  # never glob the filesystem root
            break
        for sync in _markers_under(ancestor):
            if sync not in found:
                found.append(sync)
    return found


def _web_root(sync_dir: Path) -> Path:
    """Drupal resolves a split `folder:` against DRUPAL_ROOT, the web root."""
    project = sync_dir.parent.parent
    for candidate in [project / name for name in _WEB_ROOT_NAMES] + [project]:
        if (candidate / "core" / "lib" / "Drupal.php").is_file():
            return candidate
    return sync_dir.parent


@lru_cache(maxsize=None)
def _split_folders(sync_dir: Path) -> tuple[tuple[str, Path], ...]:
    from graphify.drupal.yaml_common import load_drupal_yaml

    web = _web_root(sync_dir)
    folders: list[tuple[str, Path]] = []
    for entity in sorted(sync_dir.glob(f"{SPLIT_PREFIX}*.yml")):
        data, _ = load_drupal_yaml(entity)
        folder = (data or {}).get("folder")
        if not isinstance(folder, str) or not folder.strip():
            continue
        target = Path(folder.strip())
        split_id = entity.name[len(SPLIT_PREFIX):-len(".yml")]
        folders.append((split_id, (target if target.is_absolute() else web / target).resolve()))
    return tuple(folders)


@lru_cache(maxsize=None)
def _store_of_dir(directory: Path) -> ConfigStore | None:
    if directory.name in ("install", "optional") and directory.parent.name == "config":
        owner = _owner(directory.parent.parent)
        return ConfigStore(directory.name, directory, owner=owner) if owner else None
    if directory.name == "config" and (directory.parent / "recipe.yml").is_file() \
            and directory.parent.parent.name == "recipes":
        return ConfigStore("recipe", directory, owner=directory.parent.name)
    if _is_sync_dir(directory):
        return ConfigStore("sync", directory)
    resolved = directory.resolve()
    for sync in _nearby_sync_dirs(directory):
        for split_id, folder in _split_folders(sync):
            if folder == resolved:
                return ConfigStore("split", directory, split=split_id)
    return None


def config_store(path: Path) -> ConfigStore | None:
    """The store `path` belongs to, or None when it is not Drupal configuration."""
    if path.suffix != ".yml" or _in_tests(path):
        return None
    parent = path.parent
    if parent.parent.name == "language":
        base = _store_of_dir(parent.parent.parent)
        if base is None:
            return None
        return ConfigStore(base.kind, base.directory, base.owner, base.split, parent.name)
    if path.name == "recipe.yml":
        if parent.parent.name != "recipes":
            return None
        return ConfigStore("recipe", parent, owner=parent.name)
    if parent.name == "schema" and parent.parent.name == "config":
        if not path.name.endswith(".schema.yml"):
            return None
        owner = _owner(parent.parent.parent)
        return ConfigStore("schema", parent, owner=owner) if owner else None
    return _store_of_dir(parent)


def is_config_yaml(path: Path) -> bool:
    return config_store(path) is not None


def config_name(path: Path) -> str:
    return path.name[: -len(".yml")] if path.name.endswith(".yml") else path.name


def in_config_directory(path: Path) -> bool:
    """True for any file under `config/{install,optional,schema}/`.

    Such a file is never a P1 family file, even when it is excluded here as a
    test module's configuration: `menu_test/config/install/menu_test.links.action.yml`
    is a config object whatever its suffix says.
    """
    return path.parent.name in ("install", "optional", "schema") \
        and path.parent.parent.name == "config"
