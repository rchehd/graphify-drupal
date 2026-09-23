from pathlib import Path

from graphify.drupal.discovery import (
    PluginType,
    Registry,
    build_registry,
    find_web_root,
    type_id,
)

CORE_DPM = r"""<?php
namespace Drupal\Core\Plugin;
use Drupal\Component\Plugin\PluginManagerInterface;
class DefaultPluginManager implements PluginManagerInterface {}
"""
PMI = "<?php\nnamespace Drupal\\Component\\Plugin;\ninterface PluginManagerInterface {}\n"

D11_MANAGER = r"""<?php
namespace Drupal\foo;

use Drupal\Core\Plugin\DefaultPluginManager;
use Drupal\foo\Attribute\Foo as FooAttribute;

/**
 * A manager.
 */
class FooManager extends DefaultPluginManager implements FooManagerInterface {

  public function __construct(\Traversable $namespaces, $cache, $module_handler) {
    parent::__construct(
      'Plugin/Foo',            // a comment in between
      $namespaces,
      $module_handler,
      FooInterface::class,
      FooAttribute::class,
      'Drupal\foo\Annotation\Foo'
    );
    $this->alterInfo('foo_info');
  }
}
"""

YAML_MANAGER = r"""<?php
namespace Drupal\bar;

use Drupal\Core\Plugin\Discovery\YamlDiscoveryDecorator;
use Drupal\Core\Plugin\Discovery\YamlDiscovery;

class BarManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  protected function getDiscovery() {
    if (!$this->discovery) {
      $discovery = new AnnotatedClassDiscovery($this->subdir, $this->namespaces);
      $discovery = new YamlDiscoveryDecorator($discovery, 'bar', $this->moduleHandler->getModuleDirectories());
      $this->discovery = new ContainerDerivativeDiscoveryDecorator($discovery);
    }
    return $this->discovery;
  }
}
"""

MENU_LINK_INTERFACE = r"""<?php
namespace Drupal\Core\Menu;
use Drupal\Component\Plugin\PluginManagerInterface;
interface MenuLinkManagerInterface extends PluginManagerInterface, \Countable {}
"""

MENU_LINK_MANAGER = r"""<?php
namespace Drupal\Core\Menu;
use Drupal\Core\Plugin\Discovery\YamlDiscovery;
class MenuLinkManager implements MenuLinkManagerInterface {
  protected function getDiscovery() {
    if (!isset($this->discovery)) {
      $yaml = new YamlDiscovery('links.menu', $this->moduleHandler->getModuleDirectories());
      $this->discovery = new ContainerDerivativeDiscoveryDecorator($yaml);
    }
    return $this->discovery;
  }
}
"""

CORE_SERVICES = "services:\n  plugin.manager.menu.link:\n    class: Drupal\\Core\\Menu\\MenuLinkManager\n"


def _site(root: Path, files: dict[str, str]) -> Path:
    base = {
        "web/core/lib/Drupal.php": "<?php\n",
        "web/core/lib/Drupal/Core/Plugin/DefaultPluginManager.php": CORE_DPM,
        "web/core/lib/Drupal/Component/Plugin/PluginManagerInterface.php": PMI,
        "web/core/core.services.yml": "services: {}\n",
    }
    for rel, text in {**base, **files}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _module(name: str, services: str, php: dict[str, str] | None = None) -> dict[str, str]:
    d = f"web/modules/custom/{name}"
    files = {f"{d}/{name}.info.yml": f"name: {name}\ntype: module\n",
             f"{d}/{name}.services.yml": services}
    for rel, text in (php or {}).items():
        files[f"{d}/{rel}"] = text
    return files


FOO_SERVICES = (
    "services:\n"
    "  plugin.manager.foo:\n"
    "    class: Drupal\\foo\\FooManager\n"
    "    parent: default_plugin_manager\n"
)


def _reasons(registry: Registry) -> list[tuple[str, str]]:
    return sorted((u["reason"], u["class"]) for u in registry.unresolved)


