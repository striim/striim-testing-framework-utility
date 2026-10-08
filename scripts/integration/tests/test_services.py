"""Fast, no-Docker tests for `inttest.services.ensure_provisioned_once`/
`clear_provision_registry` (docs/internals/INTEGRATION-ENGINE.md#token-isolation), plus
`compose_down`'s "did the container actually go away?" check.

`do_provision` is an injected callable and the registry/lock are plain
filesystem state, so none of this needs a real container -- mirrors
scripts/live/tests/test_services.py's equivalent tests, plus a genuine
multi-thread test (not just sequential calls) since that's the actual claim
`ensure_provisioned_once` makes: N uncoordinated concurrent callers run
`do_provision` exactly once, not just "two sequential calls happen to see
each other's state."
"""
from __future__ import annotations

import concurrent.futures
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from inttest import cli, services
from inttest import tokens as tokens_mod
from inttest.services import clear_provision_registry, ensure_provisioned_once


def test_ensure_provisioned_once_provisions_a_key_once(tmp_path):
    calls = []
    assert ensure_provisioned_once(
        "postgres-setup", lambda: calls.append(1), state_dir=tmp_path) is True
    assert ensure_provisioned_once(
        "postgres-setup", lambda: calls.append(1), state_dir=tmp_path) is False
    assert calls == [1]


def test_ensure_provisioned_once_is_per_key(tmp_path):
    calls = []
    assert ensure_provisioned_once(
        "postgres-setup", lambda: calls.append("p"), state_dir=tmp_path) is True
    assert ensure_provisioned_once(
        "oracle-setup", lambda: calls.append("o"), state_dir=tmp_path) is True
    assert calls == ["p", "o"]


def test_clear_provision_registry_forces_reprovision(tmp_path):
    calls = []
    ensure_provisioned_once("postgres-setup", lambda: calls.append(1), state_dir=tmp_path)
    clear_provision_registry(tmp_path)  # controller, next session: stale record gone
    assert ensure_provisioned_once(
        "postgres-setup", lambda: calls.append(1), state_dir=tmp_path) is True
    assert calls == [1, 1]


def test_clear_provision_registry_absent_is_a_noop(tmp_path):
    clear_provision_registry(tmp_path)  # must not raise when nothing was ever written


def test_ensure_provisioned_once_runs_do_provision_exactly_once_under_real_concurrency(tmp_path):
    """The actual claim this function makes, proven under real thread
    contention rather than sequential calls: N uncoordinated concurrent
    callers racing on the SAME key must see `do_provision` run exactly once,
    and every caller must observe the provisioned state afterward."""
    calls = []
    calls_lock = threading.Lock()
    provisioned = threading.Event()

    def _do_provision():
        # A real do_provision (docker compose up, PgAdmin.ensure_setup) takes
        # real time -- sleeping here widens the race window so concurrent
        # callers are actually likely to overlap inside the FileLock, not just
        # get lucky with scheduling.
        import time
        time.sleep(0.05)
        with calls_lock:
            calls.append(1)
        provisioned.set()

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(ensure_provisioned_once, "postgres-setup", _do_provision, tmp_path)
            for _ in range(8)
        ]
        for f in futures:
            results.append(f.result())

    assert calls == [1], "do_provision must run exactly once across all concurrent callers"
    assert results.count(True) == 1, "exactly one caller's return value reports doing the work"
    assert results.count(False) == 7
    assert provisioned.is_set()


# --- a container that survives `compose down` must not read as a clean teardown ------------

def _compose_repo(tmp_path, name, body):
    (tmp_path / "services" / name).mkdir(parents=True)
    (tmp_path / "services" / name / "compose.yaml").write_text(body)
    return tmp_path


def _use_root(monkeypatch, root):
    # services and cli resolve the services root and state through inttest.paths at call time.
    monkeypatch.setenv("SLT_INT_SERVICES_DIR", str(root / "services"))
    monkeypatch.setenv("SLT_STATE_DIR", str(root))


def test_stop_reports_a_container_from_another_project(monkeypatch, tmp_path):
    # `down` is project-scoped, so a same-named container owned by a different project (one
    # started before the `gcs` -> `int-gcs` project rename) survives it and exits 0. Silence
    # there resurfaces one command later as "container name already in use".
    _compose_repo(tmp_path, "postgres",
                  "name: int-postgres\nservices:\n  int-postgres:\n"
                  "    container_name: ${INT_STACK_PREFIX:+${INT_STACK_PREFIX}-}int-postgres\n")
    _use_root(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "compose_down", lambda svc, lock: None)
    monkeypatch.setattr(services, "container_exists", lambda c: c == "int-postgres")

    assert cli.cmd_down(["postgres"]) == 1


