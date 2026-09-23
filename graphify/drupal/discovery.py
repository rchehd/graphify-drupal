"""The plugin-type registry, learned from a Drupal site's plugin managers.

A plugin type is whatever a plugin manager discovers. Managers are found the
way Drupal's container finds them: every `*.services.yml` names a class, PSR-4
maps the class to a file, and the file's `extends`/`implements` chain decides
whether it is a manager. A second net tests every `src/**/*Manager.php` no
service reached, because some managers are built with `new` or swapped in by a
`ServiceProvider`. What the chain cannot follow becomes an `unresolved` entry,
never an exception.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from graphify.drupal.paths import extension_machine_name, is_drupal_info_yaml
from graphify.drupal.php_classes import Arg, PhpClass, read_php_class
from graphify.drupal.yaml_common import load_drupal_yaml
from graphify.ids import make_id

DEFAULT_PLUGIN_MANAGER = "Drupal\\Core\\Plugin\\DefaultPluginManager"
PLUGIN_MANAGER_INTERFACE = "Drupal\\Component\\Plugin\\PluginManagerInterface"

_PRUNED_DIRS = frozenset({"vendor", "node_modules", "tests", "Tests"})
#: Keys under `services:` that configure the file rather than name a service (P1).
_RESERVED_SERVICE_KEYS = frozenset({"_defaults", "_instanceof"})
_MAX_DEPTH = 16
_SERVICES_SUFFIX = ".services.yml"
_MANAGER_PREFIX = "plugin.manager."

_ANNOTATION_DISCOVERIES = frozenset({"AnnotatedClassDiscovery"})
_ATTRIBUTE_DISCOVERIES = frozenset({"AttributeClassDiscovery"})
_BOTH_DISCOVERIES = frozenset({"AttributeDiscoveryWithAnnotations"})


@dataclass(frozen=True)
class PluginType:
    plugin_type: str
    manager_class: str
    class_file: str          # absolute POSIX path
    line: int
    owner: str               # extension machine name ("core" for core/lib and core.services.yml)
    registered: bool
    manager_service: str = ""
    discovery: str = "annotation"   # annotation|attribute|yaml|mixed|dynamic
    subdir: str = ""
    interface: str = ""
    annotation_class: str = ""
    attribute_class: str = ""
    yaml_name: str = ""
    alter_hook: str = ""
    deferred_to: str = ""


@dataclass
class Registry:
    web_root: str | None
    types: dict[str, PluginType] = field(default_factory=dict)
    unresolved: list[dict[str, str]] = field(default_factory=list)
    extensions: dict[str, str] = field(default_factory=dict)
    root_yaml: list[str] = field(default_factory=list)

    def by_yaml_name(self) -> dict[str, PluginType]:
        """Non-deferred types that read `<ext>.<yaml_name>.yml` files."""
        return {t.yaml_name: t for t in self.types.values() if t.yaml_name and not t.deferred_to}

    def by_class_file(self) -> dict[str, list[PluginType]]:
        result: dict[str, list[PluginType]] = {}
        for t in self.types.values():
            result.setdefault(t.class_file, []).append(t)
        return result

    def to_json(self) -> dict:
        return {
            "web_root": self.web_root,
            "types": {k: asdict(v) for k, v in self.types.items()},
            "unresolved": [dict(u) for u in self.unresolved],
            "extensions": dict(self.extensions),
            "root_yaml": list(self.root_yaml),
        }

    @classmethod
    def from_json(cls, data: dict) -> Registry:
        return cls(
            web_root=data.get("web_root"),
            types={k: PluginType(**v) for k, v in (data.get("types") or {}).items()},
            unresolved=[dict(u) for u in data.get("unresolved") or []],
            extensions=dict(data.get("extensions") or {}),
            root_yaml=list(data.get("root_yaml") or []),
        )


def type_id(plugin_type: str) -> str:
    return make_id("drupal", "plugin_type", plugin_type)


def _has_core(path: Path) -> bool:
    return (path / "core" / "lib" / "Drupal.php").is_file()


def find_web_root(scan_root: Path) -> Path | None:
    """The directory holding `core/lib/Drupal.php`, looked for at, below and above `scan_root`."""
    root = Path(scan_root).absolute()
    if _has_core(root):
        return root
    for child in ("web", "docroot"):
        if _has_core(root / child):
            return root / child
    for ancestor in root.parents:
        if _has_core(ancestor):
            return ancestor
    return None


@dataclass
class _Walk:
    extensions: dict[str, str] = field(default_factory=dict)
    services: list[Path] = field(default_factory=list)
    managers: list[Path] = field(default_factory=list)
    providers: list[Path] = field(default_factory=list)
    root_yaml: list[str] = field(default_factory=list)


def _walk(base: Path, core_dir: Path | None) -> _Walk:
    found = _Walk()
    if core_dir is not None:
        found.extensions["core"] = core_dir.as_posix()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _PRUNED_DIRS and not d.startswith("."))
        directory = Path(dirpath)
        names = sorted(filenames)
        info = [n for n in names if is_drupal_info_yaml(Path(n))]
        exts = [extension_machine_name(Path(n)) for n in info]
        for ext in exts:
            found.extensions.setdefault(ext, directory.as_posix())
        is_core_dir = core_dir is not None and directory == core_dir
        in_src = "src" in directory.relative_to(base).parts
        for name in names:
            path = directory / name
            if name.endswith(_SERVICES_SUFFIX):
                found.services.append(path)
            if name.endswith("Manager.php") and in_src:
                found.managers.append(path)
            if name.endswith("ServiceProvider.php"):
                found.providers.append(path)
            if name.startswith(".") or not name.endswith(".yml") or name in info:
                continue
            if any(name.startswith(f"{ext}.") for ext in exts) or (is_core_dir and name.startswith("core.")):
                found.root_yaml.append(path.as_posix())
    return found


class _Builder:
    def __init__(self, web_root: Path | None, walk: _Walk) -> None:
        self.web_root = web_root
        self.walk = walk
        self.unresolved: list[dict[str, str]] = []
        self.types: dict[str, PluginType] = {}
        self._classes: dict[str, tuple[Path, PhpClass] | None] = {}
        self._reaches: dict[str, bool] = {}
        self._noted: set[tuple[str, str, str]] = set()
        self._psr4_misses: set[str] = set()
        self.psr4: list[tuple[str, Path]] = []
        for ext, directory in walk.extensions.items():
            if ext != "core":
                self.psr4.append((f"Drupal\\{ext}\\", Path(directory) / "src"))
        if web_root is not None:
            lib = web_root / "core" / "lib" / "Drupal"
            self.psr4.append(("Drupal\\Core\\", lib / "Core"))
            self.psr4.append(("Drupal\\Component\\", lib / "Component"))
        self.psr4.sort(key=lambda item: len(item[0]), reverse=True)

    # -- recording -----------------------------------------------------------

    def note(self, cls: str, file: str, reason: str) -> None:
        """Record an entry once: a class backing several services is one problem."""
        key = (cls, file, reason)
        if key not in self._noted:
            self._noted.add(key)
            self.unresolved.append({"class": cls, "file": file, "reason": reason})

    def parse_error(self, path: Path) -> None:
        self.note("", path.as_posix(), "parse_error")

    # -- classes -------------------------------------------------------------

    def psr4_path(self, fqcn: str) -> Path | None:
        for prefix, directory in self.psr4:
            if fqcn.startswith(prefix):
                rest = fqcn[len(prefix):]
                return directory / (rest.replace("\\", "/") + ".php") if rest else None
        return None

    def psr4_missed(self, fqcn: str) -> bool:
        """True when PSR-4 found no file for `fqcn`, or a file declaring another class."""
        return fqcn in self._psr4_misses

    def lookup(self, fqcn: str) -> tuple[Path, PhpClass] | None:
        if fqcn in self._classes:
            return self._classes[fqcn]
        self._classes[fqcn] = None
        path = self.psr4_path(fqcn)
        if path is None or not path.is_file():
            self._psr4_misses.add(fqcn)
            return None
        cls = read_php_class(path)
        if cls is None:
            self.parse_error(path)
        elif cls.fqcn == fqcn:
            self._classes[fqcn] = (path, cls)
        else:
            self._psr4_misses.add(fqcn)
        return self._classes[fqcn]

    def remember(self, path: Path, cls: PhpClass) -> None:
        self._classes.setdefault(cls.fqcn, (path, cls))

    def reaches_manager(self, fqcn: str, depth: int = 0) -> bool:
        """True when `fqcn` is, extends or implements a manager root."""
        if fqcn in (DEFAULT_PLUGIN_MANAGER, PLUGIN_MANAGER_INTERFACE):
            return True
        if fqcn in self._reaches:
            return self._reaches[fqcn]
        if depth >= _MAX_DEPTH:
            return False
        self._reaches[fqcn] = False           # a cycle ends here
        found = self.lookup(fqcn)
        result = False
        if found is not None:
            _path, cls = found
            result = any(self.reaches_manager(p, depth + 1) for p in (*cls.extends, *cls.implements))
        self._reaches[fqcn] = result
        return result

    def is_manager(self, cls: PhpClass) -> bool:
        return cls.kind == "class" and self.reaches_manager(cls.fqcn)

    def chain(self, cls: PhpClass) -> list[PhpClass]:
        """The class and its ancestor classes, nearest first, stopping before DefaultPluginManager.

        DefaultPluginManager's own `getDiscovery()` is the default rule 9 names,
        so it is not read as an override.
        """
        result = [cls]
        current = cls
        while len(result) < _MAX_DEPTH and current.kind != "interface" and current.extends:
            parent = current.extends[0]
            if parent == DEFAULT_PLUGIN_MANAGER:
                break
            found = self.lookup(parent)
            if found is None or found[1] in result:
                break
            current = found[1]
            result.append(current)
        return result

    # -- types ---------------------------------------------------------------

    def owner_of(self, path: Path) -> str:
        target = path.as_posix()
        if self.web_root is not None and target.startswith((self.web_root / "core" / "lib").as_posix() + "/"):
            return "core"
        best, best_len = "", -1
        for ext, directory in self.walk.extensions.items():
            if target.startswith(directory + "/") and len(directory) > best_len:
                best, best_len = ext, len(directory)
        return best

    def add_type(self, plugin_type: str, path: Path, cls: PhpClass, owner: str, service: str) -> None:
        if plugin_type in self.types:
            return
        facts = _facts(self.chain(cls))
        manager_file = path.as_posix()
        dynamic = facts.pop("_dynamic", False)
        deferred = facts["deferred_to"]
        if deferred and facts["discovery"] == "dynamic":
            # Deferred types are listed as deferred, not as unresolved; both deferred
            # families are YAML-defined and P5/P6 read them.
            facts["discovery"] = "yaml"
        elif dynamic:
            self.note(cls.fqcn, manager_file, "dynamic_discovery")
        self.types[plugin_type] = PluginType(
            plugin_type=plugin_type,
            manager_class=cls.fqcn,
            class_file=manager_file,
            line=cls.line,
            owner=owner,
            registered=bool(service),
            manager_service=service,
            **facts,
        )


def _fqcn_segments(value: str) -> list[str]:
    return value.lstrip("\\").split("\\")[:-1]


def _short(fqcn: str) -> str:
    return fqcn.rsplit("\\", 1)[-1]


def _facts(chain: list[PhpClass]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "discovery": "annotation", "subdir": "", "interface": "", "annotation_class": "",
        "attribute_class": "", "yaml_name": "", "alter_hook": "", "deferred_to": "",
    }
    args: tuple[Arg, ...] = next((c.construct_args for c in chain if c.construct_args is not None), ())
    if args and args[0].kind == "string":
        facts["subdir"] = args[0].value
    if len(args) > 3 and args[3].kind in ("class", "string"):
        facts["interface"] = args[3].value.lstrip("\\")
    for arg in args[4:]:
        if arg.kind not in ("class", "string"):
            continue
        segments = _fqcn_segments(arg.value)
        if "Attribute" in segments and not facts["attribute_class"]:
            facts["attribute_class"] = arg.value.lstrip("\\")
        elif "Annotation" in segments and not facts["annotation_class"]:
            facts["annotation_class"] = arg.value.lstrip("\\")
    facts["alter_hook"] = next((c.alter_info for c in chain if c.alter_info), "") or ""

    source = next((c for c in chain if c.has_get_discovery or c.construct_discoveries), None)
    manager = chain[0].fqcn
    directory_discovery = False
    if source is None:
        has_attr, has_ann = bool(facts["attribute_class"]), bool(facts["annotation_class"])
        facts["discovery"] = "mixed" if has_attr and has_ann else "attribute" if has_attr else "annotation"
    else:
        kinds: set[str] = set()
        literal = True
        # A getDiscovery() override that builds discoveries wins; a manager that
        # assigns `$this->discovery` in its constructor is read the same way.
        built = source.discoveries or source.construct_discoveries
        for d in built:
            short = _short(d.cls)
            if short == "YamlDiscovery" or short == "YamlDiscoveryDecorator":
                index = 0 if short == "YamlDiscovery" else 1
                kinds.add("yaml")
                if len(d.args) > index and d.args[index].kind == "string":
                    facts["yaml_name"] = facts["yaml_name"] or d.args[index].value
                else:
                    literal = False
            elif short == "YamlDirectoryDiscovery":
                kinds.add("yaml")
                directory_discovery = True
            elif short in _ANNOTATION_DISCOVERIES:
                kinds.add("annotation")
            elif short in _ATTRIBUTE_DISCOVERIES:
                kinds.add("attribute")
            elif short in _BOTH_DISCOVERIES:
                kinds.update(("annotation", "attribute"))
        if not kinds or not literal:
            facts["discovery"] = "dynamic"
            facts["_dynamic"] = True
        else:
            facts["discovery"] = next(iter(kinds)) if len(kinds) == 1 else "mixed"

    if facts["yaml_name"] == "component" or manager.endswith("\\ComponentPluginManager"):
        facts["deferred_to"] = "P5"
    elif directory_discovery or manager.endswith("\\MigrationPluginManager"):
        facts["deferred_to"] = "P6"
    return facts


def _service_class(sid: str, services: dict, depth: int = 0) -> str:
    """A service's class: `class:`, else an FQCN id, else its same-file parent's class."""
    definition = services.get(sid)
    if definition is None:
        definition = {}
    if not isinstance(definition, dict) or depth > _MAX_DEPTH:
        return ""
    cls = definition.get("class")
    if isinstance(cls, str) and cls:
        return cls.lstrip("\\")
    if "\\" in sid:
        return sid.lstrip("\\")
    parent = definition.get("parent")
    if isinstance(parent, str) and parent in services:
        return _service_class(parent, services, depth + 1)
    return ""


def _read_services(builder: _Builder, path: Path) -> set[str]:
    """Register the manager services one file declares; return every class it reached."""
    reached: set[str] = set()
    data, error = load_drupal_yaml(path)
    if error:
        builder.parse_error(path)
        return reached
    services = (data or {}).get("services")
    if not isinstance(services, dict):
        return reached
    stem = path.name[: -len(_SERVICES_SUFFIX)]
    for raw_sid, definition in services.items():
        sid = str(raw_sid)
        if sid in _RESERVED_SERVICE_KEYS or isinstance(definition, str):
            continue
        if isinstance(definition, dict) and ("alias" in definition or definition.get("abstract") is True):
            continue
        if definition is not None and not isinstance(definition, dict):
            continue
        fqcn = _service_class(sid, services)
        if not fqcn.startswith("Drupal\\"):
            continue
        found = builder.lookup(fqcn)
        if found is None:
            if builder.psr4_missed(fqcn):
                builder.note(fqcn, path.as_posix(), "psr4_unresolved")
            reached.add(fqcn)
            continue
        reached.add(fqcn)
        class_path, cls = found
        if builder.is_manager(cls):
            plugin_type = sid[len(_MANAGER_PREFIX):] if sid.startswith(_MANAGER_PREFIX) else sid
            builder.add_type(plugin_type, class_path, cls, stem, sid)
    return reached


def _read_provider(builder: _Builder, path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if "function alter(" in text and "->setClass(" in text:
        cls = read_php_class(path)
        builder.note(cls.fqcn if cls else "", path.as_posix(), "service_provider_alter")


def build_registry(scan_root: Path) -> Registry:
    """Learn every plugin type the site's managers define. Never raises on bad input."""
    scan_root = Path(scan_root).absolute()
    web_root = find_web_root(scan_root)
    base = web_root if web_root is not None else scan_root
    walk = _walk(base, web_root / "core" if web_root is not None else None)
    builder = _Builder(web_root, walk)
    if web_root is None:
        builder.note("", "", "no_drupal_core")

    reached: set[str] = set()
    for path in walk.services:
        reached |= _read_services(builder, path)
    for path in walk.providers:
        _read_provider(builder, path)
    for path in walk.managers:
        cls = read_php_class(path)
        if cls is None:
            builder.parse_error(path)
            continue
        builder.remember(path, cls)
        if cls.fqcn in reached or not builder.is_manager(cls):
            continue
        reached.add(cls.fqcn)
        builder.note(cls.fqcn, path.as_posix(), "not_a_service")
        builder.add_type(f"class:{cls.fqcn}", path, cls, builder.owner_of(path), "")

    return Registry(
        web_root=web_root.as_posix() if web_root is not None else None,
        types=builder.types,
        unresolved=builder.unresolved,
        extensions=walk.extensions,
        root_yaml=walk.root_yaml,
    )


