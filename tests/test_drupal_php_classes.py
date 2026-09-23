from pathlib import Path

from graphify.drupal.php_classes import Arg, read_php_class, resolve_name

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

INTERFACE = r"""<?php
namespace Drupal\Core\Menu;
use Drupal\Component\Plugin\PluginManagerInterface;
interface MenuLinkManagerInterface extends PluginManagerInterface, \Countable {}
"""

ABSTRACT = "<?php\nnamespace A;\nabstract class Base extends \\Drupal\\Core\\Plugin\\DefaultPluginManager {}\n"


def _write(tmp_path: Path, text: str, name: str = "X.php") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_d11_constructor_is_read_with_names_resolved(tmp_path):
    cls = read_php_class(_write(tmp_path, D11_MANAGER))
    assert cls.fqcn == "Drupal\\foo\\FooManager"
    assert cls.kind == "class"
    assert cls.line == 10
    assert cls.extends == ("Drupal\\Core\\Plugin\\DefaultPluginManager",)
    assert cls.implements == ("Drupal\\foo\\FooManagerInterface",)
    assert cls.construct_args == (
        Arg("string", "Plugin/Foo"),
        Arg("other", ""),
        Arg("other", ""),
        Arg("class", "Drupal\\foo\\FooInterface"),
        Arg("class", "Drupal\\foo\\Attribute\\Foo"),
        Arg("string", "Drupal\\foo\\Annotation\\Foo"),
    )
    assert cls.alter_info == "foo_info"
    assert cls.has_get_discovery is False
    assert cls.discoveries == ()


def test_get_discovery_lists_every_discovery_outermost_first(tmp_path):
    cls = read_php_class(_write(tmp_path, YAML_MANAGER))
    assert cls.extends == ("Drupal\\Core\\Plugin\\DefaultPluginManager",)
    assert cls.has_get_discovery is True
    assert [d.cls for d in cls.discoveries] == [
        "Drupal\\bar\\AnnotatedClassDiscovery",
        "Drupal\\Core\\Plugin\\Discovery\\YamlDiscoveryDecorator",
        "Drupal\\bar\\ContainerDerivativeDiscoveryDecorator",
    ]
    assert cls.discoveries[1].args[1] == Arg("string", "bar")
    assert cls.construct_args is None


def test_an_interface_lists_all_its_parents(tmp_path):
    cls = read_php_class(_write(tmp_path, INTERFACE))
    assert cls.kind == "interface"
    assert cls.extends == ("Drupal\\Component\\Plugin\\PluginManagerInterface", "Countable")


def test_an_abstract_class_is_marked(tmp_path):
    assert read_php_class(_write(tmp_path, ABSTRACT)).kind == "abstract"


def test_unparsable_or_classless_files_return_none(tmp_path):
    assert read_php_class(_write(tmp_path, "<?php\nfunction f() {}\n")) is None
    assert read_php_class(tmp_path / "missing.php") is None


def test_resolve_name():
    uses = {"B": "A\\B", "C": "X\\Y"}
    assert resolve_name("\\Q\\R", "N", uses) == "Q\\R"
    assert resolve_name("B\\Sub", "N", uses) == "A\\B\\Sub"
    assert resolve_name("C", "N", uses) == "X\\Y"
    assert resolve_name("Local", "N", uses) == "N\\Local"
    assert resolve_name("Local", "", uses) == "Local"


CTOR_DISCOVERY = r"""<?php
namespace Drupal\m;
use Drupal\Core\Plugin\Discovery\YamlDiscovery;
class ContextManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  public function __construct($module_handler) {
    $yaml_discovery = new YamlDiscovery('m.contexts', $module_handler->getModuleDirectories());
    $this->discovery = new ContainerDerivativeDiscoveryDecorator($yaml_discovery);
    $this->helper = new Helper();
  }
}
"""


def test_discoveries_built_in_the_constructor_are_read(tmp_path):
    cls = read_php_class(_write(tmp_path, CTOR_DISCOVERY))
    assert cls.has_get_discovery is False
    assert cls.discoveries == ()
    assert cls.construct_args is None
    assert [d.cls for d in cls.construct_discoveries] == [
        "Drupal\\Core\\Plugin\\Discovery\\YamlDiscovery",
        "Drupal\\m\\ContainerDerivativeDiscoveryDecorator",
    ]
    assert cls.construct_discoveries[0].args[0] == Arg("string", "m.contexts")


def test_a_class_without_constructor_discoveries_has_none(tmp_path):
    assert read_php_class(_write(tmp_path, D11_MANAGER)).construct_discoveries == ()


TWO_DECLARATIONS = r"""<?php
namespace Drupal\redis\Cache;
use Drupal\Core\Cache\CacheTagsChecksumInterface;
interface ChecksumPreloadInterface {}
class Checksum implements CacheTagsChecksumInterface {}
"""


def test_the_declaration_named_after_the_file_wins(tmp_path):
    cls = read_php_class(_write(tmp_path, TWO_DECLARATIONS, "Checksum.php"))
    assert (cls.fqcn, cls.kind, cls.line) == ("Drupal\\redis\\Cache\\Checksum", "class", 5)
    assert cls.implements == ("Drupal\\Core\\Cache\\CacheTagsChecksumInterface",)


def test_a_file_named_after_no_declaration_reads_the_first(tmp_path):
    cls = read_php_class(_write(tmp_path, TWO_DECLARATIONS, "Other.php"))
    assert (cls.fqcn, cls.kind) == ("Drupal\\redis\\Cache\\ChecksumPreloadInterface", "interface")


def test_an_anonymous_class_method_is_not_the_managers_own(tmp_path):
    """Methods are read from the class body's own members: a `getDiscovery()`
    or `__construct()` of an anonymous class built inside another method is
    not the manager's (and the class body is no longer walked in full per
    method, which kept the corpus registry build near its 5 s budget)."""
    source = r"""<?php
namespace Drupal\foo;
class FooManager extends \Drupal\Core\Plugin\DefaultPluginManager {
  public function helper() {
    return new class {
      public function __construct() { parent::__construct('Plugin/Nope'); }
      protected function getDiscovery() { return new YamlDiscovery('nope', []); }
    };
  }
}
"""
    path = tmp_path / "FooManager.php"
    path.write_text(source, encoding="utf-8")
    cls = read_php_class(path)
    assert cls is not None
    assert cls.has_get_discovery is False
    assert cls.discoveries == ()
    assert cls.construct_args is None


def test_pathologically_nested_source_returns_none_rather_than_raising(tmp_path):
    # The limit is pinned: another test in the suite may have raised it, and
    # the walk must exceed it for the RecursionError to happen at all.
    import sys

    saved = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        depth = 3000
        p = tmp_path / "DeepManager.php"
        p.write_text(
            "<?php\nclass DeepManager {\n  public function __construct() {\n"
            f"    $x = {'(' * depth}1{')' * depth};\n  }}\n}}\n",
            encoding="utf-8",
        )
        assert read_php_class(p) is None
    finally:
        sys.setrecursionlimit(saved)
