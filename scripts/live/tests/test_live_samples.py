"""The shipped live samples (samples/live) are complete and self-consistent, checked without a Striim server.

Each sample's `exact:` and `lifecycle:` blocks are validated by the same validators the engine uses
(`canon.check_manifest`, `lifecycle.parse_spec`); the rest of the manifest must load with today's
`load_manifest`; every golden is derived from its seed or change script, never from a run.
"""
from __future__ import annotations

import csv
import io
import re
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from livetest import canon, lifecycle
from livetest.manifest import load_manifest

SAMPLES = Path(__file__).resolve().parents[3] / "samples" / "live"
COMMON = {"README.md", "app.tql", "test.yaml", "expected/rows.csv"}
ASSETS = {
    "01-plain-replication": COMMON | {"ddl_source.sql", "ddl_target.sql", "seed.sql"},
    "02-transform": COMMON | {"ddl_source.sql", "ddl_target.sql", "seed.sql"},
    "03-file-output": COMMON | {"ddl_source.sql", "seed.sql"},
    "04-lifecycle-check": COMMON | {"ddl_source.sql", "ddl_target.sql", "slot.sql", "changes.sql",
                                    "sentinel_insert.sql", "sentinel_delete.sql"},
}
# MySQL samples: no exact:/lifecycle: (both read Postgres only); a count plus a text match instead.
MYSQL = {
    "05-mysql-initial-load": COMMON | {"ddl_source.sql", "ddl_target.sql", "seed.sql"},
    "06-mysql-cdc": COMMON | {"ddl_source.sql", "ddl_target.sql", "changes.sql"},
}
MODES = {"01-plain-replication": ("initial-load", "db"), "02-transform": ("initial-load", "db"),
         "03-file-output": ("initial-load", "file"), "04-lifecycle-check": ("cdc", "db")}


def _raw(name: str) -> dict:
    return yaml.safe_load((SAMPLES / name / "test.yaml").read_text())


def _golden(name: str) -> tuple[list[str], list[dict]]:
    text = (SAMPLES / name / "expected" / "rows.csv").read_text()
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return list(reader.fieldnames or []), list(reader)


def _columns(ddl: str) -> list[str]:
    body = re.search(r"CREATE\s+TABLE\s+\S+\s*\((.*)\)\s*;", ddl, re.I | re.S).group(1)
    return [line.strip().split()[0] for line in body.splitlines() if line.strip()]


_ROW4 = re.compile(r"\(\s*(\d+)\s*,\s*'([^']*)'\s*,\s*([\d.]+)\s*,\s*TIMESTAMPTZ\s*'([^']+)'\s*\)")


def _seed4(name: str) -> list[tuple]:
    return _ROW4.findall((SAMPLES / name / "seed.sql").read_text())


def _ts(literal: str) -> datetime:
    """A golden or seed timestamp; a bare `+00` offset is widened to `+00:00`."""
    return datetime.fromisoformat(re.sub(r"([+-]\d\d)$", r"\1:00", literal.strip()))


def test_the_samples_are_numbered_simplest_first():
    assert sorted(p.name for p in SAMPLES.iterdir() if p.is_dir()) == list(ASSETS) + list(MYSQL)


@pytest.mark.parametrize("name", list(ASSETS) + list(MYSQL))
def test_sample_ships_exactly_its_assets(name):
    root = SAMPLES / name
    assert {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} == {**ASSETS, **MYSQL}[name]
    assert not (root / "gold-targets.yaml").exists()


@pytest.mark.parametrize("name", ASSETS)
def test_exact_block_is_valid(name):
    resolved = canon.check_manifest(_raw(name), case_dir=SAMPLES / name)
    assert resolved is not None and len(resolved["specs"]) == 1
    kind = resolved["specs"][0][0]
    assert kind == ("file" if name == "03-file-output" else "data")


@pytest.mark.parametrize("name", ASSETS)
def test_lifecycle_block_is_valid(name):
    raw = _raw(name)
    spec = lifecycle.parse_spec(raw["lifecycle"], SAMPLES / name / "test.yaml", timeout=raw["timeout"])
    assert (spec.mode, spec.sink) == MODES[name]
    assert spec.stability_s > 0