# -- the registry in the pipeline --------------------------------------------
#
# `prepare_run` builds the registry once at the start of every `detect()` (see
# graphify/drupal/register.py's `_patch_detect`), keeps it in this process via
# `set_current`, and persists it to `<out>/drupal-discovery.json` so a spawned
# `ProcessPoolExecutor` extraction worker — a separate process that never runs
# `detect()` itself — can still read it through `current_registry()`.

ENV_VAR = "GRAPHIFY_DRUPAL_DISCOVERY"

_REGISTRY_FILENAME = "drupal-discovery.json"

_current: Registry | None = None
_previous: Registry | None = None

# current_registry() caches the file it loads from ENV_VAR by (path, mtime), so
# a worker process that calls it many times over one run reads the file once.
_env_cache_path: str | None = None
_env_cache_mtime: float | None = None
_env_cache_registry: Registry | None = None


def out_dir(root: Path, cache_root: Path | None = None) -> Path:
    """The graphify-out directory for this run, matching core's own resolution.

    Mirrors `graphify.cache.cache_dir`'s `_out if _out.is_absolute() else
    Path(location).resolve() / _out`, where `location` is `cache_root` when
    given, else `root` — so a `GRAPHIFY_OUT` override (relative or absolute)
    and an `extract --out`-style `cache_root` land the registry file exactly
    where the rest of graphify's output already goes.
    """
    from graphify.paths import GRAPHIFY_OUT

    location = cache_root if cache_root is not None else root
    out = Path(GRAPHIFY_OUT)
    return out if out.is_absolute() else Path(location).resolve() / out


