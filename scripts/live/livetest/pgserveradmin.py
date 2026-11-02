"""The ``postgres-admin`` route: a superuser connection for the few server-level statements a
case needs to make a target commit land with its reply lost (an in-doubt commit).

It accepts only:

- ``ALTER SYSTEM SET synchronous_standby_names = 'slt_no_standby'`` and
  ``ALTER SYSTEM RESET synchronous_standby_names``: commits then wait for a standby that never
  answers;
- ``SELECT pg_reload_conf()``;
- ``ALTER ROLE qasource|qatarget SET synchronous_commit = <value>`` and ``... RESET
  synchronous_commit``: a role whose commits must not wait (the source) sets ``local``;
- ``SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'qatarget' AND
  wait_event = 'SyncRep'``: retried until it ends at least one such backend, or the step's
  timeout passes. A backend ended while it waits on SyncRep has committed locally; its client
  sees the connection drop.

Anything else fails ``postgres-admin-unsupported`` before a connection opens. The settings are
server-wide, so the route refuses a parallel run (``postgres-admin-parallel``). Teardown resets
them when the route was used (:meth:`PgServerAdmin.reset`), and every PostgreSQL setup resets a
``synchronous_standby_names`` left at the sentinel by a killed run (:func:`clear_stale`).
"""
from __future__ import annotations

import os
import re
import time

SENTINEL = "slt_no_standby"

_STATEMENTS = [
    re.compile(r"ALTER\s+SYSTEM\s+SET\s+synchronous_standby_names\s*(=|TO)\s*'" + SENTINEL + r"'", re.I),
    re.compile(r"ALTER\s+SYSTEM\s+RESET\s+synchronous_standby_names", re.I),
    re.compile(r"SELECT\s+pg_reload_conf\s*\(\s*\)", re.I),
    re.compile(r"ALTER\s+ROLE\s+(qasource|qatarget)\s+SET\s+synchronous_commit\s*(=|TO)\s*'?"
               r"(on|off|local|remote_write|remote_apply)'?", re.I),
    re.compile(r"ALTER\s+ROLE\s+(qasource|qatarget)\s+RESET\s+synchronous_commit", re.I),
]
_TERMINATE = re.compile(r"SELECT\s+pg_terminate_backend\s*\(\s*pid\s*\)\s+FROM\s+pg_stat_activity\s+"
                        r"WHERE\s+usename\s*=\s*'qatarget'\s+AND\s+wait_event\s*=\s*'SyncRep'", re.I)

RESET = [
    "ALTER SYSTEM RESET synchronous_standby_names",
    "ALTER ROLE qasource RESET synchronous_commit",
    "ALTER ROLE qatarget RESET synchronous_commit",
    "SELECT pg_reload_conf()",
]

TERMINATE_TIMEOUT_S = 120.0
_POLL_S = 0.5


class PgAdminRouteError(ValueError):
    """A statement or run the ``postgres-admin`` route refuses; the message starts with its code."""


def statements(sql: str) -> list[str]:
    """The script's statements: split on ';', each trimmed, comment lines and blanks dropped."""
    out = []
    for part in sql.split(";"):
        lines = [ln for ln in part.splitlines() if not ln.strip().startswith("--")]
        text = " ".join(" ".join(lines).split())
        if text:
            out.append(text)
    return out


def check(sql: str) -> list[str]:
    """The statements, or :class:`PgAdminRouteError` naming the first one the route refuses."""
    stmts = statements(sql)
    for s in stmts:
        if not (_TERMINATE.fullmatch(s) or any(p.fullmatch(s) for p in _STATEMENTS)):
            raise PgAdminRouteError(f"postgres-admin-unsupported: {s!r}; the route takes only "
                                    "synchronous_standby_names, pg_reload_conf(), a data role's "
                                    "synchronous_commit and the qatarget SyncRep terminate")
    return stmts


class PgServerAdmin:
    """Runs checked statements as the PostgreSQL superuser of a :class:`PgAdmin`'s dsn."""

    def __init__(self, pg_admin, parallel=None, clock=time.monotonic, sleep=time.sleep):
        self._pg = pg_admin
        self._parallel = parallel
        self._clock = clock
        self._sleep = sleep
        #: Whether a statement ran: teardown then resets (:meth:`reset`).
        self.touched = False

    def run_sql(self, sql: str, *, phase="ddl", timeout=None, run_id=None, owners=None) -> None:
        stmts = check(sql)
        parallel = self._parallel if self._parallel is not None else bool(
            os.environ.get("SLT_PARALLEL") not in (None, "", "0"))
        if parallel:
            raise PgAdminRouteError("postgres-admin-parallel: the route changes server-wide "
                                    "settings, which would stall other cases' commits; mark the "
                                    "case disabled_parallel")
        self.touched = True
        for s in stmts:
            if _TERMINATE.fullmatch(s):
                self._terminate(s, timeout or TERMINATE_TIMEOUT_S)
            else:
                _run(self._pg, s)

    def _terminate(self, sql: str, timeout: float) -> None:
        deadline = self._clock() + timeout
        while True:
            _, rows = self._pg._query(sql, which="admin")
            if any(r[0] for r in rows):
                return
            if self._clock() >= deadline:
                raise PgAdminRouteError(f"postgres-admin-no-waiter: no qatarget backend waited on "
                                        f"SyncRep within {timeout:.0f}s")
            self._sleep(_POLL_S)

    def reset(self) -> None:
        """Teardown: the settings back to the server's own, when this route ran a statement."""
        if not self.touched:
            return
        for s in RESET:
            _run(self._pg, s)
        self.touched = False


def clear_stale(pg_admin) -> bool:
    """Resets ``synchronous_standby_names`` left at the sentinel (a run killed before teardown).

    :return: whether it was reset
    """
    _, rows = pg_admin._query("SHOW synchronous_standby_names", which="admin")
    if not rows or rows[0][0] != SENTINEL:
        return False
    for s in RESET:
        _run(pg_admin, s)
    return True


def _run(pg_admin, statement: str) -> None:
    """One statement as the superuser: a SELECT read, anything else executed (no result set)."""
    if statement.upper().startswith("SELECT"):
        pg_admin._query(statement, which="admin")
    else:
        pg_admin._exec([statement], which="admin")
