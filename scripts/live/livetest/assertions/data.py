from __future__ import annotations

import csv
import io
import time
from pathlib import Path

from livetest.assertions import AssertionFailed
from livetest.assertions.expected import load_expected_text
from livetest.resultschema import build_assertion_result


class DataSpecError(Exception):
    pass


#: Golden cell meaning "this column must be ABSENT from the record", as distinct from
#: present-and-empty. A CSV row is rectangular and `csv.DictReader` yields '' for a blank
#: cell, so without this there is no way to write a golden that asserts a value column on
#: one record and its absence on another -- which is what an assertion covering both
#: enriched and unenriched records needs. `_project` keeps only the keys a record actually
#: carries, so the expected row has to drop the key entirely to match.
ABSENT = "<absent>"


#: Golden cell meaning "this column is PRESENT and its value is NULL", as distinct from both
#: absent and present-and-empty. `_stringify` leaves None as None while a blank CSV cell reads
#: as '', so before this marker a null cell could not be written at all: an empty cell asserted
#: the empty STRING and ABSENT asserted the key was missing, and neither matches a record that
#: carries the key with a null value. Found on a live run of csv-format-options, whose
#: nullToken makes REGION null for exactly one row (8.128).
NULL = "<null>"


def apply_null(rows: list[dict]) -> list[dict]:
    """Turn every NULL marker into a real None, so the expected cell matches a null value.

    Applied to GOLDEN rows only, never to projected event rows -- a record whose value is
    literally the string "<null>" keeps it, exactly as `strip_absent` treats "<absent>".
    The two markers are the two halves of the same problem: a rectangular CSV cannot say
    "missing" or "null" without a spelling for each.
    """
    return [{k: (None if v == NULL else v) for k, v in row.items()} for row in rows]


def strip_absent(rows: list[dict]) -> list[dict]:
    """Drop every cell whose value is the ABSENT marker, so the expected row lacks that key.

    Applied to GOLDEN rows only, never to projected event rows: a record whose value is
    literally the string "<absent>" keeps it. Compare `strip_absent` against `_project`'s
    filtering -- the two together are what let one golden describe records of different
    shapes.
    """
    return [{k: v for k, v in row.items() if v != ABSENT} for row in rows]


def load_golden(path: Path, tokens: dict | None = None) -> list[dict]:
    # NOT strip_absent: this is the `assert.data` path, which compares against SQL result
    # rows, and a SELECT always returns every column it names -- NULL arrives as None, never
    # as an absent key. So the marker could never match here. Rather than let such a golden
    # fail as a value mismatch after burning the full assertion timeout -- a message that
    # reads "expected '<absent>', got 'Widget Pro'" and never names the cause -- REFUSE it
    # here and say which column and which file. The marker is a `file`-assertion feature; see
    # strip_absent and TEST-YAML.md.
    text = load_expected_text(path, tokens)
    rows = [dict(row) for row in csv.DictReader(io.StringIO(text))]
    for row in rows:
        for col, val in row.items():
            if val == ABSENT:
                raise DataSpecError(
                    f"{path}: column {col!r} uses the {ABSENT} marker, which is a `file`"
                    " assertion feature and has no meaning under `assert.data`: a SELECT"
                    " always returns every column it names, so a value is None rather than"
                    f" an absent key. Use the {NULL} marker to assert the NULL, or move the"
                    " check to `assert.file`.")
    # <null> DOES belong here: a SELECT returns NULL as None, which is exactly what this
    # marker produces. It is the answer to the sentence above, which used to say "assert the
    # NULL directly" while offering no way to write one (8.128).
    return apply_null(rows)


def _stringify(v):
    # A parsed JSON event carries real Python bools, but a golden CSV is read by
    # csv.DictReader as plain strings, and authors write JSON's own lowercase true/false.
    # str(True) == "True" would never match "true" without this normalization.
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def normalized_row(row: dict) -> dict:
    """One row exactly as `distinct_set` compares it.

    Failure output must render THIS, not the raw row. A parsed JSON event holds real bools
    while a golden CSV holds text, and printing both raw shows `True` beside `'True'` -- which
    reads as a type mismatch when the comparison actually saw `'true'` vs `'True'`, a spelling
    one. That exact display cost three weeks of a quarantined test being blamed on the
    operator, and then a misdiagnosis that nearly changed this harness instead of the golden.
    """
    return {str(k): _stringify(v) for k, v in row.items()}


def distinct_set(rows: list[dict]) -> set:
    return {frozenset(normalized_row(r).items()) for r in rows}


