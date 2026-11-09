"""Hermetic: BigtableAdmin's row helpers give the same results over the explicitly bound data client.

The admin's data side talks to an in-process google.bigtable.v2.Bigtable service built on the real
bigtable_v2 messages: MutateRow applies SetCell / DeleteFromRow, and ReadRows answers in cell
chunks, splitting every value across two chunks the way a server may. No emulator, no network
beyond this process's loopback socket.
"""
import time
from concurrent import futures

import grpc
import pytest
from google.cloud.bigtable_v2.types import (MutateRowRequest, MutateRowResponse, Mutation,
                                            ReadRowsRequest, ReadRowsResponse)

from inttest import bigtableadmin
from inttest.bigtableadmin import BigtableAdmin

TABLE = "projects/test-project/instances/test-instance/tables/t1_rows"


class _Bigtable:
    """Rows as {key: [(family, qualifier, timestamp_micros, value)]}."""

    def __init__(self):
        self.rows = {}

    def mutate_row(self, raw, context):
        request = MutateRowRequest.pb().FromString(raw)
        assert request.table_name == TABLE
        for mutation in request.mutations:
            kind = mutation.WhichOneof("mutation")
            if kind == "set_cell":
                c = mutation.set_cell
                self.rows.setdefault(request.row_key, []).append(
                    (c.family_name, c.column_qualifier, c.timestamp_micros, c.value))
            elif kind == "delete_from_row":
                self.rows.pop(request.row_key, None)
            else:
                context.abort(grpc.StatusCode.UNIMPLEMENTED, kind)
        return MutateRowResponse.pb()().SerializeToString()

    def read_rows(self, raw, context):
        request = ReadRowsRequest.pb().FromString(raw)
        assert request.table_name == TABLE
        keys = sorted(set(request.rows.row_keys) or self.rows)
        limit = (request.filter.cells_per_column_limit_filter
                 if request.HasField("filter") else None)
        for key in keys:
            if key not in self.rows:
                continue
            columns = {}
            for fam, qual, ts, value in self.rows[key]:
                columns.setdefault((fam, qual), []).append((ts, value))
            response = ReadRowsResponse.pb()()
            first, last_family = True, None
            for (fam, qual), cells in sorted(columns.items()):
                for ts, value in sorted(cells, reverse=True)[:limit]:
                    half = len(value) // 2 if len(value) > 1 else len(value)
                    head = response.chunks.add()
                    if first:
                        head.row_key = key
                        first = False
                    if fam != last_family:
                        head.family_name.value = fam
                        last_family = fam
                    head.qualifier.value = qual
                    head.timestamp_micros = ts
                    head.value = value[:half]
                    if half < len(value):
                        head.value_size = len(value)
                        response.chunks.add().value = value[half:]
            response.chunks[-1].commit_row = True
            yield response.SerializeToString()


@pytest.fixture
def admin(monkeypatch):
    service = _Bigtable()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("google.bigtable.v2.Bigtable", {
        "MutateRow": grpc.unary_unary_rpc_method_handler(service.mutate_row),
        "ReadRows": grpc.unary_stream_rpc_method_handler(service.read_rows)}),))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    monkeypatch.setattr(bigtableadmin, "_CLIENTS", {})
    monkeypatch.delenv("BIGTABLE_EMULATOR_HOST", raising=False)
    try:
        yield BigtableAdmin({"project": "test-project", "instance": "test-instance",
                             "emulator_host": f"127.0.0.1:{port}"})
    finally:
        server.stop(0)


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_written_cells_read_back_newest_per_column_as_hex(admin):
    admin.write_cells("t1_rows", [{"row": "r2", "cells": {"cf:name": "bob"}},
                                  {"row": "r1", "cells": {"cf:name": "al", "cf:age": "0x1f", "meta:x": b"\x00"}}])
    time.sleep(0.002)                                  # a later millisecond: the newer version
    admin.write_cells("t1_rows", [{"row": "r1", "cells": {"cf:name": "alice"}}])
    assert admin.read_all("t1_rows") == [
        {"row": b"r1".hex(), "cells": {"cf:age": "1f", "cf:name": b"alice".hex(), "meta:x": "00"}},
        {"row": b"r2".hex(), "cells": {"cf:name": b"bob".hex()}},
    ]
    assert admin.read_all("t1_rows", cells_per_column_limit=2)[0]["cells"]["cf:name"] in (
        b"alice".hex(), b"al".hex())


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_read_row_finds_one_row_or_none(admin):
    admin.write_cells("t1_rows", [{"row": "r1", "cells": {"cf:name": "alice"}}])
    assert admin.read_row("t1_rows", "r1") == {"cells": {"cf:name": b"alice".hex()}}
    assert admin.read_row("t1_rows", "missing") is None


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_delete_row_removes_the_row(admin):
    admin.write_cells("t1_rows", [{"row": "r1", "cells": {"cf:a": "1"}}, {"row": "r2", "cells": {"cf:a": "2"}}])
    admin.delete_row("t1_rows", "r1")
    assert [r["row"] for r in admin.read_all("t1_rows")] == [b"r2".hex()]


# --- merging ReadRows chunks into rows ----------------------------------------------------

def _response(*chunks):
    response = ReadRowsResponse.pb()()
    for fields in chunks:
        chunk = response.chunks.add()
        for name, value in fields.items():
            if name in ("family_name", "qualifier"):
                getattr(chunk, name).value = value
            else:
                setattr(chunk, name, value)
    return response


def _rows(*responses):
    return [(r.row_key, [(c.family, c.qualifier, c.timestamp_micros, c.value) for c in r.cells])
            for r in bigtableadmin._rows_from_chunks(responses)]


def test_chunks_merge_split_values_and_carry_family_and_qualifier():
    assert _rows(
        _response({"row_key": b"r1", "family_name": "cf", "qualifier": b"a", "timestamp_micros": 2000,
                   "value": b"he", "value_size": 5}, {"value": b"llo"},
                  {"qualifier": b"b", "timestamp_micros": 1000, "value": b"x"},
                  {"family_name": "zz", "qualifier": b"c", "value": b"", "commit_row": True}),
        _response({"row_key": b"r2", "family_name": "cf", "qualifier": b"a", "value": b"y",
                   "commit_row": True}),
    ) == [(b"r1", [("cf", b"a", 2000, b"hello"), ("cf", b"b", 1000, b"x"), ("zz", b"c", 0, b"")]),
          (b"r2", [("cf", b"a", 0, b"y")])]


def test_a_reset_row_discards_what_was_sent_for_that_row():
    assert _rows(_response(
        {"row_key": b"r1", "family_name": "cf", "qualifier": b"a", "value": b"stale"},
        {"reset_row": True},
        {"row_key": b"r1", "family_name": "cf", "qualifier": b"a", "value": b"fresh", "commit_row": True},
    )) == [(b"r1", [("cf", b"a", 0, b"fresh")])]


def test_a_stream_ending_inside_a_row_is_an_error():
    with pytest.raises(ValueError, match="ended inside a row"):
        _rows(_response({"row_key": b"r1", "family_name": "cf", "qualifier": b"a", "value": b"v"}))
