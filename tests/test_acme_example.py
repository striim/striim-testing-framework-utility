"""The worked example (examples/acme-retail) does offline what its README says, in a copy.

A copy of the example stands in for a reader's repository: its validator passes, `striim-test list`
selects its three cases and `run --dry-run` selects one, all without Striim, Docker or a network.
The validator still catches a wrong golden, and still passes after PROMPTS.md step 6 adds a fourth
case. The example's validator also passes with the engine at the tag its framework.pin names.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "cli"))
from _clikit import clean_env, framework_env, run_cli  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ACME = REPO / "examples" / "acme-retail"
TEMPLATE = REPO / "templates" / "consumer-repo"


@pytest.fixture
def acme(tmp_path):
    """The example copied out of the framework, as the README's setup does."""
    repo = tmp_path / "striim-work" / "acme-retail-tests"
    shutil.copytree(ACME, repo, ignore=shutil.ignore_patterns(".state", ".env", ".venv", "__pycache__"))
    return repo


def _validate(repo, env=None):
    return subprocess.run([sys.executable, "scripts/validate-example.py"], cwd=repo,
                          env=framework_env() if env is None else env,
                          capture_output=True, text=True, timeout=300)


def _live_ids(result):
    return [line for line in result.ids() if line.startswith("live:")]


def test_the_readme_offline_steps_pass_in_a_copy(acme):
    r = _validate(acme)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().endswith("PASS: 3 manifests and all offline contract checks"), r.stdout
    r = run_cli(["list", "--targets", "gold-targets.yaml"], cwd=acme)
    assert r.rc == 0, r.stdout + r.stderr
    assert _live_ids(r) == ["live:tests/live/customers-load-cdc::customers-load-cdc",
                            "live:tests/live/events-kafka::events-kafka",
                            "live:tests/live/orders-cdc::orders-cdc"], r.stdout
    r = run_cli(["run", "tests/live/orders-cdc", "--targets", "gold-targets.yaml", "--dry-run"], cwd=acme)
    assert r.rc == 0, r.stdout + r.stderr
    assert "live:tests/live/orders-cdc::orders-cdc" in r.stdout, r.stdout
    assert (acme / ".state" / "runs").is_dir()          # where gold-targets.yaml's stateDir says


def test_the_validator_catches_a_wrong_golden(acme):
    golden = acme / "tests/live/orders-cdc/expected/rows.csv"
    assert "118.00" in golden.read_text()
    golden.write_text(golden.read_text().replace("118.00", "120.25"))
    r = _validate(acme)
    assert r.returncode != 0
    assert "FAIL: acme-orders-cdc" in r.stdout + r.stderr


def _add_orders_cancel(repo):
    """What PROMPTS.md step 6 asks for: a new case for the existing orders app."""
    new = repo / "tests/live/orders-cancel"
    shutil.copytree(repo / "tests/live/orders-cdc", new)
    manifest = (new / "test.yaml").read_text()
    for old, value in (("name: acme-orders-cdc", "name: acme-orders-cancel"),
                       ("fixtures/changes.sql", "fixtures/cancel_changes.sql")):
        assert old in manifest
        manifest = manifest.replace(old, value)
    (new / "test.yaml").write_text(manifest)
    (repo / "apps/orders/fixtures/cancel_changes.sql").write_text(
        "BEGIN;\nINSERT INTO ${TID}orders (order_id, customer_id, amount, status) VALUES\n"
        "  (2001, 3001, 40.00, 'placed'),\n  (2002, 3002, 15.00, 'placed');\nCOMMIT;\n"
        "BEGIN;\nUPDATE ${TID}orders SET status = 'cancelled' WHERE order_id = 2001;\n"
        "UPDATE ${TID}orders SET status = 'shipped' WHERE order_id = 2002;\nCOMMIT;\n")
    # 2001 is inserted placed and updated to cancelled; 2002 is inserted placed and updated to shipped.
    (new / "expected/rows.csv").write_text(
        "order_id,customer_id,amount,status\n2001,3001,40.00,cancelled\n2002,3002,15.00,shipped\n")
    return new