def sorted_rows_for_display(rows) -> list:
    # Render a set of frozenset({(col, value|None), ...}) rows for an error message.
    # Both the inner and outer sorts must use repr as the key to avoid comparing None
    # with str, which would raise '<' not supported between instances of 'NoneType' and 'str'.
    # When sorting tuples with None values, we must convert to strings first.
    per_row_sorted = [sorted(row, key=repr) for row in rows]
    return sorted(per_row_sorted, key=repr)


def parse_data_specs(raw: list) -> list[dict]:
    if not isinstance(raw, list):
        raise DataSpecError("'assert.data' must be a list of specs")
    for spec in raw:
        if not isinstance(spec, dict) or not spec.get("target"):
            raise DataSpecError(f"data spec needs a 'target': {spec!r}")
        if "project" in spec:
            if spec["project"] != "kafka_record":
                raise DataSpecError("data 'project' must be kafka_record when supplied")
            endpoint = spec.get("target_db", spec.get("db", "kafka"))
            if endpoint != "kafka":
                raise DataSpecError("project: kafka_record requires a Kafka target")
            paths = spec.get("keys")
            if (not isinstance(paths, list) or not paths
                    or any(not isinstance(p, str) or p.split(".")[0] not in ("key", "value")
                           or any(not segment for segment in p.split(".")) for p in paths)):
                raise DataSpecError("project: kafka_record needs non-empty keys paths rooted at key or value")
        if "ordered" in spec and (not isinstance(spec["ordered"], bool)
                                  or spec.get("project") != "kafka_record" or "match" not in spec):
            raise DataSpecError("data 'ordered' requires a boolean, project: kafka_record and match")
        if not any(k in spec for k in ("min_rows", "rows", "match")):
            raise DataSpecError(f"data spec {spec['target']!r} needs one of min_rows/rows/match")
        if "min_rows" in spec:
            try:
                min_rows = int(spec["min_rows"])
            except (TypeError, ValueError) as e:
                raise DataSpecError(
                    f"data spec {spec['target']!r}: 'min_rows' must be an integer: {spec['min_rows']!r}") from e
            if min_rows <= 0:
                raise DataSpecError(
                    f"data spec {spec['target']!r}: 'min_rows' must be positive, got {min_rows}")
        if "rows" in spec:
            try:
                rows = int(spec["rows"])
            except (TypeError, ValueError) as e:
                raise DataSpecError(
                    f"data spec {spec['target']!r}: 'rows' must be an integer: {spec['rows']!r}") from e
            if rows < 0:
                raise DataSpecError(
                    f"data spec {spec['target']!r}: 'rows' must be non-negative, got {rows}")
    return raw


def _load_want(spec: dict, basedir, tokens: dict | None = None):
    path = Path(basedir) / spec["match"]
    try:
        rows = load_golden(path, tokens)
    except FileNotFoundError as e:
        raise DataSpecError(f"{spec['target']}: golden file not found: {path}") from e
    want = distinct_set(rows)
    if not want:
        raise DataSpecError(
            f"{spec['target']}: golden {spec['match']} is empty; a match spec must expect at least one row")
    return rows, want


def _committed(pg, specs: list[dict]) -> bool:
    """True unless an exact `rows:` spec has a committed-read count that disagrees.

    Only an admin exposing `count_rows_committed` (mssql, whose `count_rows` reads uncommitted so it
    cannot deadlock against the writer) is asked; every other admin's count is already committed.
    A lock timeout counts as "not yet": the caller polls again.
    """
    confirm = getattr(pg, "count_rows_committed", None)
    if not callable(confirm):
        return True
    for spec in specs:
        if "rows" in spec and spec.get("project") != "kafka_record":
            try:
                if confirm(spec["target"]) != int(spec["rows"]):
                    return False
            except Exception:      # noqa: BLE001 -- lock timeout or transient: poll again
                return False
    return True



# How many offending rows to print for a failed "must be empty" assertion. Enough to show a
# pattern, few enough that a wide view cannot bury the summary it is attached to.
_EMPTY_SAMPLE_LIMIT = 10



def _with_empty_sample(pg, spec, detail):
    """Append the offending rows to a failed ``rows: 0`` detail, once, at timeout.

    Kafka targets are excluded: ``project: kafka_record`` would re-consume the topic, and a
    plain Kafka target's reader carries its own multi-second poll budget. Neither belongs in a
    diagnostic.
    """
    if spec.get("rows") is None or int(spec["rows"]) != 0:
        return detail
    if spec.get("project") == "kafka_record" or spec.get("db") == "kafka":
        return detail
    return f"{detail}\n  the rows that should not exist:" + _sample_rows(pg, spec["target"])

