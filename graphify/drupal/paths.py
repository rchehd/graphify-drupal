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
    # `*/core/tests/*` is not padding: Drupal core ships extension fixtures under
    # core/tests (module_handler_test, test_stable, ...). A bare `*/core/*` would
    # cover them, but it also claims any contrib module with a `core/` directory
    # of its own, and `core` is checked first — so the patterns stay specific.
    "core": (
        "*/core/modules/*",
        "*/core/themes/*",
        "*/core/profiles/*",
        "*/core/lib/*",
        "*/core/tests/*",
        # The `core` pseudo-extension has no *.info.yml; its own files sit
        # directly in core/, and core/assets holds the scaffold copies it ships.
        "*/core/core.*.yml",
        "*/core/assets/*",
        # Core's own default configuration and the recipes it ships.
        "*/core/config/*",
        "*/core/recipes/*",
    ),
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
