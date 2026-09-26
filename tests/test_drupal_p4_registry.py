"""The registry learns the P4 maps (P4 Task 1, spec §5.1, §5.4, §6.1, §7.1, §7.2, §8).

`\\Drupal::` shortcuts, custom constructor/`create()` facts, and entity
types, forms and `*Events` constants across the boundary, plus the PHP
readers behind them (Doctrine annotations, class facts).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from graphify.drupal.discovery import Registry, affected_files, build_registry, prepare_run
from graphify.drupal.php_classes import (
    Annotation,
    ClassFacts,
    CtorParam,
    read_class_annotations,
    read_class_facts,
    read_drupal_shortcuts,
)
from tests.test_drupal_discovery import _module, _site
from tests.test_drupal_discovery_seam import _isolated_discovery_state  # noqa: F401


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- \Drupal:: shortcuts (spec §7.1) ------------------------------------------

DRUPAL_PHP = r"""<?php

use Drupal\Core\DependencyInjection\ContainerNotInitializedException;

class Drupal {

  protected static $container;

  public static function getContainer() {
    if (static::$container === NULL) {
      throw new ContainerNotInitializedException('\Drupal::$container is not initialized yet.');
    }
    return static::$container;
  }

  public static function service($id) {
    return static::getContainer()->get($id);
  }

  public static function request() {
    return static::getContainer()->get('request_stack')->getCurrentRequest();
  }

  public static function entityTypeManager() {
    return static::getContainer()->get('entity_type.manager');
  }

  public static function database() {
    return static::getContainer()->get('database');
  }

  public static function currentUser() {
    return static::$container->get('current_user');
  }

  public static function config($name) {
    return static::getContainer()->get('config.factory')->get($name);
  }

  protected static function hidden() {
    return static::getContainer()->get('hidden');
  }

  public function notStatic() {
    return static::getContainer()->get('not_static');
  }

  public static function notOnlyReturn() {
    $x = 1;
    return static::getContainer()->get('two_statements');
  }

}
"""


def test_shortcuts_are_the_exact_container_get_returns(tmp_path):
    path = _write(tmp_path / "core/lib/Drupal.php", DRUPAL_PHP)

    assert read_drupal_shortcuts(path) == {
        "entityTypeManager": "entity_type.manager",
        "database": "database",
        "currentUser": "current_user",
    }


def test_shortcuts_of_a_missing_or_broken_file_are_empty(tmp_path):
    assert read_drupal_shortcuts(tmp_path / "nope.php") == {}
    broken = _write(tmp_path / "Drupal.php", "<?php class Drupal { public static function x( {")
    assert read_drupal_shortcuts(broken) == {}


# -- annotations (spec §5.1) --------------------------------------------------

WEBFORM_HANDLER = r"""<?php

namespace Drupal\foo\Plugin\WebformHandler;

use Drupal\webform\Annotation\WebformHandler;
use Drupal\webform\Plugin\WebformHandlerBase;

/**
 * Sends a thing.
 *
 * @WebformHandler(
 *   id = "x",
 *   label = @Translation("X"),
 *   category = @Translation("Notification"),
 *   description = @Translation("Sends a thing."),
 *   cardinality = \Drupal\webform\Plugin\WebformHandlerInterface::CARDINALITY_UNLIMITED,
 *   results = \Drupal\webform\Plugin\WebformHandlerInterface::RESULTS_PROCESSED,
 *   tokens = TRUE,
 * )
 *
 * @see \Drupal\webform\Plugin\WebformHandlerBase
 */
class XHandler extends WebformHandlerBase {
}
"""

FOO_ENTITY = r"""<?php

namespace Drupal\foo\Entity;

use Drupal\Core\Entity\ContentEntityBase;

