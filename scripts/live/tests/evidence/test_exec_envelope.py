"""The complete envelope from a real ``livetest.plugin`` execution (pytester, the
tests/lifecycle/exec_harness.py fakes at the infrastructure edges only). The input snapshot, the send-site
recorder, the exact reads, the identities, redaction, validation, the exclusive write and the outcome folding
are real; the checks read what the run left on disk."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from livetest import evidence, resultschema

pytest_plugins = ["pytester"]   # run_case drives a fresh pytest process through pytester

# -p xdist.plugin assumes plugin autoload is off, as in striim-test's live child; with it on, xdist is
# registered twice and the child pytest cannot start.
XDIST_ENV = {"SLT_PARALLEL": "1"}   # run_case's child already has plugin autoload off

LIFECYCLE = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "lifecycle"



@pytest.fixture(autouse=True)
def _consumer_outside_git(monkeypatch, tmp_path):
    # inputs.source reads the consumer project (paths.project_root()), which defaults to this
    # checkout; point it at tmp_path so the envelope sees a project outside any git worktree.
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    # Host secrets (a password of Striim would rewrite the reasons asserted below) are kept from
    # the child by run_case itself; test_a_host_secret_does_not_reach_the_redactor.


def _h():
    import _slt_exec_harness as h
    return h


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _live_bytes(run) -> bytes:
    return b"".join(p.read_bytes() for p in sorted((run.root / "live").rglob("*")) if p.is_file())


def test_exec_blockless_missing_server_file_still_skips_without_a_cluster(run_case):
    """The input snapshot runs before the cluster is resolved; for a case without an exact: block a
    missing referenced input must not turn the no-cluster skip into a failure (the outcome at 2689e45)."""
    add = "server_files:\n  - {file: missing.csv, dest: '/tmp/${NS}-in/missing.csv', when: pre_deploy}\n"
    run = run_case("legacy", {"no_cluster": True}, edit=lambda y: y + add, env={"SLT_ALLOW_NO_CLUSTER": "1"})
    junit_status, _props, xml = run.junit()
    assert junit_status == "skipped" and run.ret == 0, run.text
    assert "no reachable Striim" in xml and "input-missing" not in run.text


def test_exec_fresh_complete_envelope_all_hashes_and_identities(run_case):
    run = run_case("initial-load", {"initial_load": True}, env={"SLT_INVOCATION_ID": "4f7c2a9e0b1d4c3e8a6f5b2d1c0e9f87"})
    env = _h()._agree(run, "passed", qualifies=True)
    [path] = (run.root / "live" / "evidence").glob("*/*/evidence.json")
    read = evidence.read(path)
    assert read.complete and read.qualifies
    case = run.root / "cases" / "initial-load"
    inputs = env["inputs"]
    assert inputs["caseAssets"] == {"manifestSha256": _sha(case / "test.yaml"), "tqlSha256": _sha(case / "app.tql"),
                                    "goldens": {"expected/tgt.csv": _sha(case / "expected" / "tgt.csv")},
                                    "files": {n: _sha(case / n) for n in ("ddl_source.sql", "ddl_target.sql", "seed.sql")}}
    sent = {(r["role"], r["name"]): r for r in inputs["renderedInputs"]}
    assert set(sent) == {("ddl", "ddl_source.sql"), ("ddl", "ddl_target.sql"), ("seed", "seed.sql")}
    assert all(r["uses"] == 1 for r in sent.values())
    tid = run.dump()["tid"]
    ddl = sent[("ddl", "ddl_source.sql")]
    assert ddl["templateSha256"] == _sha(case / "ddl_source.sql")
    rendered = (case / "ddl_source.sql").read_text().replace("${TID}", tid).encode()
    assert ddl["renderedSha256"] == "sha256:" + hashlib.sha256(rendered).hexdigest()
    assert inputs["bindings"]["TID"] == tid and inputs["source"] == {"reason": "not-a-git-worktree"}
    observed = env["runtime"]["striim"]["observed"]
    assert observed["version"] == "5.4.0.6" and observed["imageId"].startswith("sha256:") and observed["consoleReachable"]
    assert env["runtime"]["striim"]["expected"]["STRIIM_VERSION"] == "5.4.0.6"
    [svc] = env["runtime"]["services"]
    assert svc["name"] == "postgres" and svc["imageId"].startswith("sha256:") and svc["image"] == "slt-postgres:5.4.0.6"
    [comp] = env["data"]["comparisons"]
    assert comp["profile"] == "slt-canon/1" and comp["equal"] and comp["actual"]["owned"] and comp["index"] == 0
    assert env["integrity"] == {"goldens": {"expected/tgt.csv": {"inputSha256": _sha(case / "expected" / "tgt.csv"),
                                                                 "finalSha256": _sha(case / "expected" / "tgt.csv"),
                                                                 "unchanged": True}}, "evidenceErrors": []}
    assert env["reports"][0]["invocation"] == env["run"]["invocation"] == "4f7c2a9e0b1d4c3e8a6f5b2d1c0e9f87"
    assert env["run"]["nodeid"] == "cases/initial-load/test.yaml::initial-load"


def test_exec_evidence_write_failure_fails_the_run_rc1_junit_v1(run_case):
    blocked = []

    def prepare(root, name):
        d = root / "live" / "evidence"
        d.mkdir(parents=True)
        d.chmod(0o555)
        blocked.append(d)
    try:
        run = run_case("initial-load", {"initial_load": True}, prepare=prepare)
    finally:
        for d in blocked:
            d.chmod(0o755)
    assert run.ret == 1, run.text
    status, props, xml = run.junit()
    assert status == "error" and "slt_evidence_error" in props, xml
    assert run.sidecar()["status"] == "error"
    assert not list((run.root / "live").rglob("evidence.json"))


def test_exec_existing_envelope_is_evidence_error_not_overwrite(run_case):
    prior = []

    def prepare(root, name):
        p = root / "live" / "evidence" / "initial-load" / _h().RUN / "evidence.json"
        p.parent.mkdir(parents=True)
        p.write_text("prior evidence under review\n")
        prior.append(p)
    run = run_case("initial-load", {"initial_load": True}, prepare=prepare)
    assert run.ret == 1, run.text
    status, props, _xml = run.junit()
    assert status == "error" and props["slt_evidence_error"].startswith("evidence-exists")
    assert prior[0].read_text() == "prior evidence under review\n"


def test_exec_golden_changed_during_run_does_not_qualify(run_case):
    run = run_case("initial-load", {"initial_load": True, "rewrite_on_deploy": ["cases/initial-load/expected/tgt.csv"]})
    env = _h()._agree(run, "passed", qualifies=False)
    golden = env["integrity"]["goldens"]["expected/tgt.csv"]
    assert golden["unchanged"] is False and golden["inputSha256"] != golden["finalSha256"]
    assert env["data"]["comparisons"][0]["equal"] is True                 # compared against the snapshot bytes
    assert env["run"]["qualifiesReason"] == "golden changed during the run: expected/tgt.csv"


def test_exec_redacted_secret_absent_from_envelope_and_failure_text(run_case):
    secret = "hunter2-xyz-exec-planted"
    rows = [{"id": 1, "note": f"carries {secret}"}, {"id": 2, "note": "b"}, {"id": 3, "note": "c"}]
    run = run_case("initial-load", {"initial_load": True, "rows": {"qatarget.<TID>tgt": rows}},
                   env={"SLT_PG_TARGET_PASSWORD": secret})
    env = _h()._agree(run, "failed")
    assert secret.encode() not in _live_bytes(run)
    assert evidence.REDACTED in json.dumps(env["data"]["comparisons"][0]["samples"])
    assert evidence.REDACTED in run.junit()[2] and secret not in run.junit()[2]


def test_a_secret_a_test_sets_itself_reaches_the_child(run_case, monkeypatch):
    """run_case removes the host's secrets, not the test's: one set with monkeypatch.setenv is still a known secret
    in the child, and redacted there."""
    secret = "hunter2-xyz-monkeypatched"
    monkeypatch.setenv("SLT_PG_TARGET_PASSWORD", secret)
    rows = [{"id": 1, "note": f"carries {secret}"}, {"id": 2, "note": "b"}, {"id": 3, "note": "c"}]
    run = run_case("initial-load", {"initial_load": True, "rows": {"qatarget.<TID>tgt": rows}})
    env = _h()._agree(run, "failed")
    assert secret.encode() not in _live_bytes(run)
    assert evidence.REDACTED in json.dumps(env["data"]["comparisons"][0]["samples"])


def test_exec_decimal_declaration_with_a_colliding_secret_qualifies(run_case):
    """A known password that collides with canonical type syntax (``decimal`` against the
    declared ``decimal:2``) must not be redacted out of the declaration the validator and ``declarationSha256`` read.
    The real guard, plugin and writer keep the passing case at rc 0 with qualifying evidence, and the password is
    still absent from every evidence byte."""
    run = run_case("initial-load", {"initial_load": True}, env={"REVIEW_PASSWORD": "decimal"},
                   edit=lambda y: y.replace("id: integer", "id: decimal:2"))
    env = _h()._agree(run, "passed", qualifies=True)
    [comp] = env["data"]["comparisons"]
    assert comp["declaration"]["columns"] == {"id": "decimal:2"} and comp["equal"] is True
    assert b"[redacted]:2" not in _live_bytes(run)


def test_exec_op_case_without_observed_artifact_does_not_qualify(run_case):
    run = run_case("initial-load", {"initial_load": True, "op_stub": True}, env={"SLT_OPS_PRELOADED": "1"},
                   edit=lambda y: y.replace("requires: [postgres]\n", "requires: [postgres]\nop: {jar: java/OpenProcessors/StubOp}\n"))
    env = run.envelope()
    assert env["inputs"]["productBuild"] == {"unobserved": "op/udf artifact bytes are not observed at upload (artifact capture is unavailable)"}
    assert env["run"]["qualifies"] is False
    assert env["run"]["status"] != "passed" or env["run"]["qualifiesReason"] == "not observed or unreadable: inputs.productBuild"


def test_exec_services_image_unreadable_is_unobserved(run_case):
    run = run_case("initial-load", {"initial_load": True, "inspect_fail": ["slt-postgres"]})
    env = _h()._agree(run, "passed", qualifies=False)
    [svc] = env["runtime"]["services"]
    assert "unobserved" in svc["image"] and "slt-postgres" in svc["image"]["unobserved"]
    assert env["run"]["qualifiesReason"] == "not observed or unreadable: runtime.services[0].image"


def test_exec_v1_sidecar_unchanged_and_valid(run_case):
    run = run_case("initial-load", {"initial_load": True})
    env = _h()._agree(run, "passed", qualifies=True)
    doc = json.loads((run.root / "live" / "junit.slt.json").read_text())
    resultschema.validate(doc)
    [record] = doc["tests"]
    assert set(record) == {"name", "nodeid", "status", "topology", "services", "duration", "skip_reason", "assertions"}
    assert [a["type"] for a in record["assertions"]] == ["smoke", "data"]
    projected = evidence.to_v1(env)
    assert projected == {k: record[k] for k in projected}


def test_exec_xdist_worker_evidence_error_fails_the_controller_run(run_case):
    """an xdist worker's evidence-write failure reaches the controller's
    session outcome, so a parallel run cannot end green."""
    blocked = []

    def prepare(root, name):
        d = root / "live" / "evidence"
        d.mkdir(parents=True)
        d.chmod(0o555)
        blocked.append(d)
    try:
        run = run_case("legacy", {}, prepare=prepare, args=("-n", "2", "-p", "xdist.plugin"), env=XDIST_ENV)
    finally:
        for d in blocked:
            d.chmod(0o755)
    assert run.ret == 1, run.text
    status, props, xml = run.junit()
    assert status == "error" and "Permission denied" in props["slt_evidence_error"], xml
    assert not list((run.root / "live").rglob("evidence.json"))


def test_exec_sidecar_write_failure_fails_the_run(run_case):
    """A v1 sidecar that cannot be written is an evidence error that fails the run."""
    def prepare(root, name):
        (root / "live" / "junit.slt.json").mkdir(parents=True)
    run = run_case("initial-load", {"initial_load": True}, prepare=prepare)
    assert run.ret == 1, run.text
    xml = (run.root / "live" / "junit.xml").read_text()
    assert 'property name="slt_evidence_error" value="evidence error: session: sidecar-write-failed:' in xml, xml


@pytest.mark.parametrize("when", ["pre_deploy", "post_start", "load"])
def test_exec_server_file_records_the_transferred_bytes(run_case, when):
    """The server-file record hashes the bytes handed to the transfer edge, even when the case
    file changes after the input snapshot and before the send."""
    name = "payload.jar" if when == "load" else "payload.txt"
    entry = (f"  - file: {name}\n    dest: {name}\n    load: true\n" if when == "load"
             else f"  - file: {name}\n    dest: ${{OWNED_DIR}}/{name}\n    when: {when}\n")
    rel = f"cases/initial-load/{name}"

    def prepare(root, case):
        (root / rel).write_text("id\n1\n")
    rewrite = {"rewrite_on_deploy": [rel]} if when == "post_start" else {"rewrite_on_resolve": [rel]}
    run = run_case("initial-load", {"initial_load": True, **rewrite}, prepare=prepare,
                   edit=lambda y: y + "\nserver_files:\n" + entry)
    assert run.ret == 0, run.text
    sent = [s for e in run.events() if e["event"] in ("placed", "uploaded")
            for s in ([e["sha"]] if isinstance(e["sha"], str) else e["sha"])]
    [record] = [r for r in run.envelope()["inputs"]["renderedInputs"] if r["role"] == "server-file"]
    assert sent == [record["renderedSha256"]] == ["sha256:" + hashlib.sha256(b"id\n9\n").hexdigest()], (sent, record)
    assert record["templateSha256"] != record["renderedSha256"]


def test_exec_image_label_disagrees_with_runtime_version_does_not_qualify(run_case):
    """The image tag is image metadata; the observed runtime version decides, and a runtime
    that is not the expected release does not qualify."""
    run = run_case("initial-load", {"initial_load": True, "runtime_version": "5.4.3"})
    env = _h()._agree(run, "passed", qualifies=False)
    observed = env["runtime"]["striim"]["observed"]
    assert observed["version"] == "5.4.3" and observed["image"] == "slt-striim:5.4.0.6", observed
    assert env["run"]["qualifiesReason"] == "the running Striim version 5.4.3 is not the expected release 5.4.0.6"


@pytest.mark.parametrize("dpkg", ["5.4.0.6", None], ids=["dpkg", "jar-only"])
def test_exec_real_runtime_probe_at_the_expected_release_qualifies(run_case, dpkg):
    """The real runtime_version probe runs; the fake container answers the fixed script."""
    listing = "Platform-5.4.0.6.jar\nStriimParser-5.4.0.6.jar\nslt-dpkg\n" + (f"{dpkg}\n" if dpkg else "")
    run = run_case("initial-load", {"initial_load": True, "runtime_probe": "real", "runtime_listing": listing})
    env = _h()._agree(run, "passed", qualifies=True)
    assert env["runtime"]["striim"]["observed"]["version"] == "5.4.0.6"
    assert [e for e in run.events() if e["event"] == "runtime_probe"] == [{"event": "runtime_probe", "container": env["runtime"]["striim"]["observed"]["container"]}]


def test_exec_real_runtime_probe_at_another_release_does_not_qualify(run_case):
    """A runtime answering 5.4.3 is observed as 5.4.3 and is not the expected release."""
    run = run_case("initial-load", {"initial_load": True, "runtime_probe": "real",
                                    "runtime_listing": "Platform-5.4.3.jar\nslt-dpkg\n5.4.3\n"})
    env = _h()._agree(run, "passed", qualifies=False)
    assert env["runtime"]["striim"]["observed"]["version"] == "5.4.3"
    assert env["run"]["qualifiesReason"] == "the running Striim version 5.4.3 is not the expected release 5.4.0.6"


@pytest.mark.parametrize("source", ["shell", "machine-settings", "framework-dotenv"])
def test_a_host_secret_does_not_reach_the_redactor(run_case, monkeypatch, tmp_path, source):
    """A host holds a password of "Striim" -- in its shell, in the machine settings file under its XDG_CONFIG_HOME,
    or in the framework checkout's own .env -- and the redactor would mask "Striim" in every reason (a licence name of
    Striim is public, #66, but a password is not). run_case keeps all three from the child."""
    prepare = None
    if source == "shell":
        monkeypatch.setenv("STRIIM_PASS", "Striim")
    elif source == "machine-settings":
        machine = tmp_path / "xdg" / "striim-test" / "machine.env"
        machine.parent.mkdir(parents=True)
        machine.write_text("STRIIM_PASS=Striim\n")
        machine.chmod(0o600)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    else:
        # The framework checkout's .env, simulated inside the child: its default project root holds STRIIM_PASS.
        checkout = tmp_path / "framework-checkout"
        checkout.mkdir()
        (checkout / ".env").write_text("STRIIM_PASS=Striim\n")

        def prepare(root, name):
            with (root / "conftest.py").open("a") as f:
                f.write(f"\nimport livetest.paths as _slt_paths\n"
                        f"_slt_paths._default_project_root = lambda: Path({str(checkout)!r})\n")
    run = run_case("initial-load", {"initial_load": True, "runtime_version": "5.4.3"}, prepare=prepare)
    env = _h()._agree(run, "passed", qualifies=False)
    assert env["run"]["qualifiesReason"] == "the running Striim version 5.4.3 is not the expected release 5.4.0.6"


