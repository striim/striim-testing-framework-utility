"""Bounded, typed progress barriers for recovery interruptions."""
from __future__ import annotations

import math
import re
import time
from decimal import Decimal

from livetest.recovery import RecoveryError


class ProgressError(RecoveryError):
    pass


_FIELDS = {
    "target-count": {"db", "table", "above", "below"},
    "checkpoint": {"field", "above"},
    "kafka-end": {"topic", "partition", "above"},
    "kafka-consumer": {"topic", "partition", "group", "above"},
    "postgres-slot": {"db", "slot", "above"},
}
_DBS = {"postgres-source", "postgres-target", "spanner-google", "spanner-postgres"}


def lsn(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}", value):
        raise ValueError("invalid Postgres LSN")
    high, low = value.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value < 0 or value != int(value):
        raise ValueError("expected a nonnegative integer position")
    return int(value)


def normalize(raw):
    from livetest.manifest import _parse_zeroable_duration
    try:
        if not isinstance(raw, dict) or raw.get("kind") not in _FIELDS:
            raise ValueError("unknown trigger kind")
        kind = raw["kind"]
        if set(raw) - (_FIELDS[kind] | {"kind", "timeout", "poll"}):
            raise ValueError("unknown trigger key")
        s = dict(raw)
        for key, default in (("timeout", "60s"), ("poll", "100ms")):
            s[key] = float(_parse_zeroable_duration(raw.get(key, default), key))
            if not math.isfinite(s[key]) or s[key] <= 0:
                raise ValueError(f"{key} must be positive and finite")
        required = _FIELDS[kind] - ({"field", "above"} if kind == "checkpoint" else set())
        if required - set(raw):
            raise ValueError(f"missing {sorted(required - set(raw))}")
        for key in ("table", "topic", "slot", "group", "field"):
            if key in s and (not isinstance(s[key], str) or not s[key].strip()):
                raise ValueError(f"{key} must be a nonempty name")
        if "db" in s and s["db"] not in (_DBS if kind == "target-count" else {"postgres-source", "postgres-target"}):
            raise ValueError("unsupported database route")
        if kind == "checkpoint":
            s.setdefault("field", "serialNo")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", s["field"]):
                raise ValueError("field must be one checkpoint column")
        for key in ("above", "below", "partition"):
            if key in s:
                s[key] = lsn(s[key]) if kind == "postgres-slot" and key == "above" else _integer(s[key])
        if kind == "target-count" and s["below"] <= s["above"] + 1:
            raise ValueError("open count window must contain an integer")
        return s
    except Exception as e:
        raise ValueError(f"invalid-progress-trigger: {e}") from e


def checkpoint_position(rows, field):
    if not isinstance(rows, list):
        raise ValueError("invalid checkpoint history")
    try:
        return max((_integer(row[field]) for row in rows), default=0)
    except (KeyError, TypeError) as e:
        raise ValueError("checkpoint position field unavailable") from e


def kafka_position(admin, s, remaining):
    from confluent_kafka import TopicPartition
    c = admin._consumer(s.get("group", "slt-progress-probe"))
    try:
        tp = TopicPartition(s["topic"], s["partition"])
        if s["kind"] == "kafka-end":
            return _integer(c.get_watermark_offsets(tp, timeout=remaining, cached=False)[1])
        result = c.committed([tp], timeout=remaining)
        if len(result) != 1 or result[0].error:
            raise ValueError("consumer position unavailable")
        # A never-committed consumer has no passed position; it is not zero progress.
        return 0 if result[0].offset < 0 else _integer(result[0].offset)
    finally:
        c.close()


def reader(s, *, admins, tokens, client, app, owned):
    from livetest.substitute import render
    from livetest.lifecycle import PgProbe, _table
    kind = s["kind"]
    if kind == "checkpoint":
        return lambda left: checkpoint_position(client.checkpoint_history(app, timeout=left), s["field"])
    name_key = "table" if kind == "target-count" else "slot" if kind == "postgres-slot" else "topic"
    name = render(s[name_key], tokens)
    db = s.get("db", "kafka")
    resource = "pg-table" if db.startswith("postgres") and kind == "target-count" else (
        "spanner-table" if kind == "target-count" else "pg-slot" if kind == "postgres-slot" else "topic")
    if not owned(resource, db, name):
        raise ProgressError(f"progress-target-not-owned: {name}")
    admin = admins[db]["admin"]
    if kind.startswith("kafka"):
        from livetest.kafkaadmin import _TOPIC
        if not _TOPIC.fullmatch(name): raise ProgressError("invalid-progress-target")
        return lambda left: kafka_position(admin, {**s, "topic": name, "group": render(s.get("group", "slt-progress-probe"), tokens)}, left)
    if kind == "postgres-slot":
        if not re.fullmatch(r"[a-z0-9_]+", name): raise ProgressError("invalid-progress-target")
        def position(left):
            rows = PgProbe(admin).query("SELECT confirmed_flush_lsn::text FROM pg_replication_slots WHERE slot_name = %s", (name,), left)
            if len(rows) != 1 or rows[0][0] is None: raise ValueError("slot position unavailable")
            return lsn(rows[0][0])
        return position
    if db.startswith("postgres"):
        sql = "SELECT count(*) FROM " + _table(name)
        return lambda left: _integer(PgProbe(admin).query(sql, remaining=left)[0][0])
    from livetest.spanneradmin import _check_table
    name = _check_table(name)
    def count(left):
        with admin._db().snapshot() as snapshot:
            rows = list(snapshot.execute_sql(f"SELECT count(*) FROM {name}", timeout=left))
        return _integer(rows[0][0])
    return count


