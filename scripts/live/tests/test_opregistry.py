import pytest

from livetest.opregistry import ensure_registered


# --- validating the record against the server, instead of wiping it ---------------------
# The registry used to be deleted at the start of every run, because a record kept on the
# host cannot see a `compose down -v` that destroyed the jar inside the container. Clearing
# was the safe direction but it made the content-hash diff useless across runs: every run
# re-registered every OP jar, byte-identical or not, and each re-registration is a
# destroy-then-recreate of shared cluster state. `verify` closes that hole directly -- ask
# the server whether it still has the OP, and only then trust the record.

def test_a_matching_record_is_trusted_when_the_server_confirms_it(tmp_path):
    calls = []
    ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", lambda: calls.append("first"))
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1",
                             lambda: calls.append("again"), verify=lambda: True) is False
    assert calls == ["first"], "a confirmed record must not reload"


def test_a_matching_record_is_re_registered_when_the_server_lost_it(tmp_path):
    # The `compose down -v` case the old clear() existed for: record says loaded, cluster
    # says otherwise. Asking is strictly better than assuming either way.
    calls = []
    ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", lambda: calls.append("first"))
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1",
                             lambda: calls.append("recovered"), verify=lambda: False) is True
    assert calls == ["first", "recovered"]


def test_an_unknown_verify_answer_trusts_the_record(tmp_path):
    # A transient LIST failure must not be read as "nothing is loaded" -- that would turn one
    # unreachable moment into a cluster-wide reload of every OP jar.
    calls = []
    ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", lambda: calls.append("first"))
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1",
                             lambda: calls.append("again"), verify=lambda: None) is False
    assert calls == ["first"]


def test_verify_is_not_consulted_without_a_matching_record(tmp_path):
    # No record means we do not know what bytes the server holds, so its own answer to
    # "is something loaded under this name" cannot authorise a skip.
    asked = []
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", lambda: None,
                             verify=lambda: asked.append(1) or True) is True
    assert asked == [], "verify only ever upgrades a record, never substitutes for one"


# --- remembering a failure, so it is not retried once per test --------------------------
# do_register() runs BEFORE the record is written, so a failed LOAD left nothing behind and
# every later test in the run re-attempted the same destructive reload. One broken jar cost
# one UNLOAD+LOAD per remaining test -- 28 of them in a single observed run.

def test_one_failure_still_gets_a_retry(monkeypatch, tmp_path):
    # ONE failure must not stop the next caller trying. do_register() spans a 900s lock wait,
    # a docker cp and an HTTP call -- a single LockTimeout from one worker waiting out a slow
    # drain would otherwise fail every remaining test for that jar without any of them
    # attempting anything, where before they retried and typically succeeded.
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-a")
    monkeypatch.delenv("SLT_OP_RETRY_FAILED", raising=False)
    attempts = []
    def boom():
        attempts.append(1)
        raise RuntimeError("timed out waiting for the exclusive lock")
    with pytest.raises(RuntimeError, match="timed out"):
        ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    assert attempts == [1, 1], "the second caller must get a real attempt"


def test_a_failed_registration_is_re_raised_without_touching_the_server_again(monkeypatch,
                                                                             tmp_path):
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-a")
    monkeypatch.delenv("SLT_OP_RETRY_FAILED", raising=False)
    attempts = []
    def boom():
        attempts.append(1)
        raise RuntimeError("LOAD OPEN PROCESSOR failed: ZipFile invalid LOC header")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="invalid LOC header"):
            ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    assert attempts == [1, 1]
    # Two identical failures is evidence, not noise: from here the storm is suppressed.
    with pytest.raises(RuntimeError, match="invalid LOC header"):
        ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    assert attempts == [1, 1], "the third caller must fail fast, not reload again"


def test_a_remembered_failure_expires_with_the_run(monkeypatch, tmp_path):
    # Scoped to the run ON PURPOSE. do_register() spans a 900s lock wait, a docker cp and an
    # HTTP call, so most of what fails there is transient -- lock starvation, a container
    # mid-restart, a reset connection, a poisoned loader a JVM restart clears. Remembering
    # those against the BYTES alone would let one bad moment brick every test for that jar
    # until someone deleted the registry by hand, including on a brand-new cluster.
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-a")
    monkeypatch.delenv("SLT_OP_RETRY_FAILED", raising=False)
    def boom(): raise RuntimeError("transient")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-b")
    calls = []
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1",
                             lambda: calls.append("fresh run")) is True
    assert calls == ["fresh run"]


