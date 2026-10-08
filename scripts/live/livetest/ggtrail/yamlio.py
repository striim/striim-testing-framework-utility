from __future__ import annotations
from pathlib import Path

import yaml

from .model import Column, Schema, Table
from .workload import WorkloadSpec

# workload.yaml loader (spec §9.3) -- schema AND workload in one checked-in file, so a
# regression test ships ~30 readable lines instead of committed binary trail fixtures.
#
# Every rejection names the FILE and the KEY. These files are hand-written by test authors
# and a silently-defaulted typo (`table_weight:` for `table_weights:`) would produce a
# plausible-looking but wrong workload -- so unknown keys are errors, not noise.

_TOP_KEYS = {"seed", "keys", "tables", "workload", "max_records_per_file", "partial_updates"}
_WORKLOAD_KEYS = {"initial_rows", "ops", "mix", "table_weights", "txn_ops"}
_COLUMN_KEYS = {"name", "type", "pk", "fk", "nullable", "null_rate"}


class WorkloadFileError(ValueError):
    """A workload.yaml that does not parse as the documented grammar."""


def load_workload(path) -> tuple[Schema, WorkloadSpec, dict]:
    """Parse a workload.yaml into (Schema, WorkloadSpec, extras).

    `extras` carries the non-schema, non-workload knobs the runner needs -- currently just
    `max_records_per_file` -- kept out of WorkloadSpec because they describe the OUTPUT
    encoding, not the data.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except FileNotFoundError as e:
        raise WorkloadFileError(f"workload file not found: {path}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise WorkloadFileError(f"{path}: not valid YAML: {e}") from e
    if raw is None:
        raise WorkloadFileError(f"{path}: file is empty")
    if not isinstance(raw, dict):
        raise WorkloadFileError(
            f"{path}: top level must be a mapping, got {type(raw).__name__}"
        )

    _reject_unknown(path, "", raw, _TOP_KEYS)
    schema = _load_schema(path, raw)
    spec = _load_spec(path, raw)
    partial = raw.get("partial_updates", False)
    if not isinstance(partial, bool):
        raise WorkloadFileError(f"{path}: partial_updates must be true or false, got {partial!r}")
    extras = {
        "max_records_per_file": _opt_int(
            path, "max_records_per_file", raw.get("max_records_per_file")
        ),
        # UPDATE images carry only the key and the changed columns, in both sinks: the
        # trail writer (runner.partial_after) and the perf sink (a perf
        # workload generator).
        "partial_updates": partial,
    }
    return schema, spec, extras


# --- schema ---------------------------------------------------------------------------


def _load_schema(path: Path, raw: dict) -> Schema:
    tables_raw = raw.get("tables")
    if not tables_raw:
        raise WorkloadFileError(
            f"{path}: 'tables' is required and must name at least one table"
        )
    if not isinstance(tables_raw, dict):
        raise WorkloadFileError(
            f"{path}: 'tables' must be a mapping of TABLE -> {{columns: [...]}}, "
            f"got {type(tables_raw).__name__}"
        )

    tables = []
    for name, body in tables_raw.items():
        where = f"tables.{name}"
        if not isinstance(body, dict):
            raise WorkloadFileError(
                f"{path}: {where} must be a mapping with a 'columns' list"
            )
        _reject_unknown(path, where, body, {"columns"})
        cols_raw = body.get("columns")
        if not isinstance(cols_raw, list) or not cols_raw:
            raise WorkloadFileError(f"{path}: {where}.columns must be a non-empty list")
        tables.append(
            Table(
                name=str(name),
                columns=[
                    _load_column(path, f"{where}.columns[{i}]", c)
                    for i, c in enumerate(cols_raw)
                ],
            )
        )
    return Schema(tables=tables)


def _load_column(path: Path, where: str, raw) -> Column:
    if not isinstance(raw, dict):
        raise WorkloadFileError(
            f"{path}: {where} must be a mapping, got {type(raw).__name__}"
        )
    _reject_unknown(path, where, raw, _COLUMN_KEYS)
    if not raw.get("name"):
        raise WorkloadFileError(f"{path}: {where} needs a 'name'")
    if not raw.get("type"):
        raise WorkloadFileError(f"{path}: {where} ({raw['name']}) needs a 'type'")
    null_rate = raw.get("null_rate", 0.0)
    try:
        null_rate = float(null_rate)
    except (TypeError, ValueError) as e:
        raise WorkloadFileError(
            f"{path}: {where}.null_rate must be a number, got {null_rate!r}"
        ) from e
    return Column(
        name=str(raw["name"]),
        dtype=str(raw["type"]),
        pk=bool(raw.get("pk", False)),
        fk=(str(raw["fk"]) if raw.get("fk") else None),
        nullable=bool(raw.get("nullable", False)),
        null_rate=null_rate,
    )


# --- workload -------------------------------------------------------------------------


def _load_spec(path: Path, raw: dict) -> WorkloadSpec:
    wl = raw.get("workload") or {}
    if not isinstance(wl, dict):
        raise WorkloadFileError(
            f"{path}: 'workload' must be a mapping, got {type(wl).__name__}"
        )
    _reject_unknown(path, "workload", wl, _WORKLOAD_KEYS)

    return WorkloadSpec(
        seed=_opt_int(path, "seed", raw.get("seed"), default=0),
        initial_rows=_int_map(path, "workload.initial_rows", wl.get("initial_rows")),
        ops=_opt_int(path, "workload.ops", wl.get("ops"), default=0),
        mix=_float_map(path, "workload.mix", wl.get("mix")) or {"insert": 1.0},
        table_weights=_float_map(
            path, "workload.table_weights", wl.get("table_weights"), upper=True
        ),
        txn_ops=_txn_ops(path, wl.get("txn_ops")),
        keys=_keys(path, raw.get("keys")),
    )


def _keys(path: Path, raw) -> object:
    if raw is None:
        return "sequential"
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        # Per-table override; tables absent from the mapping keep the default strategy.
        return {str(k).upper(): str(v) for k, v in raw.items()}
    raise WorkloadFileError(
        f"{path}: 'keys' must be a strategy name or a {{TABLE: strategy}} mapping, "
        f"got {type(raw).__name__}"
    )


def _txn_ops(path: Path, raw) -> tuple:
    if raw is None:
        return (1, 1)
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise WorkloadFileError(
            f"{path}: workload.txn_ops must be a [lo, hi] pair, got {raw!r}"
        )
    try:
        lo, hi = int(raw[0]), int(raw[1])
    except (TypeError, ValueError) as e:
        raise WorkloadFileError(
            f"{path}: workload.txn_ops entries must be integers, got {raw!r}"
        ) from e
    if lo < 1 or hi < lo:
        raise WorkloadFileError(
            f"{path}: workload.txn_ops must satisfy 1 <= lo <= hi, got {raw!r}"
        )
    return (lo, hi)


# --- scalar helpers -------------------------------------------------------------------


def _reject_unknown(path: Path, where: str, mapping: dict, allowed: set) -> None:
    unknown = sorted(str(k) for k in mapping if str(k) not in allowed)
    if unknown:
        prefix = f"{where}: " if where else ""
        raise WorkloadFileError(
            f"{path}: {prefix}unknown key(s) {unknown}; allowed keys are {sorted(allowed)}"
        )


def _opt_int(path: Path, where: str, raw, default=None):
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise WorkloadFileError(
            f"{path}: '{where}' must be an integer, got {raw!r}"
        ) from e
    if value < 0:
        raise WorkloadFileError(f"{path}: '{where}' must be >= 0, got {value}")
    return value


def _int_map(path: Path, where: str, raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WorkloadFileError(
            f"{path}: '{where}' must be a mapping, got {type(raw).__name__}"
        )
    out = {}
    for k, v in raw.items():
        try:
            value = int(v)
        except (TypeError, ValueError) as e:
            raise WorkloadFileError(
                f"{path}: '{where}.{k}' must be an integer, got {v!r}"
            ) from e
        if value < 0:
            raise WorkloadFileError(f"{path}: '{where}.{k}' must be >= 0, got {value}")
        out[str(k).upper()] = value
    return out


def _float_map(path: Path, where: str, raw, upper: bool = False) -> dict:
    # `upper` uppercases the KEYS: table_weights is keyed by table name (which the model
    # uppercases), mix is keyed by op name (which it does not).
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WorkloadFileError(
            f"{path}: '{where}' must be a mapping, got {type(raw).__name__}"
        )
    out = {}
    for k, v in raw.items():
        try:
            value = float(v)
        except (TypeError, ValueError) as e:
            raise WorkloadFileError(
                f"{path}: '{where}.{k}' must be a number, got {v!r}"
            ) from e
        if value < 0:
            raise WorkloadFileError(f"{path}: '{where}.{k}' must be >= 0, got {value}")
        out[str(k).upper() if upper else str(k)] = value
    return out
