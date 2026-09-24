"""Realm resolution driven by composer, with P0's path rules as fallback.

`realm_of` never returns `unknown` -- every path is core, contrib, vendor, or
custom (spec S4.1). Composer, when present, is authoritative: `composer.lock`
lists every package with its type, and `composer.json`'s
`extra.installer-paths` says where each type (or exact package name) installs.
A path not covered by any installer-paths pattern, or with no composer.json at
all, falls back to the vendor directory and then to P0's `paths.resolve_realm`.

A *project root* is a directory holding both `composer.json` and
`composer.lock` -- a `composer.json` on its own is just a package manifest (a
contrib module or a vendor package ships one to declare its own dependencies,
with no lock of its own) and is skipped in the search for one, not treated as
the project. `install_map` walks up from a path until it finds such a
directory, or runs out of ancestors.

`realm_of` and `boundary_dir` are called once per node/directory during a
normal run, so every ancestor search here (project root, `.graphifyrc`, web
root) is cached per starting directory; `clear_caches()` forgets all of them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from graphify.drupal.paths import DEFAULT_REALM_RULES, load_realm_rules, resolve_realm

# `discovery` imports `yaml_common`, which imports this module for `realm_of` --
# importing `find_web_root` at module load time would be a circular import, so
# it is imported lazily inside the function that needs it.

REALMS = ("core", "contrib", "vendor", "custom")

_RC_INCLUDE_KEY = "drupal.include"

#: Package type -> realm (spec S4.1).
_TYPE_REALM = {
    "drupal-core": "core",
    "drupal-module": "contrib",
    "drupal-theme": "contrib",
    "drupal-profile": "contrib",
    "drupal-recipe": "contrib",
    "drupal-drush": "contrib",
    "drupal-library": "contrib",
    "npm-asset": "contrib",
    "bower-asset": "contrib",
}


def _type_to_realm(package_type: str) -> str:
    if package_type in _TYPE_REALM:
        return _TYPE_REALM[package_type]
    if package_type.startswith("drupal-custom-"):
        return "custom"
    return "vendor"


@dataclass(frozen=True)
class InstallMap:
    project_root: str                          # dir holding composer.json+lock (absolute POSIX)
    paths: tuple[tuple[str, str, str], ...]     # (absolute install dir, realm, reason), longest first
    error: str = ""                             # non-empty when an existing composer.json/lock failed to parse


def _dir_of(path: Path) -> str:
    """The directory a lookup should be cached against: `path` itself when it
    already is one, else its parent."""
    return (path if path.is_dir() else path.parent).absolute().as_posix()


@lru_cache(maxsize=None)
def _project_root_for(directory: str) -> str | None:
    """Nearest ancestor of `directory` (inclusive) holding *both*
    `composer.json` and `composer.lock`. A `composer.json` with no lock beside
    it is a package's own manifest, not a project root -- kept walking past."""
    start = Path(directory)
    for candidate in (start, *start.parents):
        if (candidate / "composer.json").is_file() and (candidate / "composer.lock").is_file():
            return candidate.as_posix()
    return None


@lru_cache(maxsize=None)
def _graphifyrc_dir_for(directory: str) -> str | None:
    """Directory of the nearest `.graphifyrc` at or above `directory`."""
    start = Path(directory)
    for candidate in (start, *start.parents):
        if (candidate / ".graphifyrc").is_file():
            return candidate.as_posix()
    return None


@lru_cache(maxsize=None)
def _web_root_for(directory: str) -> str | None:
    """`discovery.find_web_root`, tried from `directory` and every ancestor
    above it.

    `find_web_root(scan_root)` only checks `scan_root/web` and
    `scan_root/docroot` for the exact directory it is given, not for each
    ancestor it walks up to -- so calling it once with a deep directory (e.g.
    under `vendor/`, a sibling of the web root rather than an ancestor of it)
    misses a web root that is a *child* of some ancestor. Retrying at each
    level finds it without changing `find_web_root` itself.
    """
    from graphify.drupal.discovery import find_web_root

    start = Path(directory)
    for candidate in (start, *start.parents):
        web_root = find_web_root(candidate)
        if web_root is not None:
            return web_root.as_posix()
    return None


