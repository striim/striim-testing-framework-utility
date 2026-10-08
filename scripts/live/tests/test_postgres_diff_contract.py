"""Contract checks for the production postgres-diff regression case."""
from __future__ import annotations

import math
import re
from pathlib import Path

from livetest import canon
from livetest.lifecycle import LifecycleSpec
from livetest.manifest import load_manifest
from livetest.plugin import substitute_targets
from livetest.substitute import render


CASE = Path(__file__).resolve().parents[1] / "regression/services/postgres/postgres-diff"
TOKENS = {
    "PG_SOURCE_SCHEMA": "qasource",
    "PG_TARGET_SCHEMA": "qatarget",
    "TID": "w307_",
}


def _manifest():
    return load_manifest(CASE / "test.yaml")


def _seeded_rows() -> int:
    seed = (CASE / "seed.sql").read_text()
    values = re.search(r"\bVALUES\b(.*);\s*$", seed, re.IGNORECASE | re.DOTALL)
    assert values, "seed must contain one finite INSERT ... VALUES statement"
    return len(re.findall(r"\(\s*'[^']+'\s*,\s*'[^']+'\s*\)", values.group(1)))


def _rendered_diff(manifest) -> dict:
    (spec,) = substitute_targets(manifest.assert_["diff"], TOKENS)
    return spec


def _exact_declaration(manifest):
    (kind, index, declaration), = manifest.exact["specs"]
    assert (kind, index) == ("diff", 0)
    return declaration


def test_postgres_diff_declares_initial_load_and_exact_contract():
    manifest = _manifest()
    spec = _rendered_diff(manifest)
    declaration = _exact_declaration(manifest)

    assert isinstance(manifest.lifecycle, LifecycleSpec)
    lifecycle = manifest.lifecycle
    assert lifecycle.version == 1
    assert lifecycle.mode == "initial-load"
    assert lifecycle.sink == "db"
    assert lifecycle.readiness["kind"] == "baseline-landed"
    assert lifecycle.completion["kind"] == "source-count"
    assert lifecycle.stability_s > 0
    assert math.isfinite(lifecycle.stability_s)
    assert 0 < lifecycle.readiness_s <= manifest.timeout
    assert 0 < lifecycle.completion_s <= manifest.timeout
    assert math.isfinite(lifecycle.readiness_s)
    assert math.isfinite(lifecycle.completion_s)
    assert lifecycle.reset == "owned"
    for phase in (lifecycle.readiness, lifecycle.completion):
        assert phase["source"]["db"] == spec["source_db"]
        assert render(phase["source"]["table"], TOKENS) == spec["source"]
        assert phase["target"]["db"] == spec["target_db"]
        assert render(phase["target"]["table"], TOKENS) == spec["target"]

    assert manifest.seed_files[0][:3] == ("postgres-source", "seed.sql", "pre_deploy")
    assert _seeded_rows() == 3

    assert manifest.exact["block"] == {"version": 1}
    (kind, index, declaration), = manifest.exact["specs"]
    assert (kind, index) == ("diff", 0)
    assert declaration.to_dict() == {
        "columns": {},
        "ignore": [],
        "keys": None,
        "order": "any",
        "order_by": None,
        "profile": "slt-canon/1",
    }

    assert spec["source"] == "qasource.w307_src"
    assert spec["target"] == "qatarget.w307_tgt"
    assert spec["source"] != spec["target"]
    assert spec["source_db"] == "postgres-source"
    assert spec["target_db"] == "postgres-target"
    assert spec["exact"] is True

    rows = [
        {"id": "1", "msg": "alpha"},
        {"id": "2", "msg": "bravo"},
        {"id": "3", "msg": "charlie"},
    ]
    positive = canon.compare(rows, list(reversed(rows)), declaration, expected_observed=True)
    assert positive["profile"] == canon.PROFILE
    assert positive["equal"]

    missing = canon.compare(rows, rows[:-1], declaration, expected_observed=True)
    assert not missing["equal"]
    assert missing["samples"]["missing"]

    extra = canon.compare(rows, rows + [rows[-1]], declaration, expected_observed=True)
    assert not extra["equal"]
    assert extra["samples"]["extra"]

    duplicate = canon.compare(rows, [rows[0], rows[0], rows[1]], declaration, expected_observed=True)
    assert not duplicate["equal"]
    assert duplicate["samples"]["missing"]
    assert duplicate["samples"]["extra"]
