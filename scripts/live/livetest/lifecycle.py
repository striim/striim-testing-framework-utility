"""Deterministic lifecycle witnesses for live cases (contract set 1.7.0, C7.2-C7.3).

A case that declares a ``lifecycle:`` block starts ordered changes only after POSITIVE capture
readiness and asserts only after POSITIVE completion, each bounded by a monotonic deadline and
followed by a stability interval. Every wait produces a record (condition, observations, deadline,
reason); a failed witness raises ``LifecycleError`` (an ``AssertionFailed``) with the record kept.
A case without the block runs the legacy lifecycle, recorded as such and never qualifying.

Bounded I/O: every probe runs in a worker thread joined for the remaining deadline. Postgres
probes (``PgProbe``) open their own connections with ``connect_timeout`` and
``statement_timeout``; on expiry the statement is cancelled and the connection closed, and the
probe is never reused. Sentinel SQL runs through a probe, never through the admin connection that
cleanup uses. Striim status reads pass a request ``timeout``. Currently the lifecycle routes are
Postgres (``postgres-source``/``postgres-target``) and file sinks under ``${OWNED_DIR}``; any
other route is rejected at load (unsupported resource type).

``livetest.plugin`` and ``livetest.manifest`` reach this module through ``lifecycle-hooks@1``.
"""
from __future__ import annotations

import collections
import math
import posixpath
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from livetest.assertions import AssertionFailed

MODES = ("initial-load", "cdc")
SINKS = ("db", "file")
READINESS_KINDS = ("baseline-landed", "source-progress", "sentinel")
COMPLETION_KINDS = ("source-count", "row-count", "file-lines", "sentinel")
DB_ROUTES = ("postgres-source", "postgres-target")
OWNED_DIR_PREFIX = "${OWNED_DIR}/"
DEFAULT_STABILITY_S = 5.0
DEFAULT_READINESS_S = 60.0
MAX_OBSERVATIONS = 50
# After a probe's deadline: the bound on cancel(), close() and joining the probe thread. A probe
# thread that is still inside a statement after that is reported as cancel-failed (never "cancelled").
CANCEL_BOUND_S = 2.0
TERMINAL_STATUSES = frozenset({"CRASH", "HALT", "TERMINATED", "DEPLOY_FAILED"})   # livetest.striim
RECORD_KEYS = ("kind", "condition", "witness", "at", "startedAt", "endedAt", "deadlineS",
               "observations", "reason")
SENTINEL_ID_MAX = 2 ** 31 - 1          # ids are 1 + randbelow(2**31 - 1): a signed 32-bit INT

_BLOCK_KEYS = {"version", "mode", "sink", "readiness", "completion", "stability", "deadlines",
               "reset", "sentinel"}
_KIND_KEYS = {
    "baseline-landed": {"kind", "source", "target", "path"},
    "source-progress": {"kind", "db"},
    "sentinel": {"kind"},
    "source-count": {"kind", "source", "target"},
    "row-count": {"kind", "db", "table", "expect"},
    "file-lines": {"kind", "path", "lines"},
}
_SENTINEL_KEYS = {"db", "insert", "delete", "observe"}
_OBSERVE_KEYS = {"db", "table", "key"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m)$")


class LifecycleSpecError(ValueError):
    """An invalid ``lifecycle:`` block (surfaced by load_manifest as ManifestError)."""


class LifecycleError(AssertionFailed):
    """A lifecycle witness failed. Carries the witness record and any assertion records."""

    def __init__(self, message: str, record: dict | None = None, records: list | None = None):
        super().__init__(message, list(records or []))
        self.record = record


class ProbeHung(Exception):
    reason = "probe-hung"


class ProbeCancelled(Exception):
    reason = "probe-cancelled"


class ProbeCancelFailed(Exception):
    reason = "cancel-failed"


class ProbeSpent(Exception):
    reason = "probe-spent"


class ReadLimitExceeded(Exception):
    """An exact read over its row or byte budget, refused before the rows are transferred (r1 F7)."""


# ---------------------------------------------------------------------------------------------
# The block (C7.2), validated at load
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LifecycleSpec:
    version: int
    mode: str
    sink: str
    readiness: dict
    completion: dict
    stability_s: float
    readiness_s: float
    completion_s: float
    reset: str
    sentinel: dict | None

    def uses_sentinel(self) -> bool:
        return self.readiness["kind"] == "sentinel" or self.completion["kind"] == "sentinel"