def test_stop_is_clean_when_nothing_survives(monkeypatch, tmp_path):
    _compose_repo(tmp_path, "postgres",
                  "services:\n  int-postgres:\n    container_name: int-postgres\n")
    _use_root(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "compose_down", lambda svc, lock: None)
    monkeypatch.setattr(services, "container_exists", lambda c: False)

    assert cli.cmd_down(["postgres"]) == 0


def test_the_check_does_not_run_inside_compose_down(monkeypatch, tmp_path):
    # Raising from compose_down would skip `ensure_down`'s registry bookkeeping (leaving the
    # name stuck in .int-services-up.json forever) and let a teardown error REPLACE the real
    # failure of a test using ensure_up_for_test's finally-block cleanup.
    _compose_repo(tmp_path, "postgres",
                  "services:\n  int-postgres:\n    container_name: int-postgres\n")
    _use_root(monkeypatch, tmp_path)
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(services, "container_exists", lambda c: True)

    services.compose_down("postgres", FileLock(str(tmp_path / "l.lock")))   # must not raise


def test_every_declared_container_is_checked_not_just_the_primary(monkeypatch, tmp_path):
    # gcs runs a sidecar (int-gcs-token) that service.yaml never names; checking only the
    # primary calls the service clean while the sidecar still holds its name.
    _compose_repo(tmp_path, "gcs",
                  "services:\n  int-gcs:\n    container_name: int-gcs\n"
                  "  token:\n    container_name: int-gcs-token\n")
    _use_root(monkeypatch, tmp_path)
    assert services.compose_container_names("gcs") == ["int-gcs", "int-gcs-token"]

    monkeypatch.setattr(services, "container_exists", lambda c: c == "int-gcs-token")
    assert services.surviving_containers("gcs") == ["int-gcs-token"]


def test_container_names_carry_the_stack_prefix(monkeypatch, tmp_path):
    # compose interpolates ${INT_STACK_PREFIX:+…-} into container_name; the check has to look
    # for the same name compose actually created, or a prefixed stack never sees a leftover.
    _compose_repo(tmp_path, "gcs",
                  "services:\n  int-gcs:\n"
                  "    container_name: ${INT_STACK_PREFIX:+${INT_STACK_PREFIX}-}int-gcs\n")
    _use_root(monkeypatch, tmp_path)

    monkeypatch.setenv("INT_STACK_PREFIX", "alt")
    assert services.compose_container_names("gcs") == ["alt-int-gcs"]
    monkeypatch.delenv("INT_STACK_PREFIX")
    assert services.compose_container_names("gcs") == ["int-gcs"]


def test_quoted_and_commented_container_names_are_read(monkeypatch, tmp_path):
    # A silent miss here disables the whole check, so the scan must survive the ordinary YAML
    # spellings rather than only the one the repo happens to use today.
    _compose_repo(tmp_path, "spanner",
                  "services:\n  a:\n    container_name: \"int-spanner\"\n"
                  "  b:\n    container_name: int-spanner-ui  # the console\n"
                  "  c:\n    # container_name: int-commented-out\n")
    _use_root(monkeypatch, tmp_path)
    assert services.compose_container_names("spanner") == ["int-spanner", "int-spanner-ui"]


def test_a_compose_file_without_container_names_skips_the_check(monkeypatch, tmp_path):
    _compose_repo(tmp_path, "spanner", "services:\n  int-spanner:\n    image: x\n")
    _use_root(monkeypatch, tmp_path)
    monkeypatch.setattr(services, "container_exists",
                        lambda c: pytest.fail("asked Docker about a service that names none"))
    assert services.surviving_containers("spanner") == []


# --- docker_env: the published-port override the token resolver used to ignore -----------
# Added 2026-08-19, porting the fix scripts/live/livetest/services.py::resolve got the same
# day. compose.yaml has always interpolated ${INT_*_HOST_PORT}, but the integration tier's
# token resolvers built their base from docker_defaults alone -- so a second stack from
# another checkout moved every container and not one of the clients dialing them, and the
# whole regression tier hung on refused sockets (or, when the default stack happened to be
# up, silently drove the OTHER checkout's databases).

