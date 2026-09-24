"""Realm resolution driven by composer, with P0's path rules as fallback.

`realm_of` never returns `unknown` -- every path is core, contrib, vendor, or
custom (spec S4.1). Composer, when present, is authoritative: `composer.lock`
lists every package with its type, and `composer.json`'s
`extra.installer-paths` says where each type (or exact package name) installs.
A path not covered by any installer-paths pattern, or with no composer.json at
all, falls back to the vendor directory and then to P0's `paths.resolve_realm`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from graphify.drupal.paths import DEFAULT_REALM_RULES, load_realm_rules, resolve_realm

# `discovery` imports `yaml_common`, which imports this module for `realm_of` --
# importing `find_web_root` at module load time would be a circular import, so
# it is imported lazily inside the functions that need it.

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
    project_root: str                          # dir holding composer.json (absolute POSIX)
    paths: tuple[tuple[str, str, str], ...]     # (absolute install dir, realm, reason), longest first
    error: str = ""                             # non-empty when composer.json/lock was unreadable


def _find_composer_json(path: Path) -> Path | None:
    """Nearest `composer.json` at or above `path`."""
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        composer_json = candidate / "composer.json"
        if composer_json.is_file():
            return composer_json
    return None


def _locate_web_root(path: Path):
    """`discovery.find_web_root`, tried from `path` and every ancestor above it.

    `find_web_root(scan_root)` only checks `scan_root/web` and
    `scan_root/docroot` for the exact directory it is given, not for each
    ancestor it walks up to -- so calling it once with a deep file (e.g. a
    path under `vendor/`, a sibling of the web root rather than an ancestor
    of it) misses a web root that is a *child* of some ancestor. Retrying at
    each level finds it without changing `find_web_root` itself.
    """
    from graphify.drupal.discovery import find_web_root

    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        web_root = find_web_root(candidate)
        if web_root is not None:
            return web_root
    return None


def _find_graphifyrc(path: Path) -> Path | None:
    """Nearest `.graphifyrc` at or above `path` (same search core's own
    `.graphifyrc` readers use)."""
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        rc = candidate / ".graphifyrc"
        if rc.is_file():
            return rc
    return None


def _substitute(pattern: str, package_name: str) -> str:
    vendor, _, name = package_name.partition("/")
    if not _:
        vendor, name = "", package_name
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
) -> str:
    """The install directory for one package, per spec S4.1, relative-resolved."""
    for pattern, selectors in installer_paths.items():
        if _selector_matches(selectors, package_type, package_name):
            relative = _substitute(pattern, package_name)
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
        install_dir = _install_dir_for(name, package_type, installer_paths, root)
        if install_dir is None:
            vendor, _, pkg_name = name.partition("/")
            install_dir = (Path(vendor_dir) / vendor / pkg_name).as_posix()
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
    when no `composer.json` exists at or above it. Cached per project root."""
    composer_json = _find_composer_json(Path(path).absolute())
    if composer_json is None:
        return None
    return _build_install_map(composer_json.parent.as_posix())


def boundary_dir(path: Path) -> tuple[str, str] | None:
    """`(realm, reason)` when `path` is or is inside a core/contrib/vendor
    install dir (after `.graphifyrc`'s `drupal.include`); else `None`."""
    abs_path = Path(path).absolute()
    imap = install_map(abs_path)
    rc = _find_graphifyrc(abs_path)
    included = included_realms(rc.parent) if rc is not None else frozenset()
    if imap is not None and not imap.error:
        target = abs_path.as_posix()
        for install_dir, realm, reason in imap.paths:
            if realm not in ("core", "contrib", "vendor"):
                continue
            if realm in included:
                continue
            if target == install_dir or target.startswith(install_dir + "/"):
                return (realm, reason)
        return None
    # No composer (or unreadable) -- fall back to path-rule realms.
    realm = resolve_realm(abs_path)
    if realm in ("core", "contrib") and realm not in included:
        return (realm, "path_rule")
    web_root = _locate_web_root(abs_path)
    if web_root is not None and "vendor" not in included:
        vendor_dir = (web_root.parent / "vendor").as_posix()
        target = abs_path.as_posix()
        if target == vendor_dir or target.startswith(vendor_dir + "/"):
            return ("vendor", "vendor_dir")
    return None


def realm_of(path: Path) -> str:
    """`core` | `contrib` | `vendor` | `custom`, per spec S4.1. Never `unknown`."""
    abs_path = Path(path).absolute()
    rc = _find_graphifyrc(abs_path)
    if rc is not None:
        rc_rules = load_realm_rules(rc.parent)
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
        target = abs_path.as_posix()
        for install_dir, realm, _reason in imap.paths:
            if target == install_dir or target.startswith(install_dir + "/"):
                return realm
        # Inside a composer project but not under any known install dir.
        return "custom"

    # No composer.json, or it (or the lock) was unreadable: path rules.
    realm = resolve_realm(abs_path)
    if realm in ("core", "contrib"):
        return realm
    web_root = _locate_web_root(abs_path)
    if web_root is not None:
        vendor_dir = (web_root.parent / "vendor").as_posix()
        target = abs_path.as_posix()
        if target == vendor_dir or target.startswith(vendor_dir + "/"):
            return "vendor"
    return "custom"


def included_realms(project_root: Path) -> frozenset[str]:
    """`.graphifyrc` `drupal.include = contrib, core, vendor` (default empty)."""
    rc_path = Path(project_root) / ".graphifyrc"
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


def clear_caches() -> None:
    _build_install_map.cache_clear()
