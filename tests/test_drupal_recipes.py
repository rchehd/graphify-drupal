"""recipe.yml (spec §5.2)."""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.yaml_common import config_id, recipe_id
from graphify.drupal.yaml_config import extract_drupal_config
from graphify.drupal.yaml_extract import extension_id

RECIPE = """\
name: 'Blog'
type: 'Content type'
recipes:
  - core/recipes/tags_taxonomy
  - basic_html_format_editor
install:
  - node
  - claro
config:
  strict: false
  import:
    node:
      - node.type.article
    claro: '*'
  actions:
    node.type.article:
      setDescription: '${description}'
    user.role.editor:
      grantPermissions:
        - 'create article content'
"""


def _write(root: Path, text: str = RECIPE) -> Path:
    path = root / "recipes/blog/recipe.yml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    (root / "recipes/blog/content").mkdir()
    return path


def _by_target(result):
    return {e["target"]: e for e in result["edges"]}


def test_recipe_node(tmp_path):
    result = extract_drupal_config(_write(tmp_path))
    [recipe] = result["nodes"]
    assert recipe["id"] == recipe_id("blog")
    assert (recipe["label"], recipe["layer"]) == ("Blog", "extension")
    assert recipe["recipe_type"] == "Content type"
    assert recipe["has_content"] is True


def test_recipe_edges(tmp_path):
    edges = _by_target(extract_drupal_config(_write(tmp_path)))
    assert edges[extension_id("node")]["relation"] == "installs_extension"
    assert edges[recipe_id("tags_taxonomy")]["relation"] == "applies_recipe"
    assert edges[recipe_id("basic_html_format_editor")]["relation"] == "applies_recipe"
    assert edges[config_id("user.role.editor")]["relation"] == "config_action"
    assert edges[config_id("user.role.editor")]["confidence"] == "EXTRACTED"


def test_an_action_with_input_is_ambiguous_and_wins_over_import(tmp_path):
    article = _by_target(extract_drupal_config(_write(tmp_path)))[config_id("node.type.article")]
    assert article["relation"] == "config_action"
    assert article["confidence"] == "AMBIGUOUS"
    assert article["actions"] == ["setDescription"]


def test_a_wildcard_import_targets_the_extension(tmp_path):
    """claro is both installed and imported whole: install wins the pair, wildcard is kept."""
    text = RECIPE.replace("  - claro\n", "")
    claro = _by_target(extract_drupal_config(_write(tmp_path, text)))[extension_id("claro")]
    assert (claro["relation"], claro["wildcard"]) == ("imports_config", True)


def test_recipe_config_defines_config_edge(tmp_path):
    """A config file at recipes/blog/config/node.type.article.yml yields a defines_config edge."""
    _write(tmp_path)
    config_path = tmp_path / "recipes/blog/config/node.type.article.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("status: true\n", encoding="utf-8")
    result = extract_drupal_config(config_path)
    edges = _by_target(result)
    assert edges[config_id("node.type.article")]["relation"] == "defines_config"
    assert edges[config_id("node.type.article")]["source"] == recipe_id("blog")
    assert edges[config_id("node.type.article")]["install_mode"] == "recipe"