def test_docker_env_overrides_the_published_port(monkeypatch):
    monkeypatch.setenv("INT_SPANNER_GRPC_HOST_PORT", "19110")
    monkeypatch.setenv("INT_SPANNER_REST_HOST_PORT", "19120")
    monkeypatch.delenv("INT_SPANNER_PORT", raising=False)
    monkeypatch.delenv("INT_SPANNER_ADMIN_PORT", raising=False)
    t = tokens_mod.service_tokens("spanner")
    assert t["SPANNER_GRPC_PORT"] == "19110", "the admin client must follow the remapped gRPC port"
    assert "19110" in t["SPANNER_GSQL_URL"]


def test_docker_env_absent_leaves_the_default(monkeypatch):
    for var in ("INT_SPANNER_GRPC_HOST_PORT", "INT_SPANNER_REST_HOST_PORT",
                "INT_SPANNER_PORT", "INT_SPANNER_ADMIN_PORT", "INT_SPANNER_HOST"):
        monkeypatch.delenv(var, raising=False)
    assert tokens_mod.service_tokens("spanner")["SPANNER_GRPC_PORT"] == "19010", \
        "an unset override must not disturb the default"


def test_live_env_still_wins_over_docker_env(monkeypatch):
    """The two layers are not redundant: INT_*_HOST_PORT says where docker PUBLISHED,
    INT_*_PORT says where the client should DIAL. They differ only when a service is
    reached through something other than its own published port (an ssh tunnel, a
    later-phase broker), and the client-side statement has to win there."""
    monkeypatch.setenv("INT_PG_HOST_PORT", "15532")
    monkeypatch.setenv("INT_PG_PORT", "16000")
    assert tokens_mod.service_tokens("postgres")["POSTGRES_PORT"] == "16000"


def test_every_service_publishing_an_overridable_port_declares_docker_env():
    """The gap is generic -- it was found in postgres and all four services share the
    shape. Mirrors scripts/live/tests/test_spanner_service.py's guard of the same name."""
    services_dir = Path(__file__).resolve().parents[1] / "services"
    missing = []
    for compose in sorted(services_dir.glob("*/compose.yaml")):
        if not (compose.parent / "service.yaml").exists():
            continue
        published = re.findall(r"\$\{(INT_[A-Z_]*HOST_PORT)", compose.read_text())
        if not published:
            continue
        raw = tokens_mod._load_service_def(compose.parent.name, services_dir)
        declared = set((raw.get("docker_env") or {}).values())
        for var in published:
            if var not in declared:
                missing.append(f"{compose.parent.name}:{var}")
    assert not missing, (
        "compose.yaml publishes these host ports but service.yaml's docker_env does not "
        f"map them onto a resolved key, so the client will not follow a remap: {missing}")


def test_the_hermetic_scrub_covers_every_bare_env_name_the_fixture_path_reads():
    """`conftest._hermetic_stack_env` scrubs INT_* by prefix and the `provides:` token
    names by derivation -- but `plugin.py`'s fixture-path resolvers may read a bare name
    that is neither (SPANNER_ADMIN_PORT is one today). Anything they read and the scrub
    misses is a variable a developer's shell can still use to rewrite a hermetic test's
    expected values, which is the whole failure this guard exists to prevent."""
    import conftest

    # Every module, both idioms, either quote style. Scoping this to plugin.py and to
    # `os.getenv` would match today's code exactly -- and that is the problem: it would bless
    # the convention instead of checking it, so a resolver that lands in gcsadmin.py, or
    # spells it os.environ.get('POSTGRES_PORT'), would walk straight past the guard.
    read = set()
    for src in sorted((Path(__file__).resolve().parents[1] / "inttest").glob("*.py")):
        read |= set(re.findall(r"""os\.(?:getenv|environ\.get)\(\s*['"]([A-Z][A-Z0-9_]*)['"]""",
                               src.read_text()))
    # Reading the env is not the same as being redirectable by it. These say how to RUN or
    # where the BUILD comes from, not which stack to talk to, so they are deliberately left
    # alone. Anything new that lands here has to be classified on purpose -- which is the
    # point: the failure this guards against is a redirection variable slipping in unnoticed.
    read -= {"INT_SHARED_SERVICES", "SLT_PARALLEL", "SLT_RUN_DISABLED",   # how to RUN
             "PYTEST_XDIST_WORKER", "SLT_LOCK_DIR",
             "STRIIM_HOME", "JAVA_HOME"}                                  # what to BUILD with
    uncovered = sorted(n for n in read
                       if not n.startswith("INT_") and n not in conftest._stack_env_names())
    assert not uncovered, (
        "inttest/ reads these env vars as bare overrides but conftest.py's hermetic scrub "
        f"does not clear them (add them to the scrub, or to the run-mode set): {uncovered}")


