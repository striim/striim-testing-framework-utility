from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import transactions


def steps():
    return [{"session": "a", "op": "begin", "db": "postgres-source", "when": "post_start"},
            {"session": "b", "op": "begin", "db": "postgres-source", "when": "post_start"},
            {"session": "a", "op": "sql", "file": "a.sql", "when": "post_start"},
            {"session": "a", "op": "savepoint", "name": "keep", "when": "post_start"},
            {"session": "a", "op": "sql", "file": "discard.sql", "when": "post_start"},
            {"session": "a", "op": "rollback_to", "name": "keep", "when": "post_start"},
            {"session": "b", "op": "commit", "when": "post_start"},
            {"session": "a", "op": "commit", "when": "post_recover"}]


class Connection:
    def __init__(self, name, log): self.name, self.log, self.closed = name, log, False
    def cursor(self): return self
    def execute(self, sql, params=None): self.log.append((self.name, sql))
    def commit(self): self.log.append((self.name, "commit"))
    def rollback(self): self.log.append((self.name, "rollback"))
    def close(self): self.closed = True


def test_overlapping_sessions_hold_through_recover_and_commit_in_chosen_order():
    log, conns = [], []
    def connect(admin, timeout):
        conn = Connection("a" if not conns else "b", log); conns.append(conn); return conn
    pool = transactions.Pool({"postgres-source": {"admin": object()}}, {},
        source_sql=lambda f: "INSERT INTO source VALUES (1)", connect=connect)
    pool.run_phase(steps(), "post_start")
    assert len(conns) == 2 and conns[0].closed is False and conns[1].closed is True
    assert ("b", "commit") in log and ("a", "commit") not in log
    pool.run_phase(steps(), "post_recover")
    assert log.index(("b", "commit")) < log.index(("a", "commit"))
    assert any(sql == 'ROLLBACK TO SAVEPOINT "keep"' for _, sql in log)
    assert all(c.closed for c in conns)


def test_error_closes_and_rolls_back_every_owned_session():
    conn = Connection("a", [])
    pool = transactions.Pool({"postgres-source": {"admin": object()}}, {},
        source_sql=lambda f: "COMMIT;", connect=lambda *a: conn)
    with pytest.raises(ValueError, match="transaction-control-in-sql"):
        pool.run_phase(steps()[:1] + [steps()[2]], "post_start")
    assert conn.closed and ("a", "rollback") in conn.log


@pytest.mark.parametrize("change", [lambda s: s[0].update(db="mysql-source"),
    lambda s: s[0].update(unknown=1), lambda s: s[0].update(op="sql"),
    lambda s: s[5].update(name="missing"), lambda s: s.pop(),
    lambda s: s[2].update(file="../escape.sql")])
def test_invalid_transaction_schedule(change, tmp_path):
    s = steps(); change(s)
    with pytest.raises(ValueError, match="invalid-transaction-seed"):
        transactions.normalize(s, tmp_path / "test.yaml", recover=True, lifecycle=True)
