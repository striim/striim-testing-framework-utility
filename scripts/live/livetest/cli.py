from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from livetest.preflight import (
    gcs_public_host_wanted as preflight_gcs_public_host_wanted,
    known_test_ids,
    provision as preflight_provision,
    provision_cluster as preflight_provision_cluster,
    provision_services as preflight_provision_services,
    teardown as preflight_teardown,
    teardown_cluster as preflight_teardown_cluster,
    teardown_services as preflight_teardown_services,
)
from livetest.registry import all_services

# The Striim cluster is a startable target but NOT a registry service: it has compose files
# and no service.yaml, and striim_provision owns its lifecycle. Named here so `start live`
# yields something you can actually run a pipeline against, rather than six databases and no
# engine. `all` includes it for the same reason.
CLUSTER = "striim"


def _resolve_targets(targets: list) -> tuple[list, list, bool]:
    """Split `targets` into (service names, test ids, wants_cluster); SystemExit on a bad name.

    'all' expands to every registered service PLUS the cluster. Anything naming a service is a
    service; anything naming a manifest is a test id, which is how the console drives this
    module (`start <test-id>` provisions exactly that test's service + OP/UDF union). A name
    that is neither must be fatal here -- preflight's own miss handling logs a warning and
    returns 0, so a typo would otherwise look like a bring-up that started nothing.
    """
    known = set(all_services())
    known_tests = known_test_ids()
    services: list = []
    test_ids: list = []
    unknown: list = []
    cluster = False
    for target in targets:
        if target == "all":
            services.extend(s for s in all_services() if s not in services)
            cluster = True
        elif target == CLUSTER:
            cluster = True
        elif target in known:
            if target not in services:
                services.append(target)
        elif target in known_tests:
            test_ids.append(target)
        else:
            unknown.append(target)
    if unknown:
        raise SystemExit(
            f"unknown target(s) {unknown}: expected 'all', {CLUSTER!r}, "
            f"a service {sorted(known)}, or a live test id")
    return services, test_ids, cluster


def _as_coordinator() -> None:
    """This module IS the parallel coordinator (preflight's docstring: it MUST run with
    SLT_PARALLEL=1). Only then do `services.resolve` and the cold-DB setup route through the
    shared provision registry, which is the whole point -- without it the containers come up
    but go unrecorded, and the test subprocesses redo the bring-up they were meant to skip."""
    os.environ["SLT_PARALLEL"] = "1"


def cmd_up(targets: list) -> int:
    _as_coordinator()
    services, test_ids, cluster = _resolve_targets(targets)
    # `all` is a DERIVED list, so the opt-in gate applies to it -- otherwise it hands the
    # operator the very emulators they declined. Named services always come up.
    derived = "all" in targets
    # Cluster first: `provision_cluster` publishes SLT_GCS_PUBLIC_HOST off the resolved
    # cluster, and `docker compose up` freezes that value into the gcs container -- so the
    # engine has to come up first for `start live` to configure the emulator in one command.
    cluster_rc = preflight_provision_cluster(
        publish_gcs_host=preflight_gcs_public_host_wanted(services, derived)) if cluster else 0
    # Test ids BEFORE services: the full pre-flight opens with `clear_provision_registry`, so
    # running it second would discard the record provision_services just wrote -- the next
    # bring-up misses, `_compose_reset` runs `down -v`, and a service the first half brought
    # up and SET UP is destroyed and rebuilt seconds later. Skipped when the cluster failed:
    # that path resolves the same cluster and would fail identically, adding only noise.
    tests_rc = 0
    if test_ids:
        if cluster_rc:
            print("[preflight] skipping test-id pre-flight: the Striim cluster is not up")
            tests_rc = cluster_rc
        else:
            tests_rc = preflight_provision(test_ids)
    # A failed cluster does NOT skip the services: they need no engine (provision_services
    # deliberately resolves none), and `start live` on a machine with no Striim distribution
    # returning zero databases would be a worse answer than the databases plus an error.
    services_rc = (preflight_provision_services(services, apply_gate=derived)
                   if services else 0)
    return cluster_rc or services_rc or tests_rc


