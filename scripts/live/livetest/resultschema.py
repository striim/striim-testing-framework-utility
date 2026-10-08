from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Shared contract for the live-test-framework structured JSON sidecar (`.slt.json`),
# schema_version 1. Both `scripts/live/livetest` (the writer, via the pytest plugin) and
# the console (the reader, via `results.py`) import this module so the two sides can
# never silently drift on field names/enums. The dataclasses and validation below
# define the serialized contract.

SCHEMA_VERSION = 1

VALID_TEST_STATUSES = {"passed", "failed", "skipped", "error"}
VALID_ASSERTION_STATUSES = {"passed", "failed"}
VALID_ASSERTION_TYPES = {
    "smoke", "data", "diff", "file", "gcs", "json", "halt", "monitor", "checkpoint_history",
    "jmx"}
# The spec's worked example only shows single|cluster, but `manifest.py`'s
# VALID_TOPOLOGIES also allows "agent" (single-node-with-agent deploys), and this field
# is meant to carry the EFFECTIVE topology the framework actually ran under (see spec §3
# "Note on topology") — so it must accept whatever the manifest/framework can produce,
# not just the two topologies the example happened to illustrate.
VALID_TOPOLOGIES = {"single", "agent", "cluster"}
VALID_VALUE_KINDS = {"rows", "count", "bytes"}

_DOC_KEYS = {"schema_version", "tests"}
_TEST_KEYS = {
    "name", "nodeid", "status", "topology", "services", "duration", "skip_reason",
    "assertions",
}
_ASSERTION_KEYS = {"type", "target", "db", "spec", "status", "detail", "expected", "actual"}
# §85.3. OPTIONAL, and declared rather than smuggled: _require_keys refuses any key it has not
# been told about, so an undeclared extra field fails validation at the call site. Absent on every
# assertion type but `diff`, and absent on a diff that never converged.
_ASSERTION_OPTIONAL_KEYS = {"elapsed_s"}
_VALUE_ALLOWED_KEYS = {"kind", "rows", "count", "size", "truncated"}


class SchemaError(ValueError):
    """Raised by validate() on any structural violation of the .slt.json contract."""


# --------------------------------------------------------------------------------------
# Convenience dataclasses mirroring the schema. These are optional typed wrappers for
# code that wants attribute access when *building* a result; validate()/write_sidecar()
# themselves operate on plain dicts (the actual JSON shape read from / written to disk),
# since that is what pytest-plugin collection and the console's JSON parsing both use.
# --------------------------------------------------------------------------------------


@dataclass
class ValueSnapshot:
    """The `expected`/`actual` shape: {kind, rows?, count?, size?, truncated}."""
    kind: str
    rows: list | None = None
    count: int | None = None
    size: int | None = None
    truncated: bool = False

    def to_dict(self) -> dict:
        d: dict = {"kind": self.kind, "truncated": self.truncated}
        if self.rows is not None:
            d["rows"] = self.rows
        if self.count is not None:
            d["count"] = self.count
        if self.size is not None:
            d["size"] = self.size
        return d


@dataclass
class AssertionResult:
    type: str
    status: str
    spec: dict
    target: str | None = None
    db: str | None = None
    detail: str | None = None
    expected: dict | None = None
    actual: dict | None = None
    # §85.3. Seconds from the first poll to convergence, on a `diff` assertion only. None
    # everywhere else, and None on failure -- a run that never converged has no catch-up time,
    # and reporting the timeout as one would make the slower writer look like the faster.
    elapsed_s: float | None = None

    def to_dict(self) -> dict:
        d = {
            "type": self.type,
            "target": self.target,
            "db": self.db,
            "spec": self.spec,
            "status": self.status,
            "detail": self.detail,
            "expected": self.expected,
            "actual": self.actual,
        }
        # ⚠ OMITTED when unset, exactly as build_assertion_result omits it. Emitting it
        # unconditionally as null made every non-diff record fail validation -- 'elapsed_s' is
        # pinned to diff -- and silently broke the byte-identity this field was supposed to
        # preserve. Found in review; the dataclass has no live caller, so nothing failed.
        if self.elapsed_s is not None:
            d["elapsed_s"] = self.elapsed_s
        return d