def test_exec_resolved_credential_redacted_when_service_setup_fails(run_case):
    """A credential that only the service resolution returned (never in the environment)
    is a known secret before service setup runs, so a setup failure quoting it is redacted in the written envelope."""
    secret = "r2-only-resolved-password"

    def prepare(root, name):
        conf = root / "conftest.py"
        conf.write_text(conf.read_text() + (
            "\n_real_resolve_secret = I.resolve_service\n"
            "def _resolve_with_secret(*args, **kw):\n"
            "    resolved = _real_resolve_secret(*args, **kw)\n"
            f"    resolved.base['source_password'] = {secret!r}\n"
            "    return resolved\n"
            "I.resolve_service = _resolve_with_secret\n"
            "def _setup_fails_quoting_the_password(self):\n"
            "    raise RuntimeError('setup failed: password=' + self.dsn['source_password'])\n"
            "P.PgAdmin.ensure_setup = _setup_fails_quoting_the_password\n"))
    run = run_case("initial-load", {"initial_load": True}, prepare=prepare)
    assert run.ret == 1, run.text
    env = run.envelope()
    assert env["run"]["status"] in ("failed", "error") and evidence.REDACTED in env["run"]["failure"], env["run"]
    assert secret.encode() not in b"".join(p.read_bytes() for p in (run.root / "live" / "evidence").rglob("*") if p.is_file())
