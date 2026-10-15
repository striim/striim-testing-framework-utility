"""Bootstrap and route the credential-free local BigQuery emulator."""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from urllib.parse import urlsplit

ENV_KEYS = ("SLT_BIGQUERY_CPUS", "SLT_BIGQUERY_MEM_LIMIT", "SLT_BIGQUERY_IMAGE",
            "SLT_BIGQUERY_TOKEN_IMAGE", "SLT_BIGQUERY_WRITER_ENDPOINT", "SLT_BIGQUERY_TOKEN_URI")


def local_http(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("BigQuery emulator endpoints must be plain local HTTP URLs without credentials")
    if not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("BigQuery emulator endpoint must name a host, without an API path")
    if parsed.hostname not in ("localhost", "host.docker.internal"):
        address = ipaddress.ip_address(parsed.hostname)
        if address.version != 4 or not (address.is_private or address.is_loopback) or address.is_unspecified:
            raise ValueError("BigQuery emulator endpoint must resolve inside the local test stack")
    if parsed.port is None:
        raise ValueError("BigQuery emulator endpoint must include its test port")
    return value.rstrip("/")


def identifiers(project: str, dataset: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        raise ValueError("invalid BigQuery test project identifier")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,1023}", dataset):
        raise ValueError("invalid BigQuery test dataset identifier")


def unsupported_mode(mode):
    if mode != "docker" and not all(os.environ.get(k) for k in (
            "SLT_BIGQUERY_HOST", "SLT_BIGQUERY_WRITER_ENDPOINT", "SLT_BIGQUERY_TOKEN_URI")):
        return "native BigQuery tests require an explicit local emulator, writer endpoint and token URI"
    return None


def unavailable(env, mode):
    if env.get("SLT_BIGQUERY_HOST"):
        return None
    image = env.get("SLT_BIGQUERY_IMAGE")
    if not image:
        return None
    try:
        result = subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "cannot inspect the local BigQuery emulator image; verify Docker and build the pinned recipe"
    if result.returncode:
        return ("BigQuery emulator image is missing; build services/bigquery/Dockerfile with "
                "docker buildx build --load -t slt-bigquery:0.8.1-framework.0 . "
                "or set SLT_BIGQUERY_IMAGE to your qualified local image")
    return None


def build_required(env):
    return not bool(env.get("SLT_BIGQUERY_IMAGE"))


def compose_env(env):
    identifiers(env.get("SLT_BIGQUERY_PROJECT") or "test-project", env.get("SLT_BIGQUERY_DATASET") or "slt")
    return {}


class BigQueryAdmin:
    """Explicit REST client with anonymous credentials; no ADC or Storage Read API."""

    def __init__(self, base):
        from google.api_core.client_options import ClientOptions
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import bigquery
        self.project, self.dataset = base["project"], base["dataset"]
        identifiers(self.project, self.dataset)
        self.endpoint = local_http(f"http://{base['host']}:{base['port']}")
        self.client = bigquery.Client(project=self.project, credentials=AnonymousCredentials(),
                                     client_options=ClientOptions(api_endpoint=self.endpoint))

    def ensure_setup(self):
        from google.api_core.exceptions import NotFound
        name = f"{self.project}.{self.dataset}"
        try:
            self.client.get_dataset(name, retry=None, timeout=5)
        except NotFound:
            self.client.create_dataset(name, exists_ok=True, retry=None, timeout=5)
        rows = list(self.client.query("SELECT 1", retry=None, job_retry=None, timeout=5).result(retry=None, job_retry=None, timeout=5))
        if len(rows) != 1 or rows[0][0] != 1:
            raise RuntimeError("BigQuery emulator query healthcheck returned an unexpected result")


def admins(base, defn):
    return {"bigquery-target": {"admin": BigQueryAdmin(base), "schema": base["dataset"]}}


def provision(admins, client, progress):
    admins["bigquery-target"]["admin"].ensure_setup()