def test_a_d11_manager_service_is_a_type(tmp_path):
    _site(tmp_path, _module("foo", FOO_SERVICES, {"src/FooManager.php": D11_MANAGER}))
    r = build_registry(tmp_path)
    assert r.web_root == (tmp_path / "web").as_posix()
    assert r.types["foo"] == PluginType(
        plugin_type="foo",
        manager_class="Drupal\\foo\\FooManager",
        class_file=(tmp_path / "web/modules/custom/foo/src/FooManager.php").as_posix(),
        line=10,
        owner="foo",
        registered=True,
        manager_service="plugin.manager.foo",
        discovery="mixed",
        subdir="Plugin/Foo",
        interface="Drupal\\foo\\FooInterface",
        annotation_class="Drupal\\foo\\Annotation\\Foo",
        attribute_class="Drupal\\foo\\Attribute\\Foo",
        alter_hook="foo_info",
    )
    assert list(r.types) == ["foo"]
    assert r.unresolved == []
    assert r.extensions == {
        "core": (tmp_path / "web/core").as_posix(),
        "foo": (tmp_path / "web/modules/custom/foo").as_posix(),
    }


def test_facts_are_inherited_through_an_intermediate_class(tmp_path):
    base_manager = r"""<?php
namespace Drupal\foo;
use Drupal\Core\Plugin\DefaultPluginManager;
abstract class BaseManager extends DefaultPluginManager {
  public function __construct($namespaces, $cache, $module_handler) {
    parent::__construct('Plugin/Foo', $namespaces, $module_handler, 'Drupal\foo\FooInterface',
      'Drupal\foo\Annotation\Foo');
    $this->alterInfo('foo_info');
  }
}
"""
    foo_manager = "<?php\nnamespace Drupal\\foo;\nclass FooManager extends BaseManager {}\n"
    _site(tmp_path, _module("foo", FOO_SERVICES, {
        "src/BaseManager.php": base_manager, "src/FooManager.php": foo_manager}))
    r = build_registry(tmp_path)
    assert list(r.types) == ["foo"]
    t = r.types["foo"]
    assert (t.manager_class, t.line, t.registered) == ("Drupal\\foo\\FooManager", 3, True)
    assert (t.subdir, t.interface, t.annotation_class, t.attribute_class) == (
        "Plugin/Foo", "Drupal\\foo\\FooInterface", "Drupal\\foo\\Annotation\\Foo", "")
    assert (t.discovery, t.alter_hook) == ("annotation", "foo_info")
    assert r.unresolved == []


def test_an_interface_only_manager_with_yaml_discovery(tmp_path):
    _site(tmp_path, {
        "web/core/core.services.yml": CORE_SERVICES,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManagerInterface.php": MENU_LINK_INTERFACE,
        "web/core/lib/Drupal/Core/Menu/MenuLinkManager.php": MENU_LINK_MANAGER,
    })
    r = build_registry(tmp_path)
    t = r.types["menu.link"]
    assert (t.manager_class, t.owner, t.registered, t.manager_service) == (
        "Drupal\\Core\\Menu\\MenuLinkManager", "core", True, "plugin.manager.menu.link")
    assert (t.discovery, t.yaml_name, t.deferred_to) == ("yaml", "links.menu", "")
    assert r.by_yaml_name() == {"links.menu": t}
    assert r.unresolved == []


def test_a_yaml_discovery_decorator_is_mixed(tmp_path):
    services = "services:\n  plugin.manager.bar:\n    class: Drupal\\bar\\BarManager\n"
    _site(tmp_path, _module("bar", services, {"src/BarManager.php": YAML_MANAGER}))
    r = build_registry(tmp_path)
    t = r.types["bar"]
    assert (t.discovery, t.yaml_name, t.line) == ("mixed", "bar", 7)
    assert r.unresolved == []


def test_a_service_class_from_its_id_or_its_parent(tmp_path):
    services = (
        "services:\n"
        "  Drupal\\foo\\FooManager:\n"
        "    parent: default_plugin_manager\n"
        "  foo.base:\n"
        "    abstract: true\n"
        "    class: Drupal\\foo\\FooManager\n"
        "  plugin.manager.child:\n"
        "    parent: foo.base\n"
        "  foo.alias: '@plugin.manager.child'\n"
        "  foo.alias2:\n"
        "    alias: plugin.manager.child\n"
        "  _defaults:\n"
        "    autowire: true\n"
    )
    _site(tmp_path, _module("foo", services, {"src/FooManager.php": D11_MANAGER}))
    r = build_registry(tmp_path)
    assert sorted(r.types) == ["Drupal\\foo\\FooManager", "child"]
    assert r.types["child"].manager_service == "plugin.manager.child"
    assert r.types["Drupal\\foo\\FooManager"].manager_service == "Drupal\\foo\\FooManager"
    assert {t.manager_class for t in r.types.values()} == {"Drupal\\foo\\FooManager"}
    assert list(r.by_class_file()) == [(tmp_path / "web/modules/custom/foo/src/FooManager.php").as_posix()]
    assert len(r.by_class_file()[r.types["child"].class_file]) == 2
    assert r.unresolved == []


