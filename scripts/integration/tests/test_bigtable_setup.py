"""`setup: bigtable:` -- the manifest form and the plugin's create/drop around the case.

Hermetic: no Docker. dbroutes.setup_bigtable and teardown_bigtable are replaced, so only the
manifest rules, the token rendering and the create/drop bookkeeping are tested.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from inttest import dbroutes
from inttest.manifest import ManifestError, load_manifest
from inttest.plugin import IntYamlItem

BASE = """
name: bigtable-setup-case
op:
  jar: java/OpenProcessors/BigtableWriter
requires: [bigtable]
assert:
  smoke: true
"""


def _write(tmp_path, setup_yaml: str):
    test_dir = tmp_path / "bt-setup"
    test_dir.mkdir()
    path = test_dir / "test.yaml"
    path.write_text(BASE + setup_yaml)
    return path


def _setup(body: str) -> str:
    return "setup:\n  bigtable:\n" + body


def test_setup_is_parsed_with_defaults(tmp_path):
    path = _write(tmp_path, _setup(
        "    - table: ${TID}emp\n"
        "      families: {emp: null}\n"
        "      gc: {emp: {age_days: 7}}\n"
        "      seed:\n"
        "        - {row: '01', cells: {'emp:name': 'ann'}}\n"))
    (spec,) = load_manifest(path).bigtable_setup
    assert spec.table == "${TID}emp"
    assert spec.db == "bigtable-target"
    assert spec.families == {"emp": None}
    assert spec.gc_rules == {"emp": {"age_days": 7}}
    assert spec.seed == [{"row": "01", "cells": {"emp:name": "ann"}}]


def test_no_setup_key_is_an_empty_list(tmp_path):
    path = _write(tmp_path, "")
    assert load_manifest(path).bigtable_setup == []


@pytest.mark.parametrize("body,match", [
    ("    - table: emp\n      families: {emp: null}\n      extra: 1\n", "unknown key"),
    ("    - table: 'SELECT x'\n      families: {emp: null}\n", "needs 'table'"),
    ("    - table: emp\n      families: {emp: {age_days: 7}}\n", "must be null"),
    ("    - table: emp\n      families: {emp: null}\n      gc: {other: {age_days: 7}}\n", "does not declare"),
    ("    - table: emp\n      families: {}\n", "needs 'families'"),
    ("    - table: emp\n      db: postgres-target\n      families: {emp: null}\n", "must be a bigtable"),
    ("    - table: emp\n      families: {emp: null}\n      seed: [{cells: {}}]\n", "needs 'row'"),
])
def test_setup_refuses_a_bad_entry(tmp_path, body, match):
    with pytest.raises(ManifestError, match=match):
        load_manifest(_write(tmp_path, _setup(body)))


def test_setup_refuses_an_unknown_setup_key(tmp_path):
    path = _write(tmp_path, "setup:\n  postgres: []\n")
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


def _item(tokens):
    return SimpleNamespace(_variant_tokens=lambda variant: tokens)


def _manifest(tmp_path):
    return load_manifest(_write(tmp_path, _setup(
        "    - table: ${TID}emp\n"
        "      families: {emp: null}\n"
        "      seed:\n"
        "        - {row: '01', cells: {'emp:name': 'ann'}}\n")))


def test_setup_creates_the_rendered_table_and_remembers_it(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(dbroutes, "setup_bigtable",
                        lambda table, families, **kw: calls.append((table, families, kw)))
    tokens = {"TID": "t_"}
    item = _item(tokens)
    IntYamlItem._setup_bigtable(item, _manifest(tmp_path), tokens)
    (table, families, kw) = calls[0]
    assert table == "t_emp"
    assert families == {"emp": None}
    assert kw["seed"] == [{"row": "01", "cells": {"emp:name": "ann"}}]
    assert kw["route"] == "bigtable-target"
    assert item._bigtable_created == [("bigtable-target", "t_emp", tokens)]


def test_setup_failure_is_a_test_failure_and_stays_remembered(tmp_path, monkeypatch):
    def boom(table, families, **kw):
        raise RuntimeError("emulator down")

    monkeypatch.setattr(dbroutes, "setup_bigtable", boom)
    tokens = {"TID": "t_"}
    item = _item(tokens)
    with pytest.raises(pytest.fail.Exception, match="setup bigtable 't_emp'.*emulator down"):
        IntYamlItem._setup_bigtable(item, _manifest(tmp_path), tokens)
    # Remembered before the create, so teardown still tries to drop a partly created table.
    assert item._bigtable_created == [("bigtable-target", "t_emp", tokens)]


def test_teardown_drops_each_created_table_once(tmp_path, monkeypatch):
    dropped = []
    monkeypatch.setattr(dbroutes, "teardown_bigtable",
                        lambda table, route="bigtable-target", tokens=None: dropped.append((table, route)))
    item = SimpleNamespace(_bigtable_created=[("bigtable-target", "t_emp", {"TID": "t_"})])
    IntYamlItem._teardown_bigtable(item)
    IntYamlItem._teardown_bigtable(item)
    assert dropped == [("t_emp", "bigtable-target")]


def test_teardown_reports_a_failed_drop_and_does_not_raise(monkeypatch, capsys):
    def boom(table, route="bigtable-target", tokens=None):
        raise RuntimeError("admin unreachable")

    monkeypatch.setattr(dbroutes, "teardown_bigtable", boom)
    item = SimpleNamespace(_bigtable_created=[("bigtable-target", "t_emp", {})])
    IntYamlItem._teardown_bigtable(item)
    assert "failed to drop bigtable table 't_emp'" in capsys.readouterr().out


def test_teardown_with_nothing_created_is_a_no_op():
    item = SimpleNamespace()
    IntYamlItem._teardown_bigtable(item)