@lru_cache(maxsize=None)
def _realm_rules_for(rc_dir: str) -> dict[str, tuple[str, ...]]:
    return load_realm_rules(Path(rc_dir))


@lru_cache(maxsize=None)
def _included_realms_for(rc_dir: str) -> frozenset[str]:
    rc_path = Path(rc_dir) / ".graphifyrc"
    if not rc_path.is_file():
        return frozenset()
    try:
        text = rc_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key.strip() != _RC_INCLUDE_KEY:
            continue
        return frozenset(p.strip() for p in val.split(",") if p.strip())
    return frozenset()


def _substitute(pattern: str, package_name: str, installer_name: str | None = None) -> str:
    vendor, sep, name = package_name.partition("/")
    if not sep:
        vendor, name = "", package_name
    if installer_name:
        name = installer_name
    return pattern.replace("{$vendor}", vendor).replace("{$name}", name)


def _selector_matches(selectors: object, package_type: str, package_name: str) -> bool:
    if not isinstance(selectors, list):
        return False
    for selector in selectors:
        if not isinstance(selector, str):
            continue
        if selector == package_name:
            return True
        if selector == f"type:{package_type}":
            return True
    return False


def _install_dir_for(
    package_name: str,
    package_type: str,
    installer_paths: dict,
    project_root: Path,
    installer_name: str | None = None,
) -> str:
    """The install directory for one package, per spec S4.1, relative-resolved.

    `installer_name` is composer's own `extra.installer-name` on the package
    -- a package can override the directory `{$name}` substitutes to (e.g.
    `drupal/nouislider_js` installs as `nouislider`, seen on the reference
    corpus)."""
    for pattern, selectors in installer_paths.items():
        if _selector_matches(selectors, package_type, package_name):
            relative = _substitute(pattern, package_name, installer_name)
            return (project_root / relative).absolute().as_posix()
    return None


