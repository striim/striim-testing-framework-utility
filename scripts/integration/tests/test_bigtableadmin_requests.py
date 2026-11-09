"""Hermetic: the admin requests BigtableAdmin builds are valid google-cloud-bigtable messages.

The table-admin client is a recorder, but every request it receives is the SDK's own type, so a
field the message does not have, or a oneof set twice, fails here as it would before any RPC.
"""
from types import SimpleNamespace

import pytest
from google.cloud.bigtable_admin_v2.types import DropRowRangeRequest

from inttest.bigtableadmin import BigtableAdmin

TABLE = "projects/test-project/instances/test-instance/tables/t1_rows"


class _Recorder:
    def __init__(self):
        self.requests = []

    def drop_row_range(self, request, **_deadline):
        self.requests.append(request)


def _admin():
    tac = _Recorder()
    dsn = {"project": "test-project", "instance": "test-instance", "emulator_host": "localhost:9470"}
    return BigtableAdmin(dsn, client=SimpleNamespace(table_admin_client=tac), data_client=object()), tac


def _target(request):
    return DropRowRangeRequest.pb(request).WhichOneof("target")


@pytest.mark.parametrize("prefix, expected", [("user#", b"user#"), (b"\x00\x01", b"\x00\x01"),
                                              ("0x0a0b", b"\x0a\x0b")])
def test_drop_row_range_sends_a_prefix_request(prefix, expected):
    admin, tac = _admin()
    admin.drop_row_range("t1_rows", row_key_prefix=prefix)
    (request,) = tac.requests
    assert isinstance(request, DropRowRangeRequest)
    assert request.name == TABLE
    assert _target(request) == "row_key_prefix" and request.row_key_prefix == expected


@pytest.mark.parametrize("bounds", [{"start_row_key": "a"}, {"end_row_key": "z"},
                                    {"row_key_prefix": "a", "start_row_key": "a", "end_row_key": "z"}])
def test_drop_row_range_refuses_start_and_end_keys(bounds):
    # DropRowRangeRequest has no start/end fields: only a prefix or the whole table.
    admin, tac = _admin()
    with pytest.raises(ValueError, match="start/end"):
        admin.drop_row_range("t1_rows", **bounds)
    assert tac.requests == []


@pytest.mark.parametrize("prefix", [None, "", b""])
def test_drop_row_range_refuses_an_empty_prefix(prefix):
    # An empty prefix names no target; emptying a table is drop_all_rows.
    admin, tac = _admin()
    with pytest.raises(ValueError, match="non-empty row_key_prefix"):
        admin.drop_row_range("t1_rows", row_key_prefix=prefix)
    assert tac.requests == []


def test_drop_all_rows_sends_a_delete_all_request():
    admin, tac = _admin()
    admin.drop_all_rows("t1_rows")
    (request,) = tac.requests
    assert _target(request) == "delete_all_data_from_table" and request.delete_all_data_from_table