def test_a_new_fingerprint_clears_a_remembered_failure(monkeypatch, tmp_path):
    # A rebuild is the fix for most load failures, so new bytes must always get a real try.
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-a")
    monkeypatch.delenv("SLT_OP_RETRY_FAILED", raising=False)
    def boom(): raise RuntimeError("nope")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    calls = []
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp2",
                             lambda: calls.append("retry")) is True
    assert calls == ["retry"]


def test_a_falsy_retry_flag_does_not_disable_the_memory(monkeypatch, tmp_path):
    # A bare truthiness test read "0" as ON, and for a flag that DISABLES a safety memory
    # that is the wrong way to be wrong.
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-a")
    def boom(): raise RuntimeError("nope")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    monkeypatch.setenv("SLT_OP_RETRY_FAILED", "0")
    attempts = []
    with pytest.raises(RuntimeError, match="not retried"):
        ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1",
                          lambda: attempts.append(1))
    assert attempts == []


def test_the_environment_can_force_a_retry_within_one_run(monkeypatch, tmp_path):
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-a")
    monkeypatch.delenv("SLT_OP_RETRY_FAILED", raising=False)
    def boom(): raise RuntimeError("nope")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    monkeypatch.setenv("SLT_OP_RETRY_FAILED", "1")
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", lambda: None) is True


def test_the_failure_message_names_the_way_out(monkeypatch, tmp_path):
    # A dead end whose message names neither the flag nor the file is a support ticket.
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-a")
    monkeypatch.delenv("SLT_OP_RETRY_FAILED", raising=False)
    def boom(): raise RuntimeError("nope")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    with pytest.raises(RuntimeError) as ei:
        ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    msg = str(ei.value)
    assert "SLT_OP_RETRY_FAILED" in msg and ".slt-op-registry.json" in msg


def test_a_success_clears_a_remembered_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-a")
    monkeypatch.delenv("SLT_OP_RETRY_FAILED", raising=False)
    def boom(): raise RuntimeError("nope")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", boom)
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", lambda: None,
                             retry_failed=True) is True
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", lambda: None,
                             verify=lambda: True) is False


def test_a_remembered_failure_does_not_disturb_the_success_map(tmp_path):
    import json
    def boom(): raise RuntimeError("nope")
    ensure_registered(tmp_path, "A.jar", "fpA", lambda: None)
    with pytest.raises(RuntimeError):
        ensure_registered(tmp_path, "B.jar", "fpB", boom)
    data = json.loads((tmp_path / ".slt-op-registry.json").read_text())
    assert data["A.jar"] == "fpA"
    assert "B.jar" not in data, "a failure is not a registration"


def test_registers_once_across_calls(tmp_path):
    calls = []
    def reg(): calls.append("x")
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", reg) is True
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", reg) is False  # already there
    assert calls == ["x"]                                                     # registered once


def test_reregisters_when_fingerprint_changes(tmp_path):
    calls = []
    ensure_registered(tmp_path, "FooOp-5.4.jar", "fp1", lambda: calls.append(1))
    assert ensure_registered(tmp_path, "FooOp-5.4.jar", "fp2", lambda: calls.append(2)) is True
    assert calls == [1, 2]


def test_registry_file_is_json_map(tmp_path):
    import json
    ensure_registered(tmp_path, "A.jar", "fpA", lambda: None)
    ensure_registered(tmp_path, "B.jar", "fpB", lambda: None)
    data = json.loads((tmp_path / ".slt-op-registry.json").read_text())
    assert data == {"A.jar": "fpA", "B.jar": "fpB"}


