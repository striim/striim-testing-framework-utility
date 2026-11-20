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
    ("    - table: ${TID}emp\n      families: {emp: null}\n      extra: 1\n", "unknown key"),
    ("    - table: 'SELECT x'\n      families: {emp: null}\n", "needs 'table'"),
    ("    - table: ${TID}emp\n      families: {emp: {age_days: 7}}\n", "must be null"),
    ("    - table: ${TID}emp\n      families: {emp: null}\n      gc: {other: {age_days: 7}}\n", "does not declare"),
    ("    - table: ${TID}emp\n      families: {}\n", "needs 'families'"),
    ("    - table: ${TID}emp\n      db: postgres-target\n      families: {emp: null}\n", "bigtable-"),
    ("    - table: ${TID}emp\n      families: {emp: null}\n      seed: [{cells: {}}]\n", "needs 'row'"),
])
def test_setup_refuses_a_bad_entry(tmp_path, body, match):
    with pytest.raises(ManifestError, match=match):
        load_manifest(_write(tmp_path, _BASE + "setup:\n  bigtable:\n" + body))


def test_setup_refuses_an_unknown_setup_kind(tmp_path):
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(_write(tmp_path, _BASE + "setup:\n  postgres: []\n"))


def test_setup_needs_bigtable_in_requires(tmp_path):
    body = _BASE.replace("requires: [bigtable]", "requires: [postgres]") + (
        "setup:\n  bigtable:\n    - table: ${TID}emp\n      families: {emp: null}\n")
    with pytest.raises(ManifestError, match=r"requires: \[bigtable\]"):
        load_manifest(_write(tmp_path, body))


def test_setup_table_must_carry_the_run_tid(tmp_path):
    with pytest.raises(ManifestError, match="must contain"):
        load_manifest(_write(tmp_path, _BASE + "setup:\n  bigtable:\n    - table: emp\n      families: {emp: null}\n"))


def test_a_bad_bigtable_assertion_is_refused_at_load(tmp_path):
    body = _BASE.replace("assert:\n  smoke: true\n", "assert:\n  bigtable:\n    - {table: t, expected: [], dbb: x}\n")
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(_write(tmp_path, body))


def test_a_bigtable_assertion_needs_bigtable_in_requires(tmp_path):
    body = (_BASE.replace("requires: [bigtable]", "requires: [postgres]")
            .replace("assert:\n  smoke: true\n", "assert:\n  bigtable:\n    - {table: t, expected: []}\n"))
    with pytest.raises(ManifestError, match=r"requires: \[bigtable\]"):
        load_manifest(_write(tmp_path, body))


def test_the_bigtable_tier_can_be_xfailed(tmp_path):
    body = _BASE + "xfail:\n  reason: r\n  tiers: [bigtable]\n"
    assert "bigtable" in load_manifest(_write(tmp_path, body)).xfail["tiers"]


# --- the plugin's setup, through the ownership ledger ---------------------------------------------

from types import SimpleNamespace  # noqa: E402

from livetest import ownership, runident  # noqa: E402
from livetest.bigtablespec import BigtableSetup  # noqa: E402
from livetest.plugin import setup_bigtable_tables  # noqa: E402


class _Admin:
    """The BigtableAdmin calls setup and the ledger make, against an in-memory instance."""

    def __init__(self, tables=(), fail_create=False):
        self.tables, self.fail_create = set(tables), fail_create
        self.dsn = {"project": "p", "instance": "i", "emulator_host": "localhost:9470"}
        self.created, self.written = [], []

    def list_owned_tables(self, **_):
        return set(self.tables)

    def create_table(self, table, families, gc_rules=None):
        if self.fail_create:
            raise RuntimeError("emulator down")
        self.tables.add(table)
        self.created.append((table, families, gc_rules))

    def write_cells(self, table, rows):
        self.written.append((table, rows))

    def drop_owned_table(self, table):
        self.tables.discard(table)


