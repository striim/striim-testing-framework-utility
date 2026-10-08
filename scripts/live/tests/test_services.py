import pytest
from livetest.services import resolve, ResolvedService, ServiceError, DockerUnavailable, _default_compose_up, derive_per_test_base
from livetest.registry import load_service

def test_live_mode_when_override_env_set():
    env = {"SLT_PG_HOST": "db.example", "SLT_PG_PORT": "5432",
           "SLT_PG_DB": "prod", "SLT_PG_SOURCE_USER": "u", "SLT_PG_SOURCE_PASSWORD": "p"}
    r = resolve("postgres", env=env, started=set(),
                compose_up=lambda d: (_ for _ in ()).throw(AssertionError("must not up in live mode")))
    assert r.mode == "live"
    assert r.base["host"] == "db.example" and r.base["dbname"] == "prod"
    assert r.started is False


def test_docker_mode_brings_up_once():
    ups = []
    started = set()
    r1 = resolve("postgres", env={}, started=started, compose_up=lambda d: ups.append(d.name))
    assert r1.mode == "docker" and r1.base["port"] in (5432, "5432")
    assert r1.base["view_host"] == "localhost"   # native default; SLT_STRIIM_VIEW_HOST overrides
    assert r1.started is True and ups == ["postgres"]
    # second resolve in same session must NOT up again
    r2 = resolve("postgres", env={}, started=started, compose_up=lambda d: ups.append(d.name))
    assert r2.started is False and ups == ["postgres"]


def test_docker_mode_admin_host_follows_slt_services_host():
    """When the test process is containerized (SLT_SERVICES_HOST set, e.g. host.docker.internal),
    the admin/pytest base host must target the docker-host gateway, not the localhost default —
    otherwise the DB connection from inside the test container is refused (DPY-6005)."""
    r = resolve("postgres", env={"SLT_SERVICES_HOST": "host.docker.internal"}, started=set(),
                compose_up=lambda d: None)
    assert r.base["host"] == "host.docker.internal"


def test_docker_mode_defaults_to_localhost_without_services_host():
    """No SLT_SERVICES_HOST (pytest running ON the docker host) keeps the localhost default."""
    r = resolve("postgres", env={}, started=set(), compose_up=lambda d: None)
    assert r.base["host"] == "localhost"


def test_docker_unavailable_is_a_service_error():
    assert issubclass(DockerUnavailable, ServiceError)


def test_default_compose_up_raises_docker_unavailable_when_docker_missing(monkeypatch):
    import livetest.services as services_mod

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("docker: command not found")

    monkeypatch.setattr(services_mod.subprocess, "run", fake_run)
    defn = load_service("postgres")
    with pytest.raises(DockerUnavailable):
        _default_compose_up(defn)

def test_derive_per_test_base_kafka_inserts_tid_between_slt_and_suffix():
    base = {"src_topic": "slt_src", "tgt_topic": "slt_tgt", "host": "localhost"}
    out = derive_per_test_base("kafka", base, "MyTest_123")
    assert out["src_topic"] == "slt_mytest_123_src"
    assert out["tgt_topic"] == "slt_mytest_123_tgt"
    assert out["host"] == "localhost"        # untouched keys pass through
    assert base["src_topic"] == "slt_src"    # original dict is not mutated


def test_derive_per_test_base_gcs_prepends_tid_between_slt_and_suffix():
    # tid placement MATCHES kafka: between "slt" and "src"/"tgt" (hyphenated -- GCS
    # bucket names forbid "_"). slt-<tid>-src, not the old suffix form slt-src-<tid>.
    base = {"src_bucket": "slt-src", "tgt_bucket": "slt-tgt", "project": "test-project"}
    out = derive_per_test_base("gcs", base, "MyTest_123")
    assert out["src_bucket"] == "slt-mytest-123-src"
    assert out["tgt_bucket"] == "slt-mytest-123-tgt"