def test_registry_defaults_to_the_machine_wide_lock_dir(monkeypatch, tmp_path):
    # The record describes what a CLUSTER has loaded, and the cluster is machine-wide --
    # so the record has to be too. It used to live in the checkout (scripts/live), which
    # left two agents in separate checkouts or worktrees each keeping their own view of
    # what was loaded, so the second redundantly re-ran UNLOAD + LOAD OPEN PROCESSOR for a
    # jar already loaded at the identical fingerprint. LOAD replaces the artifact for every
    # app on the server, so that reload was never free.
    #
    # The in-use lock beside it was ALREADY machine-wide for exactly this reason
    # (_use_lock's docstring: "so separate checkouts/worktrees pointed at one cluster
    # actually serialise"); this pins the registry to the same home.
    from livetest import opregistry, stack
    # NB patch the module global, not the env var: stack._LOCK_DIR is bound at IMPORT
    # (Path(os.environ.get("SLT_LOCK_DIR", "/tmp/slt-locks"))), so setenv here would do
    # nothing and the test would silently be asserting against the real /tmp/slt-locks.
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")

    assert opregistry.registry_path().parent == tmp_path / "locks", \
        "the default registry must sit in the machine-wide lock dir, not the checkout"
    # and it must land beside the in-use lock it coordinates with
    assert opregistry.registry_path().parent == opregistry._use_lock().parent


def test_two_checkouts_share_one_registry_and_register_once(monkeypatch, tmp_path):
    # The behaviour the move buys: two callers that pass NOTHING (production shape) see one
    # record, so the second skips the reload entirely. Passing a directory still isolates --
    # that override is what the tmp_path-based tests above rely on.
    from livetest import opregistry, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    calls = []
    assert opregistry.ensure_registered(None, "Shared-5.4.jar", "fp1",
                                        lambda: calls.append("checkout-a")) is True
    assert opregistry.ensure_registered(None, "Shared-5.4.jar", "fp1",
                                        lambda: calls.append("checkout-b")) is False
    assert calls == ["checkout-a"], "the second checkout must not reload an identical jar"

    # A DIFFERENT branch means different bytes means a different fingerprint, and only one
    # build of a package can be loaded at a time -- so that one still reloads, by design.
    assert opregistry.ensure_registered(None, "Shared-5.4.jar", "fp2",
                                        lambda: calls.append("other-branch")) is True
    assert calls == ["checkout-a", "other-branch"]


def test_two_striim_urls_do_not_share_a_record(monkeypatch, tmp_path):
    # The hole prefix-scoping alone cannot close. SLT_STACK_PREFIX identifies the cluster
    # only while the cluster IS this checkout's compose stack -- compose.yaml prefixes the
    # project name and every container_name, so two stacks at one prefix collide in docker
    # before any record is read. Point one checkout at a native install or a remote server
    # via STRIIM_URL and that stops holding: two clusters, one prefix, one shared record.
    #
    # The failure is the dangerous direction -- the second checkout reads the first's
    # record and SKIPS a load its own server never had, which is the "loaded? no" state
    # that ends in a wedged cluster and needs a restart to clear.
    from livetest import opregistry, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    a = {"STRIIM_URL": "http://localhost:9080"}
    b = {"STRIIM_URL": "http://otherbox:9080"}

    assert opregistry.registry_path(None, a) != opregistry.registry_path(None, b)
    # the in-use lock must move WITH it, or two clusters would serialise against each
    # other's reloads for no reason
    assert opregistry._use_lock(None, a) != opregistry._use_lock(None, b)
    # ...and the same address must land on the same record, or nothing is ever shared
    assert opregistry.registry_path(None, a) == opregistry.registry_path(None, dict(a))


def test_unset_striim_url_keeps_the_historical_filename(monkeypatch, tmp_path):
    # Back-compat: the overwhelmingly common path (compose stack, no STRIIM_URL) must keep
    # the exact name it had before the cluster tag existed, so an in-flight run's records
    # are not orphaned by upgrading.
    from livetest import opregistry, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    assert opregistry.registry_path(None, {}).name == ".slt-op-registry.json"
    assert opregistry.registry_path(None, {"SLT_STACK_PREFIX": "alt"}).name \
        == ".alt-slt-op-registry.json"


def test_cluster_tag_is_stable_and_short(monkeypatch):
    from livetest import opregistry
    t1 = opregistry.cluster_tag({"STRIIM_URL": "http://h:9080"})
    t2 = opregistry.cluster_tag({"STRIIM_URL": "http://h:9080"})
    assert t1 == t2 and len(t1) == 8          # stable across calls, short enough to read
    assert opregistry.cluster_tag({}) == ""   # default stack contributes nothing