def _ledger(tmp_path):
    ident = runident.derive("case", {"SLT_RUN_EPOCH": "run"}, attempt="abcd1234")
    return ownership.Ledger(tmp_path / "ledger.json", ident, {"entries": [], "bindings": {}})


_SPEC = BigtableSetup(table="${TID}emp", db="bigtable-target", families={"emp": None},
                      gc_rules={"emp": {"age_days": 7}}, seed=[{"row": "01", "cells": {"emp:name": "ann"}}])


def _admins(admin):
    return {"bigtable-target": {"admin": admin, "schema": None}}


def test_setup_creates_seeds_and_confirms_the_table_in_the_ledger(tmp_path):
    admin, ledger = _Admin(), _ledger(tmp_path)
    assert setup_bigtable_tables([_SPEC], _admins(admin), {"TID": "t1_"}, ledger, "case") == ["t1_emp"]
    assert admin.created == [("t1_emp", {"emp": None}, {"emp": {"age_days": 7}})]
    assert admin.written == [("t1_emp", [{"row": "01", "cells": {"emp:name": "ann"}}])]
    (entry,) = ledger.entries
    assert (entry["kind"], entry["name"], entry["db"], entry["state"]) == (
        "bigtable-table", "t1_emp", "bigtable-target", "confirmed")


def test_setup_refuses_a_table_that_already_exists_and_leaves_it(tmp_path):
    # Another run's table (or a kept one) is never dropped and recreated under it.
    admin, ledger = _Admin(tables={"t1_emp"}), _ledger(tmp_path)
    with pytest.raises(pytest.fail.Exception, match="collision:bigtable-table:t1_emp"):
        setup_bigtable_tables([_SPEC], _admins(admin), {"TID": "t1_"}, ledger, "case")
    assert admin.created == [] and admin.tables == {"t1_emp"} and ledger.entries == []


def test_a_failed_create_leaves_no_ledger_entry(tmp_path):
    admin, ledger = _Admin(fail_create=True), _ledger(tmp_path)
    with pytest.raises(pytest.fail.Exception, match="setup bigtable 't1_emp'.*emulator down"):
        setup_bigtable_tables([_SPEC], _admins(admin), {"TID": "t1_"}, ledger, "case")
    assert ledger.entries == []


def test_setup_needs_the_route_admin(tmp_path):
    with pytest.raises(AssertionError, match="needs service 'bigtable-target'"):
        setup_bigtable_tables([_SPEC], {}, {"TID": "t1_"}, _ledger(tmp_path), "case")


@pytest.mark.parametrize("keep_reason,left", [(None, set()), ("SLT_KEEP_RESOURCES is set", {"t1_emp"})])
def test_ledger_cleanup_drops_the_table_unless_the_run_is_kept(tmp_path, keep_reason, left):
    admin, ledger = _Admin(), _ledger(tmp_path)
    setup_bigtable_tables([_SPEC], _admins(admin), {"TID": "t1_"}, ledger, "case")
    result = ledger.cleanup(client=None, admins=_admins(admin), ctx=SimpleNamespace(mode="docker"),
                            tokens={}, keep_reason=keep_reason)
    assert admin.tables == left
    if keep_reason is None:
        assert result.owned[0]["state"] == "verified-absent"


def test_a_listing_failure_after_create_confirms_rather_than_leaks(tmp_path):
    # An unconfirmed intent is never dropped; a confirmed entry is, and the drop tolerates absence.
    class _Flaky(_Admin):
        calls = 0

        def list_owned_tables(self, **_):
            _Flaky.calls += 1
            if _Flaky.calls > 1:
                raise RuntimeError("listing down")
            return set(self.tables)
    ledger = _ledger(tmp_path)
    setup_bigtable_tables([_SPEC], _admins(_Flaky()), {"TID": "t1_"}, ledger, "case")
    assert ledger.entries[0]["state"] == "confirmed"
    assert any("could not confirm" in g for g in ledger.gaps)