@dataclass
class TestResult:
    name: str
    nodeid: str
    status: str
    topology: str
    services: list = field(default_factory=list)
    duration: float = 0.0
    skip_reason: str | None = None
    assertions: list = field(default_factory=list)   # list[dict], each an AssertionResult.to_dict()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "nodeid": self.nodeid,
            "status": self.status,
            "topology": self.topology,
            "services": self.services,
            "duration": self.duration,
            "skip_reason": self.skip_reason,
            "assertions": self.assertions,
        }


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _is_number(x) -> bool:
    # bool is a subclass of int in Python; a status/flag masquerading as a duration would
    # be a bug worth catching, not a valid duration.
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _require_dict(obj, ctx: str) -> dict:
    if not isinstance(obj, dict):
        raise SchemaError(f"{ctx}: expected an object, got {type(obj).__name__}")
    return obj


def _require_keys(d: dict, required: set, ctx: str, optional: set = frozenset()) -> None:
    missing = required - d.keys()
    if missing:
        raise SchemaError(f"{ctx}: missing required key(s) {sorted(missing)}")
    extra = d.keys() - required - optional
    if extra:
        raise SchemaError(f"{ctx}: unexpected key(s) {sorted(extra)}")


def _validate_value_snapshot(v, ctx: str) -> None:
    if v is None:
        return
    v = _require_dict(v, ctx)
    extra = v.keys() - _VALUE_ALLOWED_KEYS
    if extra:
        raise SchemaError(f"{ctx}: unexpected key(s) {sorted(extra)}")
    if "kind" not in v:
        raise SchemaError(f"{ctx}: missing required key 'kind'")
    kind = v["kind"]
    if kind not in VALID_VALUE_KINDS:
        raise SchemaError(f"{ctx}: 'kind' must be one of {sorted(VALID_VALUE_KINDS)}, got {kind!r}")
    if "truncated" not in v:
        raise SchemaError(f"{ctx}: missing required key 'truncated'")
    if not isinstance(v["truncated"], bool):
        raise SchemaError(f"{ctx}: 'truncated' must be a bool, got {type(v['truncated']).__name__}")

    if kind == "rows":
        if "rows" not in v:
            raise SchemaError(f"{ctx}: kind 'rows' requires a 'rows' key")
        if not isinstance(v["rows"], list):
            raise SchemaError(f"{ctx}: 'rows' must be a list, got {type(v['rows']).__name__}")
        for i, row in enumerate(v["rows"]):
            if not isinstance(row, dict):
                raise SchemaError(f"{ctx}: rows[{i}] must be an object, got {type(row).__name__}")
        if "count" in v and not _is_number(v["count"]):
            raise SchemaError(f"{ctx}: 'count' must be a number, got {type(v['count']).__name__}")
        if "size" in v:
            raise SchemaError(f"{ctx}: 'size' is not valid alongside kind 'rows'")
    elif kind == "count":
        if "count" not in v:
            raise SchemaError(f"{ctx}: kind 'count' requires a 'count' key")
        if not _is_number(v["count"]):
            raise SchemaError(f"{ctx}: 'count' must be a number, got {type(v['count']).__name__}")
        if "rows" in v or "size" in v:
            raise SchemaError(f"{ctx}: kind 'count' must not carry 'rows'/'size'")
    else:  # "bytes"
        if "size" not in v:
            raise SchemaError(f"{ctx}: kind 'bytes' requires a 'size' key")
        if not _is_number(v["size"]):
            raise SchemaError(f"{ctx}: 'size' must be a number, got {type(v['size']).__name__}")
        if "rows" in v or "count" in v:
            raise SchemaError(f"{ctx}: kind 'bytes' must not carry 'rows'/'count'")


