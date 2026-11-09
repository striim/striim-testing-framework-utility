"""Hermetic: which emulator BigtableAdmin's real google-cloud-bigtable clients dial.

These build the SDK's own admin and data clients -- no fakes -- and read the target of the gRPC
channel each one holds. A gRPC channel connects lazily, so nothing here opens a connection.
Two admins in one process must each reach the endpoint in their own DSN, and building them must
leave BIGTABLE_EMULATOR_HOST as it found it: that variable redirects every Bigtable client in the
process.
"""
import os

import pytest

from google.cloud.bigtable.data import BigtableDataClient

from livetest import bigtableadmin
from livetest.bigtableadmin import BigtableAdmin

ENV = "BIGTABLE_EMULATOR_HOST"


def _dsn(endpoint):
    return {"project": "test-project", "instance": "test-instance", "emulator_host": endpoint}


def _target(channel) -> str:
    # grpc.Channel -> its C-core channel's target; the data client wraps its channel once more.
    while not hasattr(channel, "target"):
        channel = channel._channel
    return channel.target().decode().removeprefix("dns:///")


def _targets(admin: BigtableAdmin):
    tac = admin._admin().table_admin_client
    data = admin._data()
    assert isinstance(data, BigtableDataClient)
    return _target(tac.transport.grpc_channel), _target(data.transport.grpc_channel)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(bigtableadmin, "_CLIENTS", {})
    monkeypatch.delenv(ENV, raising=False)
    yield


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_two_endpoints_in_one_process_each_dial_their_own():
    first = BigtableAdmin(_dsn("localhost:9470"))
    second = BigtableAdmin(_dsn("localhost:19470"))
    assert _targets(first) == ("localhost:9470", "localhost:9470")
    assert _targets(second) == ("localhost:19470", "localhost:19470")
    assert ENV not in os.environ


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_a_conflicting_emulator_variable_is_refused_and_left_alone(monkeypatch):
    monkeypatch.setenv(ENV, "localhost:1")
    admin = BigtableAdmin(_dsn("localhost:9470"))
    with pytest.raises(ValueError, match="BIGTABLE_EMULATOR_HOST"):
        admin._pair()
    assert os.environ[ENV] == "localhost:1"
    assert bigtableadmin._CLIENTS == {}


@pytest.mark.filterwarnings("ignore:Connecting to Bigtable emulator:RuntimeWarning")
def test_a_matching_emulator_variable_is_used_and_kept(monkeypatch):
    monkeypatch.setenv(ENV, "localhost:9470")
    assert _targets(BigtableAdmin(_dsn("localhost:9470"))) == ("localhost:9470", "localhost:9470")
    assert os.environ[ENV] == "localhost:9470"


@pytest.fixture
def _no_client_may_be_built(monkeypatch):
    # Any SDK client constructor reached records itself instead of building a client.
    import google.cloud.bigtable.client
    import google.cloud.bigtable.data
    import google.cloud.bigtable_admin_v2
    built = []
    for module, name in ((google.cloud.bigtable.client, "Client"),
                         (google.cloud.bigtable.data, "BigtableDataClient"),
                         (google.cloud.bigtable_admin_v2, "BaseBigtableTableAdminClient")):
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