def _sample_rows(pg, target):
    """Rows from a view that should have been empty, rendered for a failure message.

    Best-effort by design: this runs on a path that is ALREADY failing, so a diagnostic that
    raised would replace a useful message with a confusing one and hide the real assertion.
    """
    try:
        rows = pg.select_rows(target)
    except Exception as exc:                                  # noqa: BLE001 - diagnostics only
        return f" (could not be read: {exc})"
    if not rows:
        # Counted non-zero but selected nothing: worth saying, not worth raising.
        return " (count was non-zero but the select returned nothing)"
    out = []
    for row in rows[:_EMPTY_SAMPLE_LIMIT]:
        out.append("\n    " + ", ".join(f"{k}={v!r}" for k, v in row.items())
                   if hasattr(row, "items") else "\n    " + repr(row))
    if len(rows) > _EMPTY_SAMPLE_LIMIT:
        out.append(f"\n    ... and {len(rows) - _EMPTY_SAMPLE_LIMIT} more")
    return "".join(out)


def _evaluate(pg, spec: dict, golden_rows, want):
    # Mirrors the pass/fail decision that _check_spec used to make alone, but also
    # returns the expected/actual snapshot for the sidecar record -- used both inside
    # the poll loop (to decide satisfied/timeout) and once at the end to build records
    # for every spec, including the already-passing ones.
    target = spec["target"]
    projected_records = None
    if spec.get("project") == "kafka_record":
        reader = getattr(pg, "select_records", None)
        if not callable(reader):
            raise DataSpecError("project: kafka_record requires a Kafka target")
        projected_records = [_record_projection(row, spec["keys"]) for row in reader(target)]
        count = len(projected_records)
    else:
        count = pg.count_rows(target)
    expected = actual = None

    if "rows" in spec:
        expected = {"kind": "count", "count": int(spec["rows"])}
        actual = {"kind": "count", "count": count}
        if count != int(spec["rows"]):
            return False, f"{target}: rows={count}, want exactly {spec['rows']}", expected, actual

    if "min_rows" in spec:
        expected = {"kind": "count", "count": int(spec["min_rows"])}
        actual = {"kind": "count", "count": count}
        if count < int(spec["min_rows"]):
            return False, f"{target}: rows={count}, want min_rows>={spec['min_rows']}", expected, actual

    if want is not None:
        rows = projected_records if projected_records is not None else pg.select_rows(target)
        keys = spec.get("keys")
        if keys is not None:
            rows = [{k: v for k, v in r.items() if k in keys} for r in rows]
        got = distinct_set(rows)
        expected = {"kind": "rows", "rows": golden_rows}
        actual = {"kind": "rows", "rows": rows}
        if got != want:
            detail = (f"{target}: distinct row set {sorted_rows_for_display(got)} "
                      f"!= golden {sorted_rows_for_display(want)}")
            return False, detail, expected, actual
        if spec.get("ordered") and [normalized_row(r) for r in rows] != [normalized_row(r) for r in golden_rows]:
            return False, f"{target}: ordered records differ from golden", expected, actual

    return True, None, expected, actual


def _record_projection(record: dict, paths: list[str]) -> dict:
    """Project stable nested Kafka fields; absent paths are represented by NULL."""
    projected = {}
    for path in paths:
        value = record
        for segment in path.split("."):
            value = value.get(segment) if isinstance(value, dict) else None
        projected[path] = value
    return projected


