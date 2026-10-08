from __future__ import annotations
import csv
import datetime
import json
import time
from pathlib import Path

from .model import Schema, wire_schemas
from .workload import Op, WorkloadEngine, WorkloadSpec
from .writer import TrailFileWriter
from .yamlio import load_workload

# Runner: engine Op stream -> trail files + schema.def + expected/ goldens (spec §8).
#
# This is the seam livetest/plugin.py's `generate:` hook and livetest/cli.py's `ggtrail`
# subcommand both call, so generate_from_yaml/stream_from_yaml's signatures and returned
# dict shape are a fixed contract.
#
#
# THE expected/ops.csv PROJECTION
# -------------------------------
# ops.csv is the golden the `file:` assertion matches, so its columns are dictated by what
# the regression app's CQ emits -- NOT by what would be most informative to read. For
# regression/services/ggtrail/ggtrail-cdc-file-diff/app.tql that CQ is:
#
#     SELECT TO_STRING(META(s,'TableName'))     AS TABLE_NAME,
#            TO_STRING(META(s,'OperationName')) AS OP_TYPE,
#            TO_STRING(data[0]) AS C0, TO_STRING(data[1]) AS C1,
#            C2 = data[2], as BigDecimal.toPlainString() at scale 2 for SCOTT.ORDERS (a DECIMAL
#                 the reader delivers as a Double), TO_STRING otherwise
#
# so ops.csv's header is exactly `TABLE_NAME,OP_TYPE,C0,C1,C2` -- livetest/assertions/file.py
# projects each JSON event's `data` dict (whose keys ARE those field names, since the CQ
# names them) and compares it to the golden's csv.DictReader rows via distinct_set().
#
# Three consequences worth stating, because each one is load-bearing:
#
#  * WIDTH. `data[i]` indexes the table's columns positionally, so projecting a fixed
#    prefix only type-checks if every table is at least that wide. The width is therefore
#    min(3, narrowest table) -- the widest COMMON prefix, capped at the 3 columns app.tql
#    reads. A schema whose narrowest table has 2 columns yields C0,C1 and needs an app.tql
#    reading data[0..1]; the width lands in summary.json so the two can be checked.
#
#  * IMAGE. Which image supplies C0..Cn follows what Layer 0 actually puts on the wire:
#    the AFTER image for insert/update (record.write_update encodes the after-image only)
#    and the BEFORE image for delete (record.write_delete encodes the pre-image). That is
#    Op.image.
#
#  * SET, NOT SEQUENCE. distinct_set() compares DISTINCT ROW SETS, so ops.csv's row order
#    is irrelevant to the assertion and duplicate projections collapse on both sides. Rows
#    are still emitted in stream order because a human reads this file when a diff fails.
#
# ops_detail.csv carries the richer per-op projection spec §8 describes
# (seq,txn_id,txn_part,table,op,pk,changed_cols). It is NOT an assertion input -- it exists
# so a failing live diff can be traced back to the op that produced it.
#
# CALIBRATION CAVEAT (see the regression test's expected/README.md): GGTrailParser's
# TableName casing and its rendering of SINT64/timestamp columns are only knowable against
# a real Striim. If the first live run diffs, the fix belongs in _project_row() here, never
# in a hand-edited golden.

# Batch mode's clock: base + op-seq seconds. Never wall-clock -- a fixed seed must yield
# byte-identical trail files, and the record timestamp is part of those bytes.
BASE_TIME = datetime.datetime(2026, 1, 1, 0, 0, 0)
MAX_PROJECT_WIDTH = 3

OPS_CSV = "ops.csv"
OPS_DETAIL_CSV = "ops_detail.csv"
SUMMARY_JSON = "summary.json"

# GGTrailParser's META OperationName for each engine op kind.
_OP_NAMES = {"insert": "INSERT", "update": "UPDATE", "delete": "DELETE"}


def projection_width(schema: Schema) -> int:
    """Widest column prefix common to every table, capped at what app.tql reads."""
    return min(MAX_PROJECT_WIDTH, min(len(t.columns) for t in schema.tables))


def generate_from_yaml(workload: Path, out_dir: Path) -> dict:
    """Batch generation from a workload.yaml. The `generate:` manifest hook's entry point."""
    schema, spec, extras = load_workload(workload)
    max_records = extras.get("max_records_per_file") or 1000
    return generate(schema, spec, Path(out_dir), max_records_per_file=max_records,
                    partial_updates=bool(extras.get("partial_updates", False)))


