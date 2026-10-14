from types import SimpleNamespace

import pytest

from livetest import ownership, runident


def ledger(tmp_path):
    ident = runident.derive("sink", {"SLT_RUN_EPOCH": "run"}, attempt="abcd1234")
    return ownership.Ledger(tmp_path / "ledger.json", ident, {"entries": [], "bindings": {}})


def test_topic_collision_is_refused_before_clear(tmp_path):
    l = ledger(tmp_path)
    admin = SimpleNamespace(topic_exists=lambda name: True)
    with pytest.raises(ownership.OwnershipError, match="collision:kafka-topic"):
        l.acquire_topic(admin, "rows", lambda: pytest.fail("must not clear"))


def test_fresh_topic_is_confirmed(tmp_path):
    l = ledger(tmp_path)
    names = set()
    admin = SimpleNamespace(topic_exists=lambda name: name in names)
    l.acquire_topic(admin, "rows", lambda: names.add("rows"))
    assert l.owns("topic", "kafka", "rows")


def test_spanner_collision_is_refused_before_ddl(tmp_path):
    l = ledger(tmp_path)
    admin = SimpleNamespace(list_owned_tables=lambda: {"test_rows"})
    with pytest.raises(ownership.OwnershipError, match="collision:spanner-table"):
        l.run_ddl_files([("spanner-google", "ddl.sql")], admins={"spanner-google": {"admin": admin}},
            tokens={}, acquire_sinks=True, render_file=lambda name: "CREATE TABLE test_rows (id INT64) PRIMARY KEY(id);",
            run_one=lambda *a: pytest.fail("must not run ddl"))
