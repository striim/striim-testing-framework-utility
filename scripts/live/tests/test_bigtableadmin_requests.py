"""Hermetic: the admin requests BigtableAdmin builds are valid google-cloud-bigtable messages.

The table-admin client is a recorder, but every request it receives is the SDK's own type, so a
field the message does not have, or a oneof set twice, fails here as it would before any RPC.
"""
from types import SimpleNamespace

import pytest
from google.cloud.bigtable_admin_v2.types import CreateTableRequest, DropRowRangeRequest, GcRule

from livetest.bigtableadmin import BigtableAdmin, _gc_to_rule

TABLE = "projects/test-project/instances/test-instance/tables/t1_rows"


class _Recorder:
    def __init__(self):
        self.requests = []

    def drop_row_range(self, request, **_deadline):
        self.requests.append(request)

    def create_table(self, request, **_deadline):
        self.requests.append(request)

    def delete_table(self, name, **_deadline):
        pass


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


# --- GC rules: GcRule's `rule` is a oneof, so a combined policy is an explicit union ----------

def _which(rule):
    return GcRule.pb(rule).WhichOneof("rule")


def test_num_versions_is_a_versions_only_rule():
    rule = _gc_to_rule({"num_versions": 3})
    assert isinstance(rule, GcRule)
    assert _which(rule) == "max_num_versions" and rule.max_num_versions == 3


@pytest.mark.parametrize("gc, seconds, nanos", [
    ({"age_millis": 1500}, 1, 500_000_000),
    ({"age_seconds": 90}, 90, 0),
    ({"age_days": 2}, 172_800, 0),
])
def test_an_age_rule_expires_versions_by_age_only(gc, seconds, nanos):
    # Bigtable's own max_age: every version older than the age is collected, and no other.
    rule = _gc_to_rule(gc)
    assert _which(rule) == "max_age"
    duration = GcRule.pb(rule).max_age
    assert (duration.seconds, duration.nanos) == (seconds, nanos)


@pytest.mark.parametrize("gc", [{"num_versions": 0}, {"age_days": 0}, {"age_millis": -1}])
def test_a_rule_that_is_not_positive_is_refused(gc):
    with pytest.raises(ValueError, match="greater than 0"):
        _gc_to_rule(gc)


@pytest.mark.parametrize("gc", [{"num_versions": 2, "age_days": 1}, {"age_seconds": 1, "age_days": 1}])
def test_a_rule_naming_two_limits_is_refused(gc):
    with pytest.raises(ValueError, match="exactly one"):
        _gc_to_rule(gc)


def test_create_table_sends_the_age_rule_on_its_family():
    admin, tac = _admin()
    admin.create_table("t1_rows", {"cf": None, "plain": None}, gc_rules={"cf": {"age_days": 7}})
    (request,) = tac.requests
    assert isinstance(request, CreateTableRequest)
    families = request.table.column_families
    assert _which(families["cf"].gc_rule) == "max_age"
    assert _which(families["plain"].gc_rule) is None          # no rule: the family never expires
