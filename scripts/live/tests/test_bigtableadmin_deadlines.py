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

from livetest import bigtableadmin
from livetest.bigtableadmin import BigtableAdmin

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


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_an_assertion_on_a_stalled_table_fails_at_its_timeout_and_keeps_its_record(stalled_endpoint):
    from livetest.assertions import AssertionFailed
    from livetest.assertions.bigtable import assert_bigtable
    started = time.monotonic()
    with pytest.raises(AssertionFailed) as failed:
        assert_bigtable(_admin(stalled_endpoint), [{"table": "t1_rows", "expected": []}],
                        timeout=BUDGET_S, poll=0.05)
    assert time.monotonic() - started < BUDGET_S + 1.5
    (record,) = failed.value.records
    assert record["status"] == "failed" and record["target"] == "t1_rows"
    assert "did not finish within" in record["detail"]