def stream_from_yaml(
    workload: Path, out_dir: Path, rate: float, duration: float | None = None
) -> dict:
    """Rate-mode generation: the same engine, appended over wall-clock time so a running
    Striim tails the directory like a live GoldenGate extract (spec §8, CLI-only).

    Unlike batch mode this uses the writer's default wall clock -- the point is to look
    like an extract running NOW, and nothing asserts byte-determinism on a streamed run.
    """
    schema, spec, extras = load_workload(workload)
    max_records = extras.get("max_records_per_file") or 1000
    if rate is None or rate <= 0:
        raise ValueError(f"stream rate must be a positive ops/sec, got {rate!r}")
    return generate(
        schema,
        spec,
        Path(out_dir),
        max_records_per_file=max_records,
        rate=rate,
        duration=duration,
        partial_updates=bool(extras.get("partial_updates", False)),
    )


def generate(
    schema: Schema,
    spec: WorkloadSpec,
    out_dir: Path,
    *,
    max_records_per_file: int = 1000,
    rate: float | None = None,
    duration: float | None = None,
    partial_updates: bool = False,
) -> dict:
    """Stream the workload into `out_dir` and return the produced artifacts.

    `partial_updates`: an UPDATE record carries only the key and the changed columns, as a
    trail does under partial-column logging; the record builder emits only the columns in
    the dict, and GGTrailReader marks the rest not-present.

    Returns {"trail_files": [Path], "def_file": Path, "expected_dir": Path,
             "summary": dict} -- plugin.py places trail_files + def_file on the server and
    leaves expected/ local for the assertions.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_dir = out_dir / "expected"
    state_dir = expected_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    engine = WorkloadEngine(schema, spec)
    width = projection_width(schema)
    order = [t.name for t in engine.order]

    # The clock reads a mutable holder the emit loop stamps with the current op's seq, so
    # every record's timestamp is a pure function of its position in the stream.
    cursor = {"seq": 0}

    def clock() -> datetime.datetime:
        return BASE_TIME + datetime.timedelta(seconds=cursor["seq"])

    def wall_clock() -> datetime.datetime:
        # Rate mode looks like an extract running NOW, so it stamps real time. Spelled out
        # rather than left to the writer's default because that default is utcnow(), which
        # is deprecated -- this is the same naive-UTC value without the warning.
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    writer = TrailFileWriter(
        out_dir,
        wire_schemas(schema),
        max_records_per_file=max_records_per_file,
        clock=wall_clock if rate else clock,
    )
    started = time.monotonic()
    written = 0
    try:
        # Both CSVs are written INCREMENTALLY as ops stream past: nothing here ever holds
        # the op list, so total volume stays bounded only by disk (R14).
        with open(expected_dir / OPS_CSV, "w", newline="") as ops_fh, open(
            expected_dir / OPS_DETAIL_CSV, "w", newline=""
        ) as detail_fh:
            ops_csv = csv.writer(ops_fh)
            detail_csv = csv.writer(detail_fh)
            ops_csv.writerow(
                ["TABLE_NAME", "OP_TYPE"] + [f"C{i}" for i in range(width)]
            )
            detail_csv.writerow(
                ["seq", "txn_id", "txn_part", "table", "op", "pk", "changed_cols"]
            )

            for group in _transactions(engine.ops()):
                cursor["seq"] = group[0].seq
                _write_group(writer, group, schema if partial_updates else None)
                for op in group:
                    ops_csv.writerow(_project_row(schema, op, width))
                    detail_csv.writerow(_detail_row(schema, op))
                written += len(group)
                if rate and not _pace(started, written, rate, duration):
                    break
    finally:
        writer.close()

    _write_state(schema, engine, state_dir)
    trail_files = sorted(p for p in out_dir.glob("rt*") if p.is_file())
    summary = _summary(
        schema,
        spec,
        engine,
        width,
        order,
        trail_files,
        max_records_per_file,
        written,
        rate,
        duration,
    )
    (out_dir / SUMMARY_JSON).write_text(
        json.dumps(summary, indent=2, default=str) + "\n"
    )
    return {
        "trail_files": trail_files,
        "def_file": out_dir / "schema.def",
        "expected_dir": expected_dir,
        "summary": summary,
    }


# --- emit ---------------------------------------------------------------------------


def _transactions(ops):
    """Regroup the flat Op stream back into transactions, using the parts the engine
    already assigned. Buffers one transaction at a time -- never the stream."""
    group: list[Op] = []
    for op in ops:
        group.append(op)
        if op.txn_part in ("SOLE", "END"):
            yield group
            group = []
    if group:  # pragma: no cover - engine closes groups
        yield group


def partial_after(schema: Schema, op: Op) -> dict:
    """The key and the changed columns of an update -- what partial-column logging puts on
    the wire. The engine's `_do_update` changes a random subset of the mutable units, so the
    present-set varies per event."""
    table = schema.table(op.table)
    before, after = op.before or {}, op.after or {}
    return {c.name: after.get(c.name) for c in table.columns
            if c.pk or before.get(c.name) != after.get(c.name)}


def _write_group(writer: TrailFileWriter, group: list[Op], partial_schema: Schema | None = None) -> None:
    # A single-op group goes through the writer's scalar methods, which already emit
    # txn_part SOLE and consume one txn id -- exactly what the engine assigned. Multi-op
    # groups go through .transaction(), which frames BEGIN/MIDDLE/END under one txn id.
    # Both paths advance the writer's txn counter once per group, so writer txn ids stay
    # in lockstep with Op.txn_id.
    def after_of(op: Op) -> dict:
        return partial_after(partial_schema, op) if partial_schema is not None else op.after

    if len(group) == 1:
        op = group[0]
        if op.kind == "insert":
            writer.insert(op.table, op.after)
        elif op.kind == "delete":
            writer.delete(op.table, op.before)
        else:
            writer.update(op.table, op.before, after_of(op))
        return
    writer.transaction(
        [
            (
                (op.table, op.kind, op.before, after_of(op))
                if op.kind == "update"
                else (op.table, op.kind, op.image)
            )
            for op in group
        ]
    )


def _pace(started: float, written: int, rate: float, duration: float | None) -> bool:
    """Sleep until this many ops are 'due' at `rate`. Returns False when the run's
    duration is spent (the caller stops draining)."""
    elapsed = time.monotonic() - started
    if duration is not None and elapsed >= duration:
        return False
    target = written / float(rate)
    if target > elapsed:
        time.sleep(
            min(
                target - elapsed,
                (duration - elapsed) if duration is not None else target,
            )
        )
    return duration is None or (time.monotonic() - started) < duration


# --- expected/ ------------------------------------------------------------------------


def _render(value) -> str:
    # None renders as an EMPTY csv field. A JSON null projects to Python None while the
    # golden reads back as "", so a workload with nullable columns needs its app.tql to
    # coalesce -- called out here because it is the one known projection asymmetry.
    return "" if value is None else str(value)


def _project_row(schema: Schema, op: Op, width: int) -> list:
    table = schema.table(op.table)
    image = op.image or {}
    prefix = [image.get(c.name) for c in table.columns[:width]]
    return [op.table, _OP_NAMES[op.kind]] + [_render(v) for v in prefix]


def _detail_row(schema: Schema, op: Op) -> list:
    table = schema.table(op.table)
    image = op.image or {}
    pk = "|".join(_render(image.get(n)) for n in table.pk_names)
    if op.kind == "update":
        changed = [
            c.name
            for c in table.columns
            if (op.before or {}).get(c.name) != (op.after or {}).get(c.name)
        ]
    elif op.kind == "insert":
        changed = [c.name for c in table.columns]
    else:
        changed = []
    return [
        op.seq,
        op.txn_id,
        op.txn_part,
        op.table,
        _OP_NAMES[op.kind],
        pk,
        " ".join(changed),
    ]


def _write_state(schema: Schema, engine: WorkloadEngine, state_dir: Path) -> None:
    # Final live rows per table, sorted by PK -- the golden a DatabaseWriter-target variant
    # diffs against once the file-target path is green (spec §8).
    for table in engine.order:
        ledger = engine.ledgers[table.name]
        path = state_dir / f"{table.name}.csv"
        with open(path, "w", newline="") as fh:
            out = csv.writer(fh)
            out.writerow([c.name for c in table.columns])
            for _pk, row in ledger.rows_sorted():
                out.writerow([_render(row.get(c.name)) for c in table.columns])


def _summary(
    schema: Schema,
    spec: WorkloadSpec,
    engine: WorkloadEngine,
    width: int,
    order: list,
    trail_files: list,
    max_records_per_file: int,
    written: int,
    rate,
    duration,
) -> dict:
    return {
        "seed": spec.seed,
        "config": {
            "initial_rows": dict(spec.initial_rows),
            "ops": spec.ops,
            "mix": dict(spec.mix),
            "table_weights": dict(spec.table_weights),
            "txn_ops": list(spec.txn_ops),
            "keys": spec.keys,
            "max_records_per_file": max_records_per_file,
            "rate": rate,
            "duration": duration,
        },
        "tables": order,
        "projection_width": width,
        "projection_columns": ["TABLE_NAME", "OP_TYPE"]
        + [f"C{i}" for i in range(width)],
        "records": written,
        "transactions": engine.transactions,
        "by_op": dict(engine.counts),
        "by_table": {t: dict(counts) for t, counts in engine.table_counts.items()},
        "fallbacks": dict(engine.fallbacks),
        "final_live_rows": engine.live_counts(),
        "trail_files": [p.name for p in trail_files],
    }
