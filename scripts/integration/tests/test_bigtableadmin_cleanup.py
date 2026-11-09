"""Hermetic: BigtableAdmin's cleanup hooks fail closed, on the SDK's own exception types.

Only a NotFound from delete_table means the table is gone. Any other failure, and any failure to
list the instance's tables, must reach the caller instead of reading as "deleted" or "no tables".
"""
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc

from inttest.bigtableadmin import BigtableAdmin

INSTANCE = "projects/test-project/instances/test-instance"


class _TableAdmin:
    """A table-admin client that raises the given SDK exceptions from delete_table and list_tables."""

    def __init__(self, delete_error=None, list_error=None, listed=()):
        self.delete_error, self.list_error, self.listed = delete_error, list_error, list(listed)
        self.deleted = []

    def delete_table(self, name):
        self.deleted.append(name)
        if self.delete_error is not None:
            raise self.delete_error

    def list_tables(self, request):
        assert request.parent == INSTANCE
        if self.list_error is not None:
            raise self.list_error
        return [SimpleNamespace(name=f"{INSTANCE}/tables/{n}") for n in self.listed]


def _admin(tac):
    dsn = {"project": "test-project", "instance": "test-instance", "emulator_host": "localhost:9470"}
    return BigtableAdmin(dsn, client=SimpleNamespace(table_admin_client=tac), data_client=object())


def test_drop_suppresses_a_confirmed_not_found_only():
    tac = _TableAdmin(delete_error=gexc.NotFound("table not found"))
    _admin(tac).drop_owned_table("test_rows")
    assert tac.deleted == [f"{INSTANCE}/tables/test_rows"]


@pytest.mark.parametrize("error", [gexc.ServiceUnavailable("emulator down"),
                                   gexc.PermissionDenied("no"), gexc.DeadlineExceeded("slow")])
def test_drop_propagates_every_other_failure(error):
    with pytest.raises(type(error)):
        _admin(_TableAdmin(delete_error=error)).drop_owned_table("test_rows")


def test_listing_failure_propagates_instead_of_reporting_no_tables():
    with pytest.raises(gexc.ServiceUnavailable):
        _admin(_TableAdmin(list_error=gexc.ServiceUnavailable("emulator down"))).list_owned_tables()


def test_listing_names_the_instance_tables():
    assert _admin(_TableAdmin(listed=["test_rows", "other"])).list_owned_tables() == {"test_rows", "other"}