def parse_duration(value, what: str, path) -> float:
    if isinstance(value, bool):
        _err(path, f"{what} must be a duration (e.g. 5s, 500ms, 2m) or seconds, got {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str) and _DURATION.match(value.strip()):
        n, unit = _DURATION.match(value.strip()).groups()
        seconds = float(n) * {"ms": 0.001, "s": 1.0, "m": 60.0}[unit]
    else:
        _err(path, f"{what} must be a duration (e.g. 5s, 500ms, 2m) or seconds, got {value!r}")
    if not math.isfinite(seconds):
        _err(path, f"{what} must be a finite duration, got {value!r}")
    if seconds < 0:
        _err(path, f"{what} must not be negative, got {value!r}")
    return seconds


def _err(path, msg: str):
    raise LifecycleSpecError(f"{path}: lifecycle: {msg}")


def _route(path, value, what: str) -> str:
    if value not in DB_ROUTES:
        _err(path, f"{what} {value!r} is an unsupported lifecycle route for this resource type (supported: "
                   f"{', '.join(DB_ROUTES)}; other engines are unsupported)")
    return value


def _positive_int(path, value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _err(path, f"{what} must be an integer, got {value!r}")
    if value <= 0:
        _err(path, f"{what} is {value}: zero is never a completion witness (declare completion kind "
                   f"sentinel for an empty final result)")
    return value


def _table_ref(path, value, what: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"db", "table"}:
        _err(path, f"{what} must be a mapping with exactly db and table, got {value!r}")
    _route(path, value["db"], f"{what}.db")
    if not isinstance(value["table"], str) or not value["table"]:
        _err(path, f"{what}.table must be a non-empty string")
    return dict(value)


def _kind_block(path, value, what: str, kinds) -> dict:
    if not isinstance(value, dict) or "kind" not in value:
        _err(path, f"{what} must be a mapping with a kind (one of {', '.join(kinds)})")
    kind = value["kind"]
    if kind not in kinds:
        _err(path, f"unknown {what} kind {kind!r} (one of {', '.join(kinds)})")
    extra = set(value) - _KIND_KEYS[kind]
    if extra:
        _err(path, f"{what} kind {kind} has unknown key(s) {sorted(extra)}")
    return dict(value)


def parse_spec(block, path, *, timeout=120) -> LifecycleSpec:
    if not isinstance(block, dict):
        _err(path, f"must be a mapping, got {block!r}")
    extra = set(block) - _BLOCK_KEYS
    if extra:
        _err(path, f"unknown key(s) {sorted(extra)}")
    if "version" not in block:
        _err(path, "'version' is required (only 1 is accepted)")
    version = block["version"]
    if isinstance(version, bool) or version != 1:
        _err(path, f"version {version!r} is not supported (only 1 is accepted)")
    mode = block.get("mode")
    if mode not in MODES:
        _err(path, f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    sink = block.get("sink")
    if sink not in SINKS:
        _err(path, f"sink must be one of {', '.join(SINKS)}, got {sink!r}")
    readiness = _kind_block(path, block.get("readiness"), "readiness", READINESS_KINDS)
    completion = _kind_block(path, block.get("completion"), "completion", COMPLETION_KINDS)
    rk, ck = readiness["kind"], completion["kind"]

    if mode == "initial-load" and rk != "baseline-landed":
        _err(path, f"mode initial-load requires readiness kind baseline-landed, got {rk!r}")
    if mode == "cdc" and rk == "baseline-landed":
        _err(path, "readiness kind baseline-landed is for mode initial-load; mode cdc uses "
                   "source-progress or sentinel")
    if mode == "initial-load" and ck == "sentinel":
        _err(path, "completion kind sentinel is for mode cdc")
    if mode == "cdc" and ck == "source-count":
        _err(path, "completion kind source-count is for mode initial-load")
    if sink == "file" and (rk == "sentinel" or ck == "sentinel"):
        _err(path, "sink file cannot use a sentinel kind")
    if sink == "file" and ck != "file-lines":
        _err(path, f"sink file observes completion with kind file-lines, got {ck!r}")
    if sink == "db" and ck == "file-lines":
        _err(path, "completion kind file-lines requires sink file")

    if rk == "baseline-landed":
        readiness["source"] = _table_ref(path, readiness.get("source"), "readiness.source")
        if sink == "db":
            if "path" in readiness:
                _err(path, "readiness.path is only for sink file")
            readiness["target"] = _table_ref(path, readiness.get("target"), "readiness.target")
        else:
            if "target" in readiness:
                _err(path, "readiness.target is only for sink db")
            if not isinstance(readiness.get("path"), str):
                _err(path, "readiness.path is required for sink file")
    elif rk == "source-progress":
        db = readiness.get("db")
        if db != "postgres-source":
            _err(path, f"readiness source-progress db {db!r} is an unsupported lifecycle route in "
                       f"this readiness kind (only postgres-source: pg_replication_slots.active for ${{PG_SLOT}}; "
                       f"other engines are unsupported)")
    if ck == "source-count":
        completion["source"] = _table_ref(path, completion.get("source"), "completion.source")
        completion["target"] = _table_ref(path, completion.get("target"), "completion.target")
    elif ck == "row-count":
        _route(path, completion.get("db"), "completion.db")
        if not isinstance(completion.get("table"), str) or not completion["table"]:
            _err(path, "completion.table must be a non-empty string")
        _positive_int(path, completion.get("expect"), "completion.expect")
    elif ck == "file-lines":
        if not isinstance(completion.get("path"), str):
            _err(path, "completion.path is required for kind file-lines")
        _positive_int(path, completion.get("lines"), "completion.lines")

    sentinel = block.get("sentinel")
    needed = rk == "sentinel" or ck == "sentinel"
    if needed and sentinel is None:
        _err(path, "a sentinel readiness or completion kind requires the sentinel block")
    if not needed and sentinel is not None:
        _err(path, "the sentinel block is given but no readiness or completion kind uses it")
    if sentinel is not None:
        if not isinstance(sentinel, dict):
            _err(path, "sentinel must be a mapping")
        if set(sentinel) != _SENTINEL_KEYS:
            _err(path, f"sentinel needs exactly {sorted(_SENTINEL_KEYS)}, got {sorted(sentinel)}")
        _route(path, sentinel["db"], "sentinel.db")
        for k in ("insert", "delete"):
            if not isinstance(sentinel[k], str) or not sentinel[k]:
                _err(path, f"sentinel.{k} must name a case SQL file")
        observe = sentinel["observe"]
        if not isinstance(observe, dict) or set(observe) != _OBSERVE_KEYS:
            _err(path, f"sentinel.observe needs exactly {sorted(_OBSERVE_KEYS)}")
        _route(path, observe["db"], "sentinel.observe.db")
        if not isinstance(observe["table"], str) or "${TID}" not in observe["table"]:
            _err(path, "sentinel.observe.table must contain ${TID} (a run-owned table)")
        if not isinstance(observe["key"], str) or not _IDENT.match(observe["key"]):
            _err(path, f"sentinel.observe.key must be a column name, got {observe['key']!r}")
        sentinel = {**sentinel, "observe": dict(observe)}

    stability = parse_duration(block.get("stability", DEFAULT_STABILITY_S), "stability", path)
    deadlines = block.get("deadlines") or {}
    if not isinstance(deadlines, dict) or set(deadlines) - {"readiness", "completion"}:
        _err(path, "deadlines accepts only readiness and completion")
    readiness_s = parse_duration(deadlines.get("readiness", DEFAULT_READINESS_S), "deadlines.readiness", path)
    completion_s = parse_duration(deadlines.get("completion", timeout), "deadlines.completion", path)
    if readiness_s <= 0 or completion_s <= 0:
        _err(path, "deadlines must be greater than zero")
    reset = block.get("reset", "owned")
    if reset == "checkpoint-continuation":
        _err(path, "reset checkpoint-continuation is reserved and not accepted until its own contract exists")
    if reset != "owned":
        _err(path, f"reset must be owned, got {reset!r}")
    return LifecycleSpec(version=1, mode=mode, sink=sink, readiness=readiness, completion=completion,
                         stability_s=stability, readiness_s=readiness_s, completion_s=completion_s,
                         reset=reset, sentinel=sentinel)


def parse_manifest_block(raw: dict, path) -> LifecycleSpec | None:
    """The manifest's ``lifecycle`` block (None: legacy lifecycle). Also enforces the owned-directory
    rule: in a lifecycle case every ``assert.file`` path, ``server_files`` dest and file sink path
    starts with ``${OWNED_DIR}/``."""
    block = raw.get("lifecycle")
    if block is None:
        return None
    timeout = raw.get("timeout", 120)
    spec = parse_spec(block, path, timeout=timeout if isinstance(timeout, (int, float)) else 120)
    paths = []
    for i, fs in enumerate((raw.get("assert") or {}).get("file") or []):
        if isinstance(fs, dict) and isinstance(fs.get("path"), str):
            paths.append((f"assert.file[{i}].path", fs["path"]))
    for i, sf in enumerate(raw.get("server_files") or []):
        if isinstance(sf, dict) and isinstance(sf.get("dest"), str) and not sf.get("load"):
            paths.append((f"server_files[{i}].dest", sf["dest"]))
    if spec.completion["kind"] == "file-lines":
        paths.append(("completion.path", spec.completion["path"]))
    if spec.readiness["kind"] == "baseline-landed" and spec.sink == "file":
        paths.append(("readiness.path", spec.readiness["path"]))
    for what, p in paths:
        if not p.startswith(OWNED_DIR_PREFIX):
            _err(path, f"{what} {p!r} must start with {OWNED_DIR_PREFIX} in a lifecycle case (the "
                       f"framework-allocated owned directory)")
        if any(seg in ("", ".", "..") for seg in p[len(OWNED_DIR_PREFIX):].split("/")):
            _err(path, f"{what} {p!r} must stay inside {OWNED_DIR_PREFIX} (no '.', '..' or empty path "
                       f"segments)")
    return spec


# ---------------------------------------------------------------------------------------------
# Clock, deadlines, records
# ---------------------------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Clock:
    """Injectable time. ``monotonic`` bounds every wait; ``wall`` only stamps records."""
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)
    wall = staticmethod(now_iso)


class Deadline:
    def __init__(self, seconds: float, clock=None):
        self.clock = clock or Clock
        self.seconds = float(seconds)
        self.end = self.clock.monotonic() + self.seconds

    def remaining(self) -> float:
        return max(0.0, self.end - self.clock.monotonic())

    def expired(self) -> bool:
        return self.clock.monotonic() >= self.end


class Watch:
    def __init__(self, kind: str, condition: str, deadline: Deadline):
        self.kind, self.condition, self.deadline = kind, condition, deadline
        self.clock = deadline.clock
        self.started = self.clock.wall()
        self.observations = collections.deque(maxlen=MAX_OBSERVATIONS)

    def observe(self, value) -> None:
        self.observations.append({"at": self.clock.wall(), "value": value})

    def finish(self, reason: str, witness: str | None = None) -> dict:
        ended = self.clock.wall()
        return {"kind": self.kind, "condition": self.condition,
                "witness": witness if reason == "satisfied" else None,
                "at": ended, "startedAt": self.started, "endedAt": ended,
                "deadlineS": self.deadline.seconds, "observations": list(self.observations),
                "reason": reason}


def legacy_record(kind: str) -> dict:
    witness = {"ready": "app RUNNING (not capture readiness)",
               "completion": "assertion polling (not positive completion)"}[kind]
    return {"kind": "legacy", "condition": "no lifecycle block", "witness": witness, "at": None,
            "startedAt": None, "endedAt": None, "deadlineS": None, "observations": [],
            "reason": "legacy"}


class State:
    """Lifecycle state of one case execution; the evidence envelope serializes it."""

    def __init__(self, mode: str):
        self.mode = mode
        self.started_at = now_iso()
        self.running = None
        self.baseline = None
        self.ready = None
        self.completion = None
        self.stability = None

    @classmethod
    def for_manifest(cls, m) -> "State":
        spec = getattr(m, "lifecycle", None)
        state = cls("legacy" if spec is None else spec.mode)
        if spec is None:
            state.ready, state.completion = legacy_record("ready"), legacy_record("completion")
        return state

    def ready_satisfied(self) -> bool:
        return bool(self.ready) and self.ready.get("reason") == "satisfied"

    def completion_satisfied(self) -> bool:
        return bool(self.completion) and self.completion.get("reason") == "satisfied"

    def to_dict(self) -> dict:
        return {"mode": self.mode, "startedAt": self.started_at, "running": self.running,
                "baseline": self.baseline, "ready": self.ready, "completion": self.completion,
                "stability": self.stability}


# ---------------------------------------------------------------------------------------------
# Bounded probes
# ---------------------------------------------------------------------------------------------

def bounded(fn, remaining: float):
    """Run ``fn`` in a daemon thread joined for ``remaining`` seconds; ``ProbeHung`` if it has not
    returned (the thread is abandoned, never joined again)."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = e

    t = threading.Thread(target=run, daemon=True, name="slt-lifecycle-probe")
    t.start()
    t.join(max(0.0, float(remaining)))
    if t.is_alive():
        raise ProbeHung(f"probe did not return within {remaining:.1f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def status_bounded(client, app: str, remaining: float, get=None) -> str:
    """The app status through the REST API with a request timeout (the client's own call has none)."""
    api = client.api
    if get is None:
        import requests
        get = requests.get
    resp = get(api.url_base + "/api/v2/applications/" + app, headers=api.getHeader("application/json"),
               timeout=max(0.1, min(float(remaining), 10.0)))
    return resp.json()["status"]


def _pg_connect(**kw):
    import psycopg2
    return psycopg2.connect(**kw)


# Bounded reads (C8.3): rows per FETCH of an exact read, lowered so one batch of the widest row fits the budget
READ_FETCH_ROWS = 1000
_EXACT_CURSOR = "slt_exact_read"


def _driver_connection(conn) -> bool:
    from psycopg2 import extensions
    return isinstance(conn, extensions.connection)


def register_exact_json(conn) -> None:
    """r1 F1: json and jsonb (and their arrays) on ``conn`` decode to ``canon.JsonText``, the raw text that
    canon parses losslessly. Registered on this connection only: psycopg2's global typecasters and every other
    connection keep ``json.loads``."""
    from psycopg2 import extras
    from livetest.canon import JsonText
    if conn is None:
        raise ValueError("exact JSON typecasters are registered per connection, never globally")
    extras.register_default_json(conn, globally=False, loads=JsonText)
    extras.register_default_jsonb(conn, globally=False, loads=JsonText)


def exact_read(conn, table_sql: str, order_by, max_rows: int, max_transfer: int, cancelled,
               fetch_rows: int | None = None) -> tuple[list, list]:
    """One exact read on a fresh connection (C8.3; r1 F1, F7): ``(column names, typed rows)``.

    - json/jsonb arrive as raw text (``register_exact_json``) on a driver connection.
    - One REPEATABLE READ, READ ONLY snapshot. The server first measures the selected rows (count, total and
      widest row text); over ``max_rows`` or ``max_transfer`` is ``ReadLimitExceeded`` with no row sent.
    - Then a server-side cursor fetches batches sized so the widest row cannot overshoot the budget, and
      counts the server-measured bytes of every row it pulls.
    - ``cancelled()`` is checked before each statement, so nothing more is sent once the probe expired."""
    if _driver_connection(conn):
        register_exact_json(conn)
    conn.autocommit = False
    row_text = "octet_length(ROW(_slt_r.*)::text)"

    def stop():
        if cancelled():
            raise ProbeCancelled("the exact read was cancelled; no further statement sent")

    cur = conn.cursor()
    try:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        stop()
        cur.execute(f"SELECT count(*), coalesce(sum({row_text}), 0), coalesce(max({row_text}), 0) "
                    f"FROM (SELECT * FROM {table_sql} LIMIT %s) _slt_r", (max_rows + 1,))
        count, total, widest = (int(v) for v in cur.fetchone())
    finally:
        cur.close()
    if count > max_rows:
        raise ReadLimitExceeded(f"{table_sql} has more than {max_rows} rows")
    if total + count > max_transfer:
        raise ReadLimitExceeded(f"{table_sql} holds {total + count} bytes of row text (widest row {widest}), over "
                                f"the {max_transfer}-byte read budget; no row was transferred")
    stop()
    sql = f"SELECT {row_text}, _slt_r.* FROM {table_sql} _slt_r"
    if order_by:
        sql += " ORDER BY " + ", ".join(f'_slt_r."{c}"' for c in order_by)
    step = max(1, min(fetch_rows or READ_FETCH_ROWS, max_transfer // (widest + 1)))
    names, rows, pulled = None, [], 0
    named = conn.cursor(name=_EXACT_CURSOR)
    try:
        named.execute(sql + " LIMIT %s", (max_rows + 1,))
        while True:
            stop()
            batch = named.fetchmany(step)
            if names is None and named.description:
                names = [d[0] for d in named.description][1:]
            for row in batch:
                pulled += int(row[0]) + 1
                rows.append(tuple(row[1:]))
            if pulled > max_transfer or len(rows) > max_rows:       # the snapshot makes this unreachable
                raise ReadLimitExceeded(f"{table_sql} returned more than its measured {total + count} bytes / "
                                        f"{count} rows; the read stopped at {pulled} bytes")
            if len(batch) < step:
                break
    finally:
        named.close()
    return names or [], rows


class PgProbe:
    """One bounded, cancellable Postgres connection per probe. On expiry: ``cancel()`` then
    ``close()``; the probe is spent and never reused."""

    def __init__(self, admin, which: str | None = None, connect=None):
        dsn = admin.dsn
        which = which or getattr(admin, "role", "source")
        self.params = {"host": dsn["host"], "port": int(dsn["port"]), "dbname": dsn["dbname"],
                       "user": dsn.get(f"{which}_user", dsn.get("source_user")),
                       "password": dsn.get(f"{which}_password", dsn.get("source_password"))}
        self._connect = connect or _pg_connect
        self.spent = False

    def _call(self, sql: str, params, remaining: float, fetch: bool, read=None):
        """The statement runs in a probe thread joined for ``remaining``. The thread takes the lock
        before sending anything: once the caller has marked the probe cancelled, a connection that
        opens late is closed without executing. A statement already sent is cancelled, the
        connection closed and the thread joined, each within ``CANCEL_BOUND_S``; if the thread is
        still inside the statement, or the statement completed after the deadline, the result is
        ``ProbeCancelFailed``, never ``ProbeCancelled``. ``read(conn, cancelled)`` replaces the single
        statement with its own statements (the exact read); it counts as executed only once it returned."""
        if self.spent:
            raise ProbeSpent("a probe is never reused after a timeout")
        remaining = max(0.05, float(remaining))
        lock = threading.Lock()
        box = {"cancelled": False, "sent": False}
        out = {}

        def work():
            conn = self._connect(**self.params, connect_timeout=max(1, int(min(remaining, 10.0))),
                                 options=f"-c statement_timeout={max(1, int(remaining * 1000))}")
            with lock:
                if box["cancelled"]:
                    conn.close()        # opened after the deadline: nothing is ever sent
                    return None
                box["conn"], box["sent"] = conn, True
            try:
                if read is not None:    # Bounded reads (C8.3): the exact read's bounded statements
                    value = read(conn, lambda: box["cancelled"])
                    box["executed"] = True
                    return value
                conn.autocommit = True
                cur = conn.cursor()
                try:
                    if params is None:
                        cur.execute(sql)
                    else:
                        cur.execute(sql, params)
                    box["executed"] = True
                    return cur.fetchall() if fetch and cur.description else []
                finally:
                    cur.close()
            finally:
                conn.close()

        def run():
            try:
                out["value"] = work()
            except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
                out["error"] = e

        t = threading.Thread(target=run, daemon=True, name="slt-lifecycle-probe")
        t.start()
        t.join(remaining)
        if not t.is_alive():
            if "error" in out:
                raise out["error"]
            return out.get("value")
        self.spent = True
        with lock:
            box["cancelled"] = True
            conn = box.get("conn") if box["sent"] else None
        if conn is None:
            raise ProbeCancelled(f"no connection within {remaining:.1f}s; cancelled before any statement "
                                 f"was sent") from None
        problems = []
        try:
            bounded(conn.cancel, CANCEL_BOUND_S)
        except ProbeHung:
            problems.append(f"cancel() did not return within {CANCEL_BOUND_S:.1f}s")
        except Exception as e:          # noqa: BLE001
            problems.append(f"cancel() failed: {e}")
        if not problems:
            t.join(CANCEL_BOUND_S)
        if t.is_alive():
            try:
                bounded(conn.close, CANCEL_BOUND_S)
            except Exception:           # noqa: BLE001 - the thread state below decides the outcome
                pass
            t.join(CANCEL_BOUND_S)
        if t.is_alive():
            problems.append(f"the statement was still running {CANCEL_BOUND_S:.1f}s after cancellation")
        elif box.get("executed"):
            problems.append("the statement completed after the deadline")
        if problems:
            raise ProbeCancelFailed(f"statement did not finish within {remaining:.1f}s and " +
                                    "; ".join(problems)) from None
        raise ProbeCancelled(f"statement cancelled after {remaining:.1f}s") from None

    def query(self, sql: str, params=None, remaining: float = 10.0) -> list:
        return self._call(sql, params, remaining, fetch=True)

    def read_exact(self, table_sql: str, order_by, max_rows: int, max_transfer: int,
                   remaining: float = 10.0) -> tuple[list, list]:
        """``(column names, rows)`` of one ``exact_read``; the same cancel protocol as ``query``."""
        return self._call(None, None, remaining, fetch=True,
                          read=lambda conn, cancelled: exact_read(conn, table_sql, order_by, max_rows,
                                                                  max_transfer, cancelled))

    def run(self, sql: str, remaining: float = 10.0) -> None:
        self._call(sql, None, remaining, fetch=False)


def make_probe(admin) -> PgProbe:
    return PgProbe(admin)


def _admin(admins: dict, route: str):
    entry = admins.get(route)
    if entry is None:
        raise LifecycleError(f"lifecycle route {route!r} has no admin (add its service to requires)")
    return entry["admin"]


def _table(rendered: str) -> str:
    m = _TABLE.match(rendered)
    if not m:
        raise LifecycleError(f"lifecycle table must render to schema.table, got {rendered!r}")
    return f'"{m.group(1)}"."{m.group(2)}"'


def _render(text: str, tokens: dict) -> str:
    from livetest.substitute import render
    return render(text, tokens)


def _lines(text: str) -> int:
    return sum(1 for line in (text or "").splitlines() if line.strip())


def timed_run(remaining: float):
    """A ``run`` callable for server file reads: every command it starts is killed when the remaining
    time is spent (``subprocess.run`` terminates the child on timeout), so no reader outlives the wait."""
    end = time.monotonic() + max(0.05, float(remaining))

    def run(argv):
        left = end - time.monotonic()
        if left <= 0:
            raise ProbeHung("file read did not finish within the deadline")
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=left)
        except subprocess.TimeoutExpired:
            raise ProbeHung(f"file read killed after {remaining:.1f}s") from None
    return run


def _read_lines(read_files, path: str, remaining: float) -> int:
    return _lines(read_files(path, run=timed_run(remaining)))


def check_witness_refs(spec: LifecycleSpec, tokens: dict, owned) -> str | None:
    """Every table and file a witness observes must be a confirmed resource of this attempt, on the
    route the witness reads (``owned(kind, db, name)``, the ownership ledger), and a delivery witness
    must observe a target that is not its own source. Returns the refusal reason, or None."""
    refs = []
    r, c = spec.readiness, spec.completion
    if r["kind"] == "baseline-landed":
        refs.append(("readiness.source", r["source"]))
        if spec.sink == "db":
            refs.append(("readiness.target", r["target"]))
    if c["kind"] == "source-count":
        refs += [("completion.source", c["source"]), ("completion.target", c["target"])]
    elif c["kind"] == "row-count":
        refs.append(("completion.table", {"db": c["db"], "table": c["table"]}))
    if spec.sentinel is not None:
        refs.append(("sentinel.observe", spec.sentinel["observe"]))
    names = {}
    for what, ref in refs:
        rendered = _render(ref["table"], tokens)
        m = _TABLE.match(rendered)
        if not m:
            return f"witness-unresolvable: {what} {rendered!r} does not render to schema.table"
        name = f"{m.group(1)}.{m.group(2)}".lower()
        names[what] = name
        if not owned("pg-table", ref["db"], name):
            return (f"witness-not-owned: {what} {ref['db']}:{name} is not a confirmed resource of this "
                    f"attempt; a witness never observes a table this run did not create")
    for side in ("readiness", "completion"):
        if f"{side}.source" in names and f"{side}.target" in names \
                and names[f"{side}.source"] == names[f"{side}.target"]:
            return (f"witness-self-observation: {side}.target is the source table {names[side + '.source']}; "
                    f"it cannot observe downstream delivery")
    paths = []
    if r["kind"] == "baseline-landed" and spec.sink == "file":
        paths.append(("readiness.path", r["path"]))
    if c["kind"] == "file-lines":
        paths.append(("completion.path", c["path"]))
    for what, raw in paths:
        rendered = _render(raw, tokens)
        if posixpath.normpath(rendered) != rendered or not owned("owned-file", None, rendered):
            return (f"witness-not-owned: {what} {rendered!r} is not inside a confirmed owned directory of "
                    f"this attempt")
    return None


def _refuse_refs(spec, tokens, owned, kind: str, clock) -> None:
    why = check_witness_refs(spec, tokens, owned)
    if why:
        watch = Watch(kind, "witness references are run-owned", Deadline(0, clock))
        watch.observe({"error": why})
        raise LifecycleError(f"lifecycle {why}", watch.finish(why.split(":", 1)[0]))


# ---------------------------------------------------------------------------------------------
# Waits
# ---------------------------------------------------------------------------------------------

def _poll_s(deadline: Deadline) -> float:
    return max(0.05, min(1.0, deadline.seconds / 20.0))


def _wait(watch: Watch, observe, satisfied, terminal=None) -> tuple[str, object]:
    deadline, clock = watch.deadline, watch.clock
    longest, last = 0.0, None           # the longest good read so far, and the last value it returned
    while True:
        try:
            left = deadline.remaining()     # the time this pass's reads start with
            if terminal is not None:
                st = terminal(left)
                if st:
                    watch.observe({"status": st})
                    return f"terminal-status:{st}", None
            began = clock.monotonic()
            value = observe(deadline.remaining())
            longest = max(longest, clock.monotonic() - began)
        except (ProbeCancelled, ProbeCancelFailed, ProbeHung, ProbeSpent) as e:
            watch.observe({"error": str(e)})
            if isinstance(e, ProbeHung) and deadline.expired() and last is not None \
                    and left <= max(_poll_s(deadline), 2 * longest):
                # a read that started with no more time left than a poll or a normal read takes was
                # only cut off by the deadline; one that ran for longer hung, and stays probe-hung
                return "deadline", last
            return e.reason, None
        except LifecycleError:
            raise
        except Exception as e:          # noqa: BLE001 - an unreadable observation is recorded, not fatal
            watch.observe({"error": f"{type(e).__name__}: {e}"})
            value = None
        else:
            watch.observe(value)
            last = value
            if value is not None and satisfied(value):
                return "satisfied", value
        if deadline.expired():
            return "deadline", value
        clock.sleep(min(_poll_s(deadline), deadline.remaining()))


def _hold(spec: LifecycleSpec, clock, observe, satisfied) -> tuple[dict, bool]:
    """Stability: the satisfied condition must still hold after ``spec.stability_s``."""
    started = clock.monotonic()
    if spec.stability_s > 0:
        clock.sleep(spec.stability_s)
    try:
        value = observe(max(1.0, spec.stability_s))
    except Exception as e:              # noqa: BLE001
        value = {"error": str(e)}
    ok = not (isinstance(value, dict) and "error" in value) and satisfied(value)
    return {"seconds": spec.stability_s, "measuredS": round(clock.monotonic() - started, 3),
            "heldAt": clock.wall(), "value": value, "held": ok}, ok


def _statuses(client, apps, remaining: float) -> dict:
    return {a: bounded(lambda a=a: status_bounded(client, a, remaining), remaining) for a in apps}


def _terminal_of(client, apps):
    def check(remaining):
        for a, st in _statuses(client, apps, max(0.05, remaining)).items():
            if st in TERMINAL_STATUSES:
                return f"{st} ({a})"
        return None
    return check


def await_running_bounded(client, apps, deadline: Deadline) -> dict:
    """Every app RUNNING, bounded (replaces the unbounded smoke/RUNNING waits for lifecycle cases)."""
    watch = Watch("app-running", f"RUNNING: {', '.join(apps)}", deadline)
    terminal = {}

    def observe(remaining):
        sts = _statuses(client, apps, max(0.05, remaining))
        for a, st in sts.items():
            if st in TERMINAL_STATUSES:
                terminal["st"] = f"{st} ({a})"
        return sts

    def satisfied(sts):
        return "st" in terminal or all(v == "RUNNING" for v in sts.values())

    reason, _ = _wait(watch, observe, satisfied)
    if reason == "satisfied" and "st" in terminal:
        reason = f"terminal-status:{terminal['st']}"
    return watch.finish(reason, witness=f"RUNNING observed for {', '.join(apps)}")


def _count_observer(admins, ref, tokens):
    table = _table(_render(ref["table"], tokens))
    probe = make_probe(_admin(admins, ref["db"]))

    def observe(remaining):
        return int(probe.query(f"SELECT count(*) FROM {table}", None, remaining)[0][0])
    return observe, table


def record_baseline(state: State, spec: LifecycleSpec, admins: dict, tokens: dict, clock=None, *, owned) -> dict | None:
    """After the pre-deploy seed: the positive source baseline (baseline-landed readiness and
    source-count completion). A zero baseline fails at once with reason ``zero-count``."""
    if spec.readiness["kind"] == "baseline-landed":
        ref = spec.readiness["source"]
    elif spec.completion["kind"] == "source-count":
        ref = spec.completion["source"]
    else:
        return None
    clock = clock or Clock
    try:
        _refuse_refs(spec, tokens, owned, "baseline", clock)
    except LifecycleError as e:
        state.ready = e.record
        raise
    deadline = Deadline(min(spec.readiness_s, 30.0), clock)
    watch = Watch("baseline", f"source count of {ref['table']} after the seed", deadline)
    observe, table = _count_observer(admins, ref, tokens)
    reason, count = _wait(watch, observe, lambda n: True)
    state.baseline = {"db": ref["db"], "table": table, "count": count, "at": clock.wall()}
    if reason == "satisfied" and count <= 0:
        reason = "zero-count"
    if reason != "satisfied":
        state.ready = watch.finish(reason)
        raise LifecycleError(f"lifecycle baseline {reason}: source {table} count is {count!r}; zero is "
                             f"never a readiness or completion witness", state.ready)
    return state.baseline


# ---------------------------------------------------------------------------------------------
# Sentinel (C7.2)
# ---------------------------------------------------------------------------------------------

def new_sentinel_id() -> int:
    return 1 + secrets.randbelow(SENTINEL_ID_MAX)


class Sentinel:
    """insert -> observe present -> delete -> observe absent, for one phase of one attempt."""

    def __init__(self, spec: LifecycleSpec, admins: dict, tokens: dict, ident, phase: str,
                 ids: dict, source_sql, id_source=None, record=None):
        self.spec, self.admins, self.phase, self.ids = spec, admins, phase, ids
        self.record = record        # inputs.Snapshot.record for the sentinel SQL as sent
        self.id = (id_source or new_sentinel_id)()
        ids[phase] = self.id
        per_test = getattr(ident, "per_test", "t000000000")
        attempt = getattr(ident, "attempt", "00000000")
        self.tag = f"slt-{per_test}-{attempt}-{phase}"
        self.tokens = {**tokens, "SENTINEL_ID": str(self.id), "SENTINEL_TAG": self.tag,
                       "SENTINEL_READY_ID": str(ids.get("ready", "")),
                       "SENTINEL_DONE_ID": str(ids.get("done", ""))}
        self.source_sql = source_sql

    def run(self, watch: Watch, terminal=None) -> str:
        s = self.spec.sentinel
        observe_ref = s["observe"]
        table = _table(_render(observe_ref["table"], self.tokens))
        key = observe_ref["key"]
        writer = make_probe(_admin(self.admins, s["db"]))
        reader = make_probe(_admin(self.admins, observe_ref["db"]))
        sql = f"SELECT count(*) FROM {table} WHERE CAST({key} AS text) = %s"

        def count(remaining):
            return int(reader.query(sql, (str(self.id),), remaining)[0][0])
        self.count = count

        for step, file_key, want in (("present", "insert", lambda n: n > 0), ("absent", "delete", lambda n: n == 0)):
            try:
                template = self.source_sql(s[file_key])
                sent = _render(template, self.tokens)      # not `sql`: count() above reads that name at call time
                writer.run(sent, watch.deadline.remaining())
                if self.record is not None:
                    self.record("sentinel", s[file_key], sent, template=template)
            except (ProbeCancelled, ProbeCancelFailed, ProbeHung, ProbeSpent) as e:
                watch.observe({"step": file_key, "error": str(e)})
                return e.reason
            except Exception as e:      # noqa: BLE001
                watch.observe({"step": file_key, "error": f"{type(e).__name__}: {e}"})
                return f"error:sentinel {file_key} failed: {e}"
            watch.observe({"step": file_key, "id": self.id})
            reason, _ = _wait(watch, lambda r: {"step": step, "count": count(r)}, lambda v: want(v["count"]),
                              terminal)
            if reason != "satisfied":
                return reason
        return "satisfied"


# ---------------------------------------------------------------------------------------------
# Readiness and completion (called by the plugin hooks H3/H4)
# ---------------------------------------------------------------------------------------------

def _source_sql_reader(ctx_dir):
    from pathlib import Path

    def read(name):
        return (Path(ctx_dir) / name).read_text()
    return read


def smoke_and_ready(state: State, spec: LifecycleSpec, *, client, apps, admins, tokens, ident,
                    read_files, owned, source_dir=None, clock=None, id_source=None) -> list[dict]:
    """H3: bounded RUNNING for every app (the smoke record), then the declared capture readiness and
    its stability. Returns the smoke assertion record; raises LifecycleError on any failure."""
    from livetest.resultschema import build_assertion_result
    clock = clock or Clock
    try:
        _refuse_refs(spec, tokens, owned, spec.readiness["kind"], clock)
    except LifecycleError as e:
        state.ready = e.record
        raise
    deadline = Deadline(spec.readiness_s, clock)
    state.running = await_running_bounded(client, apps, deadline)
    if state.running["reason"] != "satisfied":
        state.ready = {**state.running, "kind": spec.readiness["kind"]}
        rec = build_assertion_result(type="smoke", status="failed", spec={}, target=None, db=None,
                                     detail=f"lifecycle RUNNING {state.running['reason']}")
        raise LifecycleError(f"lifecycle readiness failed: {state.running['reason']} before RUNNING",
                             state.ready, [rec])
    smoke = build_assertion_result(type="smoke", status="passed", spec={}, target=None, db=None,
                                   detail=f"RUNNING (bounded): {', '.join(apps)}")
    kind = spec.readiness["kind"]
    terminal = _terminal_of(client, apps)
    if kind == "baseline-landed":
        if state.baseline is None:
            raise LifecycleError("lifecycle readiness baseline-landed needs the recorded baseline (H6b)",
                                 None, [smoke])
        want = state.baseline["count"]
        if spec.sink == "db":
            observe, table = _count_observer(admins, spec.readiness["target"], tokens)
            condition = f"RUNNING and {table} count == source baseline {want}"
        else:
            path = _render(spec.readiness["path"], tokens)

            def observe(remaining):
                return _read_lines(read_files, path, remaining)
            condition = f"RUNNING and {path} lines == source baseline {want}"
        watch = Watch(kind, condition, deadline)
        reason, value = _wait(watch, observe, lambda v: v == want, terminal)
        witness = f"sink observed the positive source baseline {want}"
        if reason == "satisfied":
            state.stability, held = _hold(spec, clock, observe, lambda v: v == want)
            reason = reason if held else "stability-lost"
    elif kind == "source-progress":
        probe = make_probe(_admin(admins, spec.readiness["db"]))
        slot = tokens.get("PG_SLOT", "")

        def observe(remaining):
            rows = probe.query("SELECT active FROM pg_replication_slots WHERE slot_name = %s", (slot,), remaining)
            return bool(rows[0][0]) if rows else "no-slot"

        watch = Watch(kind, f"pg_replication_slots.active for {slot}", deadline)
        reason, value = _wait(watch, observe, lambda v: v is True, terminal)
        witness = f"replication slot {slot} active"
        if reason == "satisfied":
            state.stability, held = _hold(spec, clock, observe, lambda v: v is True)
            reason = reason if held else "stability-lost"
    else:
        watch = Watch(kind, "ready sentinel inserted, observed present, deleted, observed absent", deadline)
        ids = getattr(state, "sentinel_ids", None) or {}
        state.sentinel_ids = ids
        sentinel = Sentinel(spec, admins, tokens, ident, "ready", ids,
                            _source_sql_reader(source_dir or "."), id_source, record=getattr(state, "record_input", None))
        reason = sentinel.run(watch, terminal)
        witness = f"ready sentinel {sentinel.id} observed present then absent"
        if reason == "satisfied":
            state.stability, held = _hold(spec, clock, sentinel.count, lambda n: n == 0)
            reason = reason if held else "stability-lost"
    state.ready = watch.finish(reason, witness)
    if reason != "satisfied":
        raise LifecycleError(f"lifecycle readiness {kind} failed: {reason}", state.ready, [smoke])
    return [smoke]


def complete(state: State, spec: LifecycleSpec, *, client, apps, admins, tokens, ident, read_files, owned,
             source_dir=None, clock=None, id_source=None) -> dict:
    """H4: positive completion and its stability, before any assertion. Raises LifecycleError."""
    clock = clock or Clock
    try:
        _refuse_refs(spec, tokens, owned, spec.completion["kind"], clock)
    except LifecycleError as e:
        state.completion = e.record
        raise
    deadline = Deadline(spec.completion_s, clock)
    kind = spec.completion["kind"]
    terminal = _terminal_of(client, apps)
    if kind == "source-count":
        src_obs, src = _count_observer(admins, spec.completion["source"], tokens)
        tgt_obs, tgt = _count_observer(admins, spec.completion["target"], tokens)

        def observe(remaining):
            return {"source": src_obs(remaining), "target": tgt_obs(remaining)}

        def satisfied(v):
            return v["source"] > 0 and v["source"] == v["target"]

        watch = Watch(kind, f"{tgt} count == {src} count, both > 0", deadline)
        reason, value = _wait(watch, lambda r: observe(r), lambda v: satisfied(v) or v["source"] == 0, terminal)
        if reason == "satisfied" and value["source"] == 0:
            reason = "zero-count"
        witness = f"target count equals source count {value['source'] if value else None}"
    elif kind == "row-count":
        observe, table = _count_observer(admins, {"db": spec.completion["db"], "table": spec.completion["table"]}, tokens)
        want = spec.completion["expect"]
        satisfied = lambda v: v == want          # noqa: E731
        watch = Watch(kind, f"{table} count == {want}", deadline)
        reason, value = _wait(watch, observe, satisfied, terminal)
        witness = f"{table} reached {want} rows"
    elif kind == "file-lines":
        path = _render(spec.completion["path"], tokens)
        want = spec.completion["lines"]

        def observe(remaining):
            return _read_lines(read_files, path, remaining)
        satisfied = lambda v: v == want          # noqa: E731
        watch = Watch(kind, f"{path} lines == {want}", deadline)
        reason, value = _wait(watch, observe, satisfied, terminal)
        witness = f"{path} reached {want} lines"
    else:
        watch = Watch(kind, "done sentinel inserted after the last ordered change, observed present, "
                            "deleted, observed absent", deadline)
        ids = getattr(state, "sentinel_ids", None) or {}
        state.sentinel_ids = ids
        sentinel = Sentinel(spec, admins, tokens, ident, "done", ids, _source_sql_reader(source_dir or "."),
                            id_source, record=getattr(state, "record_input", None))
        reason = sentinel.run(watch, terminal)
        witness = f"done sentinel {sentinel.id} observed present then absent"
        # stability re-reads the done sentinel's own id after the interval: it must still be absent
        observe, satisfied = sentinel.count, (lambda n: n == 0)
    if reason == "satisfied":
        state.stability, held = _hold(spec, clock, observe, satisfied)
        reason = reason if held else "stability-lost"
    state.completion = watch.finish(reason, witness)
    if reason != "satisfied":
        raise LifecycleError(f"lifecycle completion {kind} failed: {reason}", state.completion)
    return state.completion
