"""The framework tier's completeness gate.

`regression/framework/` is a gallery: one minimal, runnable test per feature of the manifest
schema, doubling as the documentation you copy from. A gallery only stays useful if adding a
feature without an example is an error, so this module is that error.

It is hermetic -- it reads manifests, never a cluster -- and it fails in exactly two ways:

  * a manifest key (or assert tier) is exercised by no framework example, and is not on the
    EXEMPT list below;
  * an EXEMPT entry names something that is now covered, or no longer exists -- so the list
    cannot quietly outlive its reason.

The second direction matters as much as the first. An exemption list nobody prunes is how a
coverage gate turns into a rubber stamp.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from livetest.manifest import VALID_MANIFEST_KEYS, load_manifest

FRAMEWORK = pathlib.Path(__file__).resolve().parents[1] / "regression" / "framework"

# Assert tiers a manifest can declare. Kept as a literal rather than imported: this is the
# checklist the gallery is measured against, so it should change only when someone edits it
# deliberately, not because a refactor moved a set around.
ASSERT_TIERS = {"smoke", "data", "diff", "file", "gcs", "json", "monitor", "jmx"}

# Features with no framework example YET. Every entry states what would have to be built,
# because "why is this exempt" is the question a reader arrives with. Shrinking this list to
# empty is the definition of done for the tier.
EXEMPT = {
    # --- manifest keys ---
    "example": "Option C pointer at a shipped OP/UDF example dir; needs a module example to point at",
    "op": "builds and loads an OpenProcessor jar (~1 min), with udf",
    "udf": "builds and loads a UDF jar, with op",
    "jmx": ("needs a plugin that registers an MBean; a test repo with such an OP runs it"),
    "generate": (
        "the HOOK is already covered hermetically -- test_generate_hook.py has 12 cases over "
        "placement, phase filtering, dest rendering and unknown-kind failure. The live "
        "demonstration is services/ggtrail/ggtrail-cdc-file-diff, which runs the ggtrail kind "
        "end to end; a framework/ example would repeat it. Add one when a second generator "
        "kind lands."
    ),
    "topology": "every example is topology: single; a cluster example needs >=2 nodes + an agent",
    "requires": "covered implicitly by every example (postgres); no example varies it on purpose",
    "timeout": "covered implicitly by every example; nothing varies it to demonstrate the effect",
    "tags": "covered implicitly by every example",
    "purpose": "covered implicitly by every example (R3 requires it)",
    "name": "structural",
    "tql": "structural",
    "assert": "structural -- the tiers inside it are checked separately",
    "ddl": "covered implicitly by every example; no example demonstrates routing on its own",
    "lifecycle": (
        "the deterministic lifecycle block (C7.2) is covered hermetically by tests/lifecycle; a live "
        "framework example needs positive readiness and completion on a real cluster, which live qualification "
        "qualifies"
    ),
    "exact": (
        "exact data semantics (C8, slt-canon/1) are covered hermetically by tests/evidence; a live framework "
        "example needs a real Postgres target and a real FileWriter output, which live qualification checks"
    ),
    # --- assert tiers ---
    "gcs": "GCS object assertions, needs the emulator",
    "json": "JSON column assertions, needs the Spanner emulator",
}


def _manifests():
    return sorted(FRAMEWORK.glob("*/test.yaml"))


def _raw(path):
    return yaml.safe_load(path.read_text()) or {}


def test_framework_tier_exists_and_loads():
    paths = _manifests()
    assert paths, f"no framework examples found under {FRAMEWORK}"
    for p in paths:
        load_manifest(p)          # raises with the file named if the gallery itself rots


def test_every_example_is_tagged_framework():
    # The tag is how a runner and a hand-run `-k framework` select the tier.
    for p in _manifests():
        assert "framework" in (_raw(p).get("tags") or []), f"{p}: missing the 'framework' tag"


def test_every_example_declares_a_purpose():
    # R3 applies here for the same reason it applies to op/udf examples, and doubly so for a
    # gallery: purpose IS the caption under the exhibit.
    for p in _manifests():
        purpose = _raw(p).get("purpose")
        assert isinstance(purpose, str) and purpose.strip(), f"{p}: needs a non-empty purpose"


def _covered_keys():
    return {k for p in _manifests() for k in _raw(p)}


def _covered_tiers():
    return {t for p in _manifests() for t in (_raw(p).get("assert") or {})}


@pytest.mark.parametrize("key", sorted(VALID_MANIFEST_KEYS))
def test_every_manifest_key_has_an_example(key):
    if key in EXEMPT:
        pytest.skip(f"exempt: {EXEMPT[key]}")
    assert key in _covered_keys(), (
        f"manifest key {key!r} is accepted by the loader but no framework example uses it.\n"
        f"Add one under {FRAMEWORK}/, or add {key!r} to EXEMPT in this file with the reason."
    )


@pytest.mark.parametrize("tier", sorted(ASSERT_TIERS))
def test_every_assert_tier_has_an_example(tier):
    if tier in EXEMPT:
        pytest.skip(f"exempt: {EXEMPT[tier]}")
    assert tier in _covered_tiers(), (
        f"assert tier {tier!r} has no framework example.\n"
        f"Add one under {FRAMEWORK}/, or add {tier!r} to EXEMPT in this file with the reason."
    )


def test_exemptions_are_still_needed():
    # The reverse direction: an exemption for something now covered is stale, and one naming a
    # key the loader no longer accepts is a leftover from a schema change.
    covered = _covered_keys() | _covered_tiers()
    known = VALID_MANIFEST_KEYS | ASSERT_TIERS
    structural = {"name", "tql", "assert", "ddl", "purpose", "requires", "timeout", "tags",
                  "topology"}
    for key, reason in sorted(EXEMPT.items()):
        assert key in known, f"EXEMPT names {key!r}, which is not a manifest key or assert tier"
        if key in structural:
            continue          # exempt precisely BECAUSE every example carries them
        assert key not in covered, (
            f"EXEMPT still lists {key!r} ({reason}), but a framework example now uses it -- "
            f"drop the exemption."
        )

def test_the_recover_mode_trio_shares_identical_fixtures():
    """framework-recover{,-quiesce,-kill} must differ ONLY by `mode:`.

    The three cases are a matched set: their whole evidentiary value is that stop, quiesce and
    kill ran against the same app, the same DDL and the same seed. `example:` would have
    expressed the sharing structurally, but it is documented as an OP/UDF example pointer, so
    the fixtures are copies -- and copies drift. Each manifest says they must stay identical;
    this is the only thing that enforces it.
    """
    base = FRAMEWORK / "framework-recover"
    for peer in ("framework-recover-quiesce", "framework-recover-kill"):
        for name in ("app.tql", "ddl_source.sql", "ddl_target.sql", "seed.sql", "slot.sql"):
            assert (FRAMEWORK / peer / name).read_bytes() == (base / name).read_bytes(), (
                f"{peer}/{name} has drifted from framework-recover/{name} -- the mode "
                f"comparison is no longer a comparison. Change both, or neither."
            )

