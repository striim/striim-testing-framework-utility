"""The byte-level ggtrail oracle tests decode generated trail files with Striim's own parser
(livetest.ggtrail.harness). They need STRIIM_HOME and the harness source a test repo ships, so in
the framework's own checkout they skip: the reason must say what is missing and how to run them,
and nothing in those files may pass without the oracle."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from livetest.ggtrail import harness

ORACLE_FILES = [f"tests/test_ggtrail_{n}.py" for n in (
    "ddl", "delete_update", "e2e_decode", "harness", "header", "insert", "keymove_tokens", "truncate_tokens", "writer")]
LIVE = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_harness_dir", lambda: tmp_path / "tools" / "ggtrail-harness")
    return tmp_path / "tools" / "ggtrail-harness"


def test_reason_names_both_gaps_and_how_to_run(monkeypatch, no_harness):
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    reason = harness.oracle_skip_reason()
    assert reason.startswith("ggtrail Java oracle not run: STRIIM_HOME is unset; ggtrail harness not in this project")
    assert reason.endswith(harness.ORACLE_HOW_TO_RUN)
    for needed in ("export STRIIM_HOME=", "SLT_PROJECT_ROOT", "tools/ggtrail-harness/TrailDumpHarness.java"):
        assert needed in reason


def test_reason_names_the_harness_when_striim_home_is_set(monkeypatch, no_harness):
    monkeypatch.setenv("STRIIM_HOME", str(no_harness.parent))
    reason = harness.oracle_skip_reason()
    assert "STRIIM_HOME is unset" not in reason
    assert f"no {no_harness / 'TrailDumpHarness.java'}" in reason and reason.endswith(harness.ORACLE_HOW_TO_RUN)


def test_reason_names_striim_home_when_the_harness_is_shipped(monkeypatch, no_harness):
    no_harness.mkdir(parents=True)
    (no_harness / "TrailDumpHarness.java").write_text("class TrailDumpHarness {}\n")
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    assert harness.oracle_skip_reason() == (
        "ggtrail Java oracle not run: STRIIM_HOME is unset. " + harness.ORACLE_HOW_TO_RUN)


def test_no_reason_when_striim_home_and_the_harness_are_present(monkeypatch, no_harness):
    """The state a test repo's gate must reach for the oracle tests to run."""
    no_harness.mkdir(parents=True)
    (no_harness / "TrailDumpHarness.java").write_text("class TrailDumpHarness {}\n")
    monkeypatch.setenv("STRIIM_HOME", str(no_harness.parent))
    assert harness.oracle_skip_reason() is None


def test_every_oracle_test_skips_here_and_says_how_to_run_it(tmp_path):
    """In this checkout (no harness source) every test that needs the oracle skips, each with a
    reason that says how to run it; the hermetic tests in the same files still pass."""
    env = {k: v for k, v in os.environ.items() if k not in ("STRIIM_HOME", "GGTRAIL_TESTDATA", "SLT_PROJECT_ROOT")}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "-o", "addopts=", "-m", "not live",
         *ORACLE_FILES], cwd=LIVE, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    reasons = re.findall(r"^SKIPPED \[(\d+)\] (\S+?):\d+: (.*)$", proc.stdout, re.M)
    assert sum(int(n) for n, _, _ in reasons) == 20, proc.stdout[-3000:]
    for _, where, reason in reasons:
        if where == "tests/test_ggtrail_harness.py":
            # Striim's own parser test data, from the product repo: its own skip names the setting.
            assert reason.endswith("-- set GGTRAIL_TESTDATA to its TestData/data directory"), reason
            continue
        # (a .env that sets SLT_PROJECT_ROOT to a test repo can supply the harness, not STRIIM_HOME)
        assert reason.startswith("ggtrail Java oracle not run: STRIIM_HOME is unset"), reason
        assert reason.endswith(harness.ORACLE_HOW_TO_RUN), reason
    summary = proc.stdout.strip().splitlines()[-1]
    assert re.search(r"\b20 skipped\b", summary) and "failed" not in summary and "error" not in summary, summary
