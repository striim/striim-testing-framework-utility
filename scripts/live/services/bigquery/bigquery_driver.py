"""Bootstrap and route the credential-free local BigQuery emulator."""
from __future__ import annotations

import os
import subprocess
import time

from livetest.bigqueryadmin import BigQueryAdmin, BigQueryError, identifiers, local_http

ENV_KEYS = ("SLT_BIGQUERY_CPUS", "SLT_BIGQUERY_MEM_LIMIT", "SLT_BIGQUERY_IMAGE",
            "SLT_BIGQUERY_TOKEN_IMAGE", "SLT_BIGQUERY_WRITER_ENDPOINT", "SLT_BIGQUERY_TOKEN_URI")


def _existing_problem(env):
    if env.get("SLT_BIGQUERY_HOST") and not all(env.get(k) for k in (
            "SLT_BIGQUERY_WRITER_ENDPOINT", "SLT_BIGQUERY_TOKEN_URI")):
        return "an existing BigQuery emulator requires SLT_BIGQUERY_WRITER_ENDPOINT and SLT_BIGQUERY_TOKEN_URI"
    return None


def unsupported_mode(mode):
    why = _existing_problem(os.environ)
    if why:
        return why
    if mode != "docker" and not all(os.environ.get(k) for k in (
            "SLT_BIGQUERY_HOST", "SLT_BIGQUERY_WRITER_ENDPOINT", "SLT_BIGQUERY_TOKEN_URI")):
        return "native BigQuery tests require an explicit local emulator, writer endpoint and token URI"
    return None


def unavailable(env, mode):
    if env.get("SLT_BIGQUERY_HOST"):
        return _existing_problem(env)
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
                "docker buildx build --load -t slt-bigquery:0.8.1-framework.8 . "
                "or set SLT_BIGQUERY_IMAGE to your qualified local image")
    return None


def build_required(env):
    return not bool(env.get("SLT_BIGQUERY_IMAGE"))


def compose_env(env):
    identifiers(env.get("SLT_BIGQUERY_PROJECT") or "test-project", env.get("SLT_BIGQUERY_DATASET") or "slt")
    return {}


def admins(base, defn):
    why = _existing_problem(os.environ)
    if why:
        raise ValueError(why)
    return {"bigquery-target": {"admin": BigQueryAdmin(base), "schema": base["dataset"]}}


def provision(admins, client, progress):
    deadline = time.monotonic() + 20
    last = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            admins["bigquery-target"]["admin"].ensure_setup(remaining=min(5, remaining))
            return
        except BigQueryError as exc:
            last = exc
            time.sleep(max(0, min(0.2, deadline - time.monotonic())))
    raise last or BigQueryError("exact-read-timeout", "BigQuery bootstrap budget exhausted")