@lru_cache(maxsize=None)
def _build_install_map(project_root: str) -> InstallMap:
    root = Path(project_root)
    composer_json_path = root / "composer.json"
    try:
        composer_json = json.loads(composer_json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return InstallMap(project_root=project_root, paths=(), error=f"composer.json unreadable: {exc}")

    composer_lock_path = root / "composer.lock"
    try:
        composer_lock = json.loads(composer_lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return InstallMap(project_root=project_root, paths=(), error=f"composer.lock unreadable: {exc}")

    installer_paths = composer_json.get("extra", {}).get("installer-paths", {})
    if not isinstance(installer_paths, dict):
        installer_paths = {}

    vendor_dir_rel = composer_json.get("config", {}).get("vendor-dir", "vendor")
    if not isinstance(vendor_dir_rel, str) or not vendor_dir_rel:
        vendor_dir_rel = "vendor"
    vendor_dir = (root / vendor_dir_rel).absolute().as_posix()

    entries: dict[str, tuple[str, str]] = {vendor_dir: ("vendor", "vendor_dir")}

    packages = []
    for key in ("packages", "packages-dev"):
        value = composer_lock.get(key)
        if isinstance(value, list):
            packages.extend(value)

    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        package_type = package.get("type", "library")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(package_type, str) or not package_type:
            package_type = "library"
        package_extra = package.get("extra")
        installer_name = package_extra.get("installer-name") if isinstance(package_extra, dict) else None
        if not isinstance(installer_name, str) or not installer_name:
            installer_name = None
        install_dir = _install_dir_for(name, package_type, installer_paths, root, installer_name)
        if install_dir is None:
            vendor, _sep, pkg_name = name.partition("/")
            install_dir = (Path(vendor_dir) / vendor / (installer_name or pkg_name)).as_posix()
        realm = _type_to_realm(package_type)
        # Longest match wins later; first writer for a given dir wins here too,
        # which only matters for exact duplicate install dirs (not expected).
        if install_dir not in entries:
            entries[install_dir] = (realm, "composer")

    sorted_paths = tuple(
        (install_dir, realm, reason)
        for install_dir, (realm, reason) in sorted(entries.items(), key=lambda kv: len(kv[0]), reverse=True)
    )
    return InstallMap(project_root=project_root, paths=sorted_paths, error="")


def install_map(path: Path) -> InstallMap | None:
    """The `InstallMap` for the composer project nearest `path`, or `None`
    when no ancestor holds both `composer.json` and `composer.lock`. Cached
    per project root."""
    project_root = _project_root_for(_dir_of(Path(path).absolute()))
    if project_root is None:
        return None
    return _build_install_map(project_root)


def _install_dir_match(imap: InstallMap, abs_path: Path) -> tuple[str, str] | None:
    """`(realm, reason)` of the install dir containing `abs_path`, or `None`
    when it is inside the composer project but under no known install dir."""
    target = abs_path.as_posix()
    for install_dir, realm, reason in imap.paths:
        if target == install_dir or target.startswith(install_dir + "/"):
            return (realm, reason)
    return None


def _fallback_realm(abs_path: Path) -> tuple[str, str]:
    """`(realm, reason)` when no usable composer install map applies: P0's
    path rules for core/contrib, a `vendor` directory that is a sibling of
    the web root, else custom. `reason` is `""` for the custom catch-all --
    it is never a boundary dir. Shared by `realm_of` and `boundary_dir` so
    the two stay in lockstep."""
    realm = resolve_realm(abs_path)
    if realm in ("core", "contrib"):
        return (realm, "path_rule")
    web_root = _web_root_for(_dir_of(abs_path))
    if web_root is not None:
        vendor_dir = (Path(web_root).parent / "vendor").as_posix()
        target = abs_path.as_posix()
        if target == vendor_dir or target.startswith(vendor_dir + "/"):
            return ("vendor", "vendor_dir")
    return ("custom", "")


def boundary_dir(path: Path) -> tuple[str, str] | None:
    """`(realm, reason)` when `path` is or is inside a core/contrib/vendor
    install dir (after `.graphifyrc`'s `drupal.include`); else `None`."""
    abs_path = Path(path).absolute()
    included = included_realms_of(abs_path)

    imap = install_map(abs_path)
    if imap is not None and not imap.error:
        match = _install_dir_match(imap, abs_path)
        if match is None:
            return None
        realm, reason = match
    else:
        realm, reason = _fallback_realm(abs_path)

    if realm not in ("core", "contrib", "vendor") or realm in included:
        return None
    return (realm, reason)


def realm_of(path: Path) -> str:
    """`core` | `contrib` | `vendor` | `custom`, per spec S4.1. Never `unknown`."""
    abs_path = Path(path).absolute()

    rc_dir = _graphifyrc_dir_for(_dir_of(abs_path))
    if rc_dir is not None:
        rc_rules = _realm_rules_for(rc_dir)
        # `.graphifyrc` `drupal.realm.*` wins, but only for the realm keys it
        # actually defines -- `load_realm_rules` only overrides those, and
        # `resolve_realm` on the result returns "unknown" for anything none of
        # the (possibly-overridden) rules cover, which we treat as "keep going".
        if rc_rules != DEFAULT_REALM_RULES:
            rc_realm = resolve_realm(abs_path, rc_rules)
            if rc_realm != "unknown":
                return rc_realm

    imap = install_map(abs_path)
    if imap is not None and not imap.error:
        match = _install_dir_match(imap, abs_path)
        if match is not None:
            return match[0]
        # Inside a composer project but not under any known install dir.
        return "custom"

    # No project root (or its json/lock failed to parse): path rules.
    realm, _reason = _fallback_realm(abs_path)
    return realm


def included_realms(project_root: Path) -> frozenset[str]:
    """`.graphifyrc` `drupal.include = contrib, core, vendor` (default empty)."""
    return _included_realms_for(Path(project_root).absolute().as_posix())


def included_realms_of(path: Path) -> frozenset[str]:
    """`included_realms` for the nearest `.graphifyrc` at or above `path`."""
    rc_dir = _graphifyrc_dir_for(_dir_of(Path(path).absolute()))
    if rc_dir is None:
        return frozenset()
    return _included_realms_for(rc_dir)


def clear_caches() -> None:
    for cached in (
        _build_install_map,
        _project_root_for,
        _graphifyrc_dir_for,
        _web_root_for,
        _realm_rules_for,
        _included_realms_for,
    ):
        cached.cache_clear()