def test_an_explicit_default_url_shares_with_an_unset_one(monkeypatch, tmp_path):
    # The framework DERIVES http://<SLT_SERVICES_HOST|localhost>:9080 when STRIIM_URL is
    # unset (plugin.py), so a checkout that sets STRIIM_URL explicitly to that same value is
    # pointed at the very same cluster and must land on the SAME record.
    #
    # Hashing the raw variable rather than the RESOLVED one split those apart: one checkout
    # got .slt-op-registry.json and the other .slt-op-registry-4d140184.json, silently
    # un-sharing the two and reinstating the redundant reload this module exists to remove.
    # It failed in the safe direction, which is exactly why it needed a test rather than a
    # bug report.
    from livetest import opregistry, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    unset = opregistry.registry_path(None, {})
    assert opregistry.registry_path(None, {"STRIIM_URL": "http://localhost:9080"}) == unset
    assert opregistry.registry_path(None, {"STRIIM_URL": "http://localhost:9080/"}) == unset, \
        "a trailing slash is the same cluster"
    # ...and a genuinely different address still separates
    assert opregistry.registry_path(None, {"STRIIM_URL": "http://localhost:9180"}) != unset

    # The same equivalence must hold on a non-default services host, since that is what the
    # default URL is built from.
    e = {"SLT_SERVICES_HOST": "box2"}
    assert opregistry.registry_path(None, dict(e, STRIIM_URL="http://box2:9080")) == \
        opregistry.registry_path(None, e)


def test_ensure_registered_locks_a_per_cluster_file(monkeypatch, tmp_path):
    # ensure_registered touches THREE files: the registry JSON, the FileLock guarding writes
    # to it, and (via the caller) the in-use reader-writer lock. All three must carry the
    # same (prefix, cluster) scoping, and the reason is not tidiness.
    #
    # Scope the LOCK but not the REGISTRY and two clusters write the SAME json while holding
    # two DIFFERENT locks -- concurrent read-modify-write with no mutual exclusion, i.e.
    # lost registrations, which is the corruption the lock exists to prevent.
    #
    # This asserts the CALL SITE, not the helper. A first version of this test called
    # _scoped(_LOCK, env) directly and passed happily against a mutant that reverted
    # ensure_registered to a bare stack.lock_path(_LOCK) -- it proved the helper was
    # consistent while the caller ignored it. Capturing the path FileLock is actually
    # constructed with is what makes it discriminate.
    from livetest import opregistry, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    seen = []

    class _RecordingLock:
        def __init__(self, path, **kw): seen.append(path)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(opregistry, "FileLock", _RecordingLock)

    # cluster_tag reads os.environ at CALL time (unlike stack._LOCK_DIR, which is bound at
    # import), so setenv is the right tool for this half.
    monkeypatch.delenv("STRIIM_URL", raising=False)
    opregistry.ensure_registered(tmp_path / "a", "X.jar", "fp", lambda: None)
    monkeypatch.setenv("STRIIM_URL", "http://otherbox:9080")
    opregistry.ensure_registered(tmp_path / "b", "X.jar", "fp", lambda: None)

    assert len(seen) == 2
    assert seen[0] != seen[1], \
        "two clusters must not guard their registries with the same lock file"
    assert opregistry.cluster_tag({"STRIIM_URL": "http://otherbox:9080"}) in seen[1]


def test_a_failed_registration_drops_the_old_record(tmp_path):
    # An OP registration UNLOADs the OP's other builds before its LOAD; if the LOAD then fails,
    # the old build's record must not vouch for a cluster that no longer has it.
    ensure_registered(tmp_path, "op:FooOp", "fp1", lambda: None)

    def fail():
        raise RuntimeError("LOAD failed")
    with pytest.raises(RuntimeError):
        ensure_registered(tmp_path, "op:FooOp", "fp2", fail)
    calls = []
    assert ensure_registered(tmp_path, "op:FooOp", "fp1", lambda: calls.append("again"),
                             verify=lambda: True) is True
    assert calls == ["again"]