def test_derive_per_test_base_gcs_bucket_name_is_valid():
    # GCS bucket names: lowercase, [a-z0-9-], <=63 chars. The production tid is the
    # hashed per-test id ("t"+9 hex, 10 chars -> an 18-char bucket + ns), but the
    # derivation must stay inside the valid charset for ANY [A-Za-z0-9_]+ input --
    # lower() + replacing "_" with "-" -- so keep stressing a long tid too.
    import re
    tid = "a" * 46
    out = derive_per_test_base("gcs", {"src_bucket": "slt-src", "tgt_bucket": "slt-tgt"}, tid)
    for bucket in (out["src_bucket"], out["tgt_bucket"]):
        assert re.match(r"^[a-z0-9-]+$", bucket)
        assert len(bucket) <= 63


def test_derive_per_test_base_gcs_unprefixed_omits_ns():
    # No SLT_STACK_PREFIX -> no ns segment: slt-<tid>-src exactly. env={} and an env
    # with an empty prefix are both "no prefix". (The old "unprefixed is byte-identical
    # to the historical fixed name" invariant ended when the tid moved to kafka-style
    # placement -- per-test isolation now holds in serial runs too, so there is no
    # fixed historical name left to preserve.)
    base = {"src_bucket": "slt-src", "tgt_bucket": "slt-tgt"}
    for env in ({}, {"SLT_STACK_PREFIX": ""}):
        out = derive_per_test_base("gcs", base, "MyTest_123", env=env)
        assert out["src_bucket"] == "slt-mytest-123-src"
        assert out["tgt_bucket"] == "slt-mytest-123-tgt"


def test_derive_per_test_base_gcs_prefixed_trails_tid_with_hyphenated_ns():
    # Under a stack prefix the ns TRAILS the tid (estate ${TID}${NS}_ order), hyphen-joined,
    # inside the slt-...-src/tgt frame: slt-<tid>-<ns>-src.
    base = {"src_bucket": "slt-src", "tgt_bucket": "slt-tgt"}
    out = derive_per_test_base("gcs", base, "MyTest_123", env={"SLT_STACK_PREFIX": "alt"})
    assert out["src_bucket"] == "slt-mytest-123-alt-src"
    assert out["tgt_bucket"] == "slt-mytest-123-alt-tgt"
    # a digit-bearing prefix is fine too (still a valid, lowercased/hyphenated bucket name)
    out2 = derive_per_test_base("gcs", base, "t1", env={"SLT_STACK_PREFIX": "b2"})
    assert out2["src_bucket"] == "slt-t1-b2-src"


def test_derive_per_test_base_gcs_underscore_prefix_fails_loud():
    # GCS forbids "_" in bucket names. Rather than silently rewrite an underscore-bearing
    # prefix (which would let the bucket namespace disagree with the container/network
    # namespace for the SAME stack), derivation reuses stack.prefix() -- the one accessor
    # that already namespaces containers/networks -- so an invalid prefix fails loud HERE,
    # exactly as it does for container naming. The tid's own underscores are still
    # hyphenated (that path is unaffected).
    from livetest import stack
    base = {"src_bucket": "slt-src", "tgt_bucket": "slt-tgt"}
    with pytest.raises(stack.StackPrefixError):
        derive_per_test_base("gcs", base, "MyTest_123", env={"SLT_STACK_PREFIX": "b_2"})


def test_derive_per_test_base_other_services_unchanged():
    base = {"host": "localhost", "port": 1521, "service": "FREEPDB1"}
    out = derive_per_test_base("oracle", base, "sometest")
    assert out == base
    assert out is not base   # still a copy, not the same object


def test_derive_per_test_base_is_idempotent():
    # Deriving again from an ALREADY-derived base (e.g. a second call in some future
    # code path) must produce the same result, not double-prefix.
    base = {"src_topic": "slt_src", "tgt_topic": "slt_tgt"}
    once = derive_per_test_base("kafka", base, "t1")
    twice = derive_per_test_base("kafka", once, "t1")
    assert once == twice


def _failing_run_recorder(calls):
    """A subprocess.run stand-in that records every argv and always reports failure
    (returncode 1) — for exercising _default_compose_up's up-failure cleanup path."""
    def fake_run(argv, **kw):
        calls.append(argv)
        class R:
            returncode = 1
            stderr = "boom"
            stdout = ""
        return R()
    return fake_run