def _format_data_failure(results: list) -> str:
    """Format expected vs actual data for test failure output."""
    lines = []
    for ok, d, exp, act in results:
        if not ok:
            lines.append("")
            lines.append(d)  # main error message
            if exp and act:
                lines.append("")
                if exp.get("kind") == "rows" and act.get("kind") == "rows":
                    exp_rows = exp.get("rows", [])
                    act_rows = act.get("rows", [])

                    # Normalized, i.e. exactly what the comparison sees -- see normalized_row.
                    lines.append("  EXPECTED ROWS (as compared):")
                    if exp_rows:
                        for row_dict in exp_rows:
                            lines.append(f"    {normalized_row(row_dict)}")
                    else:
                        lines.append("    (empty)")

                    lines.append("")
                    lines.append("  ACTUAL ROWS (as compared):")
                    if act_rows:
                        for row_dict in act_rows:
                            lines.append(f"    {normalized_row(row_dict)}")
                    else:
                        lines.append("    (empty)")

                    # Show missing and extra columns
                    exp_cols = {col for row in exp_rows for col in row.keys()}
                    act_cols = {col for row in act_rows for col in row.keys()}
                    missing_cols = exp_cols - act_cols
                    extra_cols = act_cols - exp_cols

                    if missing_cols:
                        lines.append("")
                        lines.append("  MISSING COLUMNS FROM ACTUAL:")
                        for col in sorted(missing_cols):
                            lines.append(f"    {col}")

                    if extra_cols:
                        lines.append("")
                        lines.append("  EXTRA COLUMNS IN ACTUAL:")
                        for col in sorted(extra_cols):
                            lines.append(f"    {col}")

                    # Also show row-level mismatches (different values, not missing columns)
                    exp_set = distinct_set(exp_rows)
                    act_set = distinct_set(act_rows)
                    missing_rows = exp_set - act_set
                    extra_rows = act_set - exp_set

                    # Filter out mismatches that are only due to missing/extra columns
                    def filter_structural_only(mismatch_rows, other_set, cols_to_remove):
                        real_diffs = set()
                        for mismatch_row in mismatch_rows:
                            # Remove tuples with structural difference columns
                            mismatch_filtered = {(k, v) for k, v in mismatch_row if k not in cols_to_remove}
                            # Check if this matches any row in the other set after same filtering
                            found_match = False
                            for other_row in other_set:
                                other_filtered = {(k, v) for k, v in other_row if k not in cols_to_remove}
                                if mismatch_filtered == other_filtered:
                                    found_match = True
                                    break
                            if not found_match:
                                real_diffs.add(mismatch_row)
                        return real_diffs

                    missing_rows = filter_structural_only(missing_rows, act_set, missing_cols | extra_cols)
                    extra_rows = filter_structural_only(extra_rows, exp_set, missing_cols | extra_cols)

                    if missing_rows:
                        lines.append("")
                        lines.append("  MISSING ROWS (different values):")
                        for row in sorted_rows_for_display(missing_rows):
                            lines.append(f"    {dict(row)}")

                    if extra_rows:
                        lines.append("")
                        lines.append("  EXTRA ROWS (different values):")
                        for row in sorted_rows_for_display(extra_rows):
                            lines.append(f"    {dict(row)}")
                elif exp.get("kind") == "count" and act.get("kind") == "count":
                    lines.append(f"  EXPECTED: {exp.get('count')} rows")
                    lines.append(f"  ACTUAL: {act.get('count')} rows")
    return "\n".join(lines)


def assert_data(pg, specs: list[dict], basedir, timeout: int, poll: float = 2.0,
                 status_probe=None, *, db: str | None = None, progress=None,
                 tokens: dict | None = None) -> list[dict]:
    goldens = [(_load_want(s, basedir, tokens) if "match" in s else (None, None)) for s in specs]
    # The clock starts at the first poll, as assert_diff's does (§85.3), and is reported only for
    # an exact `rows:` spec -- the one data shape with a finish line, so "time to reach it" means
    # what a diff's elapsed means. A min_rows or match spec has no such line and carries none.
    started = time.monotonic()
    deadline = started + timeout
    while True:
        if status_probe:
            status_probe()
        results = [_evaluate(pg, s, rows, want) for s, (rows, want) in zip(specs, goldens)]
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is None and not _committed(pg, specs):
            # The count was satisfied by a dirty read (mssql): the rows are executed but not yet
            # committed, or a lock timed out. Keep polling; the clock keeps running until the
            # committed count agrees.
            detail = "rows executed but not yet committed"
        if detail is None:
            elapsed = time.monotonic() - started
            return [
                build_assertion_result(type="data", status="passed", spec=s, target=s["target"], db=db,
                                        detail="ok", expected=exp, actual=act,
                                        **({"elapsed_s": elapsed} if "rows" in s else {}))
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
        if time.monotonic() >= deadline:
            records = [
                build_assertion_result(type="data", status="passed" if ok else "failed", spec=s,
                                        target=s["target"], db=db, detail="ok" if ok else d,
                                        expected=exp, actual=act)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
            # A "must be EMPTY" assertion that failed: the offending rows ARE the diagnosis, and
            # reporting the count alone throws it away. These targets are violation views --
            # gviolations, gorphan_late -- whose columns say WHICH row broke the rule and by how
            # much, and a bare "rows=2" cannot tell a marginal overshoot from the defect the view
            # was written to catch. That cost a full re-run to work out once, and the rows were
            # gone by then because the next run drops the schema.
            #
            # Sampled HERE rather than in _evaluate, which the poll loop above calls every `poll`
            # seconds: a rows: 0 spec that is non-zero would otherwise re-SELECT its target on
            # every iteration for the whole timeout. Harmless on a two-row view, not harmless on
            # the real data tables that eventhooks-initial-load-table-truncation asserts
            # rows: 0 over -- that would re-fetch both tables in full every 2s for 240s. By the
            # time we are here the run is over, so one select costs nothing.
            results = [(ok, _with_empty_sample(pg, s, d) if not ok else d, exp, act)
                       for s, (ok, d, exp, act) in zip(specs, results)]
            detailed_msg = _format_data_failure(results)
            raise AssertionFailed(f"data assertion not satisfied within {timeout}s:{detailed_msg}", records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)   # #4: floor avoids a busy-wait when poll=0 with a positive timeout
