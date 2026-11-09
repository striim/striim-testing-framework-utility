"""Hermetic, no-Docker self-test for the INTEGRATION BigtableAdmin's command surface.
Mirror of scripts/live/tests/test_bigtableadmin.py: the two BigtableAdmin modules are the
same on the command surface, differing only in `readiness()`, which is not exercised here,
so the body is the same.

Mirrors test_spanneradmin.py / test_gcsadmin.py: the admin's google-cloud-bigtable
Client + data layer are injected as FAKES (the admin's __init__ client/data_client
knobs) so a test asserts WHAT the admin asks for -- the families + GC rules it requests
at create time, the SetCell it stamps with a MILLIS-aligned timestamp, the DropRowRange
it issues with delete_all_data_from_table=True, and the read form it reads back as hex
with cells_per_column_limit(1). No Docker and no CBTEMULATOR; the proto/mutation types the
admin builds inside its methods are recording stand-ins here (test_bigtableadmin_requests.py
checks the requests on the real SDK types).

The admin offers NO change-stream surface: enable_change_stream/disable_change_stream
must RAISE rather than fake one, and that refusal is asserted here too.
"""
import sys
import types as _types

import pytest

from inttest.bigtableadmin import (BigtableAdmin, _as_bytes, _check_ident,
                                   _gc_to_rule, _GC_KINDS)