/**
 * Defines the foo entity.
 *
 * @ContentEntityType(
 *   id = "foo",
 *   label = @Translation("Foo"),
 *   bundle_entity_type = "foo_type",
 *   handlers = {
 *     "storage" = "Drupal\foo\FooStorage",
 *     "list_builder" = "Drupal\foo\FooListBuilder",
 *     "form" = {
 *       "edit" = "Drupal\foo\Form\FooForm",
 *       "delete" = "Drupal\Core\Entity\ContentEntityDeleteForm",
 *     },
 *     "route_provider" = {
 *       "html" = "Drupal\Core\Entity\Routing\AdminHtmlRouteProvider",
 *     },
 *   },
 *   base_table = "foo",
 *   admin_permission = "administer foo",
 *   entity_keys = {
 *     "id" = "id",
 *     "label" = "name",
 *   },
 *   links = {
 *     "canonical" = "/foo/{foo}",
 *   },
 * )
 */
class Foo extends ContentEntityBase {
}
"""


def test_an_annotation_keeps_only_its_literal_keys(tmp_path):
    path = _write(tmp_path / "XHandler.php", WEBFORM_HANDLER)

    found = read_class_annotations(path)

    assert found == [(
        "Drupal\\foo\\Plugin\\WebformHandler\\XHandler",
        Annotation(name="Drupal\\webform\\Annotation\\WebformHandler", line=11, values={"id": "x"}),
    )]


def test_an_entity_type_annotation_gives_nested_handler_maps(tmp_path):
    path = _write(tmp_path / "Foo.php", FOO_ENTITY)

    [(fqcn, annotation)] = read_class_annotations(path)

    assert fqcn == "Drupal\\foo\\Entity\\Foo"
    # Not `use`d: the short name resolves against the file's namespace.
    assert annotation.name == "Drupal\\foo\\Entity\\ContentEntityType"
    assert annotation.values == {
        "id": "foo",
        "bundle_entity_type": "foo_type",
        "handlers": {
            "storage": "Drupal\\foo\\FooStorage",
            "list_builder": "Drupal\\foo\\FooListBuilder",
            "form": {
                "edit": "Drupal\\foo\\Form\\FooForm",
                "delete": "Drupal\\Core\\Entity\\ContentEntityDeleteForm",
            },
            "route_provider": {"html": "Drupal\\Core\\Entity\\Routing\\AdminHtmlRouteProvider"},
        },
        "base_table": "foo",
        "admin_permission": "administer foo",
    }


def test_annotation_values_class_constants_lists_and_escaped_quotes(tmp_path):
    path = _write(tmp_path / "Bar.php", r'''<?php
namespace Drupal\foo;

use Drupal\foo\Annotation\Thing;
use Drupal\foo\Deriver\BarDeriver;

/**
 * @Thing(
 *   id: "say ""hi""",
 *   deriver = BarDeriver::class,
 *   handlers = {"a", "b", @Translation("c"), {"k" = "v"}}
 * )
 */
