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
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from graphify.drupal.boundary import boundary_digest
from graphify.drupal.hooks import HookDecl, hook_dependent_files, hook_id, hook_pattern, read_hook_stubs
from graphify.drupal.paths import extension_machine_name, is_drupal_info_yaml
from graphify.drupal.php_classes import (
    Arg,
    ClassFacts,
    PhpClass,
    read_class_semantics,
    read_drupal_shortcuts,
    read_php_class,
    resolve_name,
)
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
    hooks: dict[str, HookDecl] = field(default_factory=dict)
    #: service id -> (class, provider extension), every `*.services.yml` (P2b §4.3).
    services: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: extension name -> (type "module"|"theme"|"profile", dir), every `*.info.yml`.
    extension_info: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: `\Drupal::` method -> service id, from `<web_root>/core/lib/Drupal.php` (P4 §7.1).
    shortcuts: dict[str, str] = field(default_factory=dict)
    #: class FQCN -> `ClassFacts.to_dict()`, every class of a custom extension (P4 §7.2).
    class_facts: dict[str, dict] = field(default_factory=dict)
    #: entity type id -> (provider, class FQCN), custom and boundary (P4 §5.4).
    entity_types: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: form id -> (provider, class FQCN), custom and boundary; base form ids too (P4 §6.1).
    forms: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: "Fqcn::NAME" -> event name, the string constants of classes named `*Events` (P4 §8).
    event_constants: dict[str, str] = field(default_factory=dict)
    #: alias -> the service id it names (`x: '@y'`, `x: {alias: y}`), every `*.services.yml`.
    service_aliases: dict[str, str] = field(default_factory=dict)
    #: custom service id -> `{"arguments": [service id or "" per position], "autowire": bool}`
    #: (P4 §7.3 rules 2 and 3).
    service_wiring: dict[str, dict] = field(default_factory=dict)

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
            "hooks": {k: asdict(v) for k, v in self.hooks.items()},
            "services": {k: list(v) for k, v in self.services.items()},
            "extension_info": {k: list(v) for k, v in self.extension_info.items()},
            "shortcuts": dict(self.shortcuts),
            "class_facts": {k: ClassFacts.from_dict(v).to_dict() for k, v in self.class_facts.items()},
            "entity_types": {k: list(v) for k, v in self.entity_types.items()},
            "forms": {k: list(v) for k, v in self.forms.items()},
            "event_constants": dict(self.event_constants),
            "service_aliases": dict(self.service_aliases),
            "service_wiring": {k: _wiring(v) for k, v in self.service_wiring.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> Registry:
        return cls(
            web_root=data.get("web_root"),
            types={k: PluginType(**v) for k, v in (data.get("types") or {}).items()},
            unresolved=[dict(u) for u in data.get("unresolved") or []],
            extensions=dict(data.get("extensions") or {}),
            root_yaml=list(data.get("root_yaml") or []),
            hooks={k: HookDecl(**v) for k, v in (data.get("hooks") or {}).items()},
            services={k: tuple(v) for k, v in (data.get("services") or {}).items()},
            extension_info={k: tuple(v) for k, v in (data.get("extension_info") or {}).items()},
            shortcuts=dict(data.get("shortcuts") or {}),
            class_facts={k: ClassFacts.from_dict(v).to_dict()
                         for k, v in (data.get("class_facts") or {}).items()},
            entity_types={k: tuple(v) for k, v in (data.get("entity_types") or {}).items()},
            forms={k: tuple(v) for k, v in (data.get("forms") or {}).items()},
            event_constants=dict(data.get("event_constants") or {}),
            service_aliases={str(k): str(v) for k, v in (data.get("service_aliases") or {}).items()},
            service_wiring={str(k): _wiring(v) for k, v in (data.get("service_wiring") or {}).items()},
        )


def _wiring(data: Any) -> dict:
    """A `service_wiring` entry in its one shape (JSON round trip, old files)."""
    data = data if isinstance(data, dict) else {}
    return {"arguments": [str(a) if isinstance(a, str) else "" for a in data.get("arguments") or []],
            "autowire": data.get("autowire") is True}


def type_id(plugin_type: str) -> str:
    return make_id("drupal", "plugin_type", plugin_type)


def type_attributes(t: PluginType) -> dict[str, Any]:
    """A `drupal_plugin_type` node's §4.1 attributes: the four always present,
    the rest only when non-empty. The in-graph node and the resolver's boundary
    stub (P2b §4.3) both carry exactly these."""
    attrs: dict[str, Any] = {
        "plugin_type": t.plugin_type,
        "discovery": t.discovery,
        "manager_class": t.manager_class,
        "registered": t.registered,
    }
    for key in ("manager_service", "subdir", "interface", "annotation_class",
                "attribute_class", "yaml_name", "alter_hook", "deferred_to"):
        value = getattr(t, key)
        if value:
            attrs[key] = value
    return attrs


def _owner(extensions: dict[str, str], path: Path) -> str:
    """The extension whose directory contains `path` (longest match). The
    pseudo-extension `core` is the web root's `core/` directory, so
    `core/lib/**` (and `core/core.*.yml`) is `core`'s: no extension lives
    under `core/lib`, so no longer match can take it away."""
    target = path.as_posix()
    best, best_len = "", -1
    for ext, directory in extensions.items():
        if target.startswith(directory + "/") and len(directory) > best_len:
            best, best_len = ext, len(directory)
    return best


def registry_owner_of(registry: Registry, path: Path) -> str:
    """`_Builder.owner_of`, usable at extraction time -- the builder itself
    does not survive past `build_registry`."""
    return _owner(registry.extensions, path)


#: `registry.by_class_file()` built once per registry object and cached as an
#: attribute of the registry itself -- the same pattern `yaml_plugins._by_yaml_name`
#: uses, and for the same reason: `is_manager_class_file` is on the extractor
#: dispatch's hot path (every `.php` file in a scan), and keying a module-level
#: dict by `id(registry)` would risk handing back a freed registry's stale entry.
_CLASS_FILE_CACHE_ATTR = "_drupal_class_file_cache"


def _class_file_types(registry: Registry, path: Path) -> list[PluginType]:
    cached = getattr(registry, _CLASS_FILE_CACHE_ATTR, None)
    if cached is None:
        cached = registry.by_class_file()
        setattr(registry, _CLASS_FILE_CACHE_ATTR, cached)
    return cached.get(Path(path).absolute().as_posix(), [])


def is_manager_class_file(path: Path) -> bool:
    """True when `path` is some plugin type's manager class file, per the
    current process's registry (spec §5.4)."""
    registry = current_registry()
    if registry is None:
        return False
    return bool(_class_file_types(registry, path))


def extract_plugin_types(path: Path) -> dict[str, Any]:
    """One `drupal_plugin_type` node (plus its edges) per type whose manager
    class file is `path`, using every non-empty §4.1 attribute. Empty for any
    other file -- including when there is no registry for this process."""
    from graphify.drupal.yaml_common import edge, node, service_id
    from graphify.drupal.yaml_extract import extension_id

    registry = current_registry()
    if registry is None:
        return {"nodes": [], "edges": []}
    types = _class_file_types(registry, path)
    if not types:
        return {"nodes": [], "edges": []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for t in types:
        tid = type_id(t.plugin_type)
        nodes.append(node(tid, t.plugin_type, type="drupal_plugin_type", layer="plugin",
                          path=path, line=t.line, **type_attributes(t)))
        if t.owner:
            # A second-net manager outside every extension has no owner; an
            # edge from `extension_id("")` would invent one.
            edges.append(edge(extension_id(t.owner), tid, "defines_plugin_type", path=path,
                              line=t.line, owner=t.owner, target_name=t.plugin_type))
        if t.registered:
            edges.append(edge(service_id(t.manager_service), tid, "plugin_manager_for",
                              path=path, line=t.line, source_name=t.manager_service,
                              target_name=t.plugin_type))
        if t.alter_hook:
            # `alterInfo('foo_info')` -> the manager invokes `foo_info_alter`
            # on every plugin definition it discovers (spec §5.3).
            alter_name = f"{t.alter_hook}_alter"
            edges.append(edge(tid, hook_id(alter_name), "invokes_hook", path=path, line=t.line,
                              target_name=alter_name))
    return {"nodes": nodes, "edges": edges}


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


_INFO_TYPE_RE = re.compile(r"(?m)^type:\s*([\w.]+)")
_API_PHP_SUFFIX = ".api.php"


def _read_extension_type(path: Path) -> str:
    """`type:` of `path`'s `*.info.yml`, or `"module"` when absent/unrecognised.

    A cheap regex scan, not a full YAML parse: an info.yml is a handful of
    lines and the registry walk reads one per extension (~1,140 on the
    reference corpus), so this must stay far under the per-file cost of
    `yaml.safe_load`.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "module"
    match = _INFO_TYPE_RE.search(text)
    if match:
        value = match.group(1).strip().strip("'\"")
        if value in ("module", "theme", "profile"):
            return value
    return "module"


@dataclass
class _Walk:
    extensions: dict[str, str] = field(default_factory=dict)
    services: list[Path] = field(default_factory=list)
    managers: list[Path] = field(default_factory=list)
    providers: list[Path] = field(default_factory=list)
    root_yaml: list[str] = field(default_factory=list)
    api_php: list[Path] = field(default_factory=list)
    extension_info: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Every `.php` file but `*.api.php`, for the P4 maps (`_learn_php`).
    php: list[Path] = field(default_factory=list)


def _walk(base: Path, core_dir: Path | None,
          is_ignored: Callable[[Path], bool] | None = None) -> _Walk:
    """One pass over `base`. A directory `is_ignored` rejects is never descended,
    and a file it rejects is never read.

    `is_ignored` is asked only about directories and the files the registry
    collects, never about every walked file: on the reference corpus a
    per-file check cost about 3 s a run."""
    found = _Walk()
    if core_dir is not None:
        found.extensions["core"] = core_dir.as_posix()

    def kept(path: Path) -> bool:
        return is_ignored is None or not is_ignored(path)

    for dirpath, dirnames, filenames in os.walk(base):
        directory = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _PRUNED_DIRS and not d.startswith(".") and kept(directory / d)
        )
        names = sorted(filenames)
        info = [n for n in names if is_drupal_info_yaml(Path(n)) and kept(directory / n)]
        exts = [extension_machine_name(Path(n)) for n in info]
        for ext in exts:
            found.extensions.setdefault(ext, directory.as_posix())
        for ext, info_name in zip(exts, info):
            if ext not in found.extension_info:
                found.extension_info[ext] = (
                    _read_extension_type(directory / info_name), directory.as_posix())
        is_core_dir = core_dir is not None and directory == core_dir
        in_src = "src" in Path(dirpath[len(str(base)):]).parts
        for name in names:
            path = directory / name
            if name.endswith(_SERVICES_SUFFIX):
                if kept(path):
                    found.services.append(path)
            elif name.endswith("ServiceProvider.php"):
                if kept(path):
                    found.providers.append(path)
            elif name.endswith("Manager.php") and in_src:
                if kept(path):
                    found.managers.append(path)
            elif name.endswith(_API_PHP_SUFFIX) and len(name) > len(_API_PHP_SUFFIX):
                if kept(path):
                    found.api_php.append(path)
            if name.endswith(".php") and not name.endswith(_API_PHP_SUFFIX):
                # Not asked `kept` here: `_learn_php` asks only for the files
                # its text precheck keeps (a per-file check costs seconds).
                found.php.append(path)
            if name.startswith(".") or not name.endswith(".yml") or name in info:
                continue
            if (any(name.startswith(f"{ext}.") for ext in exts)
                    or (is_core_dir and name.startswith("core."))) and kept(path):
                found.root_yaml.append(path.as_posix())
    return found


class _Builder:
    def __init__(self, web_root: Path | None, walk: _Walk) -> None:
        self.web_root = web_root
        self.walk = walk
        self.unresolved: list[dict[str, str]] = []
        self.types: dict[str, PluginType] = {}
        self.hooks: dict[str, HookDecl] = {}
        #: service id -> (class, provider extension); every service `_read_services` reaches.
        self.services: dict[str, tuple[str, str]] = {}
        #: alias -> service id, and custom service id -> wiring (`Registry`'s maps).
        self.aliases: dict[str, str] = {}
        self.wiring: dict[str, dict] = {}
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
        return _owner(self.walk.extensions, path)

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


def _fast_loader() -> type | None:
    """`DrupalYamlLoader`'s tag tolerance on libyaml's C loader, when present."""
    import yaml

    from graphify.drupal.yaml_common import _any_tag

    base = getattr(yaml, "CSafeLoader", None)
    if base is None:
        return None
    loader = type("_FastDrupalYamlLoader", (base,), {})
    yaml.add_multi_constructor("!", _any_tag, Loader=loader)
    return loader


_FAST_LOADER = _fast_loader()
_MAX_SERVICES_BYTES = 2 * 1024 * 1024


def _load_services_yaml(path: Path) -> tuple[dict | None, str | None]:
    """`load_drupal_yaml`, ten times faster on the ~250 services files a site
    has (about 0.6 s of the registry build on the reference corpus). Anything
    the C loader does not parse cleanly goes through `load_drupal_yaml`, so
    errors and edge cases are exactly P1's."""
    import yaml

    if _FAST_LOADER is None:
        return load_drupal_yaml(path)
    try:
        if path.stat().st_size > _MAX_SERVICES_BYTES:
            return load_drupal_yaml(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        data = yaml.load(text, Loader=_FAST_LOADER)
    except (OSError, yaml.YAMLError):
        return load_drupal_yaml(path)
    return (data, None) if isinstance(data, dict) else (None, None)


def _argument_ids(arguments: Any) -> list[str]:
    """Per position of a positional `arguments:` list, the service id of a
    `'@x'` / `'@?x'` reference, else "" (P4 §7.3 rule 2). A keyed map
    (named arguments) says nothing positional: an empty list."""
    if not isinstance(arguments, list):
        return []
    out: list[str] = []
    for arg in arguments:
        ref = arg[1:] if isinstance(arg, str) and arg.startswith("@") and not arg.startswith("@@") else ""
        out.append(ref[1:] if ref.startswith("?") else ref)
    return out


def _read_services(builder: _Builder, path: Path, custom: bool = False) -> set[str]:
    """Register the manager services one file declares; return every class it
    reached. Aliases are kept from every file, and each service's wiring
    (`arguments:`, `autowire`) from a `custom` one (P4 §7.3)."""
    reached: set[str] = set()
    data, error = _load_services_yaml(path)
    if error:
        builder.parse_error(path)
        return reached
    services = (data or {}).get("services")
    if not isinstance(services, dict):
        return reached
    stem = path.name[: -len(_SERVICES_SUFFIX)]
    defaults = services.get("_defaults")
    autowire_default = isinstance(defaults, dict) and defaults.get("autowire") is True
    for raw_sid, definition in services.items():
        sid = str(raw_sid)
        if sid in _RESERVED_SERVICE_KEYS:
            continue
        if isinstance(definition, str):
            if definition.startswith("@") and len(definition) > 1:
                builder.aliases.setdefault(sid, definition[1:])
            continue
        if isinstance(definition, dict) and "alias" in definition:
            alias = definition.get("alias")
            if isinstance(alias, str) and alias.lstrip("@"):
                builder.aliases.setdefault(sid, alias.lstrip("@"))
            continue
        if isinstance(definition, dict) and definition.get("abstract") is True:
            continue
        if definition is not None and not isinstance(definition, dict):
            continue
        fqcn = _service_class(sid, services)
        if fqcn:
            builder.services.setdefault(sid, (fqcn, stem))
            if custom and sid not in builder.wiring:
                definition = definition or {}
                autowire = definition.get("autowire", autowire_default)
                builder.wiring[sid] = {"arguments": _argument_ids(definition.get("arguments")),
                                       "autowire": autowire is True}
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


_SET_CLASS = re.compile(
    r"->setClass\(\s*(?:'([^']+)'|\"([^\"]+)\"|(\\?[A-Za-z_][\w\\]*)::class)")
_USE = re.compile(r"^\s*use\s+\\?([A-Za-z_][\w\\]*)(?:\s+as\s+(\w+))?\s*;", re.MULTILINE)


def _set_classes(text: str, namespace: str) -> list[str]:
    """The classes `->setClass(...)` names: a string literal, or `X::class`
    resolved through the file's namespace and `use` statements."""
    uses = {(alias or name.rsplit("\\", 1)[-1]): name for name, alias in _USE.findall(text)}
    found = []
    for single, double, const in _SET_CLASS.findall(text):
        # A double-quoted PHP string may escape its backslashes.
        literal = single or double.replace("\\\\", "\\")
        found.append(literal.lstrip("\\") if literal else resolve_name(const, namespace, uses))
    return found


def _read_provider(builder: _Builder, path: Path) -> list[str]:
    """Record a provider whose `alter()` sets a class; return the classes it sets."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "function alter(" not in text or "->setClass(" not in text:
        return []
    cls = read_php_class(path)
    builder.note(cls.fqcn if cls else "", path.as_posix(), "service_provider_alter")
    namespace = cls.fqcn.rpartition("\\")[0] if cls else ""
    return _set_classes(text, namespace)


#: How many registry walks are in progress in this process (`registry_walk`).
#: Process-wide, not thread-local: a registry walk and the `detect()` walk it
#: must not affect never run concurrently today -- `prepare_run` finishes the
#: walk before core's own walk starts, on the same thread, and extraction
#: workers are separate processes that never walk.
_registry_walks = 0


@contextmanager
def registry_walk() -> Iterator[None]:
    """Mark the block as the registry's walk (re-entrant, cleared on exit).

    While it runs, the seam's `_is_noise_dir` wrapper answers with core's
    original only, so the registry reads core, contrib and vendor even though
    `detect()` never descends them (spec S4.2)."""
    global _registry_walks
    _registry_walks += 1
    try:
        yield
    finally:
        _registry_walks -= 1


def walking_registry() -> bool:
    """True while the registry walks the site (inside `registry_walk()`)."""
    return _registry_walks > 0


def build_registry(scan_root: Path, is_ignored: Callable[[Path], bool] | None = None) -> Registry:
    """Learn every plugin type the site's managers define. Never raises on bad input.

    `is_ignored` (core's `detect.ignored_predicate` without `.gitignore` in the
    pipeline) keeps the walk out of what the user excluded; without it every
    file under the web root counts. The walk runs inside `registry_walk()`, so
    the boundary trees `detect()` prunes are read here.

    The root is resolved, as `detect()` resolves it: a checkout scanned through
    a symlink gets the same paths `detect()` hands the extractors, so owners
    and `src/Hook/` matches agree (final review minor 9).
    """
    try:
        scan_root = Path(scan_root).resolve()
    except (OSError, RuntimeError):
        scan_root = Path(scan_root).absolute()
    web_root = find_web_root(scan_root)
    base = web_root if web_root is not None else scan_root
    with registry_walk():
        walk = _walk(base, web_root / "core" if web_root is not None else None, is_ignored)
    builder = _Builder(web_root, walk)
    if web_root is None:
        builder.note("", "", "no_drupal_core")

    where = _DirectoryFacts(builder)
    reached: set[str] = set()
    for path in walk.services:
        reached |= _read_services(builder, path, where.of(path)[1])
    swapped: list[tuple[Path, str]] = []
    for path in walk.providers:
        swapped.extend((path, fqcn) for fqcn in _read_provider(builder, path))
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

    # A manager only a provider's `alter()` installs (symfony_mailer's
    # `MailManagerReplacement`) is neither a service nor `*Manager.php`: name it
    # as the provider's entry so no manager class goes unreported (P3 follows it).
    for path, fqcn in swapped:
        found = builder.lookup(fqcn) if fqcn not in reached else None
        if found is not None and builder.is_manager(found[1]):
            builder.note(fqcn, path.as_posix(), "service_provider_alter")

    # Every `function hook_<name>(` stub in every `*.api.php` (boundary
    # included -- the walk ran inside `registry_walk()`), keyed by name; the
    # first extension to declare a name wins, matching `add_type`'s rule.
    for path in walk.api_php:
        owner = builder.owner_of(path)
        for name, line in read_hook_stubs(path):
            if name in builder.hooks:
                continue
            builder.hooks[name] = HookDecl(
                name=name, provider=owner, file=path.as_posix(), line=line,
                pattern=hook_pattern(name),
            )

    php = _learn_php(builder, walk, is_ignored, where)

    return Registry(
        web_root=web_root.as_posix() if web_root is not None else None,
        types=builder.types,
        unresolved=builder.unresolved,
        extensions=walk.extensions,
        root_yaml=walk.root_yaml,
        hooks=builder.hooks,
        services=builder.services,
        extension_info=walk.extension_info,
        shortcuts=read_drupal_shortcuts(web_root / "core" / "lib" / "Drupal.php")
        if web_root is not None else {},
        service_aliases=builder.aliases,
        service_wiring=builder.wiring,
        **php,
    )


# -- P4: what the site's PHP says (spec §5.4, §6.1, §7.2, §8) -----------------

#: Text prechecks: only a file one of them matches is parsed (`_learn_php`).
_FORM_PRECHECK = re.compile(rb"function\s+get(?:Base)?FormId\s*\(")
_ENTITY_TYPE_PRECHECK = re.compile(rb"(?:#\[|@)\\?(?:[\w\\]*\\)?(?:Content|Config)EntityType\s*\(")
_EVENTS_PRECHECK = re.compile(rb"class\s+\w*Events\b")
_CLASS_PRECHECK = re.compile(rb"\bclass\s+\w")

#: The entity type attribute classes (resolved through `use`, as PHP does).
_ENTITY_TYPE_ATTRIBUTES = frozenset({
    "Drupal\\Core\\Entity\\Attribute\\ContentEntityType",
    "Drupal\\Core\\Entity\\Attribute\\ConfigEntityType",
})
#: The entity type annotations, by short name: Drupal's annotation reader
#: matches `@ContentEntityType` without a `use` (none of the corpus's 62
#: annotated entity classes imports it).
_ENTITY_TYPE_ANNOTATIONS = frozenset({"ContentEntityType", "ConfigEntityType"})
#: ... but an annotation whose name a `use` (or a leading `\\`) fixes must be one of these (spec §5.1).
_ENTITY_TYPE_ANNOTATION_CLASSES = frozenset({
    "Drupal\\Core\\Entity\\Annotation\\ContentEntityType",
    "Drupal\\Core\\Entity\\Annotation\\ConfigEntityType",
})


def _learn_php(builder: _Builder, walk: _Walk,
               is_ignored: Callable[[Path], bool] | None,
               where: _DirectoryFacts | None = None) -> dict[str, dict]:
    """The P4 maps, from one read of each `.php` file the walk found.

    A file is parsed only when a cheap text precheck matches: `getFormId`,
    an entity type attribute or annotation, `class …Events`, or -- in a
    custom extension -- any class (spec §7.2 records every custom class).
    `is_ignored` is asked only about those files, after the precheck, so an
    ignored file may be read but never contributes. A custom entry wins over
    a boundary one with the same key -- for forms whether either is a literal
    or a base form id -- and, on one side, a literal form id wins over a base
    form id. Never raises."""
    where = where if where is not None else _DirectoryFacts(builder)
    class_facts: dict[str, dict] = {}
    entity_types: tuple[dict, dict] = ({}, {})     # (boundary, custom)
    forms: tuple[dict, dict] = ({}, {})
    base_forms: tuple[dict, dict] = ({}, {})
    event_constants: dict[str, str] = {}

    for path in walk.php:
        try:
            source = path.read_bytes()
        except OSError:
            continue
        owner, custom = where.of(path)
        # A substring test first: most files match none, and `in` is far
        # cheaper than a regex search.
        if not ((custom and _CLASS_PRECHECK.search(source))
                or (b"FormId" in source and _FORM_PRECHECK.search(source))
                or (b"EntityType" in source and _ENTITY_TYPE_PRECHECK.search(source))
                or (b"Events" in source and _EVENTS_PRECHECK.search(source))):
            continue
        if is_ignored is not None and is_ignored(path):
            continue
        facts, annotations, attributed = read_class_semantics(path, source)
        side = 1 if custom else 0
        for f in facts:
            if custom:
                class_facts.setdefault(f.fqcn, f.to_dict())
            if f.form_id:
                forms[side].setdefault(f.form_id, (owner, f.fqcn))
            if f.base_form_id:
                base_forms[side].setdefault(f.base_form_id, (owner, f.fqcn))
            if f.fqcn.rsplit("\\", 1)[-1].endswith("Events"):
                for name, value in f.constants.items():
                    event_constants.setdefault(f"{f.fqcn}::{name}", value)
        for cls in attributed:
            for attr in cls.attributes:
                if attr.name not in _ENTITY_TYPE_ATTRIBUTES:
                    continue
                named = next((a.string for a in attr.args if a.name == "id"), None)
                positional = next((a for a in attr.args if not a.name), None)
                entity_id = named or (positional.string if positional is not None else None)
                if entity_id:
                    entity_types[side].setdefault(entity_id, (owner, cls.fqcn))
        for fqcn, annotation in annotations:
            if annotation.short not in _ENTITY_TYPE_ANNOTATIONS:
                continue
            if annotation.imported and annotation.name not in _ENTITY_TYPE_ANNOTATION_CLASSES:
                continue
            entity_id = annotation.values.get("id")
            if isinstance(entity_id, str) and entity_id:
                entity_types[side].setdefault(entity_id, (owner, fqcn))

    return {
        "class_facts": class_facts,
        "entity_types": {**entity_types[0], **entity_types[1]},
        # Later wins: custom over boundary for any key (literal or base id),
        # and within one side a literal form id over a base form id.
        "forms": {**base_forms[0], **forms[0], **base_forms[1], **forms[1]},
        "event_constants": event_constants,
    }


class _DirectoryFacts:
    """`(owner extension, in the graph?)` per directory, for `_learn_php`.

    The owner is `_owner`'s longest-match answer, found by walking up from
    the directory to the nearest extension directory instead of scanning
    every extension (~1,140 on the reference corpus) per file. "In the
    graph" is `realm_of` custom, or a realm `drupal.include` opts in: the
    owning extension's directory (else the file's own) is in no boundary
    directory, decided once per directory as `hook_dependent_files` does."""

    def __init__(self, builder: _Builder) -> None:
        self._by_dir: dict[str, str] = {}
        for ext, directory in builder.walk.extensions.items():
            # `_owner` keeps the first extension of equal length.
            self._by_dir.setdefault(directory, ext)
        self._memo: dict[str, tuple[str, bool]] = {}
        self._custom: dict[str, bool] = {}

    def of(self, path: Path) -> tuple[str, bool]:
        parent = path.parent.as_posix()
        found = self._memo.get(parent)
        if found is None:
            owner, directory = "", parent
            probe = parent
            while probe:
                if probe in self._by_dir:
                    owner, directory = self._by_dir[probe], probe
                    break
                head, sep, _tail = probe.rpartition("/")
                if not sep or head == probe:
                    break
                probe = head
            found = self._memo[parent] = (owner, self._in_graph(directory))
        return found

    def _in_graph(self, directory: str) -> bool:
        if directory not in self._custom:
            from graphify.drupal.boundary import boundary_dir

            try:
                self._custom[directory] = boundary_dir(Path(directory)) is None
            except Exception:
                self._custom[directory] = False
        return self._custom[directory]


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
_force_miss: frozenset[str] = frozenset()

# current_registry() caches the file it loads from ENV_VAR by (path, mtime), so
# a worker process that calls it many times over one run reads the file once.
_env_cache_path: str | None = None
_env_cache_mtime: float | None = None
_env_cache_registry: Registry | None = None
_env_cache_force_miss: frozenset[str] = frozenset()

#: The registry file's key for `force_miss()`, read by a spawned worker. It is
#: not part of `Registry`: next run's `from_json` ignores it.
_FORCE_MISS_KEY = "force_miss"
#: The registry file's key for `boundary.boundary_digest` of the run that wrote it.
_BOUNDARY_DIGEST_KEY = "boundary_digest"


#: Prefix of the private temp file a worker reads the registry from when the
#: out dir cannot be written (see `_write_temp_registry`).
_TEMP_PREFIX = "graphify-drupal-discovery-"

_log = logging.getLogger(__name__)
_warned_unwritable = False

#: The out dir a caller fixed for the duration of a call (`using_out_dir`).
_out_override: Path | None = None

#: (resolved scan root, out dir) of the Drupal run `prepare_run` last set up.
_current_run: tuple[Path, Path] | None = None


def current_run() -> tuple[Path, Path] | None:
    """The (resolved root, out dir) of this process's current Drupal run: set by
    `prepare_run` for a Drupal tree, cleared by `set_current(None)`. The build
    seam lays the container overlay only while one is current (P3 spec S7.1)."""
    return _current_run


@contextmanager
def using_out_dir(path: Path) -> Iterator[None]:
    """Make `out_dir()` return `path` until the block exits (re-entrant).

    `detect_incremental` knows its out dir only through its `manifest_path`
    and calls `detect()` without a `cache_root`; the seam wraps it in this so
    the nested `prepare_run` and inventory land beside that manifest, where
    the full run (which did get a `cache_root`) wrote them.
    """
    global _out_override
    saved = _out_override
    _out_override = Path(path)
    try:
        yield
    finally:
        _out_override = saved


def out_dir(root: Path, cache_root: Path | None = None) -> Path:
    """The graphify-out directory for this run, matching core's own resolution.

    Mirrors `graphify.cache.cache_dir`'s `_out if _out.is_absolute() else
    Path(location).resolve() / _out`, where `location` is `cache_root` when
    given, else `root` — so a `GRAPHIFY_OUT` override (relative or absolute)
    and an `extract --out`-style `cache_root` land the registry file exactly
    where the rest of graphify's output already goes. Inside `using_out_dir`
    the directory it names wins.
    """
    from graphify.paths import GRAPHIFY_OUT

    if _out_override is not None:
        return _out_override
    location = cache_root if cache_root is not None else root
    out = Path(GRAPHIFY_OUT)
    return out if out.is_absolute() else Path(location).resolve() / out


def set_current(
    registry: Registry | None,
    previous: Registry | None = None,
    forced: frozenset[str] = frozenset(),
) -> None:
    """Set this process's in-memory registry (and the prior run's, if any),
    with the files this run must re-extract because the registry changed."""
    global _current, _previous, _force_miss, _current_run
    _current = registry
    _previous = previous
    _force_miss = frozenset(forced) if registry is not None else frozenset()
    if registry is None:
        _current_run = None
        # A non-Drupal run in the same process must not report a previous
        # site's inventory (spec §5.7); deferred import avoids a cycle with
        # inventory.py, which imports Registry/current_registry from here.
        from graphify.drupal.inventory import set_current_inventory

        set_current_inventory(None)


def _sorted_types(types: list[PluginType]) -> list[PluginType]:
    return sorted(types, key=lambda t: t.plugin_type)


def affected_files(previous: Registry | None, current: Registry | None,
                   boundary_changed: bool = False, *,
                   graph_path: Path | None = None, root: Path | None = None) -> set[str]:
    """Absolute paths whose extraction depends on what changed between two registries.

    For every `yaml_name` whose type was added, removed or changed in any field,
    the extension-root files `*.<yaml_name>.yml` of either registry (its plugins
    are typed by it); for every manager class file whose types changed, that
    file (it carries the type nodes). When the boundary moved
    (`boundary_changed`, see `prepare_run`), every in-graph file the registry
    walk knows that points at extensions, services or hooks
    (`_boundary_dependent_files`), plus every in-graph file that is the
    `source_file` of an `invokes_hook` edge in `graph_path`'s previous
    graph.json (`_invokes_hook_files`, spec §11.1): the registry never learns
    of such a file (it names no extension, service or hook of its own), so
    without this it would keep edges to a stub whose boundary status just
    changed. Empty when there is no previous registry: a first run extracts
    everything anyway.
    """
    if previous is None:
        return set()
    current = current if current is not None else Registry(web_root=None)
    result: set[str] = set()
    if boundary_changed:
        result |= _boundary_dependent_files(previous) | _boundary_dependent_files(current)
        result |= _invokes_hook_files(graph_path, root)

    old_families, new_families = previous.by_yaml_name(), current.by_yaml_name()
    suffixes = tuple(
        f".{name}.yml" for name in sorted(old_families.keys() | new_families.keys())
        if old_families.get(name) != new_families.get(name)
    )
    if suffixes:
        for path in (*previous.root_yaml, *current.root_yaml):
            if Path(path).name.endswith(suffixes):
                result.add(path)

    old_files, new_files = previous.by_class_file(), current.by_class_file()
    for path in old_files.keys() | new_files.keys():
        if _sorted_types(old_files.get(path, [])) != _sorted_types(new_files.get(path, [])):
            result.add(path)

    # A hook added, removed or changed (name, provider, pattern, or which
    # file declares it): the `*.api.php` file(s) that declared the old and/or
    # new version, so their own `drupal_hook` nodes stay current.
    old_hooks, new_hooks = previous.hooks, current.hooks
    for name in old_hooks.keys() | new_hooks.keys():
        old_decl, new_decl = old_hooks.get(name), new_hooks.get(name)
        if old_decl == new_decl:
            continue
        for decl in (old_decl, new_decl):
            if decl is not None:
                result.add(decl.file)

    # What an implementation is depends only on which names are declared,
    # their patterns and their providers (an extension implementing its own
    # hook gets no `implements_hook`): when that changes, every in-graph
    # procedural file and `src/Hook/**/*.php` (spec §5.5).
    if _hook_names(old_hooks) != _hook_names(new_hooks):
        result |= hook_dependent_files(previous) | hook_dependent_files(current)

    # A custom class whose constructor/`create()` facts changed (P4 §9): its
    # file, and every in-graph subclass's -- they inherit what it injects.
    result |= _class_facts_files(previous.class_facts, current.class_facts)
    return result


def _class_facts_files(old: dict[str, dict], new: dict[str, dict]) -> set[str]:
    """The files of every class whose facts differ between `old` and `new`
    (added and removed included), and of their subclasses in either map."""
    changed = [fqcn for fqcn in old.keys() | new.keys() if old.get(fqcn) != new.get(fqcn)]
    out: set[str] = set()
    if not changed:
        return out
    for facts in (old, new):
        children: dict[str, list[str]] = {}
        for fqcn, data in facts.items():
            parent = data.get("extends") if isinstance(data, dict) else ""
            if parent:
                children.setdefault(parent, []).append(fqcn)
        stack, seen = list(changed), set()
        while stack:
            fqcn = stack.pop()
            if fqcn in seen:
                continue
            seen.add(fqcn)
            data = facts.get(fqcn)
            if isinstance(data, dict) and data.get("file"):
                out.add(str(data["file"]))
            stack.extend(children.get(fqcn, ()))
    return out


def _boundary_dependent_files(registry: Registry) -> set[str]:
    """What a moved boundary makes stale (final review I3): the files of the
    registry's in-graph extensions (after the current boundary) that emit
    edges to extensions, services or hooks -- each `<ext>.info.yml`, every
    extension-root `*.yml` (`root_yaml`), the procedural and `src/Hook/`
    files, in-graph manager class files and `*.api.php` declarations. A file
    that just left the graph is not listed: the widening must not pull a
    pruned file back in. Absolute POSIX paths."""
    from graphify.drupal.boundary import boundary_dir

    in_graph: set[str] = set()
    out: set[str] = set()
    for ext, directory in registry.extensions.items():
        try:
            if boundary_dir(Path(directory)) is not None:
                continue
        except Exception:
            continue
        in_graph.add(directory)
        out.add(f"{directory}/{ext}.info.yml")
    out.update(p for p in registry.root_yaml if Path(p).parent.as_posix() in in_graph)
    out |= hook_dependent_files(registry)

    def kept(path: str) -> bool:
        try:
            return boundary_dir(Path(path).parent) is None
        except Exception:
            return False

    out.update(t.class_file for t in registry.types.values() if kept(t.class_file))
    out.update(d.file for d in registry.hooks.values() if kept(d.file))
    return out


_GRAPH_FILENAME = "graph.json"


def _invokes_hook_files(graph_path: Path | None, root: Path | None) -> set[str]:
    """Absolute, existing paths that are the `source_file` of an `invokes_hook`
    edge in `graph_path` (P2b's carried I3 gap, spec §11.1): a plain PHP file
    whose only Drupal fact is such a call site names no extension, service or
    hook of its own, so `_boundary_dependent_files` (built entirely from what
    the registry walk knows) never reaches it -- yet its edge points at a hook
    whose boundary/stub status a moved boundary can change.

    `source_file` is root-relative POSIX, as graph.json stores it; an
    already-absolute one is accepted as-is. Anything that is not an
    `invokes_hook` edge (a node, a P3 container-overlay item) is ignored.
    Missing `graph_path`/`root`, an unreadable or malformed graph.json, or a
    path that no longer exists on disk each add nothing.
    """
    if graph_path is None or root is None:
        return set()
    try:
        data = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(data, dict):
        return set()
    links = data.get("links")
    if not isinstance(links, list):
        links = data.get("edges")
    if not isinstance(links, list):
        return set()
    root = Path(root)
    out: set[str] = set()
    for item in links:
        if not isinstance(item, dict) or item.get("relation") != "invokes_hook":
            continue
        source_file = item.get("source_file")
        if not source_file or not isinstance(source_file, str):
            continue
        path = Path(source_file)
        if not path.is_absolute():
            path = root / path
        try:
            if path.is_file():
                out.add(path.resolve().as_posix())
        except OSError:
            continue
    return out


def _hook_names(hooks: dict[str, HookDecl]) -> dict[str, tuple[str, str]]:
    return {name: (decl.pattern, decl.provider) for name, decl in hooks.items()}


def force_miss() -> frozenset[str]:
    """The files this run must not serve from the AST cache (spec §5.5).

    Set by `prepare_run` in the process that ran `detect()`. A spawned
    extraction worker has no in-process state, so it reads the set from the
    registry file `ENV_VAR` names -- core's worker checks the cache again
    before extracting, and a stale hit there would undo the parent's miss.
    """
    if _current is not None:
        return _force_miss
    _load_env_file()
    return _env_cache_force_miss


def _load_env_file() -> None:
    """Load ENV_VAR's registry file into the (path, mtime) cache, or clear it."""
    global _env_cache_path, _env_cache_mtime, _env_cache_registry, _env_cache_force_miss
    path = os.environ.get(ENV_VAR)
    mtime: float | None = None
    if path:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = None
    if mtime is None:
        _env_cache_path = _env_cache_mtime = _env_cache_registry = None
        _env_cache_force_miss = frozenset()
        return
    if (
        _env_cache_registry is not None
        and _env_cache_path == path
        and _env_cache_mtime == mtime
    ):
        return
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        registry = Registry.from_json(data)
    except (OSError, ValueError, TypeError, KeyError):
        _env_cache_path = _env_cache_mtime = _env_cache_registry = None
        _env_cache_force_miss = frozenset()
        return
    _env_cache_path, _env_cache_mtime, _env_cache_registry = path, mtime, registry
    _env_cache_force_miss = frozenset(str(p) for p in data.get(_FORCE_MISS_KEY) or [])


def current_registry() -> Registry | None:
    """This process's registry: in-memory if set, else loaded from ENV_VAR's file.

    The file load is cached by (path, mtime) so repeated calls in one process
    (e.g. many files handled by one extraction worker) cost one stat plus at
    most one read. Returns None when neither is available, or the file cannot
    be read/parsed.
    """
    if _current is not None:
        return _current
    _load_env_file()
    return _env_cache_registry


def clear_force_miss() -> None:
    """Mark this run's `force_miss()` set consumed, in process and in the file.

    Called once an extraction has completed; until then the set stays in the
    registry file, so the next `prepare_run` carries it over.
    """
    global _force_miss
    _force_miss = frozenset()
    path = os.environ.get(ENV_VAR)
    if not path:
        return
    try:
        target = Path(path)
        data = json.loads(target.read_text(encoding="utf-8"))
        if not data.get(_FORCE_MISS_KEY):
            return
        data[_FORCE_MISS_KEY] = []
        target.write_text(json.dumps(data, sort_keys=True, indent=1), encoding="utf-8")
    except (OSError, ValueError, TypeError, AttributeError):
        return


def previous_registry() -> Registry | None:
    """The prior run's registry, as read by `prepare_run` before it overwrote
    the file. In-process only — a worker process never sees it, only the
    process that called `prepare_run` (i.e. that ran `detect()`)."""
    return _previous


def _scan_predicate(
    root: Path, extra_excludes: list[str] | None, gitignore: bool,
) -> Callable[[Path], bool]:
    """Core's own "would detect() exclude this path?" for `root`.

    Core anchors the predicate at the resolved root, while the registry walks
    absolute but unresolved paths; a symlinked checkout is mapped across so
    its ignore rules still apply.
    """
    from graphify.detect import ignored_predicate

    resolved = Path(root).resolve()
    predicate = ignored_predicate(resolved, extra_excludes=extra_excludes, gitignore=gitignore)
    walked = Path(root).absolute()
    if walked == resolved:
        return predicate

    def is_ignored(path: Path) -> bool:
        try:
            return predicate(resolved / Path(path).relative_to(walked))
        except ValueError:
            return predicate(path)
    return is_ignored


def _looks_like_drupal(root: Path) -> bool:
    """A web root at, below or above `root`, or a `*.info.yml` directly in it."""
    if find_web_root(root) is not None:
        return True
    try:
        with os.scandir(root) as entries:
            return any(e.is_file() and is_drupal_info_yaml(Path(e.name)) for e in entries)
    except OSError:
        return False


def prepare_run(
    root: Path,
    cache_root: Path | None = None,
    *,
    extra_excludes: list[str] | None = None,
    gitignore: bool = True,
) -> Registry | None:
    """Build the registry for this run and make it visible to the pipeline.

    Only a tree with a cheap Drupal marker gets one: a web root
    (`find_web_root`), or a scan root that directly holds a `*.info.yml` (a
    single extension checkout). Any other tree is not walked at all -- no
    registry, no file, `ENV_VAR` removed, `set_current(None)` -- and None is
    returned, so a non-Drupal repository pays nothing.

    Reads `<out>/drupal-discovery.json` as the previous run's registry (ignored
    if missing or unreadable), builds the new registry with `build_registry`
    (which never raises), and tries to write it back to that same path with
    sorted keys and one-space indent. A write failure (read-only tree,
    permissions, ...) never raises: the registry stays in process, and
    `ENV_VAR` points at a private temp copy for spawned workers instead
    (`_write_temp_registry`), or is removed if even that cannot be written.

    `extra_excludes` is `detect()`'s own: the walk honours the same
    `.graphifyignore`/`--exclude` rules and noise-dir pruning, so an ignored
    module defines no type and is never descended. `.gitignore` is not
    honoured, whatever `gitignore` says (see the comment below).

    The files the change from the previous registry affects (`affected_files`),
    plus any set a previous run left unconsumed, become this run's
    `force_miss()` set, kept in process and in the file until
    `clear_force_miss()` is called after a completed extraction.

    A Drupal tree also becomes the `current_run()` (root, out dir).
    """
    global _current_run
    try:
        # As `detect()` does: a symlinked checkout is scanned at its real path.
        root = Path(root).resolve()
    except (OSError, RuntimeError):
        root = Path(root)
    if not _looks_like_drupal(root):
        _drop_temp_registry()
        os.environ.pop(ENV_VAR, None)
        set_current(None)
        return None
    target = out_dir(root, cache_root) / _REGISTRY_FILENAME

    previous: Registry | None = None
    previous_digest: str | None = None
    carried: set[str] = set()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        previous = Registry.from_json(data)
        previous_digest = data.get(_BOUNDARY_DIGEST_KEY)
        # A set no extract() consumed yet (an interrupted run, or one with
        # nothing to extract) is carried over, minus files that are gone.
        carried = {str(p) for p in data.get(_FORCE_MISS_KEY) or [] if Path(str(p)).exists()}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        previous = None

    # `gitignore` is accepted for `detect()`'s signature but deliberately not
    # passed on: the registry is knowledge about the site, not graph content.
    # A composer-managed site gitignores web/core and web/modules/contrib,
    # which define almost every plugin type, so honouring .gitignore here would
    # learn next to nothing. Explicit intent (.graphifyignore, --exclude) and
    # core's noise-dir pruning still apply; the graph itself still honours
    # .gitignore, and edges to a type defined in an ignored tree point at a
    # materialised (missing) type node.
    # The boundary (core, contrib, vendor) is not graph content either: the
    # predicate is built and asked inside `registry_walk()`, so its noise-dir
    # check does not prune what `detect()` itself never descends (spec S4.2).
    with registry_walk():
        registry = build_registry(root, _scan_predicate(root, extra_excludes, gitignore=False))
    # A moved boundary (`drupal.include` toggled, a composer update moving a
    # package into contrib/vendor) changes which nodes are stubs without
    # changing any file that points at them (final review I3).
    digest = boundary_digest(root, registry.web_root)
    forced = frozenset(
        affected_files(previous, registry, boundary_changed=previous_digest != digest,
                       graph_path=target.parent / _GRAPH_FILENAME, root=root) | carried)

    payload = json.dumps({**registry.to_json(), _FORCE_MISS_KEY: sorted(forced),
                          _BOUNDARY_DIGEST_KEY: digest},
                         sort_keys=True, indent=1)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        _drop_temp_registry()
        os.environ[ENV_VAR] = str(target.absolute())
    except OSError:
        _write_temp_registry(payload)

    set_current(registry, previous, forced)
    _current_run = (root, target.parent)
    return registry


def _is_temp_registry(path: str | None) -> bool:
    if not path:
        return False
    p = Path(path)
    return p.name.startswith(_TEMP_PREFIX) and p.parent == Path(tempfile.gettempdir())


def _drop_temp_registry() -> None:
    """Delete the temp registry file ENV_VAR names, if it is one of ours."""
    path = os.environ.get(ENV_VAR)
    if _is_temp_registry(path):
        try:
            Path(path).unlink()
        except OSError:
            pass


def _write_temp_registry(payload: str) -> None:
    """Point ENV_VAR at a private temp copy of the registry (the out dir is unwritable).

    A spawn/forkserver worker (Python 3.14's Linux default, macOS's) inherits
    nothing but the environment, so without a readable file it would have no
    registry at all. The previous temp file this process made is replaced,
    not accumulated. If even that fails, ENV_VAR is removed -- a worker must
    never read an earlier run's registry through a stale path -- and the
    degradation is logged once.
    """
    global _warned_unwritable
    _drop_temp_registry()
    try:
        fd, name = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.environ[ENV_VAR] = name
    except OSError:
        os.environ.pop(ENV_VAR, None)
        if not _warned_unwritable:
            _warned_unwritable = True
            _log.warning("graphify-drupal: the plugin registry could not be written "
                         "anywhere; spawned extraction workers will run without it")