# --- recording stand-ins for the google-cloud-bigtable proto/mutation types --------
# The admin constructs these inside its methods and only ever forwards them or reads
# their fields back, so a recording stand-in is a faithful oracle: a test asserts the
# exact call the admin made (family/qualifier/value/timestamp, the GC rule,
# delete_all_data_from_table, num_cells).
class _Rec:
    """Records constructor kwargs and allows the attribute-set the admin does
    (e.g. ColumnFamily().gc_rule = ...)."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeRow(_Rec):
    """A data-layer read row: .row_key (bytes) + .cells (list of _FakeCell)."""

    def __init__(self, row_key, cells):
        super().__init__(row_key=row_key, cells=cells)


class _FakeCell(_Rec):
    """One read cell: .family (str), .qualifier (bytes), .value (bytes)."""

    def __init__(self, family, qualifier, value):
        super().__init__(family=family, qualifier=qualifier, value=value)


class _FakeDataTable:
    """A data-layer Table: records mutate_row calls (write_cells) and yields its rows
    on read_rows (read_all / read_row). A mutation folds its SetCell into the table so a
    write followed by a read in the same test sees what it just wrote."""

    def __init__(self, rows=()):
        self._rows = list(rows)
        self.mutations = []      # (row_key, [mutation]) from mutate_row
        self.read_queries = []   # queries seen by read_rows

    def mutate_row(self, key, mutations, **_deadline):
        self.mutations.append((key, list(mutations)))
        for m in mutations:
            for r in self._rows:
                if r.row_key == key:
                    r.cells.append(_FakeCell(m.family, m.qualifier, m.new_value))
                    break
            else:
                self._rows.append(_FakeRow(key,
                                           [_FakeCell(m.family, m.qualifier, m.new_value)]))

    def read_row(self, key, **_deadline):
        for r in self._rows:
            if r.row_key == key:
                return r
        return None

    def read_rows(self, query, **_deadline):
        self.read_queries.append(query)
        return list(self._rows)


class _FakeDataClient:
    """The BigtableDataClient stand-in: get_table(instance, name) -> the named table."""

    def __init__(self, tables):
        self._tables = tables
        self.get_table_calls = []

    def get_table(self, instance, name):
        self.get_table_calls.append((instance, name))
        return self._tables[name]


class _FakeTableAdminClient:
    """The admin `Client.table_admin_client` stand-in: records the schema/maintenance
    calls the admin issues (create_table / delete_table / drop_row_range / get_table /
    list_tables)."""

    def __init__(self, got_table=None, listed=()):
        self.created = []
        self.deleted = []          # names passed to delete_table
        self.dropped = []          # DropRowRangeRequest seen by drop_row_range
        self._got = got_table
        self._listed = list(listed)

    def create_table(self, req, **_deadline):
        self.created.append(req)

    def delete_table(self, name, **_deadline):
        self.deleted.append(name)

    def get_table(self, req, **_deadline):
        return self._got

    def drop_row_range(self, req, **_deadline):
        self.dropped.append(req)

    def list_tables(self, req, **_deadline):
        return list(self._listed)


class _FakeAdmin:
    """The admin `Client` stand-in (admin="instance"): .table_admin_client is the TAC;
    .instance() is off the command surface under test, so it must never be reached."""

    def __init__(self, tac):
        self._tac = tac

    @property
    def table_admin_client(self):
        return self._tac

    def instance(self, name):  # pragma: no cover -- not on the command surface
        raise AssertionError("instance() is not part of the command-surface tests")


def _dsn():
    return {"project": "test-project", "instance": "test-instance",
            "emulator_host": "localhost:9470"}


def _admin(tac=None, tables=None):
    """Build an admin with fakes injected on BOTH knobs, so any method path is satisfied
    (a method that touches the data layer finds an empty table it can use)."""
    tac = tac or _FakeTableAdminClient()
    return BigtableAdmin(_dsn(), client=_FakeAdmin(tac),
                         data_client=_FakeDataClient(tables or {}))


# --- stub the leaf google-cloud-bigtable modules the admin imports lazily -----------
# These tests assert what the admin hands the SDK's types, so the leaf modules the admin
# imports inside its methods are recording fakes here even when google-cloud-bigtable is
# installed, and a real module already imported is restored afterwards. Every OTHER google.*
# module (in particular the real google.protobuf.duration_pb2 the GC rule needs) is left intact.
@pytest.fixture(autouse=True)
def _bigtable_stub_modules():
    modules = [
        ("google.cloud.bigtable", {}),
        ("google.cloud.bigtable.data",
            {"SetCell": _Rec, "ReadRowsQuery": _Rec, "DeleteAllFromRow": _Rec}),
        ("google.cloud.bigtable.data.row_filters",
            {"CellsColumnLimitFilter": _Rec}),
        ("google.cloud.bigtable_admin_v2", {}),
        ("google.cloud.bigtable_admin_v2.types",
            {"ColumnFamily": _Rec, "Table": _Rec, "CreateTableRequest": _Rec,
             "GetTableRequest": _Rec, "DropRowRangeRequest": _Rec,
             "ListTablesRequest": _Rec, "GcRule": _Rec}),
    ]
    created = []
    restored = []
    for name, attrs in modules:
        mod = _types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        parent_name, _, leaf = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            had = hasattr(parent, leaf)
            original = getattr(parent, leaf, None)
            setattr(parent, leaf, mod)
            restored.append((parent, leaf, had, original))
        created.append((name, sys.modules.get(name)))
        sys.modules[name] = mod
    try:
        yield
    finally:
        for name, previous in reversed(created):
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        for parent, leaf, had, original in restored:
            if had:
                setattr(parent, leaf, original)
            else:
                delattr(parent, leaf)


# --- pure helpers -------------------------------------------------------------------

def test_check_ident_accepts_valid_rejects_injection():
    assert _check_ident("emp", "table") == "emp"
    assert _check_ident("a_b", "family") == "a_b"
    with pytest.raises(ValueError):
        _check_ident("emp; DROP TABLE x", "table")     # statement injection
    with pytest.raises(ValueError):
        _check_ident("emp.x", "table")                 # a dot is not a single identifier
    with pytest.raises(ValueError):
        _check_ident(123, "table")                     # non-string

def test_gc_to_rule_num_versions_and_none_and_unknown_key():
    # The num_versions shape only, on the recording stubs; the age forms are checked on the
    # real admin_v2 types in test_bigtableadmin_requests.py.
    assert _gc_to_rule({"num_versions": 3}).max_num_versions == 3
    assert _gc_to_rule(None) is None
    assert _gc_to_rule({}) is None
    with pytest.raises(ValueError, match="unknown GC rule"):
        _gc_to_rule({"bogus_key": 1})
    assert _GC_KINDS == frozenset({"num_versions", "age_millis", "age_seconds", "age_days"})

def test_as_bytes_handles_str_bytes_and_hex_tokens():
    assert _as_bytes("hi") == b"hi"                      # str -> utf-8
    assert _as_bytes(b"\x01\x02") == b"\x01\x02"        # bytes pass through
    assert _as_bytes("0x0102") == b"\x01\x02"           # 0x-prefixed hex token
    with pytest.raises(TypeError):
        _as_bytes(123)                                  # neither str nor bytes


# --- create_table (families + GC) ---------------------------------------------------

def test_create_table_builds_the_families_and_gc_rule_it_was_handed():
    tac = _FakeTableAdminClient()
    admin = _admin(tac=tac)
    admin.create_table("emp", families={"cf1": None, "cf2": None},
                       gc_rules={"cf1": {"num_versions": 3}})
    assert len(tac.created) == 1
    req = tac.created[-1]
    assert req.parent == "projects/test-project/instances/test-instance"
    assert req.table_id == "emp"
    cfs = req.table.column_families
    assert set(cfs) == {"cf1", "cf2"}
    assert cfs["cf1"].gc_rule.max_num_versions == 3      # the GC'd family carries its rule
    assert not hasattr(cfs["cf2"], "gc_rule")            # an un-GC'd family has none


def test_create_table_is_idempotent_and_drops_first():
    # A re-run is clean: create_table drops its table first, then recreates it.
    full = "projects/test-project/instances/test-instance/tables/emp"
    tac = _FakeTableAdminClient()
    admin = _admin(tac=tac)
    admin.create_table("emp", {"cf1": None})
    admin.create_table("emp", {"cf1": None})
    assert tac.deleted == [full, full]
    assert len(tac.created) == 2


# --- write_cells --------------------------------------------------------------------

def test_write_cells_stamps_one_setcell_per_cell_with_a_millis_timestamp():
    data = _FakeDataClient({"emp": _FakeDataTable()})
    admin = BigtableAdmin(_dsn(), data_client=data)
    admin.write_cells("emp", [{"row": "user1",
                               "cells": {"cf1:name": "alice", "cf1:age": "30"}}])
    key, muts = data._tables["emp"].mutations[0]
    assert key == b"user1"
    name = {(m.family, m.qualifier): m for m in muts}[("cf1", b"name")]
    assert name.new_value == b"alice"
    # Bigtable's default timestamp granularity is milliseconds: the admin millis-aligns it.
    assert isinstance(name.timestamp_micros, int)
    assert name.timestamp_micros % 1000 == 0


def test_write_cells_encodes_bytes_and_hex_values():
    data = _FakeDataClient({"emp": _FakeDataTable()})
    admin = BigtableAdmin(_dsn(), data_client=data)
    admin.write_cells("emp", [{"row": b"key\x01",
                               "cells": {"cf1:raw": b"\x01\xff", "cf1:hn": "0x4142"}}])
    key, muts = data._tables["emp"].mutations[0]
    assert key == b"key\x01"
    by_col = {(m.family, m.qualifier): m for m in muts}
    assert by_col[("cf1", b"raw")].new_value == b"\x01\xff"
    assert by_col[("cf1", b"hn")].new_value == b"AB"     # a "0x4142" hex token -> b"AB"


def test_write_cells_rejects_an_unsafe_column_name():
    admin = BigtableAdmin(_dsn(), data_client=_FakeDataClient({"emp": _FakeDataTable()}))
    with pytest.raises(ValueError, match="unsafe"):
        admin.write_cells("emp", [{"row": "k", "cells": {"cf; DROP:q": "x"}}])


# --- read_all (hex + cells_per_column_limit + sort) ----------------------------------

def test_read_all_reads_hex_sorted_by_row_key_and_threads_num_cells():
    rows = [
        _FakeRow(b"b", [_FakeCell("cf1", b"name", b"bravo")]),
        _FakeRow(b"a", [_FakeCell("cf1", b"name", b"alpha"),
                        _FakeCell("cf1", b"age", b"30")]),
    ]
    data = _FakeDataClient({"emp": _FakeDataTable(rows=rows)})
    admin = BigtableAdmin(_dsn(), data_client=data)
    got = admin.read_all("emp")
    # newest-cell-per-column hex, rows sorted by their hex key (b"a" < b"b").
    assert got == [
        {"row": b"a".hex(), "cells": {"cf1:name": b"alpha".hex(),
                                       "cf1:age": b"30".hex()}},
        {"row": b"b".hex(), "cells": {"cf1:name": b"bravo".hex()}},
    ]
    q = data._tables["emp"].read_queries[-1]
    assert q.row_filter.num_cells == 1      # the cells_per_column_limit default is threaded


def test_read_all_forwards_a_non_default_cells_per_column_limit():
    data = _FakeDataClient({"emp": _FakeDataTable()})
    admin = BigtableAdmin(_dsn(), data_client=data)
    admin.read_all("emp", cells_per_column_limit=5)
    assert data._tables["emp"].read_queries[-1].row_filter.num_cells == 5


def test_read_row_returns_none_for_an_absent_row():
    data = _FakeDataClient({"emp": _FakeDataTable()})
    admin = BigtableAdmin(_dsn(), data_client=data)
    assert admin.read_row("emp", "nope") is None


# --- drop_all_rows (DropRowRange delete-all) -----------------------------------------

def test_drop_all_rows_issues_a_droprowrange_delete_all():
    tac = _FakeTableAdminClient()
    admin = _admin(tac=tac)
    admin.drop_all_rows("emp")
    assert len(tac.dropped) == 1
    req = tac.dropped[0]
    assert req.delete_all_data_from_table is True
    assert req.name == "projects/test-project/instances/test-instance/tables/emp"


# --- list_families ------------------------------------------------------------------

def test_list_families_returns_the_tables_family_ids():
    got = _Rec(column_families={"cf1": None, "cf2": None})
    tac = _FakeTableAdminClient(got_table=got)
    admin = _admin(tac=tac)
    assert admin.list_families("emp") == ["cf1", "cf2"]


# --- change streams: NOT SUPPORTED -------------------------------------------------

def test_change_streams_refuse_rather_than_fake():
    admin = _admin()
    with pytest.raises(RuntimeError, match="change streams are not supported"):
        admin.enable_change_stream()
    with pytest.raises(RuntimeError, match="change streams are not supported"):
        admin.disable_change_stream()
