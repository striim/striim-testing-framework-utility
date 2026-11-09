"""Hermetic: every BigtableAdmin operation ends at its time budget, on the real SDK clients.

The clients dial an in-process gRPC server whose Bigtable RPCs stall far past every budget here, so
the only way an operation returns in time is the deadline it handed the SDK. No emulator and no
network beyond the loopback socket of this process.
"""
import threading
import time
from concurrent import futures

import grpc
import pytest
from google.api_core.exceptions import DeadlineExceeded

from inttest import bigtableadmin
from inttest.bigtableadmin import BigtableAdmin

STALL_S = 5.0     # how long the server holds every call
BUDGET_S = 0.5


@pytest.fixture
def stalled_endpoint(monkeypatch):
    release = threading.Event()

    def unary(request, context):
        release.wait(STALL_S)
        return b""                      # an empty message of any response type

    def stream(request, context):
        release.wait(STALL_S)
        return iter(())

    services = {
        "google.bigtable.admin.v2.BigtableTableAdmin": {
            name: grpc.unary_unary_rpc_method_handler(unary)
            for name in ("CreateTable", "DeleteTable", "GetTable", "ListTables", "DropRowRange")},
        "google.bigtable.v2.Bigtable": {
            "ReadRows": grpc.unary_stream_rpc_method_handler(stream),
            "MutateRow": grpc.unary_unary_rpc_method_handler(unary)},
    }
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    server.add_generic_rpc_handlers(tuple(grpc.method_handlers_generic_handler(svc, methods)
                                          for svc, methods in services.items()))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    monkeypatch.setattr(bigtableadmin, "_CLIENTS", {})
    monkeypatch.delenv("BIGTABLE_EMULATOR_HOST", raising=False)
    try:
        yield f"127.0.0.1:{port}"
    finally:
        release.set()
        server.stop(0)


def _admin(endpoint):
    return BigtableAdmin({"project": "test-project", "instance": "test-instance",
                          "emulator_host": endpoint})


OPERATIONS = {
    "create_table": lambda a: a.create_table("t1_rows", {"cf": None}),
    "drop_table": lambda a: a.drop_table("t1_rows"),
    "list_families": lambda a: a.list_families("t1_rows"),
    "list_owned_tables": lambda a: a.list_owned_tables(),
    "drop_all_rows": lambda a: a.drop_all_rows("t1_rows"),
    "write_cells": lambda a: a.write_cells("t1_rows", [{"row": "r1", "cells": {"cf:q": "v"}}]),
    "read_row": lambda a: a.read_row("t1_rows", "r1"),
    "read_all": lambda a: a.read_all("t1_rows"),
    "delete_row": lambda a: a.delete_row("t1_rows", "r1"),
}


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_a_stalled_rpc_ends_at_the_default_operation_budget(stalled_endpoint, monkeypatch, operation):
    monkeypatch.setattr(bigtableadmin, "OPERATION_TIMEOUT_S", BUDGET_S, raising=False)
    started = time.monotonic()
    with pytest.raises((DeadlineExceeded, TimeoutError)):
        OPERATIONS[operation](_admin(stalled_endpoint))
    assert time.monotonic() - started < BUDGET_S + 1.5


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_an_explicit_budget_overrides_the_default(stalled_endpoint):
    started = time.monotonic()
    with pytest.raises((DeadlineExceeded, TimeoutError)):
        _admin(stalled_endpoint).read_all("t1_rows", timeout=BUDGET_S)
    assert time.monotonic() - started < BUDGET_S + 1.5


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_readiness_gives_up_at_its_budget_not_after_every_attempt(stalled_endpoint):
    # 1000 attempts with no budget would hold a stalled emulator for 1000 RPC timeouts.
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="not ready within"):
        _admin(stalled_endpoint).ensure(attempts=1000, delay=0.05, timeout=BUDGET_S)
    assert time.monotonic() - started < BUDGET_S + 1.5


@pytest.mark.parametrize("pages", [1, 4], ids=["one-page", "four-pages"])
def test_listing_pages_share_one_budget(monkeypatch, pages):
    # Each page takes 0.15s; under a 0.25s budget one page fits, four must not.
    from google.cloud.bigtable_admin_v2.types import ListTablesRequest, ListTablesResponse, Table
    remaining = []

    def list_tables(raw, context):
        request = ListTablesRequest.deserialize(raw)
        remaining.append(context.time_remaining())
        time.sleep(0.15)
        page = int(request.page_token or "0")
        return ListTablesResponse.serialize(ListTablesResponse(
            tables=[Table(name=f"projects/test-project/instances/test-instance/tables/t{page}")],
            next_page_token=str(page + 1) if page + 1 < pages else ""))

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(
        "google.bigtable.admin.v2.BigtableTableAdmin",
        {"ListTables": grpc.unary_unary_rpc_method_handler(list_tables)}),))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    monkeypatch.setattr(bigtableadmin, "_CLIENTS", {})
    try:
        admin = _admin(f"127.0.0.1:{port}")
        started = time.monotonic()
        if pages == 1:
            assert admin.list_owned_tables(timeout=0.25) == {"t0"}
        else:
            with pytest.raises((DeadlineExceeded, TimeoutError)):
                admin.list_owned_tables(timeout=0.25)
            assert time.monotonic() - started < 0.25 + 0.5
            assert len(remaining) <= 2                       # no page is asked for after expiry
        assert all(r is not None and r <= 0.25 for r in remaining)
    finally:
        server.stop(0)