def test_compose_up_failure_skips_down_v_when_keep_services(monkeypatch):
    # up fails; with SLT_KEEP_SERVICES set, NO `down -v` must be issued (spec §C.6): under
    # parallel runs a worker's transient up-failure must not nuke the shared emulator's
    # volumes that sibling workers depend on.
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    calls = []
    monkeypatch.setattr("livetest.services.subprocess.run", _failing_run_recorder(calls))
    defn = load_service("postgres")
    with pytest.raises(ServiceError):
        _default_compose_up(defn)
    assert any("up" in argv for argv in calls)      # it did attempt the bring-up
    assert not any("down" in argv for argv in calls)  # ...but did NOT tear the volumes down

def test_compose_up_failure_runs_down_v_without_keep_services(monkeypatch):
    # No SLT_KEEP_SERVICES: preserve today's behavior — a failed up cleans up with `down -v`.
    monkeypatch.delenv("SLT_KEEP_SERVICES", raising=False)
    calls = []
    monkeypatch.setattr("livetest.services.subprocess.run", _failing_run_recorder(calls))
    defn = load_service("postgres")
    with pytest.raises(ServiceError):
        _default_compose_up(defn)
    assert any("down" in argv and "-v" in argv for argv in calls)


def _compose_recorder(calls, stopped: str):
    """A subprocess.run stand-in: `ps` answers `stopped` (container ids, or nothing), every other
    command succeeds."""
    def fake_run(argv, **kw):
        calls.append(argv)
        class R:
            returncode = 0
            stderr = ""
            stdout = stopped if "ps" in argv else ""
        return R()
    return fake_run


def test_kept_kafka_that_is_stopped_starts_from_clean_state(monkeypatch):
    # SLT_KEEP_SERVICES reuses a stack, but a stopped Kafka stack restarted with its old
    # ZooKeeper data exits NodeExists: it is reset (down -v) before the up.
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    calls = []
    monkeypatch.setattr("livetest.services.subprocess.run", _compose_recorder(calls, "3f2a9c\n"))
    _default_compose_up(load_service("kafka"))
    verbs = [next(v for v in ("ps", "down", "up") if v in argv) for argv in calls]
    assert verbs == ["ps", "down", "up"]
    assert "-v" in calls[1]
    assert calls[0][-10:] == ["--status", "created", "--status", "exited", "--status", "paused",
                              "--status", "restarting", "--status", "dead"]


def test_kept_kafka_that_is_running_is_reused(monkeypatch):
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    calls = []
    monkeypatch.setattr("livetest.services.subprocess.run", _compose_recorder(calls, ""))
    _default_compose_up(load_service("kafka"))
    assert not any("down" in argv for argv in calls)
    assert any("up" in argv for argv in calls)


def test_kept_postgres_is_not_checked_for_stopped_containers(monkeypatch):
    # Restarting a stopped database keeps its data and is what SLT_KEEP_SERVICES is for.
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    calls = []
    monkeypatch.setattr("livetest.services.subprocess.run", _compose_recorder(calls, "3f2a9c\n"))
    _default_compose_up(load_service("postgres"))
    assert not any("ps" in argv or "down" in argv for argv in calls)


# ---- xdist service-provision register-once (spec §C.4) -----------------------
from livetest.services import ensure_provisioned_once, clear_provision_registry


def test_ensure_provisioned_once_provisions_a_container_once(tmp_path):
    calls = []
    assert ensure_provisioned_once("slt-oracle", lambda: calls.append(1), state_dir=tmp_path) is True
    assert ensure_provisioned_once("slt-oracle", lambda: calls.append(1), state_dir=tmp_path) is False
    assert calls == [1]   # second worker skips the bring-up + post_up


def test_ensure_provisioned_once_is_per_container(tmp_path):
    calls = []
    assert ensure_provisioned_once("slt-oracle", lambda: calls.append("o"), state_dir=tmp_path) is True
    assert ensure_provisioned_once("slt-postgres", lambda: calls.append("p"), state_dir=tmp_path) is True
    assert calls == ["o", "p"]


def test_clear_provision_registry_forces_reprovision(tmp_path):
    calls = []
    ensure_provisioned_once("slt-oracle", lambda: calls.append(1), state_dir=tmp_path)
    clear_provision_registry(tmp_path)   # controller, next session: stale record gone
    assert ensure_provisioned_once("slt-oracle", lambda: calls.append(1), state_dir=tmp_path) is True
    assert calls == [1, 1]


