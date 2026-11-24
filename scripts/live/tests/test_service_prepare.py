"""Service-prepare helpers in livetest.plugin (FW-116): the Spanner/GCS/Kafka readiness retries,
the admin factories that call them, and _resolve_striim's refusals."""
from types import SimpleNamespace

import pytest

from livetest import plugin


class _Flaky:
    """Fails the first `failures` calls with the given exceptions, then succeeds."""
    def __init__(self, failures=()):
        self.calls, self._failures = [], list(failures)
    def __call__(self, *args):
        self.calls.append(args)
        if self._failures:
            raise self._failures.pop(0)


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(plugin.time, "sleep", recorded.append)
    return recorded


# (helper, admin method, extra positional args) for the three retry helpers.
_HELPERS = [
    pytest.param(lambda admin, **kw: plugin._spanner_ensure(admin, **kw), "ensure", (), id="spanner"),
    pytest.param(lambda admin, **kw: plugin._gcs_ensure(admin, "slt-src", **kw), "ensure_bucket",
                 ("slt-src",), id="gcs"),
    pytest.param(lambda admin, **kw: plugin._kafka_clear(admin, "slt_src", **kw), "clear_topic",
                 ("slt_src",), id="kafka"),
]


def _admin(method, op):
    return SimpleNamespace(**{method: op})


@pytest.mark.parametrize("helper,method,args", _HELPERS)
def test_a_ready_service_is_prepared_once_without_waiting(helper, method, args, sleeps):
    op = _Flaky()
    helper(_admin(method, op))
    assert op.calls == [args] and sleeps == []


@pytest.mark.parametrize("helper,method,args", _HELPERS)
def test_a_service_still_starting_is_retried_until_it_answers(helper, method, args, sleeps):
    op = _Flaky([ConnectionError("refused"), OSError("reset")])
    helper(_admin(method, op), attempts=5, delay=0.5)
    assert op.calls == [args] * 3 and sleeps == [0.5, 0.5]


@pytest.mark.parametrize("helper,method,args", _HELPERS)
def test_a_service_that_never_answers_raises_its_last_error(helper, method, args, sleeps):
    errors = [ConnectionError(f"refused {i}") for i in range(4)]
    op = _Flaky(errors)
    with pytest.raises(ConnectionError) as info:
        helper(_admin(method, op), attempts=4, delay=0)
    assert info.value is errors[-1] and len(op.calls) == 4


