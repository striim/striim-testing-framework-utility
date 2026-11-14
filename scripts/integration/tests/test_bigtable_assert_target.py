"""assert.target on a bigtable route: manifest form, route predicate and the plugin branch.

Hermetic: no Docker, no emulator. The plugin branch is driven through IntYamlItem's method with a
stub self, and dbroutes.assert_target is replaced, so only the dispatch and the message are tested.
The live comparison is covered by test_bigtable_live.py (emulator) and the regression case.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from inttest import dbroutes
from inttest.manifest import ManifestError, load_manifest
from inttest.plugin import IntYamlItem


def _write(tmp_path, text: str):
    test_dir = tmp_path / "bt-case"
    test_dir.mkdir()
    (test_dir / "expected").mkdir()
    (test_dir / "expected" / "rows.json").write_text(json.dumps([{"row": "01", "cells": {}}]))
    path = test_dir / "test.yaml"
    path.write_text(text)
    return path, test_dir


BASE = """
name: bigtable-assert-case
op:
  jar: java/OpenProcessors/BigtableWriter
requires: [bigtable]
"""


def _target(query: str, db: str | None = None) -> str:
    route = "" if db is None else f"      db: {db}\n"
    return BASE + f"""
target:
  input: input/events.json
assert:
  target:
    - query: {query}
{route}      match: expected/rows.json
"""


def test_is_bigtable_route():
    assert dbroutes.is_bigtable_route("bigtable-target") is True
    assert dbroutes.is_bigtable_route("postgres-target") is False
    with pytest.raises(dbroutes.RouteError):
        dbroutes.is_bigtable_route("notaroute")


def test_bigtable_entry_takes_a_bare_table_name_without_order_by(tmp_path):
    path, test_dir = _write(tmp_path, _target("emp", db="bigtable-target"))
    entry = load_manifest(path).assert_.target[0]
    assert entry.query == "emp"
    assert entry.db == "bigtable-target"
    assert entry.match_path == (test_dir / "expected/rows.json").resolve()


def test_bigtable_entry_accepts_a_token_in_the_table_name(tmp_path):
    path, _ = _write(tmp_path, _target("${TID}emp", db="bigtable-target"))
    assert load_manifest(path).assert_.target[0].query == "${TID}emp"


@pytest.mark.parametrize("query", ["SELECT * FROM emp", "emp WHERE x=1", "emp; DROP TABLE x"])
def test_bigtable_entry_rejects_sql_as_the_table_name(tmp_path, query):
    path, _ = _write(tmp_path, _target(f"'{query}'", db="bigtable-target"))
    with pytest.raises(ManifestError, match="must be a table name"):
        load_manifest(path)


def test_sql_route_still_needs_order_by(tmp_path):
    path, _ = _write(tmp_path, _target("'SELECT ID FROM T'", db="postgres-target"))
    with pytest.raises(ManifestError, match="no ORDER BY"):
        load_manifest(path)


def _stub_item():
    return SimpleNamespace(_variant_tokens=lambda variant: {"TID": "t_"})


def test_plugin_compares_the_table_through_assert_target(tmp_path, monkeypatch):
    path, _ = _write(tmp_path, _target("${TID}emp", db="bigtable-target"))
    m = load_manifest(path)
    calls = []
    monkeypatch.setattr(dbroutes, "assert_target",
                        lambda route, table, expected, tokens=None: calls.append((route, table, expected)))
    IntYamlItem._assert_target_rows(_stub_item(), m, "", None)
    assert calls == [("bigtable-target", "t_emp", [{"row": "01", "cells": {}}])]


def test_plugin_fails_with_the_mismatch_and_the_writer_report(tmp_path, monkeypatch):
    path, _ = _write(tmp_path, _target("emp", db="bigtable-target"))
    m = load_manifest(path)

    def mismatch(route, table, expected, tokens=None):
        raise AssertionError("assert.target(...) mismatch:\n  expected: []\n  actual:   []")

    monkeypatch.setattr(dbroutes, "assert_target", mismatch)
    with pytest.raises(pytest.fail.Exception, match="assert.target.*mismatch") as exc:
        IntYamlItem._assert_target_rows(_stub_item(), m, "", None, {"acknowledgeable": True})
    assert "writer report" in str(exc.value)