def _validate_assertion(a, ctx: str) -> None:
    a = _require_dict(a, ctx)
    _require_keys(a, _ASSERTION_KEYS, ctx, _ASSERTION_OPTIONAL_KEYS)

    if a["type"] not in VALID_ASSERTION_TYPES:
        raise SchemaError(f"{ctx}: 'type' must be one of {sorted(VALID_ASSERTION_TYPES)}, got {a['type']!r}")
    if a["status"] not in VALID_ASSERTION_STATUSES:
        raise SchemaError(
            f"{ctx}: 'status' must be one of {sorted(VALID_ASSERTION_STATUSES)}, got {a['status']!r}")
    if a["target"] is not None and not isinstance(a["target"], str):
        raise SchemaError(f"{ctx}: 'target' must be a string or null")
    if a["db"] is not None and not isinstance(a["db"], str):
        raise SchemaError(f"{ctx}: 'db' must be a string or null")
    _require_dict(a["spec"], f"{ctx}.spec")
    if a["detail"] is not None and not isinstance(a["detail"], str):
        raise SchemaError(f"{ctx}: 'detail' must be a string or null")
    _validate_value_snapshot(a["expected"], f"{ctx}.expected")
    _validate_value_snapshot(a["actual"], f"{ctx}.actual")
    if "elapsed_s" in a:
        if a["type"] not in ("diff", "data"):
            raise SchemaError(f"{ctx}: 'elapsed_s' is only meaningful on a diff or an exact-rows "
                              f"data assertion, got type {a['type']!r}")
        if not isinstance(a["elapsed_s"], (int, float)) or isinstance(a["elapsed_s"], bool) \
                or a["elapsed_s"] < 0:
            raise SchemaError(f"{ctx}: 'elapsed_s' must be a non-negative number, "
                              f"got {a['elapsed_s']!r}")


def _validate_test(t, ctx: str) -> None:
    t = _require_dict(t, ctx)
    _require_keys(t, _TEST_KEYS, ctx)

    if not isinstance(t["name"], str) or not t["name"]:
        raise SchemaError(f"{ctx}: 'name' must be a non-empty string")
    if not isinstance(t["nodeid"], str) or not t["nodeid"]:
        raise SchemaError(f"{ctx}: 'nodeid' must be a non-empty string")
    if t["status"] not in VALID_TEST_STATUSES:
        raise SchemaError(f"{ctx}: 'status' must be one of {sorted(VALID_TEST_STATUSES)}, got {t['status']!r}")
    if t["topology"] not in VALID_TOPOLOGIES:
        raise SchemaError(f"{ctx}: 'topology' must be one of {sorted(VALID_TOPOLOGIES)}, got {t['topology']!r}")
    if not isinstance(t["services"], list) or not all(isinstance(s, str) for s in t["services"]):
        raise SchemaError(f"{ctx}: 'services' must be a list of strings")
    if not _is_number(t["duration"]) or t["duration"] < 0:
        raise SchemaError(f"{ctx}: 'duration' must be a non-negative number")
    if t["skip_reason"] is not None and not isinstance(t["skip_reason"], str):
        raise SchemaError(f"{ctx}: 'skip_reason' must be a string or null")
    if not isinstance(t["assertions"], list):
        raise SchemaError(f"{ctx}: 'assertions' must be a list")
    for i, a in enumerate(t["assertions"]):
        _validate_assertion(a, f"{ctx}.assertions[{i}]")


def validate(doc: dict) -> None:
    """Raise SchemaError (a ValueError) if `doc` does not conform to the .slt.json
    schema (schema_version 1). Strict about required/unknown top-level shape and enum
    values; not paranoid about things like message wording."""
    doc = _require_dict(doc, "doc")
    _require_keys(doc, _DOC_KEYS, "doc")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(
            f"doc: 'schema_version' must be {SCHEMA_VERSION}, got {doc['schema_version']!r}")
    if not isinstance(doc["tests"], list):
        raise SchemaError("doc: 'tests' must be a list")
    for i, t in enumerate(doc["tests"]):
        _validate_test(t, f"doc.tests[{i}]")