@pytest.mark.parametrize("helper,method,args", _HELPERS)
def test_an_interrupt_is_not_retried(helper, method, args, sleeps):
    op = _Flaky([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        helper(_admin(method, op), attempts=5, delay=0)
    assert len(op.calls) == 1 and sleeps == []


def test_the_default_retry_budget_is_twenty_attempts_one_and_a_half_seconds_apart(sleeps):
    for helper, method, _args in [p.values for p in _HELPERS]:
        sleeps.clear()
        op = _Flaky([RuntimeError("down")] * 25)
        with pytest.raises(RuntimeError):
            helper(_admin(method, op))
        assert len(op.calls) == 20 and set(sleeps) == {1.5}


# ---- the admin factories that call them -----------------------------------------------------------

def test_spanner_admins_ensure_both_dialect_databases_on_one_emulator(monkeypatch):
    made = []
    class Admin:
        def __init__(self, dsn): self.dsn, self.ensured = dsn, 0; made.append(self)
        def ensure(self): self.ensured += 1
    monkeypatch.setattr(plugin, "SpannerAdmin", Admin)
    out = plugin._spanner_admins({"host": "localhost", "port": 9010, "project": "p", "instance": "i",
                                  "gsql_db": "g", "pg_db": "q"})
    google, pg = made
    assert out == [(google, "spanner-google"), (pg, "spanner-postgres")]
    common = {"project": "p", "instance": "i", "emulator_host": "localhost:9010"}
    assert google.dsn == {**common, "database": "g", "dialect": "google_standard_sql"}
    assert pg.dsn == {**common, "database": "q", "dialect": "postgresql"}
    assert google.ensured == pg.ensured == 1


def test_gcs_admin_ensures_then_empties_both_buckets(monkeypatch):
    log = []
    class Admin:
        def __init__(self, dsn): log.append(("init", dsn))
        def ensure_bucket(self, b): log.append(("ensure", b))
        def clear_bucket(self, b): log.append(("clear", b))
    monkeypatch.setattr(plugin, "GcsAdmin", Admin)
    plugin._gcs_admin({"host": "localhost", "port": 4443, "project": "p",
                       "src_bucket": "s", "tgt_bucket": "t"})
    assert log == [("init", {"endpoint": "http://localhost:4443", "project": "p", "seed_bucket": "s"}),
                   ("ensure", "s"), ("ensure", "t"), ("clear", "s"), ("clear", "t")]


_KAFKA_BASE = {"host": "localhost", "port": 9092, "broker_port": 19092, "registry_port": 8081,
               "src_topic": "slt_src", "tgt_topic": "slt_tgt"}


class _KafkaAdmin:
    def __init__(self, dsn):
        self.dsn, self.log = dsn, []
    def clear_topic(self, t): self.log.append(("clear", t))
    def ensure_topic(self, t, config=None): self.log.append(("ensure", t, config))


def test_kafka_admin_without_a_ledger_clears_both_topics(monkeypatch):
    monkeypatch.setattr(plugin, "KafkaAdmin", _KafkaAdmin)
    admin = plugin._kafka_admin(dict(_KAFKA_BASE))
    assert admin.dsn == {"broker": "localhost:9092", "registry_url": "http://localhost:8081",
                         "seed_topic": "slt_src"}
    assert admin.log == [("clear", "slt_src"), ("clear", "slt_tgt")]


@pytest.mark.parametrize("audit", [False, True])
def test_kafka_admin_with_a_ledger_acquires_each_topic_and_makes_only_the_audit_target_append_only(
        monkeypatch, audit):
    monkeypatch.setattr(plugin, "KafkaAdmin", _KafkaAdmin)
    acquired = []
    class Ledger:
        def acquire_topic(self, admin, topic, create):
            acquired.append(topic)
            create()
    admin = plugin._kafka_admin(dict(_KAFKA_BASE), ledger=Ledger(), audit=audit)
    append_only = {"cleanup.policy": "delete", "retention.ms": "-1", "retention.bytes": "-1"}
    assert acquired == ["slt_src", "slt_tgt"]
    assert admin.log == [("ensure", "slt_src", None),
                         ("ensure", "slt_tgt", append_only if audit else None)]


# ---- _resolve_striim ------------------------------------------------------------------------------

def _groups():
    return [{"output": [{"app": {"name": "default", "actualServers": [{"uuid": "u1"}]}},
                        {"source": {"name": "Agents", "actualServers": [{"uuid": "a1"}]}}]}]


class _Never:
    def __init__(self, what): self.what = what
    def __call__(self, *a, **k): pytest.fail(f"{self.what} was called")


@pytest.fixture
def striim(monkeypatch, tmp_path):
    """_resolve_striim with every outside effect faked; returns a runner and what it observed."""
    import socket
    from livetest import striim_provision as sp
    seen = SimpleNamespace(probes=[], sleeps=[], connects=[], settings={}, answers=[], reachable=True,
                           docker=False, port_open=False, groups=_groups, bind=None, before=None, now=0.0)
    def sleep(seconds):                     # a fake clock: every wait loop ends, however it is broken
        seen.sleeps.append(seconds)
        seen.now += seconds
    def probe(url, user, pw):
        seen.probes.append((url, user, pw))
        return seen.answers.pop(0) if seen.answers else seen.reachable
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def connect(addr, timeout=None):
        seen.connects.append(addr)
        if not seen.port_open:
            raise OSError("refused")
        return _Conn()
    class Client:
        api = SimpleNamespace(post_tungsten_line=lambda *a, **k: None)
        def list_deployment_groups(self, timeout=None):
            return seen.groups()
    monkeypatch.delenv("SLT_SERVICES_HOST", raising=False)
    monkeypatch.delenv(plugin._tryout.TRYOUT_ENV, raising=False)
    monkeypatch.setattr(plugin.paths, "setting", lambda key: seen.settings.get(key))
    monkeypatch.setattr(plugin, "probe_reachable", probe)
    monkeypatch.setattr(plugin.time, "sleep", sleep)
    monkeypatch.setattr(plugin.time, "monotonic", lambda: seen.now)
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(plugin, "_striim5_running", lambda: seen.docker)
    monkeypatch.setattr(plugin, "_cluster_provision_lock", lambda: tmp_path / "cluster.lock")
    monkeypatch.setattr(plugin, "_make_progress", lambda config: (lambda *a: None, lambda: None))
    monkeypatch.setattr(plugin, "StriimClient", SimpleNamespace(from_url=lambda *a: Client()))
    monkeypatch.setattr(plugin._slt_infra, "before_cluster_resolution", lambda *a: seen.before)
    monkeypatch.setattr(plugin._slt_infra, "bind_striim", lambda *a: seen.bind)
    for name in ("ensure_deps", "ensure_image", "cluster_up"):
        monkeypatch.setattr(sp, name, _Never(f"striim_provision.{name}"))

    def run(config=None):
        config = config or SimpleNamespace(option=SimpleNamespace(xmlpath=None))
        return plugin._resolve_striim(config), config
    return run, seen


def test_resolve_striim_dials_the_services_host_when_no_url_is_set(striim, monkeypatch):
    run, seen = striim
    monkeypatch.setenv("SLT_SERVICES_HOST", " svc-host ")
    ctx, _ = run()
    assert seen.probes[0] == ("http://svc-host:9080", "admin", "striim")
    assert ctx.url == "http://svc-host:9080" and ctx.mode == "native"


def test_resolve_striim_rewrites_only_a_localhost_url_to_the_services_host(striim, monkeypatch):
    run, seen = striim
    monkeypatch.setenv("SLT_SERVICES_HOST", "svc-host")
    seen.settings.update({"STRIIM_URL": "http://localhost:9180", "STRIIM_USER": "ops", "STRIIM_PASS": "pw"})
    ctx, _ = run()
    assert seen.probes[0] == ("http://svc-host:9180", "ops", "pw") and ctx.url == "http://svc-host:9180"
    seen.probes.clear()
    seen.settings["STRIIM_URL"] = "http://other-host:9180"
    ctx, _ = run()
    assert seen.probes[0][0] == "http://other-host:9180" and ctx.url == "http://other-host:9180"


def test_resolve_striim_resolves_a_native_striim_and_caches_it(striim):
    run, seen = striim
    seen.settings["STRIIM_URL"] = "http://native:9080"
    ctx, config = run()
    assert (ctx.mode, ctx.view_host, ctx.user, ctx.password) == ("native", "localhost", "admin", "striim")
    assert ctx.groups == {"app": "default", "source": "Agents"}
    assert ctx.topology.groups == {"default": ["u1"], "Agents": ["a1"]}
    assert config._slt_striim is ctx and config._slt_striim_provisioned is False
    seen.probes.clear()
    assert plugin._resolve_striim(config) is ctx and seen.probes == []


def test_resolve_striim_retries_one_failed_probe_before_judging_reachability(striim):
    run, seen = striim
    seen.answers = [False, True]
    ctx, _ = run()
    assert ctx is not None and ctx.mode == "native"
    assert len(seen.probes) == 2 and seen.sleeps == [2.0]


def test_resolve_striim_never_provisions_over_a_foreign_service_on_the_port(striim):
    run, seen = striim
    seen.settings["STRIIM_URL"] = "http://native:9333"
    seen.reachable, seen.port_open = False, True
    ctx, config = run()
    assert ctx is None and config._slt_striim is None
    assert config._slt_striim_reason == ("Striim on :9333 did not authenticate (check STRIIM_USER/STRIIM_PASS); "
                                         "not provisioning over an occupied port")
    assert not getattr(config, "_slt_striim_hard", False)
    assert len(seen.probes) == 3 and seen.sleeps == [2.0, 2.0] and seen.connects == [("native", 9333)]


def test_native_only_refuses_a_striim_that_does_not_authenticate(striim):
    run, seen = striim
    seen.settings["SLT_STRIIM_NATIVE_ONLY"] = "1"
    seen.reachable, seen.docker = False, True
    with pytest.raises(RuntimeError, match="SLT_STRIIM_NATIVE_ONLY=1: existing native Striim did not authenticate"):
        run()
    assert seen.connects == []


def test_native_only_uses_the_reachable_striim_even_with_a_docker_cluster_running(striim):
    run, seen = striim
    seen.settings["SLT_STRIIM_NATIVE_ONLY"] = "1"
    seen.docker = True
    ctx, _ = run()
    assert ctx.mode == "native" and ctx.view_host == "localhost"


def test_native_only_with_tryout_jars_fails_every_case_before_probing(striim, monkeypatch):
    run, seen = striim
    seen.settings["SLT_STRIIM_NATIVE_ONLY"] = "1"
    monkeypatch.setattr(plugin._tryout, "configured", lambda: ["a tryout"])
    ctx, config = run()
    assert ctx is None and config._slt_striim_hard is True
    assert config._slt_striim_reason == (f"{plugin._tryout.TRYOUT_ENV} needs the Docker cluster, and "
                                         "SLT_STRIIM_NATIVE_ONLY=1 uses only the Striim at STRIIM_URL")
    assert seen.probes == []


def test_an_unreadable_tryout_setting_fails_every_case(striim, monkeypatch):
    run, seen = striim
    def bad():
        raise plugin._tryout.TryoutError("SLT_STRIIM_TRYOUT_JARS: no such jar")
    monkeypatch.setattr(plugin._tryout, "configured", bad)
    ctx, config = run()
    assert ctx is None and config._slt_striim_hard is True
    assert config._slt_striim_reason == "SLT_STRIIM_TRYOUT_JARS: no such jar" and seen.probes == []


def test_an_ownership_refusal_before_resolution_is_a_skip_reason(striim):
    run, seen = striim
    seen.before = "exclusive run: endpoint already occupied"
    ctx, config = run()
    assert ctx is None and config._slt_striim_reason == "exclusive run: endpoint already occupied"
    assert seen.probes == []


def test_an_unreadable_topology_is_a_skip_reason(striim):
    run, seen = striim
    def boom():
        raise ConnectionError("connection reset")
    seen.groups = boom
    ctx, config = run()
    assert ctx is None and config._slt_striim_reason == "could not read Striim topology: connection reset"


def test_an_ownership_refusal_after_binding_is_a_skip_reason(striim):
    run, seen = striim
    seen.bind = "exclusive run: container does not publish the endpoint"
    ctx, config = run()
    assert ctx is None and config._slt_striim is None
    assert config._slt_striim_reason == "exclusive run: container does not publish the endpoint"


def test_a_provisioned_cluster_that_never_answers_is_a_skip_kept_for_teardown_with_its_logs(
        striim, monkeypatch, tmp_path):
    from livetest import striim_provision as sp
    run, seen = striim
    seen.reachable = False
    steps, saved = [], []
    monkeypatch.setattr(plugin, "_resolve_release", lambda config: {"STRIIM_VERSION": "5.4.2"})
    for name in ("ensure_deps", "ensure_image", "cluster_up"):
        monkeypatch.setattr(sp, name, lambda *a, name=name, **k: steps.append(name))
    monkeypatch.setattr(sp, "save_cluster_logs", lambda path, *a: saved.append(path) or path)
    ctx, config = run(SimpleNamespace(option=SimpleNamespace(xmlpath=tmp_path / "live" / "junit.xml")))
    assert ctx is None and steps == ["ensure_deps", "ensure_image", "cluster_up"]
    assert saved == [tmp_path / "live" / "cluster-logs"]
    assert config._slt_striim_reason == ("provisioned cluster did not become reachable in time; "
                                         f"node logs: {tmp_path / 'live' / 'cluster-logs'}")
    assert config._slt_striim_provisioned is True            # still torn down at session end
    assert 300 <= seen.now < 320                                # the 300 s wait budget, polled every 10 s


def test_a_running_cluster_whose_inputs_do_not_verify_is_not_reused(striim, monkeypatch):
    from livetest import striim_provision as sp
    run, seen = striim
    seen.docker = True
    monkeypatch.setattr(plugin, "_resolve_release", lambda config: {"STRIIM_VERSION": "5.4.2"})
    def refuse(*a):
        raise RuntimeError("deps manifest sha mismatch")
    monkeypatch.setattr(sp, "verify_current_inputs", refuse)
    ctx, config = run()
    assert ctx is None
    assert config._slt_striim_reason == "cannot reuse the Docker cluster: deps manifest sha mismatch"