def test_a_manager_no_service_names_is_an_unregistered_type(tmp_path):
    qux = r"""<?php
namespace Drupal\qux\Plugin;
use Drupal\Core\Plugin\DefaultPluginManager;
class QuxManager extends DefaultPluginManager {
  public function __construct($namespaces, $module_handler) {
    parent::__construct('Plugin/Qux', $namespaces, $module_handler);
  }
}
"""
    _site(tmp_path, _module("qux", "services: {}\n", {"src/Plugin/QuxManager.php": qux}))
    r = build_registry(tmp_path)
    key = "class:Drupal\\qux\\Plugin\\QuxManager"
    assert list(r.types) == [key]
    t = r.types[key]
    assert (t.plugin_type, t.registered, t.manager_service, t.owner, t.subdir, t.discovery) == (
        key, False, "", "qux", "Plugin/Qux", "annotation")
    assert r.unresolved == [{
        "class": "Drupal\\qux\\Plugin\\QuxManager",
        "file": (tmp_path / "web/modules/custom/qux/src/Plugin/QuxManager.php").as_posix(),
        "reason": "not_a_service",
    }]


def test_a_missing_drupal_class_is_psr4_unresolved_and_other_vendors_are_ignored(tmp_path):
    services = (
        "services:\n"
        "  nope.missing:\n"
        "    class: Drupal\\nope\\Missing\n"
        "  sym:\n"
        "    class: Symfony\\Component\\HttpFoundation\\RequestStack\n"
    )
    _site(tmp_path, _module("foo", services))
    r = build_registry(tmp_path)
    assert r.types == {}
    assert r.unresolved == [{
        "class": "Drupal\\nope\\Missing",
        "file": (tmp_path / "web/modules/custom/foo/foo.services.yml").as_posix(),
        "reason": "psr4_unresolved",
    }]


def test_a_service_provider_alter_is_recorded(tmp_path):
    provider = r"""<?php
namespace Drupal\foo;
use Drupal\Core\DependencyInjection\ContainerBuilder;
use Drupal\Core\DependencyInjection\ServiceProviderBase;
class FooServiceProvider extends ServiceProviderBase {
  public function alter(ContainerBuilder $c) { $c->getDefinition('x')->setClass(Y::class); }
}
"""
    _site(tmp_path, _module("foo", "services: {}\n", {"src/FooServiceProvider.php": provider}))
    r = build_registry(tmp_path)
    assert r.unresolved == [{
        "class": "Drupal\\foo\\FooServiceProvider",
        "file": (tmp_path / "web/modules/custom/foo/src/FooServiceProvider.php").as_posix(),
        "reason": "service_provider_alter",
    }]


def test_a_discovery_built_elsewhere_is_dynamic(tmp_path):
    manager = r"""<?php
namespace Drupal\foo;
class FooManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  protected function getDiscovery() {
    return $this->buildDiscovery();
  }
}
"""
    _site(tmp_path, _module("foo", FOO_SERVICES, {"src/FooManager.php": manager}))
    r = build_registry(tmp_path)
    assert r.types["foo"].discovery == "dynamic"
    assert r.unresolved == [{
        "class": "Drupal\\foo\\FooManager",
        "file": (tmp_path / "web/modules/custom/foo/src/FooManager.php").as_posix(),
        "reason": "dynamic_discovery",
    }]


