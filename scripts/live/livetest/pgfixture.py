"""Server deadlines and passive blocker diagnostics for Postgres fixture SQL."""
from __future__ import annotations

import json
import math
import re
import threading
import time


def policy(sql, phase, timeout, run_id, owners, role):
    if phase not in ("ddl", "seed"):
        raise ValueError("invalid-postgres-fixture-phase: expected ddl or seed")
    seconds = (600 if phase == "seed" else 60) if timeout is None else timeout
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0 or seconds * 1000 > 2_147_483_647):
        raise ValueError("invalid-postgres-fixture-budget: timeout must be positive finite seconds")
    if phase == "ddl" and re.search(r"\bpg_(?:create_logical_replication_slot|drop_replication_slot)\s*\(", sql, re.I):
        phase = "slot-setup"
    statement_ms = max(1, int(seconds * 1000))
    return {"phase": phase, "route": "postgres-" + role, "statement_ms": statement_ms,
            "lock_ms": min(15_000, max(1, statement_ms // 2)),
            "run_id": run_id, "owners": owners or {}}


def _connect(admin, which, *, seconds, options, application_name):
    user, password = admin._creds(which)
    conn = admin._connect(host=admin.dsn["host"], port=int(admin.dsn["port"]),
        dbname=admin.dsn["dbname"], user=user, password=password,
        connect_timeout=seconds, options=options, application_name=application_name)
    try:
        conn.autocommit = True
    except BaseException:
        conn.close()
        raise
    return conn


def _blockers(admin, pid, owners):
    conn = _connect(admin, "admin", seconds=2,
        options="-c statement_timeout=1000 -c lock_timeout=500",
        application_name="striim-test-diagnostic")
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT a.pid, a.application_name, "
                "EXTRACT(EPOCH FROM (clock_timestamp()-a.xact_start))::float8, a.state "
                "FROM pg_stat_activity a WHERE a.pid = ANY(pg_blocking_pids(%s)) ORDER BY a.pid", (pid,))
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    found = []
    for blocker, app, age, state in rows:
        namespace = (app or "").split(" - ")[-1].split(".")[0]
        owner = owners.get(namespace)
        if not owner and (app or "").startswith("striim-test:"):
            owner = app.split(":", 2)[1]
        found.append({"pid": blocker, "application_name": app, "transaction_age_s": age,
                      "state": state, "owning_run": owner, "observed_at": time.time()})
    return found


def execute(admin, statements, which, spec):
    phase, route = spec["phase"], spec["route"]
    options = f'-c lock_timeout={spec["lock_ms"]} -c statement_timeout={spec["statement_ms"]}'
    tag = f'striim-test:{spec["run_id"] or "fixture"}:{phase}'[:63]
    try:
        conn = _connect(admin, which or admin.role, seconds=5, options=options, application_name=tag)
    except Exception as exc:
        raise RuntimeError(f"postgres-fixture-connect-failed: phase={phase} route={route} "
                           f"connect_timeout_s=5 error={type(exc).__name__}") from exc
    stop = threading.Event()
    observed, diagnostic_errors = [], []
    watcher = None
    try:
        pid = getattr(conn, "get_backend_pid", lambda: None)()
        if isinstance(pid, int) and pid > 0:
            def observe():
                while not stop.wait(0.2):
                    try:
                        found = _blockers(admin, pid, spec["owners"])
                        if found:
                            observed[:] = found
                    except Exception as exc:
                        diagnostic_errors[:] = [type(exc).__name__]

            # Read only: neither this observer nor the timeout terminates a blocker.
            watcher = threading.Thread(target=observe, name="slt-pg-fixture-observer", daemon=True)
            watcher.start()
        cur = conn.cursor()
        try:
            for statement in statements:
                cur.execute(statement)
        finally:
            cur.close()
    except Exception as exc:
        stop.set()
        if watcher:
            watcher.join(4)
        if getattr(exc, "pgcode", None) in ("55P03", "57014"):
            detail = json.dumps(observed, sort_keys=True)
            raise RuntimeError(f"postgres-fixture-timeout: phase={phase} route={route} "
                f'lock_timeout_ms={spec["lock_ms"]} statement_timeout_ms={spec["statement_ms"]} '
                f"blockers={detail} diagnostic_errors={diagnostic_errors}") from exc
        raise
    finally:
        stop.set()
        if watcher:
            watcher.join(4)
        conn.close()
    if watcher and watcher.is_alive():
        raise RuntimeError(f"postgres-fixture-diagnostic-timeout: phase={phase} route={route}")