def test_clear_provision_registry_absent_is_noop(tmp_path):
    clear_provision_registry(tmp_path)   # must not raise when nothing was ever written


def test_resolve_docker_serial_does_not_touch_provision_registry(tmp_path, monkeypatch):
    # Serial run (no PYTEST_XDIST_WORKER): resolve() must bring the container up directly and
    # NOT create the xdist registry file — serial behavior is byte-identical to before.
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr("livetest.services._state_dir", lambda: tmp_path)
    ups = []
    r = resolve("postgres", env={}, started=set(), compose_up=lambda d: ups.append(d.name))
    assert r.started is True and ups == ["postgres"]
    assert not (tmp_path / ".slt-provision-registry.json").exists()


def test_resolve_docker_xdist_registers_provision_once(tmp_path, monkeypatch):
    # Under xdist, two workers (separate `started` sets) resolving the same service must
    # bring it up only ONCE across them (the container-name-conflict fix).
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setattr("livetest.services._state_dir", lambda: tmp_path)
    ups = []
    env = {"PYTEST_XDIST_WORKER": "gw0"}
    resolve("postgres", env=env, started=set(), compose_up=lambda d: ups.append(d.name))  # worker A
    resolve("postgres", env=env, started=set(), compose_up=lambda d: ups.append(d.name))  # worker B
    assert ups == ["postgres"]   # brought up once across the two workers


def test_resolve_docker_slt_parallel_registers_provision_once(tmp_path, monkeypatch):
    # Console fan-out model (spec §D.3.2): N independent single-test subprocesses set
    # SLT_PARALLEL but NOT PYTEST_XDIST_WORKER. resolve() must still coordinate the bring-up
    # through the once-across-processes registry (not `_do_up()` per process) so concurrent
    # children don't race `docker compose up` on the shared container.
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr("livetest.services._state_dir", lambda: tmp_path)
    ups = []
    env = {"SLT_PARALLEL": "1"}
    resolve("postgres", env=env, started=set(), compose_up=lambda d: ups.append(d.name))  # child A
    resolve("postgres", env=env, started=set(), compose_up=lambda d: ups.append(d.name))  # child B
    assert ups == ["postgres"]   # brought up once across the two subprocesses
    assert (tmp_path / ".slt-provision-registry.json").exists()

