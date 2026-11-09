"""Hermetic: which emulator BigtableAdmin's real google-cloud-bigtable clients dial.

These build the SDK's own clients -- no fakes -- and read the target of the gRPC channel each one
holds. A gRPC channel connects lazily, so nothing here opens a connection. Each client is bound to
the endpoint in its own DSN through an explicit channel: building one never reads or writes
BIGTABLE_EMULATOR_HOST (which redirects every SDK client in the process that does read it) and
never reaches application-default credentials, whatever else the process is doing meanwhile.
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import google.auth
import google.cloud.bigtable.data
import google.cloud.bigtable_v2
import pytest
from google.cloud.bigtable.data import BigtableDataClient

from inttest import bigtableadmin as integration_tier
from livetest import bigtableadmin as live_tier
from inttest import bigtableadmin
from inttest.bigtableadmin import BigtableAdmin

ENV = "BIGTABLE_EMULATOR_HOST"
# The SDK's data-client constructors: the veneer one reads ENV, the GAPIC one takes a transport.
DATA_CONSTRUCTORS = ((google.cloud.bigtable.data, "BigtableDataClient"),
                     (google.cloud.bigtable_v2, "BigtableClient"))


def _dsn(endpoint):
    return {"project": "test-project", "instance": "test-instance", "emulator_host": endpoint}


def _target(channel) -> str:
    # grpc.Channel -> its C-core channel's target, unwrapping any channel wrapper.
    while not hasattr(channel, "target"):
        channel = channel._channel
    return channel.target().decode().removeprefix("dns:///")


def _targets(admin: BigtableAdmin):
    tac = admin._admin().table_admin_client
    data = admin._data()
    return _target(tac.transport.grpc_channel), _target(data._gapic_client.transport.grpc_channel)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for module in (live_tier, integration_tier):
        monkeypatch.setattr(module, "_CLIENTS", {})
    monkeypatch.delenv(ENV, raising=False)
    adc = []

    def forbidden(*args, **kwargs):
        adc.append(args)
        raise RuntimeError("application-default credentials reached")

    monkeypatch.setattr(google.auth, "default", forbidden)
    yield
    assert adc == [], "a client went looking for cloud credentials"


def _hold(monkeypatch, module_attrs, thread_prefix, reached, release):
    """Wrap constructors so a call from a thread named `thread_prefix*` stops until `release`."""
    for module, name in module_attrs:
        real = getattr(module, name)

        def held(*args, _real=real, **kwargs):
            if threading.current_thread().name.startswith(thread_prefix):
                reached.set()
                assert release.wait(5)
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, name, held)


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_two_endpoints_in_one_process_each_dial_their_own():
    first = BigtableAdmin(_dsn("localhost:9470"))
    second = BigtableAdmin(_dsn("localhost:19470"))
    assert _targets(first) == ("localhost:9470", "localhost:9470")
    assert _targets(second) == ("localhost:19470", "localhost:19470")
    assert ENV not in os.environ


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_a_preset_emulator_variable_neither_redirects_nor_changes(monkeypatch):
    # Nothing in BigtableAdmin reads it any more, so another emulator named there is not refused.
    monkeypatch.setenv(ENV, "localhost:1")
    assert _targets(BigtableAdmin(_dsn("localhost:9470"))) == ("localhost:9470", "localhost:9470")
    assert os.environ[ENV] == "localhost:1"


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_a_matching_emulator_variable_is_used_and_kept(monkeypatch):
    monkeypatch.setenv(ENV, "localhost:9470")
    assert _targets(BigtableAdmin(_dsn("localhost:9470"))) == ("localhost:9470", "localhost:9470")
    assert os.environ[ENV] == "localhost:9470"


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_an_empty_emulator_variable_is_left_exactly_as_it_was(monkeypatch):
    monkeypatch.setenv(ENV, "")
    assert _targets(BigtableAdmin(_dsn("localhost:9470"))) == ("localhost:9470", "localhost:9470")
    assert os.environ[ENV] == ""


def test_a_failed_construction_leaves_the_environment_and_the_cache_alone(monkeypatch):
    monkeypatch.setenv(ENV, "")

    def broken(*args, **kwargs):
        raise RuntimeError("constructor interrupted")

    for module, name in DATA_CONSTRUCTORS:
        monkeypatch.setattr(module, name, broken)
    with pytest.raises(RuntimeError, match="constructor interrupted"):
        BigtableAdmin(_dsn("localhost:9470"))._pair()
    assert os.environ[ENV] == ""
    assert bigtableadmin._CLIENTS == {}


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_binding_never_reads_or_writes_the_emulator_variable(monkeypatch):
    seen = []

    class Watched(dict):
        def _note(self, key, how):
            if key == ENV:
                seen.append(how)

        def get(self, key, default=None):
            self._note(key, "get")
            return super().get(key, default)

        def __getitem__(self, key):
            self._note(key, "read")
            return super().__getitem__(key)

        def __contains__(self, key):
            self._note(key, "contains")
            return super().__contains__(key)

        def __setitem__(self, key, value):
            self._note(key, "write")
            super().__setitem__(key, value)

        def __delitem__(self, key):
            self._note(key, "delete")
            super().__delitem__(key)

        def pop(self, key, *default):
            self._note(key, "pop")
            return super().pop(key, *default)

    monkeypatch.setattr(os, "environ", Watched(os.environ))
    BigtableAdmin(_dsn("localhost:9470"))._pair()
    assert seen == []


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
@pytest.mark.parametrize("first, second", [(live_tier, integration_tier), (integration_tier, live_tier)],
                         ids=["live-first", "integration-first"])
def test_both_tiers_building_at_once_each_keep_their_emulator(monkeypatch, first, second):
    # The schedule that lost the emulator when the data client was pointed through ENV: the first
    # tier stops after building its admin, the second stops just before building its data client,
    # then the first finishes before the second resumes.
    first_admin_built, release_first = threading.Event(), threading.Event()
    second_at_data, release_second = threading.Event(), threading.Event()
    real_admin = first._EmulatorTableAdmin

    def hold_first(client):
        wrapped = real_admin(client)
        first_admin_built.set()
        assert release_first.wait(5)
        return wrapped

    monkeypatch.setattr(first, "_EmulatorTableAdmin", hold_first)
    _hold(monkeypatch, DATA_CONSTRUCTORS, "second", second_at_data, release_second)
    with ThreadPoolExecutor(1, thread_name_prefix="first") as a, \
            ThreadPoolExecutor(1, thread_name_prefix="second") as b:
        one = a.submit(first.BigtableAdmin(_dsn("localhost:9470"))._pair)
        assert first_admin_built.wait(5)
        two = b.submit(second.BigtableAdmin(_dsn("localhost:19470"))._pair)
        try:
            assert second_at_data.wait(5)
            release_first.set()
            one.result(timeout=5)
        finally:
            release_first.set()
            release_second.set()
        two.result(timeout=5)
    assert _targets(first.BigtableAdmin(_dsn("localhost:9470"))) == ("localhost:9470",) * 2
    assert _targets(second.BigtableAdmin(_dsn("localhost:19470"))) == ("localhost:19470",) * 2
    assert ENV not in os.environ


def test_a_client_built_elsewhere_meanwhile_sees_no_redirect(monkeypatch):
    # While BigtableAdmin is part-way through building its data client, the environment is exactly
    # what every other SDK constructor in the process would read.
    reached, release = threading.Event(), threading.Event()
    _hold(monkeypatch, DATA_CONSTRUCTORS, "binder", reached, release)
    with ThreadPoolExecutor(1, thread_name_prefix="binder") as pool:
        pending = pool.submit(BigtableAdmin(_dsn("localhost:9470"))._pair)
        try:
            assert reached.wait(5)
            observed = os.environ.get(ENV)
        finally:
            release.set()
        pending.result(timeout=5)
    assert observed is None


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_another_components_emulator_client_keeps_its_own_endpoint_meanwhile(monkeypatch):
    # Another part of the process uses an emulator through ENV; a real SDK client it builds while
    # BigtableAdmin is part-way through binding still reaches that emulator, and ours reaches ours.
    monkeypatch.setenv(ENV, "localhost:1")
    reached, release = threading.Event(), threading.Event()
    _hold(monkeypatch, DATA_CONSTRUCTORS, "binder", reached, release)
    with ThreadPoolExecutor(1, thread_name_prefix="binder") as pool:
        admin = BigtableAdmin(_dsn("localhost:9470"))
        pending = pool.submit(admin._pair)
        try:
            assert reached.wait(5)
            other = BigtableDataClient(project="other-project")
            observed = _target(other.transport.grpc_channel)
            other.close()
        finally:
            release.set()
        pending.result(timeout=5)
    assert observed == "localhost:1"
    assert _targets(admin) == ("localhost:9470", "localhost:9470")
    assert os.environ[ENV] == "localhost:1"


@pytest.fixture
def _no_client_may_be_built(monkeypatch):
    # Any SDK client constructor reached records itself instead of building a client.
    import google.cloud.bigtable.client
    import google.cloud.bigtable.data
    import google.cloud.bigtable_admin_v2
    built = []
    for module, name in ((google.cloud.bigtable.client, "Client"),
                         (google.cloud.bigtable.data, "BigtableDataClient"),
                         (google.cloud.bigtable_admin_v2, "BaseBigtableTableAdminClient"),
                         (google.cloud.bigtable_v2, "BigtableClient")):
        monkeypatch.setattr(module, name, lambda *a, _name=name, **k: built.append(_name))
    return built


@pytest.mark.parametrize("dsn", [
    {"project": "test-project", "instance": "test-instance"},
    {"project": "test-project", "instance": "test-instance", "emulator_host": ""},
    {"project": "test-project", "instance": "test-instance", "credentials": "/keys/sa.json", "tid": "t1_"},
    {"project": "test-project", "instance": "test-instance", "emulator_host": "localhost:9470",
     "credentials": "/keys/sa.json"},
], ids=["no-endpoint", "empty-endpoint", "credentials-only", "credentials-and-endpoint"])
def test_anything_but_an_emulator_is_refused_before_a_client_is_built(_no_client_may_be_built, dsn):
    # Real Bigtable is not supported yet: with ambient Google credentials, create_table's
    # drop-first would delete a real table.
    with pytest.raises(ValueError, match="emulator-only"):
        BigtableAdmin(dsn)._pair()
    assert _no_client_may_be_built == []


def test_a_credentials_route_is_refused_before_a_client_is_built(_no_client_may_be_built):
    from inttest import dbroutes
    tokens = {"BIGTABLE_PROJECT": "test-project", "BIGTABLE_INSTANCE": "test-instance",
              "BIGTABLE_HOST": "localhost", "BIGTABLE_GRPC_PORT": "19470",
              "BIGTABLE_CREDENTIALS": "/keys/sa.json", "TID": "t1_"}
    with pytest.raises(ValueError, match="emulator-only"):
        dbroutes.setup_bigtable("t1_rows", {"cf": None}, tokens=tokens)
    assert _no_client_may_be_built == []