def held(s, value, baseline=0):
    value = _integer(value)
    return value > s.get("above", baseline) and ("below" not in s or value < s["below"])


def wait(s, read, *, baseline=0, now=time.monotonic, sleep=time.sleep, status=None):
    started = now(); deadline = started + s["timeout"]
    last = None
    while now() < deadline:
        if status is not None and status() != "RUNNING":
            raise ProgressError("progress-app-not-running")
        if now() >= deadline:
            break
        try:
            last = _integer(read(deadline - now()))
        except Exception as e:
            raise ProgressError(f"progress-read-error: {e}") from e
        at = now()
        if at > deadline:
            break
        if held(s, last, baseline):
            return {"value": last, "held": True, "startedAt": started, "observedAt": at,
                    "deadline": deadline, "baseline": baseline}
        if "below" in s and last >= s["below"]:
            raise ProgressError(f"progress-window-missed: observed {last}")
        sleep(max(0, min(s["poll"], deadline - now())))
    raise ProgressError(f"progress-deadline: last observed {last}")


def after_interrupt(s, read, *, now=time.monotonic):
    """Read once after the interruption takes effect under a fresh bounded budget."""
    from datetime import datetime, timezone
    budget = min(5.0, s['timeout'])
    deadline = now() + budget
    observation = {'value': None, 'observedAt': None, 'observedUtc': None}
    try:
        observation['value'] = _integer(read(budget))
        observation['observedAt'] = now()
        observation['observedUtc'] = datetime.now(timezone.utc).isoformat()
        if now() > deadline:
            raise ValueError('post-interruption progress read exceeded its deadline')
        if s['kind'] == 'target-count' and observation['value'] >= s['below']:
            raise ValueError('work finished before interruption took effect')
    except Exception as e:
        if s['kind'] != 'target-count':
            # A whole-cluster kill removes the checkpoint API. Only the count
            # window uses this read as proof that work was still in flight.
            from livetest.striim import redact
            text = redact(str(e))
            observation.update(error=text[:1024], errorTruncated=len(text) > 1024,
                observedAt=now(), observedUtc=datetime.now(timezone.utc).isoformat())
            return observation
        error = ProgressError(f'fault-missed: {e}')
        error.after_interrupt = observation
        raise error from e
    return observation


def at_fault(s, read, interrupt, *, record, cycle, baseline=0,
             now=time.monotonic, sleep=time.sleep, status=None, after_read=None):
    """Re-read at the command boundary; retain evidence even if no command is sent.

    This is a bounded observation immediately before the command, not an atomic
    transaction with the product. No retry of a state-changing command is made.
    Timed recovery (s=None) verifies RUNNING after its existing delay.
    """
    from datetime import datetime, timezone
    from livetest.resultschema import build_assertion_result
    observation = {"cycle": cycle, "value": None, "held": False,
                   "observedAt": None, "issuedAt": None, "interrupted": False,
                   "baseline": baseline, "deadline": now() + s["timeout"] if s else None,
                   "windowOpened": None, "afterInterrupt": None}
    def observed(left):
        value = read(left)
        observation.update(value=value, observedAt=now(),
            observedUtc=datetime.now(timezone.utc).isoformat())
        return value
    def save(ok, detail):
        record(build_assertion_result(type="recovery", status="passed" if ok else "failed",
            spec={"trigger": s or {"kind": "timed", "status": "RUNNING"}},
            target=(s or {}).get("table", (s or {}).get("topic", (s or {}).get("slot", "application"))),
            db=(s or {}).get("db"), detail=detail,
            expected={"kind": "rows", "rows": [{"held": True, "interrupted": True}]},
            actual={"kind": "rows", "rows": [dict(observation)]}))
    try:
        if s is None:
            value = observed(5.0)
            observation["held"] = value == "RUNNING"
        else:
            found = wait(s, observed, baseline=baseline, now=now, sleep=sleep, status=status)
            observation["windowOpened"] = {"value": found["value"], "observedAt": found["observedAt"],
                "observedUtc": observation["observedUtc"]}
            observation["deadline"] = found["deadline"]
            left = found["deadline"] - now()
            if left <= 0:
                raise ProgressError("deadline before fault recheck")
            if status is not None and status() != "RUNNING":
                raise ProgressError("app left RUNNING before fault")
            left = found["deadline"] - now()
            if left <= 0:
                raise ProgressError("deadline before fault recheck")
            value = observed(left)
            observation["held"] = now() <= found["deadline"] and held(s, value, baseline)
        if not observation["held"]:
            raise ProgressError("precondition did not hold at the fault boundary")
    except Exception as e:
        save(False, f"fault-missed: {e}")
        raise ProgressError(f"fault-missed: {e}") from e
    observation["issuedAt"] = now()
    try:
        interrupt()
    except Exception as e:
        save(False, f"fault-interrupt-error: {e}")
        raise
    observation["interrupted"] = True
    if s is not None:
        try:
            observation["afterInterrupt"] = after_interrupt(s, after_read or read, now=now)
        except ProgressError as e:
            observation["afterInterrupt"] = e.after_interrupt
            save(False, str(e))
            raise
    save(True, "ok")
    return observation
