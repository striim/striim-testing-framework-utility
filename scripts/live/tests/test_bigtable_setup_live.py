"""`setup: bigtable:` in the live tier: the manifest rules, and the plugin's create/drop bookkeeping.

Hermetic: no Docker. The manifest is loaded through livetest.manifest.load_manifest. The plugin
block is exercised through its public behaviour only where it can be isolated; the live round trip
is the regression case, which needs the live stack.
"""
import pytest

from livetest.manifest import ManifestError, VALID_MANIFEST_KEYS, load_manifest

_BASE = "name: x\ntql: app.tql\nassert:\n  smoke: true\nrequires: [bigtable]\n"


def _write(tmp_path, body):
    p = tmp_path / "test.yaml"
    p.write_text(body)
    return p


def test_setup_is_an_allowed_manifest_key():
    assert "setup" in VALID_MANIFEST_KEYS


def test_setup_is_parsed_with_defaults(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + (
        "setup:\n  bigtable:\n"
        "    - table: ${TID}emp\n"
        "      families: {emp: null}\n"
        "      gc: {emp: {age_days: 7}}\n"
        "      seed:\n"
        "        - {row: '01', cells: {'emp:name': 'ann'}}\n")))
    (spec,) = m.bigtable_setup
    assert spec.table == "${TID}emp"
    assert spec.db == "bigtable-target"
    assert spec.families == {"emp": None}
    assert spec.gc_rules == {"emp": {"age_days": 7}}
    assert spec.seed == [{"row": "01", "cells": {"emp:name": "ann"}}]


def test_no_setup_key_is_an_empty_list(tmp_path):
    assert load_manifest(_write(tmp_path, _BASE)).bigtable_setup == []


@pytest.mark.parametrize("body,match", [
    ("    - table: emp\n      families: {emp: null}\n      extra: 1\n", "unknown key"),
    ("    - table: 'SELECT x'\n      families: {emp: null}\n", "needs 'table'"),
    ("    - table: emp\n      families: {emp: {age_days: 7}}\n", "must be null"),
    ("    - table: emp\n      families: {emp: null}\n      gc: {other: {age_days: 7}}\n", "does not declare"),
    ("    - table: emp\n      families: {}\n", "needs 'families'"),
    ("    - table: emp\n      db: postgres-target\n      families: {emp: null}\n", "bigtable-"),
    ("    - table: emp\n      families: {emp: null}\n      seed: [{cells: {}}]\n", "needs 'row'"),
])
def test_setup_refuses_a_bad_entry(tmp_path, body, match):
    with pytest.raises(ManifestError, match=match):
        load_manifest(_write(tmp_path, _BASE + "setup:\n  bigtable:\n" + body))


def test_setup_refuses_an_unknown_setup_kind(tmp_path):
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(_write(tmp_path, _BASE + "setup:\n  postgres: []\n"))