def test_sdc_and_migrations_are_deferred(tmp_path):
    sdc = r"""<?php
namespace Drupal\Core\Theme;
use Drupal\Core\Plugin\DefaultPluginManager;
class ComponentPluginManager extends DefaultPluginManager {
  protected function getDiscovery() {
    return new DirectoryWithMetadataPluginDiscovery($this->directories, 'components', $this->fs);
  }
}
"""
    migrations = r"""<?php
namespace Drupal\mig;
use Drupal\Core\Plugin\DefaultPluginManager;
use Drupal\Core\Plugin\Discovery\YamlDirectoryDiscovery;
class Loader extends DefaultPluginManager {
  protected function getDiscovery() {
    return new YamlDirectoryDiscovery($dirs, 'migrate');
  }
}
"""
    core_services = "services:\n  plugin.manager.sdc:\n    class: Drupal\\Core\\Theme\\ComponentPluginManager\n"
    mig_services = "services:\n  plugin.manager.mig:\n    class: Drupal\\mig\\Loader\n"
    _site(tmp_path, {
        "web/core/core.services.yml": core_services,
        "web/core/lib/Drupal/Core/Theme/ComponentPluginManager.php": sdc,
        **_module("mig", mig_services, {"src/Loader.php": migrations}),
    })
    r = build_registry(tmp_path)
    assert r.types["sdc"].deferred_to == "P5"
    assert r.types["mig"].deferred_to == "P6"
    assert r.types["mig"].discovery != "dynamic"
    assert r.types["sdc"].discovery != "dynamic"
    assert r.unresolved == []
    assert r.by_yaml_name() == {}


def test_find_web_root(tmp_path):
    _site(tmp_path, _module("foo", "services: {}\n"))
    web = tmp_path / "web"
    assert find_web_root(tmp_path) == web
    assert find_web_root(web / "modules/custom/foo") == web
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    assert find_web_root(empty) is None


def test_no_drupal_core_is_recorded(tmp_path):
    r = build_registry(tmp_path)
    assert r.web_root is None
    assert r.unresolved == [{"class": "", "file": "", "reason": "no_drupal_core"}]


def test_root_yaml(tmp_path):
    d = "web/modules/custom/foo"
    _site(tmp_path, {
        **_module("foo", "services: {}\n"),
        f"{d}/foo.bar.yml": "a: 1\n",
        f"{d}/.gitlab-ci.yml": "a: 1\n",
        f"{d}/config/x.yml": "a: 1\n",
        f"{d}/config/install/foo.settings.yml": "a: 1\n",
        "web/core/core.link_relation_types.yml": "a: 1\n",
    })
    r = build_registry(tmp_path)
    got = {Path(p).relative_to(tmp_path).as_posix() for p in r.root_yaml}
    assert got == {
        f"{d}/foo.bar.yml",
        f"{d}/foo.services.yml",
        "web/core/core.link_relation_types.yml",
        "web/core/core.services.yml",
    }


def test_json_round_trip_and_type_id(tmp_path):
    _site(tmp_path, _module("foo", FOO_SERVICES, {"src/FooManager.php": D11_MANAGER}))
    r = build_registry(tmp_path)
    assert Registry.from_json(r.to_json()) == r
    assert type_id("menu.link") == "drupal_plugin_type_menu_link"


def test_unparsable_inputs_are_parse_errors(tmp_path):
    _site(tmp_path, _module("foo", "services: [unclosed\n", {"src/BrokenManager.php": "<?php\n"}))
    r = build_registry(tmp_path)
    d = tmp_path / "web/modules/custom/foo"
    assert r.types == {}
    assert r.unresolved == [
        {"class": "", "file": (d / "foo.services.yml").as_posix(), "reason": "parse_error"},
        {"class": "", "file": (d / "src/BrokenManager.php").as_posix(), "reason": "parse_error"},
    ]


CTOR_YAML_MANAGER = r"""<?php
namespace Drupal\foo;
use Drupal\Core\Plugin\Discovery\YamlDiscovery;
class FooManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  public function __construct($module_handler) {
    $yaml_discovery = new YamlDiscovery('foo.contexts', $module_handler->getModuleDirectories());
    $this->discovery = new ContainerDerivativeDiscoveryDecorator($yaml_discovery);
    $this->alterInfo('foo_context_info');
  }
}
"""


def test_a_discovery_built_in_the_constructor_is_learned(tmp_path):
    _site(tmp_path, _module("foo", FOO_SERVICES, {"src/FooManager.php": CTOR_YAML_MANAGER}))
    r = build_registry(tmp_path)
    t = r.types["foo"]
    assert (t.discovery, t.yaml_name, t.alter_hook) == ("yaml", "foo.contexts", "foo_context_info")
    assert list(r.by_yaml_name()) == ["foo.contexts"]
    assert r.unresolved == []


def test_get_discovery_wins_over_constructor_discoveries(tmp_path):
    manager = r"""<?php
namespace Drupal\foo;
class FooManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  public function __construct($module_handler) {
    $this->discovery = new YamlDiscovery('from.ctor', $module_handler->getModuleDirectories());
  }
  protected function getDiscovery() {
    return new YamlDiscovery('from.method', $this->moduleHandler->getModuleDirectories());
  }
}
"""
    _site(tmp_path, _module("foo", FOO_SERVICES, {"src/FooManager.php": manager}))
    assert build_registry(tmp_path).types["foo"].yaml_name == "from.method"


