from livetest.registry import load_service
from livetest.services import resolve
from livetest.plugin import build_service_tokens

def test_gcs_service_def_loads():
    defn = load_service("gcs")
    assert defn.isolation == "none"
    assert defn.container == "slt-gcs"
    assert defn.live_override_env == "SLT_GCS_HOST"
    for key in ("GCS_PROJECT", "GCS_ENDPOINT", "GCS_SRC_BUCKET", "GCS_TGT_BUCKET"):
        assert key in defn.provides

def test_gcs_resolve_docker_defaults():
    r = resolve("gcs", env={}, started=set(), compose_up=lambda defn: None)
    assert r.mode == "docker"
    assert r.base["project"] == "test-project"
    assert r.base["port"] == 4443
    assert r.base["src_bucket"] == "slt-src" and r.base["tgt_bucket"] == "slt-tgt"
    assert r.base["view_host"] == "localhost"

def test_gcs_resolve_live_override():
    env = {"SLT_GCS_HOST": "gcshost", "SLT_GCS_PORT": "9999",
           "SLT_GCS_PROJECT": "proj", "SLT_GCS_SRC_BUCKET": "s", "SLT_GCS_TGT_BUCKET": "t"}
    r = resolve("gcs", env=env, started=set(),
                compose_up=lambda defn: (_ for _ in ()).throw(AssertionError("no compose")))
    assert r.mode == "live" and r.base["project"] == "proj" and r.base["src_bucket"] == "s"

def test_gcs_endpoint_token_uses_view_host():
    defn = load_service("gcs")
    r = resolve("gcs", env={"SLT_STRIIM_VIEW_HOST": "host.docker.internal"},
                started=set(), compose_up=lambda defn: None)
    tokens = build_service_tokens(defn, r, schema="")
    assert tokens["GCS_ENDPOINT"] == "http://host.docker.internal:4443"
    assert tokens["GCS_SRC_BUCKET"] == "slt-src"


# --- -external-url must track the published port ------------------------------------------
# Added 2026-08-19. fake-gcs-server echoes -external-url in mediaLink/selfLink; unset, it
# defaults to the CONTAINER-INTERNAL 0.0.0.0:4443. That coincides with the published port only
# on the default stack, so a second stack's every media download was refused while every
# metadata call succeeded -- the whole objectwriter suite, and invisible until you remap.

def test_external_url_tracks_the_published_host_port():
    import re
    from pathlib import Path
    compose = (Path(__file__).resolve().parents[1] / "services" / "gcs" / "compose.yaml").read_text()
    m = re.search(r'"-external-url",\s*"([^"]+)"', compose)
    assert m, "services/gcs/compose.yaml must pass -external-url explicitly: its default is the " \
              "container-internal port, which only matches the published one on the default stack"
    assert "SLT_GCS_HOST_PORT" in m.group(1), (
        f"-external-url must interpolate the published host port, got {m.group(1)!r} -- "
        "otherwise remapping the port moves the emulator and not the client following mediaLink")
