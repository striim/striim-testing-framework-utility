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



def test_kafka_nothing_sample_and_regression_share_the_delivery_contract():
    name = "18-kafka-nothing"
    root = SAMPLES / name
    m = load_manifest(root / "test.yaml")
    assert m.lifecycle.sink == "kafka" and m.requires == ["postgres", "kafka"]
    observe = m.lifecycle.sentinel["observe"]
    assert observe == {"db": "kafka", "topic": "${KAFKA_TGT_TOPIC}", "payload": "json",
                       "key": "value.data.id", "deletion": {"value.metadata.OperationName": "DELETE"}}
    check = m.assert_["data"][0]
    assert check["target"] == observe["topic"] and check["db"] == "kafka"
    assert check["nothing"] == {"window": "3s", "where": {"value.data.id": 1}}
    sql = (root / "rollback.sql").read_text()
    assert sql.startswith("BEGIN;") and "VALUES (1, 'rolled back')" in sql and sql.endswith("ROLLBACK;\n")
    twin = SAMPLES.parents[1] / "scripts/live/regression/framework/framework-kafka-nothing"
    for filename in ["app.tql", "ddl_source.sql", "slot.sql", "rollback.sql", "sentinel_insert.sql", "sentinel_delete.sql"]:
        assert (root / filename).read_bytes() == (twin / filename).read_bytes()
    tql = (root / "app.tql").read_text()
    assert "${KAFKA_TGT_TOPIC}" in tql and "Global.KafkaWriter" in tql and "Global.JSONFormatter" in tql