@pytest.mark.parametrize("name", ASSETS)
def test_rest_of_manifest_loads_with_todays_loader(name, tmp_path):
    # Until the exact:/lifecycle: hooks are in the plugin, load_manifest refuses those two keys; everything
    # else (TQL, DDL, seed, assertions, requires) must already be valid for the engine.
    case = tmp_path / name
    shutil.copytree(SAMPLES / name, case)
    raw = {k: v for k, v in _raw(name).items() if k not in ("exact", "lifecycle")}
    for spec in (raw["assert"].get("data") or []) + (raw["assert"].get("file") or []):
        spec.pop("exact", None)
    (case / "test.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    m = load_manifest(case / "test.yaml")
    assert m.requires == ["postgres"] and (case / m.tql).is_file()
    assert all((case / f).is_file() for _db, f in m.ddl_files)
    assert all((case / seed[1]).is_file() for seed in m.seed_files)


@pytest.mark.parametrize("name", ["01-plain-replication", "02-transform", "04-lifecycle-check"])
def test_source_target_and_golden_columns_agree(name):
    src = _columns((SAMPLES / name / "ddl_source.sql").read_text())
    tgt = _columns((SAMPLES / name / "ddl_target.sql").read_text())
    assert src == tgt == _golden(name)[0]


def test_plain_replication_golden_is_the_seed():
    header, rows = _golden("01-plain-replication")
    seed = _seed4("01-plain-replication")
    assert len(seed) == 3
    assert [(r["id"], r["customer_name"], Decimal(r["amount"]), _ts(r["created_at"])) for r in rows] == \
        [(i, n, Decimal(a), _ts(ts)) for i, n, a, ts in seed]


def test_transform_golden_doubles_the_seed_amount():
    _header, rows = _golden("02-transform")
    seed = _seed4("02-transform")
    assert [(r["id"], r["label"], _ts(r["created_at"])) for r in rows] == \
        [(i, n, _ts(ts)) for i, n, _a, ts in seed]
    assert [Decimal(r["amount"]) for r in rows] == [Decimal(a) * 2 for _i, _n, a, _ts in seed]
    assert "MODIFY(data[2]" in (SAMPLES / "02-transform" / "app.tql").read_text()


def test_file_output_golden_is_the_seed_and_the_file_is_owned():
    header, rows = _golden("03-file-output")
    seed = re.findall(r"\(\s*(\d+)\s*,\s*'([^']*)'\s*\)", (SAMPLES / "03-file-output" / "seed.sql").read_text())
    assert header == ["id", "name"] and [(r["id"], r["name"]) for r in rows] == seed
    raw = _raw("03-file-output")
    assert raw["assert"]["file"][0]["path"] == raw["lifecycle"]["completion"]["path"] == "${OWNED_DIR}/rows.json"
    assert raw["lifecycle"]["completion"]["lines"] == len(seed)
    assert "FileName: '${OWNED_DIR}/rows.json'" in (SAMPLES / "03-file-output" / "app.tql").read_text()


def test_file_output_writes_one_event_per_line():
    """The file-lines witnesses count lines, and JSONFormatter's default is one JSON array
    with every member on its own line (19 lines for 2 rows), so readiness never met the baseline of 2.
    The sample writes each event as one line instead: no array, members joined by a space."""
    tql = (SAMPLES / "03-file-output" / "app.tql").read_text()
    fmt = re.search(r"FORMAT USING Global\.JSONFormatter \((.*?)\) INPUT FROM", tql, re.S).group(1)
    props = dict(re.findall(r"(\w+):\s*'([^']*)'", fmt))
    assert props.get("EventsAsArrayOfJsonObjects") == "false"
    assert props.get("JsonMemberDelimiter") == " "
    raw = _raw("03-file-output")["lifecycle"]
    assert raw["completion"] == {"kind": "file-lines", "path": "${OWNED_DIR}/rows.json", "lines": 2}


def test_lifecycle_check_golden_is_empty_because_every_change_is_deleted():
    header, rows = _golden("04-lifecycle-check")
    assert rows == []
    changes = (SAMPLES / "04-lifecycle-check" / "changes.sql").read_text()
    inserted = set(re.findall(r"\(\s*(\d+)\s*,", changes))
    deleted = set(re.search(r"DELETE FROM \S+ WHERE id IN \(([^)]*)\)", changes).group(1).replace(" ", "").split(","))
    assert inserted == deleted == {"101", "102", "103"}
    raw = _raw("04-lifecycle-check")
    assert raw["seed"] == [{"file": "changes.sql", "db": "postgres-source", "when": "post_start"}]
    for f in ("sentinel_insert.sql", "sentinel_delete.sql"):
        assert "${SENTINEL_ID}" in (SAMPLES / "04-lifecycle-check" / f).read_text()
    assert "${PG_SLOT}" in (SAMPLES / "04-lifecycle-check" / "slot.sql").read_text()
    assert "ReplicationSlotName: '${PG_SLOT}'" in (SAMPLES / "04-lifecycle-check" / "app.tql").read_text()


@pytest.mark.parametrize("name", ASSETS)
def test_tql_is_namespaced_and_reads_the_owned_source(name):
    tql = (SAMPLES / name / "app.tql").read_text()
    assert "CREATE NAMESPACE ${NS};" in tql and "CREATE OR REPLACE APPLICATION ${APP};" in tql
    assert "Tables: '${PG_SOURCE_SCHEMA}.${TID}src" in tql
    assert "START APPLICATION ${APP};" in tql


@pytest.mark.parametrize("name", list(MYSQL))
def test_mysql_sample_loads_and_pins_count_and_rows(name, tmp_path):
    case = tmp_path / name
    shutil.copytree(SAMPLES / name, case)
    m = load_manifest(case / "test.yaml")
    assert m.requires == ["mysql"] and m.purpose
    raw = _raw(name)
    assert "exact" not in raw and "lifecycle" not in raw
    spec, = raw["assert"]["data"]
    assert spec["target_db"] == "mysql-target" and spec["target"].startswith("${MYSQL_TARGET_SCHEMA}.${TID}")
    _header, rows = _golden(name)
    assert spec["rows"] == len(rows) and spec["match"] == "expected/rows.csv"
    from livetest.assertions.data import parse_data_specs
    parse_data_specs(raw["assert"]["data"])
    tql = (SAMPLES / name / "app.tql").read_text()
    assert "CREATE NAMESPACE ${NS};" in tql and "START APPLICATION ${APP};" in tql
    for f in ("ddl_source.sql", "ddl_target.sql"):
        assert _columns((SAMPLES / name / f).read_text()) == _header


def test_mysql_initial_load_golden_is_the_seed():
    _header, rows = _golden("05-mysql-initial-load")
    seed = re.findall(r"\(\s*(\d+)\s*,\s*'([^']*)'\s*,\s*(\d+)\s*\)",
                      (SAMPLES / "05-mysql-initial-load" / "seed.sql").read_text())
    assert [(r["id"], r["name"], r["stock"]) for r in rows] == seed
    assert _raw("05-mysql-initial-load")["seed"][0]["when"] == "pre_deploy"


def test_mysql_cdc_golden_follows_the_changes():
    changes = (SAMPLES / "06-mysql-cdc" / "changes.sql").read_text()
    rows = {i: [c, s] for i, c, s in re.findall(r"\(\s*(\d+)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)", changes)}
    for status, i in re.findall(r"SET status = '([^']*)' WHERE id = (\d+)", changes):
        rows[i][1] = status
    for i in re.findall(r"DELETE FROM \S+ WHERE id = (\d+)", changes):
        del rows[i]
    _header, golden = _golden("06-mysql-cdc")
    assert {r["id"]: [r["customer"], r["status"]] for r in golden} == rows
    assert _raw("06-mysql-cdc")["seed"] == [{"file": "changes.sql", "db": "mysql-source", "when": "post_start"}]
    assert "StartPositionByName: true" in (SAMPLES / "06-mysql-cdc" / "app.tql").read_text()


@pytest.mark.parametrize("name", list(ASSETS) + list(MYSQL))
def test_readme_shows_how_to_run_it(name):
    assert f"striim-test run samples/live/{name}" in (SAMPLES / name / "README.md").read_text()