# --- INT_STACK_PREFIX scoping of the coordination files -----------------------

def test_state_name_scopes_by_int_stack_prefix(monkeypatch):
    from inttest import services
    monkeypatch.delenv("INT_STACK_PREFIX", raising=False)
    assert services.state_name(".int-services-up.json") == ".int-services-up.json", \
        "no prefix must keep the historical filename byte-for-byte"
    monkeypatch.setenv("INT_STACK_PREFIX", "ec")
    assert services.state_name(".int-services-up.json") == ".ec-int-services-up.json"
    assert services.state_name("plain") == "ec-plain", "non-dotfiles scope too"


def test_two_prefixes_do_not_share_the_started_registry(monkeypatch):
    # The bug this closes. These files record what THIS STACK started, and
    # INT_STACK_PREFIX is what makes two stacks distinct -- compose prefixes the project
    # and every container_name, so `ec` and `alt` own entirely separate containers.
    #
    # Unscoped, one checkout driving two prefixes shared a single record, so the second
    # stack read the first's list and SKIPPED bringing up its own services. It does not
    # fail cleanly: a stale ["postgres"] left the ec stack with no gcs, produced
    # connection errors on the Oracle-backed cases, and HUNG a run for 40 minutes
    # entering the first Spanner case -- waiting on a container that was never launched.
    #
    # Two separate CHECKOUTS never collided (each has its own scripts/integration/),
    # which is exactly why it survived: it only bites when one checkout drives two stacks.
    from inttest import services, plugin
    monkeypatch.setenv("INT_STACK_PREFIX", "ec")
    ec_started, ec_lock = plugin._started_registry(), plugin._compose_lock_path()
    ec_prov, ec_prov_lock = services._provision_registry_path(), services._provision_lock_path()

    monkeypatch.setenv("INT_STACK_PREFIX", "alt")
    alt_started, alt_lock = plugin._started_registry(), plugin._compose_lock_path()
    alt_prov, alt_prov_lock = services._provision_registry_path(), services._provision_lock_path()

    # All FOUR must move together: a started-registry that is scoped while its compose
    # lock is not would let two stacks mutate one record under two different locks.
    for ec_path, alt_path, what in ((ec_started, alt_started, "started registry"),
                                    (ec_lock, alt_lock, "compose lock"),
                                    (ec_prov, alt_prov, "provision registry"),
                                    (ec_prov_lock, alt_prov_lock, "provision lock")):
        assert ec_path != alt_path, f"{what} is shared between prefixes"
        assert "ec-int" in ec_path.name and "alt-int" in alt_path.name


def test_unprefixed_state_files_keep_their_historical_names(monkeypatch):
    # Back-compat: the overwhelmingly common path (one stack, no prefix) must keep the
    # exact filenames it had, so an in-flight run's state is not orphaned by upgrading.
    from inttest import services, plugin
    monkeypatch.delenv("INT_STACK_PREFIX", raising=False)
    assert plugin._started_registry().name == ".int-services-up.json"
    assert plugin._compose_lock_path().name == ".int-compose.lock"
    assert services._provision_registry_path().name == ".int-provision-registry.json"


@pytest.mark.parametrize('key,value', [('pre_up_check', 'always'), ('unavailable_policy', 'fail')])
def test_consumer_hook_schema_parity(tmp_path, key, value):
    root = tmp_path / 'services'
    svc = root / 'teradata'
    svc.mkdir(parents=True)
    path = svc / 'service.yaml'
    path.write_text(f'name: teradata\nisolation: none\n{key}: {value}\n')
    assert tokens_mod._load_service_def('teradata', root)[key] == value
    path.write_text(f'name: teradata\nisolation: none\n{key}: bogus\n')
    with pytest.raises(tokens_mod.ServiceConfigError, match=key):
        tokens_mod._load_service_def('teradata', root)