def test_a_file_declaring_another_class_is_psr4_unresolved(tmp_path):
    services = "services:\n  foo.wrong:\n    class: Drupal\\foo\\Wrong\n"
    wrong = "<?php\nnamespace Drupal\\other;\nclass Wrong extends \\Drupal\\Core\\Plugin\\DefaultPluginManager {}\n"
    _site(tmp_path, _module("foo", services, {"src/Wrong.php": wrong}))
    r = build_registry(tmp_path)
    assert r.types == {}
    assert r.unresolved == [{
        "class": "Drupal\\foo\\Wrong",
        "file": (tmp_path / "web/modules/custom/foo/foo.services.yml").as_posix(),
        "reason": "psr4_unresolved",
    }]


def test_an_unreadable_service_class_is_a_parse_error(tmp_path):
    services = "services:\n  foo.broken:\n    class: Drupal\\foo\\Broken\n"
    _site(tmp_path, _module("foo", services, {"src/Broken.php": "<?php\ntrait Broken {}\n"}))
    r = build_registry(tmp_path)
    assert r.types == {}
    assert r.unresolved == [{
        "class": "",
        "file": (tmp_path / "web/modules/custom/foo/src/Broken.php").as_posix(),
        "reason": "parse_error",
    }]


def test_a_class_backing_several_services_is_recorded_once(tmp_path):
    manager = r"""<?php
namespace Drupal\foo;
class FooManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  protected function getDiscovery() { return $this->buildDiscovery(); }
}
"""
    services = (
        "services:\n"
        "  plugin.manager.one:\n    class: Drupal\\foo\\FooManager\n"
        "  plugin.manager.two:\n    class: Drupal\\foo\\FooManager\n"
    )
    _site(tmp_path, _module("foo", services, {"src/FooManager.php": manager}))
    r = build_registry(tmp_path)
    assert sorted(r.types) == ["one", "two"]
    assert _reasons(r) == [("dynamic_discovery", "Drupal\\foo\\FooManager")]


REPLACEMENT_MANAGER = r"""<?php
namespace Drupal\foo;
class FooReplacement extends \Drupal\Core\Plugin\DefaultPluginManager {}
"""


def test_a_manager_a_service_provider_swaps_in_is_named(tmp_path):
    """symfony_mailer's `MailManagerReplacement` on the reference corpus: not a
    service, not `*Manager.php`, reached only through `alter()`'s `setClass()`.
    The class it sets is recorded, not just the provider (spec §6 criterion 1)."""
    provider = r"""<?php
namespace Drupal\foo;
use Drupal\Core\DependencyInjection\ServiceProviderBase;
use Drupal\foo\Other\Swapped as Alias;
class FooServiceProvider extends ServiceProviderBase {
  public function alter($c) {
    $c->getDefinition('plugin.manager.mail')->setClass('Drupal\foo\FooReplacement');
    $c->getDefinition('plugin.manager.bar')->setClass(Alias::class);
    $c->getDefinition('plugin.manager.foo')->setClass(FooManager::class);
    $c->getDefinition('x')->setClass(NotAManager::class);
  }
}
"""
    swapped = r"""<?php
namespace Drupal\foo\Other;
class Swapped extends \Drupal\Core\Plugin\DefaultPluginManager {}
"""
    _site(tmp_path, _module("foo", FOO_SERVICES, {
        "src/FooServiceProvider.php": provider,
        "src/FooReplacement.php": REPLACEMENT_MANAGER,
        "src/Other/Swapped.php": swapped,
        "src/FooManager.php": D11_MANAGER,
    }))
    r = build_registry(tmp_path)
    provider_file = (tmp_path / "web/modules/custom/foo/src/FooServiceProvider.php").as_posix()
    assert sorted(u["class"] for u in r.unresolved
                  if u["reason"] == "service_provider_alter" and u["file"] == provider_file) == [
        "Drupal\\foo\\FooReplacement",
        "Drupal\\foo\\FooServiceProvider",
        "Drupal\\foo\\Other\\Swapped",
    ]
    # FooManager is already a type; the swap does not make it unresolved too.
    assert list(r.types) == ["foo"]