def set_current(registry: Registry | None, previous: Registry | None = None) -> None:
    """Set this process's in-memory registry (and the prior run's, if any)."""
    global _current, _previous
    _current = registry
    _previous = previous


def current_registry() -> Registry | None:
    """This process's registry: in-memory if set, else loaded from ENV_VAR's file.

    The file load is cached by (path, mtime) so repeated calls in one process
    (e.g. many files handled by one extraction worker) cost one stat plus at
    most one read. Returns None when neither is available, or the file cannot
    be read/parsed.
    """
    global _env_cache_path, _env_cache_mtime, _env_cache_registry
    if _current is not None:
        return _current
    path = os.environ.get(ENV_VAR)
    if not path:
        return None
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    if (
        _env_cache_registry is not None
        and _env_cache_path == path
        and _env_cache_mtime == mtime
    ):
        return _env_cache_registry
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    registry = Registry.from_json(data)
    _env_cache_path, _env_cache_mtime, _env_cache_registry = path, mtime, registry
    return registry


def previous_registry() -> Registry | None:
    """The prior run's registry, as read by `prepare_run` before it overwrote
    the file. In-process only — a worker process never sees it, only the
    process that called `prepare_run` (i.e. that ran `detect()`)."""
    return _previous


def prepare_run(root: Path, cache_root: Path | None = None) -> Registry:
    """Build the registry for this run and make it visible to the pipeline.

    Reads `<out>/drupal-discovery.json` as the previous run's registry (ignored
    if missing or unreadable), builds the new registry with `build_registry`
    (which never raises), and tries to write it back to that same path with
    sorted keys and one-space indent. A write failure (read-only tree,
    permissions, ...) degrades to keeping the registry in-process only: no
    file is written, `ENV_VAR` is left untouched, and the run proceeds exactly
    as `build_registry` intends — never raising on bad input.
    """
    root = Path(root)
    target = out_dir(root, cache_root) / _REGISTRY_FILENAME

    previous: Registry | None = None
    try:
        previous = Registry.from_json(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError):
        previous = None

    registry = build_registry(root)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(registry.to_json(), sort_keys=True, indent=1),
            encoding="utf-8",
        )
        os.environ[ENV_VAR] = str(target.absolute())
    except OSError:
        pass

    set_current(registry, previous)
    return registry
