"""Drupal ids are global: one id declared by several files is one entity.

Core's `_disambiguate_colliding_node_ids` salts an id apart whenever two source
files declare it, which is right for two same-named functions and wrong for a
container. A tag used by forty service files, or a service a test module
overrides, is one node; splitting it strands every edge written against the
bare id, and core drops those edges without a word.
"""
from __future__ import annotations

from pathlib import Path

from graphify.drupal.merge import collapse_drupal_duplicates
from graphify.drupal.yaml_common import service_id, tag_id


def _node(nid: str, source_file: str, origin: str = "static_yaml", **extra) -> dict:
    return {"id": nid, "source_file": source_file, "_origin": origin, **extra}


def test_duplicates_collapse_to_the_first_declaration_by_path():
    nodes = [
        _node("drupal_service_x", "web/modules/b/b.services.yml", class_name="B"),
        _node("drupal_service_x", "web/core/core.services.yml", class_name="Core"),
        _node("drupal_service_y", "web/core/core.services.yml"),
    ]
    collapse_drupal_duplicates(nodes)
    assert [n["id"] for n in nodes] == ["drupal_service_x", "drupal_service_y"]
    kept = nodes[0]
    assert kept["class_name"] == "Core"
    assert kept["declared_in"] == ["web/core/core.services.yml", "web/modules/b/b.services.yml"]
    assert "declared_in" not in nodes[1]


def test_declared_in_is_relative_to_the_scan_root(tmp_path):
    nodes = [
        _node("drupal_tag_x", str(tmp_path / "web/a/a.services.yml")),
        _node("drupal_tag_x", str(tmp_path / "web/b/b.services.yml")),
    ]
    collapse_drupal_duplicates(nodes, tmp_path)
    assert nodes[0]["declared_in"] == ["web/a/a.services.yml", "web/b/b.services.yml"]


def test_nodes_from_other_producers_are_left_for_core():
    nodes = [_node("foo", "a.py", origin="ast"), _node("foo", "b.py", origin="ast")]
    collapse_drupal_duplicates(nodes)
    assert len(nodes) == 2


def _corpus(tmp_path: Path, files: dict[str, str]) -> list[Path]:
    paths = []
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def _services(sid: str, extra: str = "") -> str:
    return (
        "services:\n"
        f"  {sid}:\n"
        f"    class: Drupal\\x\\{sid.replace('.', '_')}\n"
        "    tags:\n"
        "      - {name: event_subscriber}\n"
        f"{extra}"
    )


def test_a_shared_tag_is_one_node_through_the_real_pipeline(tmp_path):
    from graphify.extract import extract

    paths = _corpus(tmp_path, {
        "web/modules/custom/foo/foo.services.yml": _services("foo.sub"),
        "web/modules/custom/bar/bar.services.yml": _services("bar.sub"),
    })
    result = extract(paths, root=tmp_path)
    tags = [n for n in result["nodes"] if n.get("type") == "drupal_service_tag"]
    assert [n["id"] for n in tags] == [tag_id("event_subscriber")]
    tagged = {e["source"] for e in result["edges"]
              if e["relation"] == "tagged_as" and e["target"] == tag_id("event_subscriber")}
    assert tagged == {service_id("foo.sub"), service_id("bar.sub")}


def test_injection_into_an_overridden_service_survives(tmp_path):
    """`datetime.time` is declared by core and redeclared by a test module."""
    from graphify.extract import extract

    paths = _corpus(tmp_path, {
        "web/core/core.services.yml": _services("datetime.time"),
        "web/core/modules/update/tests/update_test/update_test.services.yml":
            _services("datetime.time"),
        "web/modules/custom/foo/foo.services.yml":
            _services("foo.clock", "    arguments: ['@datetime.time']\n"),
    })
    result = extract(paths, root=tmp_path)
    ids = [n["id"] for n in result["nodes"]]
    assert ids.count(service_id("datetime.time")) == 1
    survivor = next(n for n in result["nodes"] if n["id"] == service_id("datetime.time"))
    assert survivor["declared_in"] == [
        "web/core/core.services.yml",
        "web/core/modules/update/tests/update_test/update_test.services.yml",
    ]
    assert (service_id("foo.clock"), "injects_service", service_id("datetime.time")) in {
        (e["source"], e["relation"], e["target"]) for e in result["edges"]
    }


def test_the_lowest_rank_survives_and_gaps_are_filled():
    nodes = [
        _node("drupal_config_system_site", "web/core/modules/system/config/install/system.site.yml",
              _rank=4, install_mode="install", active=False),
        _node("drupal_config_system_site", "config/sync/system.site.yml", _rank=0, active=True),
    ]
    collapse_drupal_duplicates(nodes)
    [kept] = nodes
    assert kept["source_file"] == "config/sync/system.site.yml"
    assert kept["active"] is True
    assert kept["install_mode"] == "install"
    assert "_rank" not in kept


def test_rank_is_removed_from_single_nodes_too():
    nodes = [_node("drupal_config_a", "config/sync/a.yml", _rank=0)]
    collapse_drupal_duplicates(nodes)
    assert "_rank" not in nodes[0]


def test_unranked_duplicates_dont_get_gap_filled():
    nodes = [
        _node("drupal_service_x", "web/core/core.services.yml", class_name="Core"),
        _node("drupal_service_x", "web/modules/b/b.services.yml", class_name="B", deprecated=True),
    ]
    collapse_drupal_duplicates(nodes)
    [kept] = nodes
    assert kept["source_file"] == "web/core/core.services.yml"
    assert kept["class_name"] == "Core"
    assert "deprecated" not in kept
