"""Longest-first ordering for parallel runs (inttest/ordering.py)."""
from types import SimpleNamespace

from inttest import ordering


def _item(nodeid, requires=()):
    return SimpleNamespace(nodeid=nodeid, manifest=SimpleNamespace(requires=list(requires)))


def _needs_teradata(item):
    return "teradata" in item.manifest.requires


def test_known_durations_run_longest_first_then_teradata_then_the_rest():
    items = [_item("a"), _item("b", ["teradata"]), _item("c"), _item("d"), _item("e", ["teradata"])]
    ordered = ordering.longest_first(items, {"c": 10.0, "d": 900.0}, _needs_teradata)
    assert [i.nodeid for i in ordered] == ["d", "c", "b", "e", "a"]


def test_no_history_keeps_collection_order_after_teradata():
    items = [_item("a"), _item("b"), _item("t", ["teradata"])]
    assert [i.nodeid for i in ordering.longest_first(items, {}, _needs_teradata)] == ["t", "a", "b"]


def test_save_merges_and_load_survives_a_bad_file(tmp_path):
    path = tmp_path / ordering.FILE_NAME
    assert ordering.load(path) == {}
    ordering.save(path, {"a": 1.5})
    ordering.save(path, {"b": 2.0, "a": 3.0})
    assert ordering.load(path) == {"a": 3.0, "b": 2.0}
    path.write_text("not json")
    assert ordering.load(path) == {}


def test_nothing_recorded_writes_nothing(tmp_path):
    path = tmp_path / ordering.FILE_NAME
    ordering.save(path, {})
    assert not path.exists()
