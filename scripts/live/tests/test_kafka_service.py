from livetest.registry import load_service
from livetest.services import resolve
from livetest.plugin import build_service_tokens, _kafka_endpoints

def test_kafka_service_def_loads():
    defn = load_service("kafka")
    assert defn.isolation == "none"
    assert defn.container == "slt-kafka"
    assert defn.live_override_env == "SLT_KAFKA_HOST"
    for key in ("KAFKA_BROKER", "KAFKA_SCHEMA_REGISTRY_URL", "KAFKA_SRC_TOPIC", "KAFKA_TGT_TOPIC"):
        assert key in defn.provides

def test_kafka_resolve_docker_defaults():
    r = resolve("kafka", env={}, started=set(), compose_up=lambda defn: None)
    assert r.mode == "docker"
    assert r.base["port"] == 9092 and r.base["broker_port"] == 19092
    assert r.base["registry_port"] == 8081
    assert r.base["src_topic"] == "slt_src" and r.base["tgt_topic"] == "slt_tgt"

def test_kafka_tokens_use_view_host_and_docker_listener():
    defn = load_service("kafka")
    r = resolve("kafka", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    # the Striim app reaches the broker over the DOCKER listener + registry via view_host
    assert tokens["KAFKA_BROKER"] == "host.docker.internal:19092"
    assert tokens["KAFKA_SCHEMA_REGISTRY_URL"] == "http://host.docker.internal:8081"
    assert tokens["KAFKA_SRC_TOPIC"] == "slt_src"

def test_kafka_admin_endpoints_use_host_listener_locally():
    # Plain local run (no SLT_SERVICES_HOST): KafkaAdmin's own connection uses the HOST listener,
    # matching its advertised address ("localhost:9092" in kafka/compose.yaml).
    r = resolve("kafka", env={}, started=set(), compose_up=lambda defn: None)
    broker, registry_url = _kafka_endpoints(r.base)
    assert broker == "localhost:9092"
    assert registry_url == "http://localhost:8081"


def test_kafka_admin_endpoints_use_docker_listener_out_of_docker():
    # docker-out-of-docker (a containerized test runner): resolve() overrides base
    # host to SLT_SERVICES_HOST. KafkaAdmin must NOT use the HOST listener here — its advertised
    # address is hardcoded "localhost", which Kafka's own broker-metadata redirect would send the
    # client back to, and "localhost" inside the console's own container is the wrong box (self,
    # not the kafka container) -> ECONNREFUSED. It must use the DOCKER listener instead (the same
    # one the Striim app uses), which is reachable via the same host.docker.internal path.
    r = resolve("kafka", env={"SLT_SERVICES_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None)
    broker, registry_url = _kafka_endpoints(r.base)
    assert broker == "host.docker.internal:19092"
    assert registry_url == "http://host.docker.internal:8081"


def test_kafka_resolve_live_override():
    env = {"SLT_KAFKA_HOST": "khost", "SLT_KAFKA_PORT": "9999",
           "SLT_KAFKA_BROKER_PORT": "9999", "SLT_KAFKA_REGISTRY_PORT": "8888",
           "SLT_KAFKA_SRC_TOPIC": "s", "SLT_KAFKA_TGT_TOPIC": "t"}
    r = resolve("kafka", env=env, started=set(),
                compose_up=lambda defn: (_ for _ in ()).throw(AssertionError("no compose")))
    assert r.mode == "live" and r.base["src_topic"] == "s" and r.base["registry_port"] == "8888"
