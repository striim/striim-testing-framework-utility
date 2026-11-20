"""Where the services' published ports bind on the host: SLT_BIND_ADDRESS.

Every `ports:` mapping in the service compose files is written twice::

    "${SLT_BIND_ADDRESS:-0.0.0.0}:<host port>:<container port>"
    "${SLT_BIND_GATEWAY:-${SLT_BIND_ADDRESS:-0.0.0.0}}:<host port>:<container port>"

Compose drops an entry identical to another, so with neither variable set (a plain `docker compose
up`, as a remote host serving other machines runs it) a port is published on all interfaces, as it
always was. The harness sets both for its own bring-ups (``compose_env``):

* SLT_BIND_ADDRESS set: that address only (both variables get it). ``0.0.0.0`` is all interfaces.
* unset: 127.0.0.1, which the framework's own clients and a native Striim use (``localhost``), plus
  the address containers reach the host on through ``host.docker.internal``. On Docker Desktop that
  is the host's loopback already, so nothing more. On a Linux engine it is the default bridge's
  gateway (what ``host-gateway`` resolves to), so the Docker Striim cluster still reaches the
  databases. Neither is reachable from another machine; forward a port over SSH for that.

A Linux engine whose ``host-gateway`` is not the default bridge's gateway (a daemon
``host-gateway-ip`` setting) needs SLT_BIND_ADDRESS. SLT_BIND_GATEWAY is derived, never a setting:
the harness always overwrites it.

Docker is asked only when SLT_BIND_ADDRESS is unset, through the bring-up's bounded query runner,
right before that bring-up's `compose up`, and once per process: the answer is kept. When Docker
does not answer (no daemon, no binary, the query timed out) the ports bind on 127.0.0.1 alone,
nothing is kept, and the `compose up` that follows reports the Docker problem itself.
"""
from __future__ import annotations

import ipaddress
import os
import sys

from livetest import dockercmd

BIND_ENV = "SLT_BIND_ADDRESS"
GATEWAY_ENV = "SLT_BIND_GATEWAY"
LOOPBACK = "127.0.0.1"


class BindAddressError(RuntimeError):
    """SLT_BIND_ADDRESS is not an IPv4 address, or the address containers reach the host on could
    not be found. The bring-up stops before `compose up`."""


def _ipv4(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def compose_env(env=None, run=None) -> dict:
    """{SLT_BIND_ADDRESS, SLT_BIND_GATEWAY} for a `docker compose up` the harness runs. ``env`` is the
    effective settings (shell over .env); it is not changed. ``run`` is the bring-up's bounded Docker
    query runner (dockercmd.run by default); it is used only when SLT_BIND_ADDRESS is unset."""
    env = os.environ if env is None else env
    explicit = (env.get(BIND_ENV) or "").strip()
    if explicit:
        if not _ipv4(explicit):
            raise BindAddressError(
                f"{BIND_ENV}={explicit!r} is not an IPv4 address: set the address the service ports "
                f"are published on (0.0.0.0 for every interface), or unset it for this machine and "
                f"its containers only")
        return {BIND_ENV: explicit, GATEWAY_ENV: explicit}
    gateway = _cached_gateway(run)
    return {BIND_ENV: LOOPBACK, GATEWAY_ENV: gateway or LOOPBACK}


_CACHE = {}
_UNANSWERED = object()


def _cached_gateway(run=None):
    """detect_gateway(), asked of Docker once per process: every bring-up of a run binds alike. When
    Docker did not answer, loopback alone (None) and nothing is kept, so a later bring-up asks again."""
    if "gateway" not in _CACHE:
        gateway = detect_gateway(run)
        if gateway is _UNANSWERED:
            print(f"[services] Docker did not answer `docker info`: service ports bind on {LOOPBACK} "
                  f"only, out of the containers' reach ({BIND_ENV} sets the address)", file=sys.stderr)
            return None
        _CACHE["gateway"] = gateway
    return _CACHE["gateway"]


def _fail(what: str, detail: str) -> BindAddressError:
    return BindAddressError(
        f"cannot tell which address containers reach this host on ({what}: {detail}); set "
        f"{BIND_ENV} to the address the service ports should be published on")


def _answer(r) -> str:
    """A query's output, or "" when it failed or printed nothing."""
    out = getattr(r, "stdout", None) if getattr(r, "returncode", 1) == 0 else None
    return out.strip() if isinstance(out, str) else ""


def detect_gateway(run=None):
    """None on Docker Desktop (containers reach the host's loopback). Otherwise the default bridge
    network's IPv4 gateway, which ``host-gateway`` resolves to. ``_UNANSWERED`` when `docker info`
    gets no answer (no binary, no daemon, timed out): Docker is not there to publish anything. Each
    query is bounded by dockercmd's query timeout. Once Docker has answered, a bridge gateway that
    cannot be read raises BindAddressError rather than binding where the containers cannot reach."""
    run = run or dockercmd.run
    try:
        engine = _answer(run(["docker", "info", "--format", "{{.OperatingSystem}}"],
                             timeout=dockercmd.QUERY_TIMEOUT_S))
    except OSError:                                          # no docker binary
        return _UNANSWERED
    if not engine:
        return _UNANSWERED
    if "docker desktop" in engine.lower():
        return None
    r = run(["docker", "network", "inspect", "bridge", "--format",
             "{{range .IPAM.Config}}{{.Gateway}} {{end}}"], timeout=dockercmd.QUERY_TIMEOUT_S)
    if r.returncode != 0:
        detail = ("timed out" if r.returncode == dockercmd.TIMED_OUT
                  else (r.stderr or r.stdout or f"exit {r.returncode}").strip()[-300:])
        raise _fail("docker network inspect bridge", detail)
    gateway = next((a for a in _answer(r).split() if _ipv4(a)), None)
    if not gateway:
        raise _fail("docker network inspect bridge", "no IPv4 gateway on the default bridge network")
    return gateway
