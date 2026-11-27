"""The port-override guard: a prefixed stack must not fall back to a default host port."""
from livetest.preflight import missing_port_overrides

EC = {"SLT_STACK_PREFIX": "ec", "SLT_KAFKA_HOST_PORT": "9292",
      "SLT_KAFKA_DOCKER_HOST_PORT": "19292", "SLT_SCHEMA_REGISTRY_HOST_PORT": "8281"}


def test_names_the_variable_behind_a_port_collision():
    # The 2026-09-23 failure exactly: the kafka ports were set in that environment and
    # SLT_ZOOKEEPER_CLIENT_PORT was not, so ec-slt-zookeeper tried to bind 2181 and the daemon
    # said "port is already allocated" about a container instead of naming the variable.
    assert ("kafka", "SLT_ZOOKEEPER_CLIENT_PORT", "2181") in missing_port_overrides(["kafka"], EC)


def test_silent_once_the_override_is_set():
    assert missing_port_overrides(["kafka"], {**EC, "SLT_ZOOKEEPER_CLIENT_PORT": "2381"}) == []


def test_silent_without_a_stack_prefix():
    # An unprefixed stack owns the default ports; overriding them is not required.
    assert missing_port_overrides(["kafka"], {"SLT_KAFKA_HOST_PORT": "9292"}) == []


def test_a_prefixed_stack_must_set_the_vertica_eon_subnet():
    # Two stacks on one /24 fail at `network create` ("Pool overlaps"), like a shared host port.
    missing = missing_port_overrides(["vertica-eon"], {"SLT_STACK_PREFIX": "ec"})
    assert ("vertica-eon", "SLT_VERTICA_EON_SUBNET", "172.16.250") in missing
    assert "SLT_VERTICA_EON_SUBNET" not in {v for _s, v, _d in missing_port_overrides(
        ["vertica-eon"], {"SLT_STACK_PREFIX": "ec", "SLT_VERTICA_EON_SUBNET": "172.16.251"})}
    assert missing_port_overrides(["vertica-eon"], {}) == []


def test_a_port_two_services_read_is_reported_once():
    # vertica-eon's compose reads minio's SLT_MINIO_HOST_PORT for its S3 endpoint.
    missing = missing_port_overrides(["minio", "vertica-eon"], {"SLT_STACK_PREFIX": "ec"})
    assert [(s, v) for s, v, _d in missing if v == "SLT_MINIO_HOST_PORT"] == [("minio", "SLT_MINIO_HOST_PORT")]
