"""Stack-prefix resolution for parallel live stacks (``SLT_STACK_PREFIX``).

One host can run TWO independent live stacks (cluster + service containers) side by side —
opt-in via ``SLT_STACK_PREFIX``. The prefix goes IN FRONT of the ``slt`` family token
(``alt`` -> ``alt-slt-striim``), for three reasons:

* the ``slt-`` marker survives intact, so name-based greps/filters still see the family;
* the same rule namespaces the compose PROJECT names (``striim`` -> ``alt-striim``), which is
  what actually isolates networks/volumes — compose derives those from the project;
* the joiner is a plain ``f"{prefix}-{name}"``, applied identically to container names,
  compose projects (interpolated in each compose.yaml as
  ``${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}``) and the per-stack coordination files.

Unset/empty prefix returns every name byte-identical to the historical value — the compose
files' ``:+`` interpolation makes the same guarantee on their side, so "no prefix" is provably
the status quo.

Only HOST-visible names are prefixed. Compose SERVICE names, ``hostname:`` values and
in-network DNS references (``slt-kafka``, ``PRIMARY_HOSTNAME=slt-striim``, the baked image
entrypoint's ``striim-node``) stay unprefixed — each stack gets its own compose project
and therefore its own network, so those names can never collide across stacks, and the Striim
image's baked-in hostnames keep working.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from livetest import lockdir

# Compose project names must match [a-z0-9][a-z0-9_-]*; keep the prefix to the dashed subset so
# one rule is valid everywhere (project names, container names, filenames).
_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Base (unprefixed) CONTAINER names — the `docker exec/cp/restart/logs/inspect` targets.
#
# The compose SERVICE names match these. The `hostname:` values do NOT: they are
# `striim-node` / `striim-agent`, matching what the image's entrypoint bakes into
# striim.node.servernode.address (and pinned as network aliases, since a hostname is not
# reliably resolvable by siblings). Hostname and baked name must move together -- changing
# one alone leaves the primary waiting for a node that never registers.
# The `slt-striim-` stem is dropped from the CONTAINER names because the compose PROJECT is
# `slt-striim`: Docker Desktop groups by project and strips that prefix off the label, so
# `slt-striim-node` showed as a bare "node" — the artifact e276393ab fixed for
# slt-gcs-token -> slt-token.
STRIIM_CONTAINER = "slt-striim"
APP_NODES = ("slt-striim", "slt-node")
AGENT_CONTAINER = "slt-agent"
CLUSTER_CONTAINERS = ("slt-striim", "slt-node", "slt-agent")


class StackPrefixError(ValueError):
    pass


def prefix(env=None) -> str:
    """The active stack prefix ("" when unset/empty). Raises on an invalid value — a bad
    prefix would otherwise surface as a cryptic compose interpolation/name error mid-run.
    """
    p = (env if env is not None else os.environ).get("SLT_STACK_PREFIX") or ""
    if p and not _PREFIX_RE.match(p):
        raise StackPrefixError(
            f"invalid SLT_STACK_PREFIX {p!r}: must match [a-z0-9][a-z0-9-]* "
            f"(lowercase letters, digits, dashes; no whitespace)"
        )
    return p


def prefixed(name: str, env=None) -> str:
    """``alt`` + ``slt-striim`` -> ``alt-slt-striim``; unset prefix -> ``name`` unchanged."""
    p = prefix(env)
    return f"{p}-{name}" if p else name


def state_name(base: str, env=None) -> str:
    """Prefix-scope a coordination filename, keeping its dotfile-ness:
    ``.slt-provision-registry.json`` -> ``.alt-slt-provision-registry.json``. Two stacks run
    from the SAME checkout, so the per-run registries/locks must not share records."""
    p = prefix(env)
    if not p:
        return base
    if base.startswith("."):
        return f".{p}-{base[1:]}"
    return f"{p}-{base}"


# ---------------------------------------------------------------------------
# Coordination locks live OUTSIDE the checkout, machine-wide.
#
# Every lock below guards a resource that is shared per MACHINE, not per working
# copy: Striim's UploadedFiles/, a module's target/<jar>, the docker containers.
# Keeping the lock files in the checkout meant two clones (or git worktrees) of
# this repo pointed at the same cluster took DIFFERENT locks and so never
# serialised against each other -- the checkout-scoped lock silently degraded to
# no lock at all. That is how a `docker cp` of a 52MB OP jar could land on top of
# a concurrent LOAD OPEN PROCESSOR and produce "ZipFile invalid LOC header".
#
# Still prefix-scoped via state_name(), so two stacks on one machine
# (SLT_STACK_PREFIX) keep their own locks.
_LOCK_DIR = Path(os.environ.get("SLT_LOCK_DIR", "/tmp/slt-locks"))


def lock_path(base: str, env=None) -> Path:
    """Machine-wide path for a coordination lock, prefix-scoped per stack."""
    lockdir.ensure_dir(_LOCK_DIR)       # shared by every user on the host
    return _LOCK_DIR / state_name(base, env)


def striim_container(env=None) -> str:
    return prefixed(STRIIM_CONTAINER, env)


def app_nodes(env=None) -> tuple[str, ...]:
    """The app-group node CONTAINER names (docker exec/cp/restart targets).

    Deliberately EXCLUDES the agent. An agent is not in the app group, so nothing the
    framework deploys `ON ALL IN <app group>` lands there -- and an OP jar copied into
    UploadedFiles/ on an agent would be inert anyway, because `LOAD OPEN PROCESSOR` is a
    server-side operation. An agent that genuinely needs an OP jar goes through
    `agent_container()` and opartifacts.place_on_agent instead.
    """
    return tuple(prefixed(n, env) for n in APP_NODES)


def agent_container(env=None) -> str:
    """The agent CONTAINER name."""
    return prefixed(AGENT_CONTAINER, env)


# Where an agent looks for classes. The agent launcher runs
# `java -cp "$WA_HOME/conf:$WA_HOME/lib/*:$CLASSPATH"` with WA_HOME=/opt/striim/agent, so a jar
# dropped here is on the agent's own classpath at its NEXT start -- see
# opartifacts.place_on_agent for why the restart is unavoidable.
AGENT_LIB_DIR = "/opt/striim/agent/lib"


def cluster_containers(env=None) -> tuple[str, ...]:
    return tuple(prefixed(n, env) for n in CLUSTER_CONTAINERS)
