"""Integration test for inttest.services.ensure_up + inttest.gcsadmin against a REAL
fake-gcs-server emulator -- the smoke test for the gcs service itself.

Gated: this carries @pytest.mark.gcs, but unlike Postgres (never gated) this
service is opt-in like Spanner, so the test calls `plugin._require_service_opt_in
("gcs")` directly -- mirrors test_spanner_live.py's module docstring on why an
explicit call is used instead of relying on a fixture: there is no session-scoped
`gcs_admin`/`gcs_client` fixture here (the YAML
`requires:` pipeline calls `inttest.plugin._ensure_gcs_bucket` directly from
`IntYamlItem.runtest` step 4b, not through a fixture), so this standalone test
gates itself the same explicit way test_plugin_wiring.py's unit tests do.

The test body's own `services.ensure_up_for_test("gcs", lock)` brings the
emulator up if it isn't already (and tears it back down after, only if this call
started it) -- mirrors test_services_live.py's identical pattern.
"""
from __future__ import annotations

import pytest
from filelock import FileLock

from inttest import gcsadmin, paths, plugin, services
from inttest.tokens import build_tokens

# Brings up Docker services: `pytest -m "not docker"` deselects this file (5.4 M1 FS4).
pytestmark = pytest.mark.docker


@pytest.mark.gcs
@pytest.mark.integration
def test_ensure_bucket_then_upload_download_roundtrip(tmp_path):
    plugin._require_service_opt_in("gcs")

    lock = FileLock(str(paths.state_dir() / ".int-compose.lock"))

    with services.ensure_up_for_test("gcs", lock):
        tokens = build_tokens(tmp_path, requires=["gcs"], parallel=False)
        assert tokens["TID"] == ""  # serial run: no per-test prefix

        gcsadmin.ensure_bucket(tokens)
        # Idempotent: calling it again against an already-existing bucket must not
        # raise (the TOCTOU/"already exists" tolerance ensure_bucket documents).
        gcsadmin.ensure_bucket(tokens)

        client = gcsadmin._client(tokens)
        bucket = client.bucket(tokens["GCS_BUCKET"])
        blob_name = "slice4-smoke-test.bin"
        payload = b"objectwriter integration-tier smoke test"
        try:
            bucket.blob(blob_name).upload_from_string(payload)
            assert bucket.blob(blob_name).download_as_bytes() == payload
        finally:
            blob = bucket.blob(blob_name)
            if blob.exists():
                blob.delete()


@pytest.mark.gcs
@pytest.mark.integration
def test_missing_objects_then_delete_objects_roundtrip(tmp_path):
    """The gcs_objects: assertion's two collaborators, against a real
    emulator: an uploaded key is not "missing"; deleting it makes it missing again;
    deleting an already-absent key (the common case -- see _prune_gcs_objects's own
    docstring) must not raise."""
    plugin._require_service_opt_in("gcs")

    lock = FileLock(str(paths.state_dir() / ".int-compose.lock"))

    with services.ensure_up_for_test("gcs", lock):
        tokens = build_tokens(tmp_path, requires=["gcs"], parallel=False)
        gcsadmin.ensure_bucket(tokens)

        key = "slice5c-smoke-test/roundtrip.bin"
        bucket = gcsadmin._bucket(tokens)
        try:
            bucket.blob(key).upload_from_string(b"slice5c gcs_objects roundtrip")
            assert gcsadmin.missing_objects(tokens, [key]) == []

            gcsadmin.delete_objects(tokens, [key])
            assert gcsadmin.missing_objects(tokens, [key]) == [key]

            # Deleting an already-absent key must not raise (NotFound tolerance).
            gcsadmin.delete_objects(tokens, [key])
        finally:
            blob = bucket.blob(key)
            if blob.exists():
                blob.delete()


def test_ensure_bucket_rejects_an_unsafe_bucket_name():
    with pytest.raises(ValueError, match="unsafe bucket name"):
        gcsadmin._check_bucket("../etc/passwd")


def test_build_tokens_resolves_gcs_provides_templates_with_no_docker(tmp_path):
    """Docker-free, ungated: `services/gcs/service.yaml`'s `provides:` templates
    render against `docker_defaults` alone -- a typo'd key (e.g. `{veiw_host}`) would
    raise `ServiceConfigError` here, at plain `pytest` collection-adjacent speed,
    rather than only surfacing inside `IntYamlItem.runtest` step 4b against a real
    Docker cluster (which nothing in this module drives -- see the module docstring
    on why `plugin._ensure_gcs_bucket` itself stays untested by this file).

    The assertions are the service.yaml DEFAULTS. Carrying no `integration` marker is
    what makes that safe: conftest.py's `_hermetic_stack_env` clears INT_GCS_HOST_PORT
    and every other stack variable first, so a developer shell running a second stack
    does not rewrite the expected values out from under it."""
    tokens = build_tokens(tmp_path, requires=["gcs"], parallel=False)
    assert tokens["GCS_PROJECT"] == "test-project"
    assert tokens["GCS_ENDPOINT"] == "http://localhost:14443"
    assert tokens["GCS_BUCKET"] == "int-test-bucket"