def test_the_validator_passes_after_the_documented_sixth_prompt(acme):
    assert "tests/live/orders-cancel" in (ACME / "PROMPTS.md").read_text()
    _add_orders_cancel(acme)
    r = run_cli(["list", "--targets", "gold-targets.yaml"], cwd=acme)
    assert r.rc == 0 and "live:tests/live/orders-cancel::orders-cancel" in r.stdout, r.stdout + r.stderr
    r = _validate(acme)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[ ok ] acme-orders-cancel:" in r.stdout
    assert r.stdout.rstrip().endswith("PASS: 4 manifests and all offline contract checks"), r.stdout


def test_the_validator_checks_a_new_case_like_the_shipped_ones(acme):
    new = _add_orders_cancel(acme)
    golden = new / "expected/rows.csv"
    golden.write_text(golden.read_text().replace("2001,3001,40.00,cancelled", "2001,3001,40.00,placed"))
    r = _validate(acme)
    assert r.returncode != 0 and "FAIL: acme-orders-cancel" in r.stdout + r.stderr, r.stdout + r.stderr


def test_the_validator_checks_requires_against_the_routes_a_case_uses(acme):
    # The services a case needs follow from the databases its files and assertions name.
    manifest = acme / "tests/live/orders-cdc/test.yaml"
    manifest.write_text(manifest.read_text().replace("requires: [postgres, mysql]", "requires: [postgres]"))
    r = _validate(acme)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "FAIL:" in out and "acme-orders-cdc" in out and "requires" in out, out


def test_the_validator_refuses_a_case_whose_app_is_not_under_apps(acme):
    new = _add_orders_cancel(acme)
    shutil.copytree(acme / "apps/orders", acme / "legacy/orders")
    manifest = new / "test.yaml"
    manifest.write_text(manifest.read_text().replace("example: apps/orders", "example: legacy/orders"))
    r = _validate(acme)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "FAIL:" in out and "acme-orders-cancel" in out and "under apps/" in out, out


def test_the_example_ignores_what_the_template_ignores():
    # RELEASE-NOTES.md tells every consumer repo to ignore .release-build-*.xml; the example was
    # copied from the template and must keep every line of its .gitignore.
    template = set((TEMPLATE / ".gitignore").read_text().split())
    example = set((ACME / ".gitignore").read_text().split())
    assert ".release-build-*.xml" in template
    assert template <= example, template - example


def test_template_and_example_share_their_script_manifest_agents_and_pin():
    for name in ("scripts/sync-framework.py", "gold-targets.yaml", "AGENTS.md", "framework.pin"):
        assert (TEMPLATE / name).read_bytes() == (ACME / name).read_bytes(), name


def _pinned_tag():
    pin = (ACME / "framework.pin").read_text().split()[0]
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet", f"refs/tags/{pin}^{{commit}}"],
                       capture_output=True, text=True, timeout=60)
    return pin, r.returncode == 0


_ENGINE = ("scripts/live/livetest", "scripts/live/services", "scripts/integration/inttest",
           "scripts/cli/striim_test")


def test_the_example_validates_with_the_engine_at_its_pin(acme, tmp_path):
    # A reader copies the example from the default branch and installs the framework at the pin;
    # the example's manifests must load and its validator pass with that engine, not this one.
    pin, present = _pinned_tag()
    if not present:
        pytest.skip(f"framework.pin names {pin}, which is not a tag in this checkout (a clone without tags)")
    engine = tmp_path / "engine"
    engine.mkdir()
    archive = tmp_path / "engine.tar"
    subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "-o", str(archive), pin, *_ENGINE],
                   check=True, capture_output=True, timeout=120)
    with tarfile.open(archive) as tar:
        tar.extractall(engine, filter="data")
    pythonpath = ":".join(str(engine / parent) for parent in ("scripts/live", "scripts/integration", "scripts/cli"))
    env = clean_env(PYTHONPATH=pythonpath, PYTHONNOUSERSITE="1")
    probe = subprocess.run([sys.executable, "-c", "import livetest; print(livetest.__file__)"], cwd=acme, env=env,
                           capture_output=True, text=True, timeout=60)
    assert probe.stdout.strip().startswith(str(engine)), probe.stdout + probe.stderr
    r = _validate(acme, env)
    assert r.returncode == 0, f"the example does not validate with the engine at {pin}:\n{r.stdout}{r.stderr}"
    assert "PASS: 3 manifests and all offline contract checks" in r.stdout
