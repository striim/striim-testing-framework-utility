"""The pull-request workflow runs the same three hermetic suites as the release tool and
CONTRIBUTING.md, with least privilege, pinned actions, no Docker and no Striim.

Three hand-kept lists of the same commands come apart one edit at a time; this keeps them one.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "hermetic-tests.yml"
_spec = importlib.util.spec_from_file_location("release", REPO / "tools" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def _workflow():
    assert WORKFLOW.is_file(), f"{WORKFLOW.relative_to(REPO)} is missing: nothing runs on a pull request"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _norm(where):
    return "." if where in ("", ".", "./") else where.strip("/")


def _release_suites():
    out = set()
    for where, argv in release.SUITES:
        assert argv[:2] == ["-m", "pytest"], argv
        out.add((_norm(where), argv[argv.index("-m", 2) + 1], argv[-1]))
    return out


def _contributing_suites():
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    out = set()
    for line in text.splitlines():
        m = re.search(r"(?:\(cd (\S+) && )?\S*python -m pytest -q -m '([^']+)' (\S+?)\)?$", line.strip())
        if m:
            out.add((_norm(m.group(1) or "."), m.group(2), m.group(3)))
    return out


def _workflow_suites(wf):
    job = wf["jobs"]["hermetic"]
    return {(_norm(s["directory"]), s["markers"], "tests") for s in job["strategy"]["matrix"]["include"]}


def test_the_workflow_release_tool_and_contributing_run_the_same_three_suites():
    suites = _workflow_suites(_workflow())
    assert len(suites) == 3, suites
    assert suites == _release_suites(), "tools/release.py SUITES differs from the workflow"
    assert suites == _contributing_suites(), "CONTRIBUTING.md's commands differ from the workflow"


def test_the_workflow_runs_each_suite_hermetically_and_counts_its_skips():
    wf = _workflow()
    assert set(wf[True]) >= {"pull_request"}, wf[True]                 # YAML reads `on:` as True
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["hermetic"]
    assert job["runs-on"].startswith("ubuntu-") and isinstance(job["timeout-minutes"], int)
    assert job["env"]["DOCKER_HOST"] == "unix:///nonexistent"
    for entry in job["strategy"]["matrix"]["include"]:
        assert isinstance(entry["max-skips"], int) and entry["max-skips"] >= 0, entry
    steps = job["steps"]
    setup = next(s for s in steps if s.get("uses", "").startswith("actions/setup-python@"))
    assert str(setup["with"]["python-version"]) == "3.12"
    run = "\n".join(s.get("run", "") for s in steps)
    assert "tools/make-test-venv.sh" in run
    assert "unset STRIIM_HOME" in run
    assert re.search(r"pytest -q -p no:cacheprovider -rs -m \"\$\{\{ matrix\.markers \}\}\" tests", run), run
    assert re.search(r"tools/check_skips\.py \S+ --max \$\{\{ matrix\.max-skips \}\}", run), run


def test_every_action_is_pinned_by_full_commit_sha_and_no_secret_is_read():
    text = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else _workflow()
    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.M)
    assert uses, "no actions found"
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a commit SHA"
    assert "secrets." not in text and "pull_request_target" not in text