# --------------------------------------------------------------------------------------
# Truncation
# --------------------------------------------------------------------------------------


def truncate(rows: list, max_rows: int = 200, max_bytes: int = 65536) -> tuple[list, bool]:
    """Shorten `rows` to fit within max_rows and (then) a cumulative JSON-encoded byte
    budget. Returns (kept_rows, truncated). A non-empty input always keeps at least one
    row, even if that single row alone exceeds max_bytes -- a byte cap that drops
    everything would be strictly less useful than one oversized-but-present sample row.
    """
    if not rows:
        return [], False

    working = rows[:max_rows]
    row_truncated = len(working) < len(rows)

    kept: list = []
    byte_truncated = False
    for row in working:
        candidate = kept + [row]
        size = len(json.dumps(candidate, default=str).encode("utf-8"))
        if size > max_bytes and kept:
            byte_truncated = True
            break
        kept.append(row)

    return kept, row_truncated or byte_truncated or len(kept) < len(working)


# --------------------------------------------------------------------------------------
# Building + writing
# --------------------------------------------------------------------------------------


def build_assertion_result(
    *,
    type: str,
    status: str,
    spec: dict,
    target: str | None = None,
    db: str | None = None,
    detail: str | None = None,
    expected: dict | None = None,
    actual: dict | None = None,
    elapsed_s: float | None = None,
    max_rows: int = 200,
    max_bytes: int = 65536,
) -> dict:
    """Build one AssertionResult dict (matching its field names as keyword args).

    `expected`/`actual`, if given, are the raw value-snapshot dict the caller has in
    hand, e.g. {"kind": "rows", "rows": [...]}, {"kind": "count", "count": N}, or
    {"kind": "bytes", "size": N}. Any 'rows' list is run through truncate(); 'count' is
    preserved as the caller's pre-truncation total (falling back to len(rows) if the
    caller didn't supply one) so a truncated sidecar still reports the true row count.
    The result is validated before being returned, so a malformed record fails loudly at
    the call site rather than silently corrupting a sidecar later.
    """

    def _finalize(snapshot: dict | None) -> dict | None:
        if snapshot is None:
            return None
        snapshot = dict(snapshot)
        if snapshot.get("kind") == "rows" and "rows" in snapshot:
            original_rows = snapshot["rows"]
            total = snapshot.get("count", len(original_rows))
            kept, was_truncated = truncate(original_rows, max_rows=max_rows, max_bytes=max_bytes)
            snapshot["rows"] = kept
            snapshot["count"] = total
            snapshot["truncated"] = was_truncated or snapshot.get("truncated", False)
        else:
            snapshot.setdefault("truncated", False)
        return snapshot

    record = {
        "type": type,
        "target": target,
        "db": db,
        "spec": spec,
        "status": status,
        "detail": detail,
        "expected": _finalize(expected),
        "actual": _finalize(actual),
    }
    # §85.3. Omitted rather than carried as null, so every existing record and every consumer
    # that has not been taught about it is byte-identical to before.
    if elapsed_s is not None:
        # ⚠ Do NOT coerce first. float(True) is 1.0, so rounding before the validator sees the
        # value makes a bool indistinguishable from a real measurement -- the type check downstream
        # can never fire. Reject the wrong type here, where it is still visible.
        if isinstance(elapsed_s, bool) or not isinstance(elapsed_s, (int, float)):
            raise SchemaError(f"build_assertion_result(): 'elapsed_s' must be a number, "
                              f"got {elapsed_s!r}")
        record["elapsed_s"] = round(float(elapsed_s), 3)
    _validate_assertion(record, "build_assertion_result()")
    return record


def write_sidecar(path, tests: list) -> None:
    """Serialize {schema_version, tests} to `path` as indent=2 JSON. Validates the
    assembled doc first so a malformed sidecar never lands on disk."""
    doc = {"schema_version": SCHEMA_VERSION, "tests": tests}
    validate(doc)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2))
