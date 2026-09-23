"""*.libraries.yml and *.breakpoints.yml."""
from __future__ import annotations

from graphify.drupal.yaml_assets import extract_drupal_breakpoints, extract_drupal_libraries
from graphify.drupal.yaml_common import breakpoint_id, library_id
from graphify.drupal.yaml_extract import extension_id

LIBRARIES = """\
main:
  version: 1.x
  css:
    theme:
      css/main.css: {}
  js:
    js/main.js: {}
  dependencies:
    - core/once
    - foo/helper
helper:
  js:
    js/helper.js: {}
"""

BREAKPOINTS = """\
foo.narrow:
  label: Narrow
  mediaQuery: 'all and (min-width: 560px)'
  weight: 1
  multipliers: ['1x', '2x']
"""


def _write(tmp_path, name, text):
    path = tmp_path / "web/themes/custom/foo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(result):
    return {(e["source"], e["relation"], e["target"]) for e in result["edges"]}


def test_library_is_declared_by_its_owner(tmp_path):
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", LIBRARIES))
    assert (extension_id("foo"), "declares_library", library_id("foo", "main")) in _rel(result)


def test_library_dependencies_resolve_to_owner_scoped_ids(tmp_path):
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", LIBRARIES))
    rel = _rel(result)
    assert (library_id("foo", "main"), "library_depends_on", library_id("core", "once")) in rel
    assert (library_id("foo", "main"), "library_depends_on", library_id("foo", "helper")) in rel


def test_assets_are_attributes_not_edges(tmp_path):
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", LIBRARIES))
    main = next(n for n in result["nodes"] if n["id"] == library_id("foo", "main"))
    assert main["css"] == ["css/main.css"]
    assert main["js"] == ["js/main.js"]
    assert main["version"] == "1.x"
    # A .css/.js node belongs to P5; no edge yet.
    assert not any(e["relation"] == "library_has_asset" for e in result["edges"])


def test_js_attributes_are_not_mistaken_for_asset_paths(tmp_path):
    """js is one level deep; a file's `attributes:` mapping is not a css-style group."""
    text = (
        "main:\n"
        "  js:\n"
        "    js/deferred.js: {attributes: {defer: true}}\n"
        "  css:\n"
        "    theme:\n"
        "      css/print.css: {media: print}\n"
    )
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", text))
    main = next(n for n in result["nodes"] if n["id"] == library_id("foo", "main"))
    assert main["js"] == ["js/deferred.js"]
    assert main["css"] == ["css/print.css"]


def test_malformed_dependency_is_skipped_not_guessed(tmp_path):
    text = "main:\n  dependencies:\n    - no_slash_here\n"
    result = extract_drupal_libraries(_write(tmp_path, "foo.libraries.yml", text))
    assert not any(e["relation"] == "library_depends_on" for e in result["edges"])


def test_breakpoints(tmp_path):
    result = extract_drupal_breakpoints(_write(tmp_path, "foo.breakpoints.yml", BREAKPOINTS))
    bp = next(n for n in result["nodes"] if n["id"] == breakpoint_id("foo", "foo.narrow"))
    assert bp["label"] == "Narrow"
    assert bp["media_query"] == "all and (min-width: 560px)"
    assert (extension_id("foo"), "declares_breakpoint", bp["id"]) in _rel(result)


def test_neither_family_emits_an_extension_node(tmp_path):
    for name, text, fn in (
        ("foo.libraries.yml", LIBRARIES, extract_drupal_libraries),
        ("foo.breakpoints.yml", BREAKPOINTS, extract_drupal_breakpoints),
    ):
        result = fn(_write(tmp_path, name, text))
        assert not any(n["id"] == extension_id("foo") for n in result["nodes"])