def cmd_down(targets: list) -> int:
    _as_coordinator()
    services, test_ids, cluster = _resolve_targets(targets)
    # Every half runs, whatever the others returned, and the worst rc wins: a teardown that
    # gives up at the first failure strands exactly the containers it was asked to remove
    # (same rule as _take_down_services). Cluster last -- tearing the engine down first would
    # leave the service teardown talking to a half-gone stack.
    rc = preflight_teardown_services(services) if services else 0
    # explicit=True: a typed `stop` is not end-of-run cleanup (see preflight.teardown). This
    # tears down that test's SERVICES, so it belongs on the service side of the cluster --
    # `stop striim <test-id>` used to run it after the engine was already gone, the exact
    # ordering the rule above forbids.
    if test_ids:
        rc = preflight_teardown(test_ids, explicit=True) or rc
    if cluster:
        rc = preflight_teardown_cluster() or rc
    return rc


def cmd_ggtrail(args) -> int:
    # Lazy import: the ggtrail package is optional, and `livetest.cli start/stop` must keep
    # working in a checkout that doesn't ship it. --max-records-per-file is NOT forwarded --
    # rollover size is a property of the workload, declared by `max_records_per_file:` in
    # the workload.yaml itself; the flag stays only as a documented override hook for when
    # the runner grows a parameter for it.
    from livetest.ggtrail.runner import generate_from_yaml, stream_from_yaml

    workload, out = Path(args.workload), Path(args.out)
    if args.rate:
        result = stream_from_yaml(workload, out, args.rate, args.duration)
    else:
        result = generate_from_yaml(workload, out)
    print(json.dumps(result.get("summary", {}), indent=2, default=str))
    return 0


def _adopt_stack_prefix(env=None) -> None:
    """Put the effective SLT_STACK_PREFIX into this process's environment.

    The service compose calls resolve settings through the .env layers, but the Striim cluster's
    compose calls (`ps`, `down -v`) read only os.environ. A prefix set only in .env would then scope
    the database containers to the reader's stack and the Striim cluster to the unprefixed one, and
    `stop striim` could remove another stack's cluster and its MDR volume. A prefix exported in the
    shell already wins in both, and is left alone."""
    from livetest import paths
    env = os.environ if env is None else env
    if (env.get("SLT_STACK_PREFIX") or "").strip():
        return
    value = (paths.effective_env(env).get("SLT_STACK_PREFIX") or "").strip()
    if value:
        env["SLT_STACK_PREFIX"] = value


def main() -> int:
    # The project manifest named by GOLD_TARGETS (as a run's tier child gets it): its
    # servicesRoots add the consumer's services, so `stop all` also stops them.
    from livetest import project
    try:
        project.load_and_activate()
    except Exception as e:                     # ProjectError, PathConfigError, LayoutError
        print(f"livetest.cli: cannot activate the project manifest: {e}", file=sys.stderr)
        return 2
    _adopt_stack_prefix()
    p = argparse.ArgumentParser(prog="python -m livetest.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("start", help="start service(s), 'all', or a test id's full pre-flight")
    up.add_argument("target", nargs="+",
                    help=f"service name(s) {all_services()}, {CLUSTER!r}, 'all', or a test id")

    down = sub.add_parser("stop", help="stop service(s), 'all', or a test id's full pre-flight")
    down.add_argument("target", nargs="+",
                      help=f"service name(s) {all_services()}, {CLUSTER!r}, 'all', or a test id")

    gg = sub.add_parser("ggtrail", help="generate GoldenGate trail files from a workload.yaml")
    gg.add_argument("--workload", required=True, help="workload.yaml describing schema + workload")
    gg.add_argument("--out", required=True, help="output directory (trail files, def, expected/)")
    gg.add_argument("--max-records-per-file", type=int, default=None,
                    help="trail-file rollover size; normally set in the workload.yaml itself")
    gg.add_argument("--rate", type=float, default=None,
                    help="ops/sec -- switches to the streaming emulator (batch mode without it)")
    gg.add_argument("--duration", type=float, default=None,
                    help="seconds to stream for (--rate mode only; unbounded when omitted)")

    args = p.parse_args()

    if args.cmd == "start":
        return cmd_up(args.target)
    if args.cmd == "stop":
        return cmd_down(args.target)
    if args.cmd == "ggtrail":
        return cmd_ggtrail(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
