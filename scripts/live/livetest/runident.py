"""Run + worker + case resource identity for live tests (contract set 1.7.0, C7.5).

One key per item, ``sha256(run id, xdist worker, case name)``. Every run-scoped name the engine
renders (``${TID}``, ``${NS}``, ``${APP}``, ``${PG_SLOT}``, ``${OWNED_DIR}``, the derived kafka/gcs
names) comes from it, so two simultaneous runs of the same case never share a name and a serial
run is isolated as well. Within one run the names are a pure function of the environment, so
teardown recomputes them. The run id is carried in full; the 36-bit ``per_test`` suffix is a
probabilistic identity and never a delete authority by itself (cleanup deletes only
ledgered names). ``attempt`` is fresh per derivation (one per ``runtest`` call), is recorded and
exported, and never enters a resource name.

Stdlib only. ``livetest.plugin`` calls it through the ``lifecycle-hooks@1`` transform.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

RUN_EPOCH_ENV = "SLT_RUN_EPOCH"
WORKER_ENV = "PYTEST_XDIST_WORKER"
DEFAULT_WORKER = "w0"
# SLT_<slug[:25]>_t<9 hex> is at most 40 characters. The binding limit is an adapter's, not ours:
# SpannerWriter fails START with "Insufficient Privilege to get Dialect" once a
# component's full name <NS>.<component> reaches 64 characters, and 62 is the longest measured to
# pass. FULL_NAME_MAX keeps that measured 62; NS_MAX 40 leaves COMPONENT_MAX 21 characters for the
# component name (SpannerTarget is 13).
# It is also below the OP checkpoint filename budget (CheckpointNaming.MAX_NAME_CHARS 60,
# striimfile.clear_op_checkpoints). A longer case name is cut, never the per-test hash, so the
# namespace stays unique per run + worker + case and deterministic.
FULL_NAME_MAX = 62
SLUG_MAX = 25
NS_MAX = 40
COMPONENT_MAX = FULL_NAME_MAX - NS_MAX - 1
OWNED_ROOT = "/opt/striim/slt-runs"


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    run_id: str        # SLT_RUN_EPOCH in full (striim-test: the run directory basename)
    worker: str        # PYTEST_XDIST_WORKER or "w0"
    case: str          # the manifest name
    attempt: str       # 8 hex, fresh per derivation; never part of a resource name
    slug: str          # readable case slug, digit-guarded (the plugin's local `slug`)
    per_test: str      # "t" + 9 lowercase hex: the un-gated hashed per-test id
    tid: str           # per_test + "_" (never empty)
    tid_upper: str     # per_test.upper() + "_"
    tid_oracle: str    # same value as tid_upper: 11 chars, Oracle's 30-byte budget unchanged
    ns: str            # SLT_<slug[:25]>_<per_test>, <= 40 chars
    ns_truncated: bool # the case slug was longer than SLUG_MAX (25)
    app: str           # <ns>.<slug>App
    app_bare: str      # <slug>App
    pg_slot: str       # slt_<per_test> (14 chars; Postgres slot names are <= 63)
    owned_dir: str     # /opt/striim/slt-runs/<ns>: the framework-allocated server directory


def _case_slug(name: str) -> str:
    # Exactly livetest.plugin._slug (the NS/APP slug, case preserved).
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def derive(case: str, env, attempt: str | None = None) -> Identity:
    """The identity of case ``case`` in the run and worker ``env`` names. ``SLT_RUN_EPOCH`` must be
    set and non-empty."""
    run_id = env.get(RUN_EPOCH_ENV) or ""
    if not run_id:
        raise IdentityError(
            f"{RUN_EPOCH_ENV} is not set: live resource identity is run + worker + case (the live "
            f"plugin stamps {RUN_EPOCH_ENV}; striim-test run sets it to the run directory basename)")
    worker = env.get(WORKER_ENV) or DEFAULT_WORKER
    key = f"{run_id}\x1f{worker}\x1f{case}"
    per_test = "t" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:9]
    raw = _case_slug(case)
    slug = "t" + raw if raw[:1].isdigit() else raw
    ns = f"SLT_{raw[:SLUG_MAX]}_{per_test}"
    assert len(ns) <= NS_MAX, ns
    app_bare = f"{raw}App"
    upper = per_test.upper() + "_"
    return Identity(run_id=run_id, worker=worker, case=case, attempt=attempt or uuid.uuid4().hex[:8],
                    slug=slug, per_test=per_test, tid=per_test + "_", tid_upper=upper, tid_oracle=upper,
                    ns=ns, ns_truncated=len(raw) > SLUG_MAX, app=f"{ns}.{app_bare}", app_bare=app_bare,
                    pg_slot=f"slt_{per_test}", owned_dir=f"{OWNED_ROOT}/{ns}")


def tokens(ident: Identity) -> dict:
    """The framework identity tokens (C7.5). The sentinel tokens arrive with increment 2."""
    return {"NS": ident.ns, "APP": ident.app, "APP_BARE": ident.app_bare,
            "TID": ident.tid, "TID_UPPER": ident.tid_upper, "TID_ORACLE": ident.tid_oracle,
            "RUN_ID": ident.run_id, "WORKER": ident.worker, "ATTEMPT": ident.attempt,
            "OWNED_DIR": ident.owned_dir}


def record(ident: Identity) -> dict:
    """The identity as evidence records it (run id in full)."""
    return {"runId": ident.run_id, "worker": ident.worker, "case": ident.case, "attempt": ident.attempt,
            "perTest": ident.per_test, "namespace": ident.ns, "namespaceTruncated": ident.ns_truncated,
            "app": ident.app, "pgSlot": ident.pg_slot, "ownedDir": ident.owned_dir}
