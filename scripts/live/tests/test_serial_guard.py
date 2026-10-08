import os
import types

import pytest

from livetest.plugin import pytest_configure


def _cfg(nprocs=None, dist=None):
    return types.SimpleNamespace(option=types.SimpleNamespace(numprocesses=nprocs, dist=dist))


def test_serial_guard_blocks_xdist_by_default(monkeypatch):
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    with pytest.raises(pytest.UsageError, match="SLT_PARALLEL"):
        pytest_configure(_cfg(nprocs=4))


def test_serial_guard_allows_xdist_when_opted_in(monkeypatch):
    monkeypatch.setenv("SLT_PARALLEL", "1")
    pytest_configure(_cfg(nprocs=4))          # must NOT raise
    pytest_configure(_cfg(dist="load"))       # must NOT raise


def test_serial_guard_still_noop_without_xdist(monkeypatch):
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    pytest_configure(_cfg())                  # no -n/--dist -> no raise


def test_configure_does_not_wipe_the_op_registry(monkeypatch, tmp_path):
    # The wipe is what made the content-hash diff useless across runs: the registry started
    # empty every session, so the first test needing each OP jar always re-registered it --
    # an UNLOAD + LOAD OPEN PROCESSOR of cluster-wide state, byte-identical or not. The
    # stale-record hazard it guarded against is now handled by validating each record against
    # LIST LIBRARIES (plugin._loaded_jar_probe) instead of discarding all of them.
    from livetest import opregistry, plugin, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    monkeypatch.delenv("SLT_OPS_PRELOADED", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(plugin, "clear_provision_registry", lambda *a, **k: None)

    opregistry.ensure_registered(None, "FooOp-5.4.jar", "fp1", lambda: None)
    pytest_configure(_cfg())
    assert opregistry.registry_path().exists(), \
        "a prior run's OP registrations must survive pytest_configure"
    calls = []
    assert opregistry.ensure_registered(None, "FooOp-5.4.jar", "fp1",
                                        lambda: calls.append("reload"),
                                        verify=lambda: True) is False
    assert calls == []


def test_configure_stamps_a_run_epoch_shared_by_xdist_workers(monkeypatch, tmp_path):
    # The epoch scopes REMEMBERED FAILURES to one run (opregistry._current_run). Stamped on the
    # CONTROLLER before any worker spawns, so every worker agrees on which run it is in and one
    # worker's failure suppresses the others' retries of the same bytes.
    from livetest import opartifacts, plugin, opregistry, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(plugin, "clear_provision_registry", lambda *a, **k: None)
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    monkeypatch.delenv("SLT_OPS_PRELOADED", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.delenv("SLT_RUN_EPOCH", raising=False)

    pytest_configure(_cfg())
    first = opregistry._current_run()
    assert first and first not in ("0", "unknown")
    pytest_configure(_cfg())
    assert opregistry._current_run() == first, \
        "a worker must not re-stamp its controller's epoch"


def test_a_worker_does_not_clear_the_provision_registry(monkeypatch, tmp_path):
    from livetest import plugin, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_PARALLEL", "1")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    cleared = []
    monkeypatch.setattr(plugin, "clear_provision_registry", lambda *a, **k: cleared.append(1))
    pytest_configure(_cfg(nprocs=2))
    assert cleared == []


def test_the_loaded_probe_retries_once_before_giving_up(monkeypatch):
    # One blip while the nodes are still settling must not be read as "cannot tell" and so
    # trust a stale record: right after a `down -v` + fresh bring-up that would skip both the
    # upload and the LOAD, and the app would deploy against a jar the cluster never had.
    from livetest import plugin
    monkeypatch.setattr(plugin.time, "sleep", lambda *_: None)
    calls = []

    class _Client:
        def loaded_libraries(self, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("503 while settling")
            return {"fooop-5.4.jar"}

    assert plugin._loaded_jar_probe(_Client(), "FooOp-5.4.jar", cache={})() is True
    assert len(calls) == 2


def test_the_loaded_probe_says_cannot_tell_and_caches_nothing_on_failure(monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin.time, "sleep", lambda *_: None)

    class _Dead:
        def loaded_libraries(self, timeout=None):
            raise RuntimeError("connection refused")

    cache = {}
    assert plugin._loaded_jar_probe(_Dead(), "FooOp-5.4.jar", cache=cache)() is None
    assert cache == {}, "a failure must not be memoised as an answer"


def test_the_loaded_probe_memoises_the_answer(monkeypatch):
    # ensure_registered calls this while holding the machine-wide registry lock, and
    # post_tungsten_line's timeout is long -- so a wedged-but-listening Striim must not
    # get to stall every worker once per jar per test.
    from livetest import plugin
    calls = []

    class _Client:
        def loaded_libraries(self, timeout=None):
            calls.append(1)
            return {"fooop-5.4.jar", "barop-5.4.jar"}

    cache = {}
    c = _Client()
    assert plugin._loaded_jar_probe(c, "FooOp-5.4.jar", cache=cache)() is True
    assert plugin._loaded_jar_probe(c, "BarOp-5.4.jar", cache=cache)() is True
    assert len(calls) == 1, "a PRESENT answer is served from the memo"

    # An ABSENT answer is re-probed instead (see the cross-worker test below), so it costs a
    # call -- but a registration recorded via _note_jar_loaded turns it into a memo hit again,
    # which is what stops a False verdict repeating for every remaining test in the process.
    assert plugin._loaded_jar_probe(c, "NopeOp-5.4.jar", cache=cache)() is False
    assert len(calls) == 2
    plugin._note_jar_loaded("NopeOp-5.4.jar", cache=cache)
    assert plugin._loaded_jar_probe(c, "NopeOp-5.4.jar", cache=cache)() is True
    assert len(calls) == 2


def test_restarting_the_app_nodes_drops_every_op_record(monkeypatch, tmp_path):
    # The one staleness the probe cannot see: LOAD puts a PropertyTemplate in the MDR, which
    # is persistent and outlives a `docker restart`, while the ModuleClassLoader it registered
    # dies with the JVM. So the cluster would still answer "loaded" for every OP on a node that
    # can no longer instantiate any of them. Verified live: after restarting the nodes, three
    # jars still collided on LOAD, i.e. their templates were still there.
    from livetest import opregistry, stack, striim_provision as sp
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    opregistry.ensure_registered(None, "FooOp-5.4.jar", "fp1", lambda: None)
    assert opregistry.registry_path().exists()
    sp.restart_app_nodes(None, run=lambda argv: None)
    assert not opregistry.registry_path().exists(), \
        "a node restart invalidates every OP registration, undetectably -- so drop them all"


def test_an_unset_epoch_is_per_process_not_a_shared_constant(monkeypatch):
    # The dangerous shape: a CONSTANT fallback matches forever across every future run, so a
    # remembered failure would never expire -- the permanent brick that run-scoping exists to
    # avoid. Reachable by any caller without the stamp: an ad-hoc script, a non-pytest entry
    # point, or a path registering under SLT_OPS_PRELOADED (which pytest_configure skips).
    from livetest import opregistry
    monkeypatch.delenv("SLT_RUN_EPOCH", raising=False)
    first = opregistry._current_run()
    assert first not in ("", "0", "unknown")
    assert opregistry._current_run() == first, "stable once stamped, within the process"


def test_the_probe_memos_do_not_leak_between_stack_prefixes(monkeypatch):
    """Two prefixed stacks on one machine are two CLUSTERS, and both env vars that identify
    one (SLT_STACK_PREFIX, STRIIM_URL) are read at call time -- so a process-wide memo keyed
    only by jar name served one stack's answer to the other.

    The leak was in the dangerous direction: stack "alt" would be told a jar was loaded that
    only the default stack had, skip both the upload and the LOAD, and deploy against a jar
    its own cluster never received. Each stack has its own registry file already, so the memos
    key on that.
    """
    from livetest import plugin

    class _Client:
        def __init__(self, libs): self.libs = libs
        def loaded_libraries(self, timeout=None): return set(self.libs)

    monkeypatch.setattr(plugin, "_LOADED_JARS_CACHE", {})
    monkeypatch.delenv("STRIIM_URL", raising=False)

    monkeypatch.delenv("SLT_STACK_PREFIX", raising=False)
    assert plugin._loaded_jar_probe(_Client({"foo-5.4.jar"}), "foo-5.4.jar")() is True

    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    assert plugin._loaded_jar_probe(_Client(set()), "foo-5.4.jar")() is False, \
        "a second stack must not inherit the first stack's loaded-jar answer"

    # ...and a registration on one stack must not mark the jar loaded on the other.
    plugin._note_jar_loaded("bar-5.4.jar")
    monkeypatch.delenv("SLT_STACK_PREFIX", raising=False)
    assert plugin._loaded_jar_probe(_Client(set()), "bar-5.4.jar")() is False


def test_the_generation_is_never_memoised(monkeypatch):
    """Caching it produced a cross-process ping-pong: whoever called restart_app_nodes kept
    the PRE-restart token for the rest of the session and wrote records keyed fp@G1, while
    another process on the same cluster computed fp@G2. Each read the other's record as a
    mismatch and re-registered on every test -- an unbounded reload storm strictly worse than
    the once-per-run reload this work removes."""
    from livetest import plugin, striim_provision as sp
    monkeypatch.delenv("STRIIM_URL", raising=False)
    monkeypatch.delenv("SLT_STACK_PREFIX", raising=False)
    seen = []

    def _fake_generation():
        seen.append(1)
        return f"gen{len(seen)}"

    monkeypatch.setattr(sp, "app_nodes_generation", _fake_generation)
    assert plugin._cluster_generation() == "gen1"
    assert plugin._cluster_generation() == "gen2", "a restart must be seen, not remembered"


def test_the_generation_is_only_trusted_for_the_local_compose_stack(monkeypatch):
    """app_nodes_generation inspects LOCAL containers by name and knows nothing about
    STRIIM_URL. Against a remote or native Striim it would fold an unrelated local stack's
    start times into the key -- restarting the wrong Striim would invalidate every record
    while restarting the real one invalidated none."""
    from livetest import plugin, striim_provision as sp
    monkeypatch.setattr(sp, "app_nodes_generation", lambda: "local-gen")
    monkeypatch.delenv("SLT_STACK_PREFIX", raising=False)
    monkeypatch.delenv("STRIIM_URL", raising=False)
    assert plugin._cluster_generation() == "local-gen"
    monkeypatch.setenv("STRIIM_URL", "http://otherbox:9080")
    assert plugin._cluster_generation() == "", \
        "a cluster whose containers we cannot inspect gets no generation scope"


def test_the_key_falls_back_to_run_scope_without_a_generation(monkeypatch, tmp_path):
    """Dropping the scope entirely would leave every record trusted across a restart the probe
    cannot see -- and LIST LIBRARIES would confirm them, because the MDR outlives the JVM.
    Run-scoping is what the deleted session-start wipe provided."""
    import types
    from livetest import opartifacts, plugin
    jar = _jar(tmp_path / "Foo-5.4.jar", [("a/A.class", "class A")], (2026, 1, 1, 0, 0, 0))
    built = types.SimpleNamespace(sha256="ignored", path=jar, name="Foo-5.4.jar")
    fp = opartifacts.jar_content_fingerprint(jar)

    monkeypatch.setattr(plugin, "_cluster_generation", lambda: "")
    monkeypatch.setenv("SLT_RUN_EPOCH", "run-xyz")
    assert plugin._registry_key(built) == f"{fp}@run-run-xyz"

    monkeypatch.setattr(plugin, "_cluster_generation", lambda: "gen9")
    assert plugin._registry_key(built) == f"{fp}@gen9"


# --- the fingerprint must survive a rebuild ---------------------------------------------
# These builds are NOT reproducible: no module pom sets project.build.outputTimestamp, so
# Maven stamps every zip entry with the build time and two `mvn clean package` runs over an
# unchanged tree emit different sha256. A raw file digest therefore reported "the jar changed"
# after any rebuild, and a changed jar MUST be re-registered -- so running the suite twice
# against one cluster reloaded every OP jar the second time for nothing, and that reload is
# what wedges Striim's OP loader.

def _jar(path, entries, when):
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in entries:
            z.writestr(zipfile.ZipInfo(name, date_time=when), body)
    return path


def test_the_fingerprint_ignores_build_timestamps(tmp_path):
    from livetest import opartifacts, plugin
    entries = [("a/A.class", "class A" * 100), ("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")]
    early = _jar(tmp_path / "early.jar", entries, (2026, 1, 1, 0, 0, 0))
    later = _jar(tmp_path / "later.jar", entries, (2026, 8, 21, 22, 9, 0))
    assert early.read_bytes() != later.read_bytes(), "the two jars must really differ on disk"
    assert opartifacts.jar_content_fingerprint(early) == opartifacts.jar_content_fingerprint(later), \
        "a rebuild of identical sources must not look like a changed jar"


def test_the_fingerprint_still_moves_on_a_real_change(tmp_path):
    from livetest import opartifacts, plugin
    when = (2026, 1, 1, 0, 0, 0)
    base = _jar(tmp_path / "base.jar", [("a/A.class", "class A")], when)
    edited = _jar(tmp_path / "edited.jar", [("a/A.class", "class B")], when)
    renamed = _jar(tmp_path / "renamed.jar", [("a/B.class", "class A")], when)
    added = _jar(tmp_path / "added.jar",
                 [("a/A.class", "class A"), ("a/C.class", "class C")], when)
    fp = opartifacts.jar_content_fingerprint
    assert fp(base) != fp(edited), "changed content must be seen"
    assert fp(base) != fp(renamed), "a renamed entry must be seen"
    assert fp(base) != fp(added), "an added entry must be seen"


def test_the_fingerprint_ignores_entry_order(tmp_path):
    # Shade does not guarantee a stable entry order across builds either.
    from livetest import opartifacts, plugin
    when = (2026, 1, 1, 0, 0, 0)
    one = _jar(tmp_path / "one.jar", [("a.class", "A"), ("b.class", "B")], when)
    two = _jar(tmp_path / "two.jar", [("b.class", "B"), ("a.class", "A")], when)
    assert opartifacts.jar_content_fingerprint(one) == opartifacts.jar_content_fingerprint(two)


def test_the_fingerprint_falls_back_for_an_unreadable_jar(tmp_path):
    from livetest import opartifacts, plugin
    bad = tmp_path / "truncated.jar"
    bad.write_bytes(b"PK\x03\x04 not really a zip")
    assert opartifacts.jar_content_fingerprint(bad) == opartifacts.file_sha256(bad)


def test_an_absent_memo_hit_is_reprobed_before_it_costs_a_reload():
    """The cross-worker half of the memo staleness.

    The memo snapshots the WHOLE library list in one call, and _note_jar_loaded can only
    correct it for jars THIS process registers -- so a jar another xdist worker loaded after
    our snapshot stayed "absent" forever, and that False verdict makes ensure_registered drop
    the record and re-register: a destructive UNLOAD + LOAD. Measured on a real cluster: three
    registrations of one OP in one run (gw0 loaded it, gw2's older snapshot said
    absent), where every OP used by a single worker got exactly one.
    """
    from livetest import plugin
    calls = []

    class _Client:
        def loaded_libraries(self, timeout=None):
            calls.append(1)
            # first snapshot predates the other worker's load; later calls see it
            return set() if len(calls) == 1 else {"foo-5.4.jar"}

    cache = {}
    probe = plugin._loaded_jar_probe(_Client(), "foo-5.4.jar", cache=cache)
    assert probe() is True, "an absent memo must be confirmed against the cluster, not trusted"
    assert len(calls) == 2


def test_a_present_memo_hit_is_served_from_the_memo():
    # Present is the cheap answer and cannot cause a reload, so it must not pay for a refetch
    # -- this is the case that repeats hundreds of times a run.
    from livetest import plugin
    calls = []

    class _Client:
        def loaded_libraries(self, timeout=None):
            calls.append(1)
            return {"foo-5.4.jar", "bar-5.4.jar"}

    cache = {}
    c = _Client()
    for _ in range(5):
        assert plugin._loaded_jar_probe(c, "foo-5.4.jar", cache=cache)() is True
        assert plugin._loaded_jar_probe(c, "bar-5.4.jar", cache=cache)() is True
    assert len(calls) == 1


def test_a_genuinely_absent_jar_still_reports_absent(monkeypatch):
    # Re-probing must not turn "the cluster really lost it" into a skipped load.
    from livetest import plugin
    monkeypatch.setattr(plugin.time, "sleep", lambda *_: None)
    calls = []

    class _Client:
        def loaded_libraries(self, timeout=None):
            calls.append(1)
            return {"other-5.4.jar"}

    cache = {}
    assert plugin._loaded_jar_probe(_Client(), "foo-5.4.jar", cache=cache)() is False
    assert len(calls) == 2, "one snapshot plus one confirmation"


def test_a_reprobe_that_cannot_reach_the_cluster_says_cannot_tell(monkeypatch):
    # Unknown must not degrade to False, which would reload every jar on the cluster.
    from livetest import plugin
    monkeypatch.setattr(plugin.time, "sleep", lambda *_: None)
    state = {"n": 0}

    class _Client:
        def loaded_libraries(self, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                return set()          # snapshot: absent
            raise RuntimeError("connection refused")   # confirmation fails

    assert plugin._loaded_jar_probe(_Client(), "foo-5.4.jar", cache={})() is None


def test_cmdline_main_parallel_flag_configures_xdist(monkeypatch):
    from livetest.plugin import pytest_cmdline_main
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    cfg = types.SimpleNamespace(option=types.SimpleNamespace(parallel=3, numprocesses=None, dist="no", tx=[]))
    pytest_cmdline_main(cfg)
    assert os.environ.get("SLT_PARALLEL") == "1"
    assert cfg.option.numprocesses == 3
    assert cfg.option.dist == "load"
    assert cfg.option.tx == ["popen", "popen", "popen"]


def test_cmdline_main_parallel_one_disables_xdist(monkeypatch):
    from livetest.plugin import pytest_cmdline_main
    monkeypatch.setenv("SLT_PARALLEL", "1")
    cfg = types.SimpleNamespace(option=types.SimpleNamespace(parallel=1, numprocesses=3, dist="load", tx=["popen"]))
    pytest_cmdline_main(cfg)
    assert os.environ.get("SLT_PARALLEL") is None
    assert cfg.option.numprocesses == 0
    assert cfg.option.dist == "no"
    assert cfg.option.tx == []



def test_loaded_jar_probe_uses_the_short_probe_timeout():
    from livetest import plugin
    seen = []
    class _Client:
        def loaded_libraries(self, timeout=None):
            seen.append(timeout)
            return {"x.jar"}
    assert plugin._loaded_jar_probe(_Client(), "x.jar", cache={})() is True
    assert seen == [plugin._PROBE_TIMEOUT]
