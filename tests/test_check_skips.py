"""tools/check_skips.py: a hermetic suite whose skips grow past its budget fails the job.

A skip is green, so a test that stopped running because a tool or file went missing goes unseen
unless something counts them. xfails are not skips.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "check_skips.py"

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5" skipped="3">
<testcase classname="t" name="ran"/>
<testcase classname="t" name="needs_java"><skipped type="pytest.skip" message="requires STRIIM_HOME for the Java oracle">x</skipped></testcase>
<testcase classname="t" name="needs_mvn"><skipped type="pytest.skip" message="mvn not found">x</skipped></testcase>
<testcase classname="t" name="known_bug"><skipped type="pytest.xfail" message="known defect">x</skipped></testcase>
<testcase classname="t" name="failed"><failure message="boom">x</failure></testcase>
</testsuite></testsuites>
"""


def _run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=60)


def test_skips_within_the_budget_pass_and_are_listed(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT)
    r = _run(str(junit), "--max", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 skipped (budget 2)" in r.stdout
    assert "requires STRIIM_HOME for the Java oracle" in r.stdout and "known defect" not in r.stdout


def test_skips_over_the_budget_fail_the_job(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT)
    r = _run(str(junit), "--max", "1")
    assert r.returncode == 1
    assert "2 skipped (budget 1)" in r.stdout and "mvn not found" in r.stdout


def test_a_missing_or_unreadable_report_is_an_error_not_a_pass(tmp_path):
    missing = _run(str(tmp_path / "absent.xml"), "--max", "5")
    assert missing.returncode == 2 and "absent.xml" in missing.stderr, missing.stderr
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites>")
    unreadable = _run(str(bad), "--max", "5")
    assert unreadable.returncode == 2 and "bad.xml" in unreadable.stderr, unreadable.stderr
