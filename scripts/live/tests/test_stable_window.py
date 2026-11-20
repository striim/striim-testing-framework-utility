"""`stable_seconds` is a finite, bounded, non-negative number of seconds on every tier that has it.

The window starts at the first pass and is held in full, past `timeout` if it must, so an
infinite window never returns and a very large one is a hang in practice; `nan` compares false
against everything, so `stable_for > 0` is false and the requested window is silently skipped.
YAML spells these `.inf`, `-.inf` and `.nan`. They are refused when the manifest loads, which is
also what `striim-test doctor` checks, and by each tier's own spec parser.

The upper bound is the one the framework already applies to a Postgres statement budget: what
fits in a signed 32-bit count of milliseconds (2,147,483.647 s, a little under 25 days).
"""
from __future__ import annotations

import math

import pytest
import yaml

from livetest.assertions.data import DataSpecError, parse_data_specs
from livetest.assertions.diff import DiffSpecError, parse_diff_specs
from livetest.assertions.file import FileSpecError, parse_file_specs
from livetest.assertions.json import JsonSpecError, parse_json_specs
from livetest.manifest import ManifestError, load_manifest

MAX_S = 2_147_483.647

#: tier -> (an otherwise valid spec as YAML flow text, its parser, the parser's error)
TIERS = {
    "data": ("{target: s.t, target_db: postgres-target, rows: 1}", parse_data_specs, DataSpecError),
    "diff": ("{source: s.src, target: s.tgt}", parse_diff_specs, DiffSpecError),
    "json": ("{target: t, db: postgres-target, key: [id], column: doc, match: expected/doc.json}",
             parse_json_specs, JsonSpecError),
    "file": ("{path: /o, min_events: 1}", parse_file_specs, FileSpecError),
}

#: YAML scalars that must be refused: non-finite, and beyond the bound.
BAD = [".inf", "-.inf", ".nan", ".NaN", "+.inf", "2147484", "1.0e+308", "1" + "0" * 400]


def _spec(tier: str, stable: str) -> dict:
    flow = TIERS[tier][0][:-1] + f", stable_seconds: {stable}}}"
    return yaml.safe_load(flow)


@pytest.mark.parametrize("stable", BAD)
@pytest.mark.parametrize("tier", sorted(TIERS))
def test_the_tier_parser_refuses_a_non_finite_or_unbounded_window(tier, stable):
    _flow, parse, error = TIERS[tier]
    spec = _spec(tier, stable)
    v = spec["stable_seconds"]
    assert isinstance(v, (int, float)) and not isinstance(v, bool)
    assert (isinstance(v, float) and not math.isfinite(v)) or v > MAX_S
    with pytest.raises(error, match="stable_seconds"):
        parse([spec])


@pytest.mark.parametrize("stable", ["0", "0.0", "2.5", "30", "2147483.647"])
@pytest.mark.parametrize("tier", sorted(TIERS))
def test_the_tier_parser_accepts_a_finite_window_up_to_the_bound(tier, stable):
    _flow, parse, _error = TIERS[tier]
    assert parse([_spec(tier, stable)])


def _case(tmp_path, tier: str, stable: str):
    """A test.yaml written as text, so the YAML scalar reaches the loader as an author wrote it."""
    case = tmp_path / f"win-{tier}"
    case.mkdir()
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    flow = TIERS[tier][0][:-1] + f", stable_seconds: {stable}}}"
    (case / "test.yaml").write_text(
        "name: win\npurpose: hermetic\ntql: app.tql\nrequires: [postgres]\n"
        f"assert:\n  {tier}:\n    - {flow}\n")
    return case / "test.yaml"


@pytest.mark.parametrize("stable", [".inf", "-.inf", ".nan", "2147484"])
@pytest.mark.parametrize("tier", sorted(TIERS))
def test_the_manifest_load_refuses_a_non_finite_or_unbounded_window(tmp_path, tier, stable):
    with pytest.raises(ManifestError, match=rf"assert\.{tier}\[0\].*stable_seconds"):
        load_manifest(_case(tmp_path, tier, stable))


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_the_manifest_load_accepts_a_finite_window(tmp_path, tier):
    m = load_manifest(_case(tmp_path, tier, "2.5"))
    assert m.assert_[tier][0]["stable_seconds"] == 2.5