@pytest.mark.parametrize('policy', ['fail', 'skip'])
@pytest.mark.parametrize('connection_only', [False, True])
def test_outer_provision_requires_applies_policy_to_startup_and_connection_only(monkeypatch, tmp_path, policy, connection_only):
    from inttest import plugin
    spec = dict(unavailable_policy=policy, compose=None if connection_only else 'compose.yaml', container=None,
                live_override_env='CUSTOM_HOST')
    def missing(*a):
        raise FileNotFoundError('docker unavailable')
    monkeypatch.setattr(plugin, '_docker_mod', SimpleNamespace(SUPPORTED_SERVICES={'teradata'},
        run_pre_up=lambda svc: True, unavailable=lambda svc: None, _service_spec=lambda svc: spec, ensure_up=missing))
    monkeypatch.setattr(plugin, '_load_service_yaml', lambda svc: spec)
    monkeypatch.setattr(plugin, '_require_service_opt_in', lambda svc: None)
    monkeypatch.delenv('CUSTOM_HOST', raising=False)
    with pytest.raises(pytest.fail.Exception if policy == 'fail' else pytest.skip.Exception):
        plugin._provision_requires('strict-teradata', ['teradata'], FileLock(str(tmp_path / 'lock')))


@pytest.mark.parametrize('named', [False, True])
@pytest.mark.parametrize('override', ['relative_int', 'absolute_int', 'shared', 'default'])
def test_outer_start_uses_same_cache_for_hook_guard_and_compose(tmp_path, monkeypatch, named, override):
    from inttest import resources
    from livetest import prestart
    integration = tmp_path / 'integration/teradata'
    live = tmp_path / 'live/teradata'
    integration.mkdir(parents=True)
    live.mkdir(parents=True)
    (integration / 'service.yaml').write_text(
        'name: teradata\ncompose: compose.yaml\n'
        'live_service_paths: {INT_TERADATA_DEPS_DIR: deps}\n'
        'pre_up: pre-up.sh\npre_up_check: always\nunavailable_policy: fail\n')
    (integration / 'pre-up.sh').write_text('# fixture hook\n')
    (live / 'service.yaml').write_text(
        'required_files: [deps/disk1.qcow2, deps/disk2.qcow2, deps/disk3.qcow2]\n'
        'required_files_env: {SLT_TERADATA_DEPS_DIR: deps}\n')
    for key in ('INT_TERADATA_DEPS_DIR', 'SLT_TERADATA_DEPS_DIR'):
        monkeypatch.delenv(key, raising=False)
    cache = live / 'deps'
    if override.endswith('_int'):
        cache = integration / 'cache'
        monkeypatch.setenv('INT_TERADATA_DEPS_DIR', './cache' if override == 'relative_int' else str(cache))
        monkeypatch.setenv('SLT_TERADATA_DEPS_DIR', str(tmp_path / 'wrong-shared-cache'))
    elif override == 'shared':
        cache = tmp_path / 'shared-cache'
        monkeypatch.setenv('SLT_TERADATA_DEPS_DIR', str(cache))
    def populate():
        cache.mkdir(parents=True, exist_ok=True)
        for i in (1, 2, 3):
            (cache / f'disk{i}.qcow2').write_bytes(b'fixture')
    if not named:
        populate()
    hook_envs, compose_envs = [], []
    def execute(path, directory, env, timeout):
        assert named, 'derived start must not run a hook'
        assert directory == integration
        hook_envs.append(env)
        populate()
        return 0, ''
    def compose(argv, **kwargs):
        assert argv[:2] == ['docker', 'compose']
        compose_envs.append(kwargs['env'])
        return SimpleNamespace(returncode=0)
    monkeypatch.setenv('SLT_PRE_UP', '1')
    monkeypatch.setattr(resources, 'service_dir', lambda name: integration)
    monkeypatch.setattr(services, 'live_service_dir', lambda name: live)
    monkeypatch.setattr(services, '_checked_compose_file', lambda name: integration / 'compose.yaml')
    monkeypatch.setattr(cli, '_resolve_services', lambda targets: ['teradata'])
    monkeypatch.setattr(cli, '_lock_file', lambda: tmp_path / 'compose.lock')
    monkeypatch.setattr(prestart, 'lock_path', lambda name: tmp_path / 'pre-up.lock')
    monkeypatch.setattr(prestart, '_execute', execute)
    monkeypatch.setattr(services.subprocess, 'run', compose)
    assert cli.cmd_up(['teradata'] if named else ['all']) == 0
    assert len(compose_envs) == 1
    assert len(hook_envs) == int(named)
    for env in hook_envs + compose_envs:
        assert env['INT_TERADATA_DEPS_DIR'] == str(cache)
    assert services.required_files('teradata') == [cache / f'disk{i}.qcow2' for i in (1, 2, 3)]