def test_default_compose_up_wipes_volumes_before_bring_up(monkeypatch):
    """The harness must `down -v` (removing persistent volumes) BEFORE it brings a
    service up, so a stale volume from a prior run never leaks into a fresh run."""
    import types
    import livetest.services as services_mod

    calls = []

    def fake_run(argv, *args, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(services_mod.subprocess, "run", fake_run)
    # This asserts the DEFAULT path, which SLT_KEEP_SERVICES deliberately suppresses
    # (services.py skips the pre-up `down -v` under it). Without clearing it the test
    # measures the ambient shell instead of the behavior it names, and fails for anyone
    # running with services kept.
    monkeypatch.delenv("SLT_KEEP_SERVICES", raising=False)
    defn = load_service("postgres")
    _default_compose_up(defn)

    downs = [c for c in calls if "down" in c]
    ups = [c for c in calls if "up" in c]
    assert downs, "expected a pre-up `down -v`"
    assert "-v" in downs[0], "pre-up down must remove volumes (-v)"
    assert ups, "expected a `compose up`"
    assert calls.index(downs[0]) < calls.index(ups[0]), "the down -v must precede the up"


def test_default_compose_up_passes_build(monkeypatch):
    # `up` alone builds ONLY when the image is absent, so postgres/mssql/oracle silently kept a
    # stale image after a Dockerfile or init-script change on any machine that had built it
    # once. Docker's layer cache makes the check ~1s when nothing changed (measured: postgres
    # 0.74s, oracle 1.05s), so it is paid per bring-up rather than guessed at with a hash.
    import livetest.services as services_mod
    seen = []

    class _Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")      # skip the _compose_reset down -v
    monkeypatch.setattr(services_mod.subprocess, "run",
                        lambda argv, **k: seen.append(argv) or _Done())
    _default_compose_up(load_service("postgres"))

    up = next(a for a in seen if "up" in a)
    assert up[-3:] == ["-d", "--wait", "--build"], up


def test_live_override_remote_host_is_its_own_view_host(monkeypatch):
    """A remote live service must not be routed through the docker gateway: SLT_STRIIM_VIEW_HOST
    is for a database on THIS machine seen from a containerised Striim."""
    from livetest.services import resolve
    env = {"SLT_MSSQL_HOST": "192.0.2.3", "SLT_STRIIM_VIEW_HOST": "host.docker.internal"}
    r = resolve("mssql", env, started=set())
    assert r.mode == "live"
    assert r.base["view_host"] == "192.0.2.3"
    env["SLT_MSSQL_VIEW_HOST"] = "mssql.internal.example"
    assert resolve("mssql", env, started=set()).base["view_host"] == "mssql.internal.example"
    local = {"SLT_MSSQL_HOST": "localhost", "SLT_STRIIM_VIEW_HOST": "host.docker.internal"}
    assert resolve("mssql", local, started=set()).base["view_host"] == "host.docker.internal"


def test_failed_provision_never_writes_success_registry(tmp_path):
    with pytest.raises(ServiceError):
        ensure_provisioned_once('kafka', lambda: (_ for _ in ()).throw(ServiceError('refused')),
                                state_dir=tmp_path)
    assert not (tmp_path / '.slt-provision-registry.json').exists()


@pytest.mark.parametrize('streaming', [False, True])
def test_kafka_failure_seam_heals_before_single_post_up_and_registry(monkeypatch, tmp_path, streaming):
    from livetest import service_healing as h, services
    from types import SimpleNamespace
    calls = []
    ctx = h.RecoveryContext(tmp_path / 'evidence', 'run', 'op', 'lane')
    monkeypatch.setenv('SLT_KEEP_SERVICES', '1')
    monkeypatch.setattr(services, '_run_preflight_compose',
                        lambda argv, env, context, name, progress: calls.append(('compose', bool(progress)))
                        or SimpleNamespace(returncode=1, stdout='first failure'))
    monkeypatch.setattr(h, 'DockerEvidence', lambda *a: 'evidence')
    monkeypatch.setattr(h, 'recover_kafka', lambda *a: calls.append(('recover', a[-1])) or True)
    defn = load_service('kafka')
    monkeypatch.setattr(services, 'load_service', lambda n: defn)
    # Emulate a post-up to prove that recovery is internal to compose-up, never replaying it.
    object.__setattr__(defn, 'post_up', '/once.sh')
    monkeypatch.setattr(services, '_state_dir', lambda: tmp_path)
    progress = (lambda *a: None) if streaming else None
    with h.preflight_context(ctx):
        services.resolve('kafka', {'SLT_PARALLEL': '1'}, set(), progress=progress,
                         post_up=lambda d: calls.append(('post', d.post_up)))
    assert calls == [('compose', streaming), ('recover', 'first failure'), ('post', '/once.sh')]
    assert (tmp_path / '.slt-provision-registry.json').exists()
    assert h.current_context() is None


def test_refused_kafka_failure_never_records_success_or_runs_post_up(monkeypatch, tmp_path):
    from livetest import service_healing as h, services
    from types import SimpleNamespace
    ctx = h.RecoveryContext(tmp_path / 'evidence', 'run', 'op', 'lane')
    monkeypatch.setenv('SLT_KEEP_SERVICES', '1')
    monkeypatch.setattr(services, '_run_preflight_compose', lambda *a: SimpleNamespace(
        returncode=1, stdout='first failure'))
    monkeypatch.setattr(h, 'DockerEvidence', lambda *a: 'evidence')
    monkeypatch.setattr(h, 'recover_kafka', lambda *a: (_ for _ in ()).throw(h.Refused('maintenance')))
    monkeypatch.setattr(services, '_state_dir', lambda: tmp_path)
    with h.preflight_context(ctx), pytest.raises(ServiceError, match='first failure.*maintenance'):
        services.resolve('kafka', {'SLT_PARALLEL': '1'}, set(),
                         post_up=lambda d: pytest.fail('post_up must not run'))
    assert not (tmp_path / '.slt-provision-registry.json').exists()