class Bar {}
''')

    [(_fqcn, annotation)] = read_class_annotations(path)

    assert annotation.name == "Drupal\\foo\\Annotation\\Thing"
    assert annotation.values == {
        "id": 'say "hi"',
        "deriver": {"class": "Drupal\\foo\\Deriver\\BarDeriver"},
        "handlers": ["a", "b", {"k": "v"}],
    }


def test_a_broken_annotation_is_skipped_and_the_next_one_read(tmp_path):
    path = _write(tmp_path / "Baz.php", r"""<?php
namespace Drupal\foo;

/**
 * @Broken(id = "x", handlers = {"a" = )
 * @Good(id = "y")
 */
class Baz {}

/**
 * @param string $x
 * @code
 * not an annotation
 * @endcode
 */
class NoAnnotation {}

/** @Unterminated(id = "z" */
class Unterminated {}
""")

    found = read_class_annotations(path)

    assert [(c, a.name, a.values) for c, a in found] == [
        ("Drupal\\foo\\Baz", "Drupal\\foo\\Good", {"id": "y"}),
    ]


def test_a_docblock_not_directly_above_the_class_is_not_its_annotation(tmp_path):
    path = _write(tmp_path / "Far.php", r"""<?php
namespace Drupal\foo;

/**
 * @Thing(id = "far")
 */

use Drupal\foo\Other;

class Far {}
""")
    assert read_class_annotations(path) == []


def test_annotation_reader_never_raises(tmp_path):
    assert read_class_annotations(tmp_path / "missing.php") == []
    deep = "{" * 5000 + "}" * 5000
    path = _write(tmp_path / "Deep.php", f"<?php\n/**\n * @Thing(handlers = {deep})\n */\nclass Deep {{}}\n")
    assert read_class_annotations(path) == []


# -- class facts (spec §6.1, §7.2, §8) ----------------------------------------

FACTS_PHP = r"""<?php

namespace Drupal\foo\Form;

use Drupal\Core\Form\FormBase;
use Drupal\foo\FooHelperInterface;
use Symfony\Component\DependencyInjection\ContainerInterface;

class PromotedForm extends FormBase {

  const MODE = 'edit';
  public const string OTHER = "other", NUMBER = 3;

  public function __construct(
    protected FooHelperInterface $helper,
    private readonly ?\Drupal\Core\Session\AccountInterface $account,
    array $options,
  ) {}

  public static function create(ContainerInterface $container) {
    return new static($container->get('foo.helper'), $container->get('current_user')->getAccount(), []);
  }

  public function getFormId() {
    return 'foo_promoted';
  }

  public function getBaseFormId() {
    return "foo_base";
  }

}

class AssignedForm extends PromotedForm {

  protected $helper2;

  public function __construct(FooHelperInterface $h, $b, string $c) {
    parent::__construct($h, $b, [$c]);
    $this->helper2 = $h;
    $this->other = $b ?? NULL;
  }

  public static function create(ContainerInterface $c) {
    $x = 1;
    return new self($c->get('a'), $x, $c->get('b'));
  }

  public function getFormId() {
    return 'foo_' . $this->mode;
  }

}

class Plain {

  public static function create(ContainerInterface $container) {
    return new Plain($container->get('named'));
  }

}
"""


def test_class_facts(tmp_path):
    path = _write(tmp_path / "PromotedForm.php", FACTS_PHP)

    promoted, assigned, plain = read_class_facts(path)

    assert promoted == ClassFacts(
        fqcn="Drupal\\foo\\Form\\PromotedForm",
        file=path.as_posix(),
        extends="Drupal\\Core\\Form\\FormBase",
        params=(
            CtorParam("helper", "Drupal\\foo\\FooHelperInterface", True),
            CtorParam("account", "Drupal\\Core\\Session\\AccountInterface", True),
            CtorParam("options", "", False),
        ),
        assigns={},
        parent_args=(),
        create_args=("foo.helper", "", ""),
        form_id="foo_promoted",
        base_form_id="foo_base",
        constants={"MODE": "edit", "OTHER": "other"},
    )
    assert assigned == ClassFacts(
        fqcn="Drupal\\foo\\Form\\AssignedForm",
        file=path.as_posix(),
        extends="Drupal\\foo\\Form\\PromotedForm",
        params=(
            CtorParam("h", "Drupal\\foo\\FooHelperInterface", False),
            CtorParam("b", "", False),
            CtorParam("c", "", False),
        ),
        assigns={"helper2": "h"},
        parent_args=("h", "b", ""),
        create_args=("a", "", "b"),
        form_id="",
        base_form_id="",
        constants={},
    )
    assert plain.fqcn == "Drupal\\foo\\Form\\Plain"
    assert plain.extends == ""
    assert plain.params == ()
    assert plain.create_args == ("named",)


def test_class_facts_round_trip_through_a_dict(tmp_path):
    path = _write(tmp_path / "PromotedForm.php", FACTS_PHP)
    for facts in read_class_facts(path):
        data = json.loads(json.dumps(facts.to_dict()))
        assert ClassFacts.from_dict(data) == facts


def test_class_facts_never_raise(tmp_path):
    assert read_class_facts(tmp_path / "missing.php") == []
    path = _write(tmp_path / "Bad.php", "<?php class { function __construct(")
    assert isinstance(read_class_facts(path), list)
    deep = "(" * 3000 + "1" + ")" * 3000
    path = _write(tmp_path / "Deep.php",
                  f"<?php class Deep {{ public function __construct($a) {{ $x = {deep}; }} }}")
    assert isinstance(read_class_facts(path), list)


# -- the registry (spec §5.4, §6.1, §7.2, §8) --------------------------------

CORE_FORM = r"""<?php
namespace Drupal\Core\Form;
class ConfirmFormBase {}
class SiteForm {
  public function getFormId() {
    return 'system_site_form';
  }
}
"""

CONTRIB_NODE = r"""<?php
namespace Drupal\node\Entity;

use Drupal\Core\Entity\Attribute\ContentEntityType;
use Drupal\Core\Entity\EditorialContentEntityBase;

#[ContentEntityType(
  id: 'node',
  label: new TranslatableMarkup('Content'),
  handlers: ['storage' => 'Drupal\node\NodeStorage'],
)]
class Node extends EditorialContentEntityBase {}
"""

CONTRIB_WEBFORM = r"""<?php
namespace Drupal\webform\Entity;

/**
 * @ConfigEntityType(
 *   id = "webform",
 *   label = @Translation("Webform"),
 * )
 */
class Webform {}
"""

CONTRIB_NODE_FORM = r"""<?php
namespace Drupal\node;

class NodeForm {
  public function getBaseFormId() {
    return 'node_form';
  }
  public function getFormId() {
    return $this->entity->bundle() . '_node_form';
  }
}
"""

CONTRIB_EVENTS = r"""<?php
namespace Drupal\webform;

final class WebformEvents {
  const SUBMISSION_SAVE = 'webform.submission.save';
  const NOT_A_STRING = 3;
}
"""

CONTRIB_HELPER = r"""<?php
namespace Drupal\webform;
class WebformHelper {
  public function __construct(Foo $foo) {}
}
"""

FOO_BASE = r"""<?php
namespace Drupal\foo;

use Drupal\foo\FooHelperInterface;

class FooBase {
  public function __construct(protected FooHelperInterface $helper) {}
}
"""

FOO_CHILD = r"""<?php
namespace Drupal\foo;

class FooChild extends FooBase {}
"""

FOO_GRANDCHILD = r"""<?php
namespace Drupal\foo;

class FooGrandChild extends FooChild {}
"""

FOO_SETTINGS_FORM = r"""<?php
namespace Drupal\foo\Form;

use Drupal\Core\Form\ConfigFormBase;

class FooSettingsForm extends ConfigFormBase {
  public function getFormId() {
    return 'foo_settings';
  }
}
"""

FOO_EVENTS = r"""<?php
namespace Drupal\foo\Event;

class FooEvents {
  const PING = 'foo.ping';
}
"""

FOO_ENTITY_ATTR = r"""<?php
namespace Drupal\foo\Entity;

use Drupal\Core\Entity\Attribute\ConfigEntityType;

#[ConfigEntityType('foo_type', label: 'Foo type')]
class FooType {}
"""


def _p4_site(tmp_path: Path) -> Path:
    files = {
        "web/core/lib/Drupal.php": DRUPAL_PHP,
        "web/core/lib/Drupal/Core/Form/SiteForm.php": CORE_FORM,
        "web/modules/contrib/node/node.info.yml": "name: Node\ntype: module\n",
        "web/modules/contrib/node/src/Entity/Node.php": CONTRIB_NODE,
        "web/modules/contrib/node/src/NodeForm.php": CONTRIB_NODE_FORM,
        "web/modules/contrib/webform/webform.info.yml": "name: Webform\ntype: module\n",
        "web/modules/contrib/webform/src/Entity/Webform.php": CONTRIB_WEBFORM,
        "web/modules/contrib/webform/src/WebformEvents.php": CONTRIB_EVENTS,
        "web/modules/contrib/webform/src/WebformHelper.php": CONTRIB_HELPER,
    }
    files.update(_module("foo", "services: {}\n", {
        "src/FooBase.php": FOO_BASE,
        "src/FooChild.php": FOO_CHILD,
        "src/FooGrandChild.php": FOO_GRANDCHILD,
        "src/Form/FooSettingsForm.php": FOO_SETTINGS_FORM,
        "src/Event/FooEvents.php": FOO_EVENTS,
        "src/Entity/Foo.php": FOO_ENTITY,
        "src/Entity/FooType.php": FOO_ENTITY_ATTR,
    }))
    return _site(tmp_path, files)


def test_the_registry_learns_the_p4_maps(tmp_path):
    root = _p4_site(tmp_path)
    registry = build_registry(root)

    assert registry.shortcuts == {
        "entityTypeManager": "entity_type.manager",
        "database": "database",
        "currentUser": "current_user",
    }
    assert registry.entity_types == {
        "node": ("node", "Drupal\\node\\Entity\\Node"),
        "webform": ("webform", "Drupal\\webform\\Entity\\Webform"),
        "foo": ("foo", "Drupal\\foo\\Entity\\Foo"),
        "foo_type": ("foo", "Drupal\\foo\\Entity\\FooType"),
    }
    assert registry.forms == {
        "system_site_form": ("core", "Drupal\\Core\\Form\\SiteForm"),
        "node_form": ("node", "Drupal\\node\\NodeForm"),
        "foo_settings": ("foo", "Drupal\\foo\\Form\\FooSettingsForm"),
    }
    assert registry.event_constants == {
        "Drupal\\webform\\WebformEvents::SUBMISSION_SAVE": "webform.submission.save",
        "Drupal\\foo\\Event\\FooEvents::PING": "foo.ping",
    }
    # Custom classes only: every class of the custom module, none of contrib's.
    assert set(registry.class_facts) == {
        "Drupal\\foo\\FooBase", "Drupal\\foo\\FooChild", "Drupal\\foo\\FooGrandChild",
        "Drupal\\foo\\Form\\FooSettingsForm", "Drupal\\foo\\Event\\FooEvents",
        "Drupal\\foo\\Entity\\Foo", "Drupal\\foo\\Entity\\FooType",
    }
    base = ClassFacts.from_dict(registry.class_facts["Drupal\\foo\\FooBase"])
    assert base.params == (CtorParam("helper", "Drupal\\foo\\FooHelperInterface", True),)
    assert base.file == (root / "web/modules/custom/foo/src/FooBase.php").resolve().as_posix()


def test_the_p4_maps_round_trip_through_json(tmp_path):
    registry = build_registry(_p4_site(tmp_path))
    data = json.loads(json.dumps(registry.to_json()))
    again = Registry.from_json(data)

    for name in ("shortcuts", "class_facts", "entity_types", "forms", "event_constants"):
        assert getattr(again, name) == getattr(registry, name), name
    assert again.to_json() == registry.to_json()


def test_an_old_registry_file_without_the_p4_maps_still_loads():
    registry = Registry.from_json({"web_root": None})
    assert (registry.shortcuts, registry.class_facts, registry.entity_types,
            registry.forms, registry.event_constants) == ({}, {}, {}, {}, {})


def test_a_realm_opted_in_through_drupal_include_gets_class_facts(tmp_path):
    root = _p4_site(tmp_path)
    (root / ".graphifyrc").write_text("drupal.include = contrib\n", encoding="utf-8")
    from graphify.drupal import boundary

    boundary.clear_caches()
    try:
        registry = build_registry(root)
    finally:
        boundary.clear_caches()
    assert "Drupal\\webform\\WebformHelper" in registry.class_facts
    assert "Drupal\\Core\\Form\\SiteForm" not in registry.class_facts


def test_an_ignored_file_contributes_nothing(tmp_path):
    root = _p4_site(tmp_path)
    ignored = (root / "web/modules/contrib/webform/src/WebformEvents.php").resolve()
    registry = build_registry(root, is_ignored=lambda p: Path(p).resolve() == ignored)
    assert "Drupal\\webform\\WebformEvents::SUBMISSION_SAVE" not in registry.event_constants
    assert "Drupal\\foo\\Event\\FooEvents::PING" in registry.event_constants


# -- affected_files (spec §9, second bullet) --------------------------------


def test_a_changed_constructor_forces_its_file_and_in_graph_subclasses(tmp_path):
    root = _p4_site(tmp_path)
    previous = build_registry(root)
    assert affected_files(previous, build_registry(root)) == set()

    src = root / "web/modules/custom/foo/src"
    (src / "FooBase.php").write_text(FOO_BASE.replace("FooHelperInterface $helper",
                                                      "FooHelperInterface $helper, $more"),
                                     encoding="utf-8")
    current = build_registry(root)

    forced = affected_files(previous, current)
    expected = {(src / name).resolve().as_posix()
                for name in ("FooBase.php", "FooChild.php", "FooGrandChild.php")}
    assert expected <= forced
    assert (src / "Form/FooSettingsForm.php").resolve().as_posix() not in forced


def test_a_removed_class_forces_its_old_file_and_subclasses(tmp_path):
    root = _p4_site(tmp_path)
    previous = build_registry(root)
    src = root / "web/modules/custom/foo/src"
    (src / "FooChild.php").unlink()
    current = build_registry(root)

    forced = affected_files(previous, current)
    assert (src / "FooChild.php").resolve().as_posix() in forced
    assert (src / "FooGrandChild.php").resolve().as_posix() in forced
    assert (src / "FooBase.php").resolve().as_posix() not in forced


# -- corpus measurement --------------------------------------------------------

CORPUS = Path(os.environ.get("DRUPAL_CORPUS", "/home/user/Projects/FormsRemote"))


@pytest.mark.skipif(not (CORPUS / "web" / "core").is_dir(), reason="reference Drupal corpus not present")
def test_corpus_p4_maps_and_prepare_run_time(tmp_path, _isolated_discovery_state):
    started = time.perf_counter()
    registry = prepare_run(CORPUS, cache_root=tmp_path)
    elapsed = time.perf_counter() - started

    assert registry is not None
    # Spec §7.1: 40 `->get(` lines in core 11.4.7's Drupal.php, 28 of them a
    # method's sole `return static::getContainer()->get('<literal>')`. The
    # other 12 are `service($id)`, `hasRequest()`, `request()`, `cache($bin)`,
    # `classResolver()`'s two (a guarded early return, so not the sole
    # statement), and the chained `keyValueExpirable`, `config`, `queue`,
    # `keyValue`, `isConfigSyncing` and `logger`.
    assert len(registry.shortcuts) == 28
    assert registry.shortcuts["entityTypeManager"] == "entity_type.manager"
    assert "service" not in registry.shortcuts and "request" not in registry.shortcuts
    # Spec §2: 8 custom entity types; core and contrib add many more.
    custom_entity_types = {k for k, (_p, cls) in registry.entity_types.items()
                           if any(cls.startswith(f"Drupal\\{ext}\\") for ext in _custom_extensions())}
    assert len(custom_entity_types) == 8, sorted(custom_entity_types)
    assert {"node", "user", "taxonomy_term"} <= set(registry.entity_types)
    assert len(registry.forms) > 34
    assert registry.class_facts
    assert all(ClassFacts.from_dict(v).fqcn == k for k, v in registry.class_facts.items())
    assert registry.event_constants
    # Global constraint: prepare_run(FormsRemote) stays under 5 s.
    assert elapsed < 5.0


def _custom_extensions() -> set[str]:
    out: set[str] = set()
    for base in ("web/modules/custom", "web/themes/custom", "web/profiles"):
        directory = CORPUS / base
        if directory.is_dir():
            out.update(p.name.split(".")[0] for p in directory.rglob("*.info.yml")
                       if "/tests/" not in p.as_posix())
    return out
