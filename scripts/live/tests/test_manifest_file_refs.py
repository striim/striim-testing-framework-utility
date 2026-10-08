"""Every file a manifest names must exist under the root the runner will read it from.

Three manifests shipped naming a file that was not there: lookup-gated-hidden-mem-kill
pointed at a config_hidden_recovery.json that had never existed in any commit, and the two
lookup-ab-throughput arms named gated_pg_target_ddl.sql, which lives in the OP's
`examples/` dir while those two arms root at `scripts/live/fixtures/`. Nothing caught it:
`example:` resolution happens at RUN time, so the error needs a provisioned cluster to
surface -- roughly 25 minutes per arm, one arm at a time. Worse, an arm that cannot start
is an arm that never ran, and a never-run arm is indistinguishable in a tier summary from
one that passed -- the same "a skip reads as a pass" shape.

Resolution goes through load_manifest().source_dir rather than re-reading `example:` here,
so this test cannot drift from where the runner actually reads. ConfigParseGuardTest (the
Java side) parses example and integration configs, but not scripts/live/fixtures, so a live
fixture's CONTENT is still only validated by running the arm; this test covers existence.
"""
from __future__ import annotations

import pathlib

import pytest

from livetest.manifest import load_manifest

LIVE_ROOT = pathlib.Path(__file__).resolve().parents[1]

MANIFESTS = sorted(LIVE_ROOT.glob("*/**/test.yaml"))


def _refs(m) -> list[str]:
    """Every per-test file the manifest names, as written."""
    refs = [m.tql]
    refs += [f for _db, f in m.ddl_files]
    refs += [f for _db, f, _when, _after in m.seed_files]
    refs += [u["from"] for u in m.op_uploads if u.get("from")]
    return [r for r in refs if isinstance(r, str)]


def test_the_tree_has_manifests():
    # A glob that silently matches nothing would make every assertion below vacuous. A test repo
    # pins its own corpus; this repo ships hello/, framework/, services/ and examples/.
    assert len(MANIFESTS) > 20, f"only {len(MANIFESTS)} manifests found -- glob wrong?"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_every_referenced_file_exists(path):
    m = load_manifest(path)
    root = m.source_dir or path.parent
    missing = sorted({r for r in _refs(m) if not (root / r).exists()})
    assert not missing, (
        f"{path.parent.name}: names {len(missing)} file(s) absent from {root}: "
        f"{missing}. The arm cannot start, and a never-run arm reads as a pass."
    )
