"""Disabled public-client regression; its success oracle stays one exact row."""
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from livetest.bigqueryadmin import local_http
from livetest.plugin import _make_fake_gcp_key


@pytest.mark.live
@pytest.mark.skip(reason=(
    "Installed BigQuery Java client 2.59.0 omits the final request when the payload "
    "is an exact multiple of the upload chunk; no load job is created."
))
def test_exact_chunk_boundary_loads_one_literal_row(tmp_path):
    """Remove the skip for a corrected client; never change the one-row expectation."""
    endpoint = local_http(os.environ["BIGQUERY_TEST_ENDPOINT"])
    token = local_http(os.environ["BIGQUERY_TEST_TOKEN_ENDPOINT"]) + "/token"
    classpath = os.environ["BIGQUERY_JAVA_CLASSPATH"]
    key = tmp_path / "fake-key.json"
    key.write_text(_make_fake_gcp_key(token, "test-project"))
    key.chmod(0o600)
    source = Path(__file__).parent / "clients" / "ExactChunkBoundary.java"
    subprocess.run(["javac", "--release", "11", "-cp", classpath, "-d", str(tmp_path),
                    str(source)], check=True, timeout=60)
    subprocess.run(["java", "-cp", str(tmp_path) + os.pathsep + classpath,
                    "ExactChunkBoundary", endpoint, str(key), "boundary_" + uuid.uuid4().hex],
                   check=True, timeout=60)
