"""Command-line interface for integration-tier service lifecycle.

Mirrors scripts/live/livetest/cli.py: start/stop one or more services, or 'all'.
Integration tier supports: postgres, oracle, spanner, sqlserver, gcs.

`start` also runs the cold-DB setup (Postgres roles + qasource/qatarget schemas) that
the pytest fixtures would otherwise do, so a container this brings up is usable rather
than merely running -- matching what the live tier's pre-flight does for the same
service. Without it `start postgres` hands you an empty database.
"""

import argparse

from filelock import FileLock

from inttest.services import (compose_up, compose_down, is_connection_only, run_pre_up,
                              surviving_containers, unavailable)

# Bring-up order for the services we have an opinion about (postgres first, then oracle,
# then spanner, then gcs -- gcs has no dependency on the others, so it goes last).
# sqlserver last of the databases: it is amd64-only, so on Apple Silicon it boots under
# emulation and is the slowest of them to come up.
_ORDER = ["postgres", "oracle", "spanner", "sqlserver", "gcs"]


def _registered() -> list:
    """Every service on disk, in _ORDER then alphabetically for anything new.

    Derived rather than hardcoded: a runner that resolves `start integration` by scanning
    services/*/service.yaml would silently drift from a literal list here -- adding a
    service directory would make it pass a name this CLI then rejects, with the whole
    test suite still green."""
    # Through the integration profile seam at call time (SLT_INT_SERVICES_DIR, else
    # scripts/integration/services).
    from inttest import resources as _resources
    # A connection-only definition (the shipped teradata) has nothing to start or stop, so it is
    # not a target (review TD1): `start all` would otherwise run compose on a file that is not there.
    found = [s for s in _resources.list_profiles() if not is_connection_only(s)]
    return [s for s in _ORDER if s in found] + [s for s in found if s not in _ORDER]


def _services() -> list:
    # Computed per call, never captured at import, so a services root set after import
    # (SLT_INT_SERVICES_DIR in the environment or .env) is the one listed.
    return _registered()


def _lock_file():
    # The compose lock lives in coordination state (SLT_STATE_DIR, else scripts/integration),
    # resolved per call; same filename as before.
    from inttest import resources as _resources
    return _resources.state_root() / ".int-compose.lock"


def _resolve_services(targets: list) -> list:
    """Resolve start/stop `targets` to the concrete, dependency-ordered service list.

    Raises SystemExit with a clear message for an unknown target.
    """
    registered = _services()
    for t in targets:
        if t != "all" and t not in registered and is_connection_only(t):
            print(f"{t}: connection only (its live_override_env names your instance); "
                  f"nothing to start or stop")
    targets = [t for t in targets if t == "all" or t in registered or not is_connection_only(t)]
    unknown = [t for t in targets if t != "all" and t not in registered]
    if unknown:
        raise SystemExit(
            f"unknown target(s) {unknown}: expected 'all' or a service {registered}")
    if "all" in targets:
        return list(registered)
    return [s for s in registered if s in targets]


def _postgres_setup() -> None:
    """Create the Postgres roles + fixed qasource/qatarget schemas.

    Tokens come from `inttest.tokens`, not `inttest.plugin`: plugin imports pytest, a
    `[dev]`-only extra, so routing through it made `start postgres` -- and therefore every
    `start integration`, whose default set always contains postgres -- die with
    ModuleNotFoundError in a runtime-only checkout. tokens.py is pytest-free by charter and
    reads the same service.yaml. (POSTGRES_HOST is `{view_host}`, which this tier resolves
    to the harness's own host -- there is no containerized Striim here.)
    """
    from inttest import pgclient, tokens

    pgclient.PgAdmin(
        pgclient.dsn_from_tokens(tokens.service_tokens("postgres")), role="source"
    ).ensure_setup()


def cmd_up(targets: list) -> int:
    """Bring up the resolved services (idempotent)."""
    with FileLock(str(_lock_file())):
        services = _resolve_services(targets)
        for service in services:
            try:
                if "all" not in targets:
                    # Named, so its pre_up hook may run (a derived `all` never runs one).
                    run_pre_up(service)
                why = unavailable(service)
                if why:
                    from inttest.services import _service_spec
                    if "all" not in targets and _service_spec(service).get("unavailable_policy") == "fail":
                        raise RuntimeError(f"{service}: {why}")
                    print(f"{service}: {why} -- skipping")
                    continue
                compose_up(service, FileLock(str(_lock_file())))
            except Exception as e:
                print(f"ERROR: Failed to start {service}: {e}")
                return 1
        if "postgres" in services:
            try:
                print("postgres: ensure_setup (roles + schemas)")
                _postgres_setup()
            except Exception as e:
                print(f"ERROR: Failed to set up postgres roles/schemas: {e}")
                return 1
    return 0


def cmd_down(targets: list) -> int:
    """Tear down the resolved services (reverse dependency order), best-effort.

    One service's failure must not strand the rest: `stop` with no names covers every
    registered service, so returning at the first error would leave postgres and oracle
    running because gcs or spanner refused. Every failure is reported and the command still
    exits non-zero (mirrors livetest.preflight._take_down_services)."""
    failed = []
    with FileLock(str(_lock_file())):
        for service in reversed(_resolve_services(targets)):
            try:
                compose_down(service, FileLock(str(_lock_file())))
                # `down` is project-scoped, so containers of the same name owned by a
                # DIFFERENT project survive it and it still exits 0. Real case: the projects
                # were renamed (`gcs` -> `int-gcs`), so anything started before that rename is
                # invisible to today's `down`, and the next `start` dies on "container name
                # already in use" with nothing pointing back here.
                survivors = surviving_containers(service)
                if survivors:
                    raise RuntimeError(
                        f"{', '.join(survivors)} still present after `compose down` — "
                        f"belongs to another compose project (likely from before the project "
                        f"rename). Remove it once with: docker rm -f {' '.join(survivors)}")
            except Exception as e:
                print(f"ERROR: Failed to stop {service}: {e}")
                failed.append(service)
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m inttest.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("start", help="start service(s) or 'all'")
    up.add_argument("target", nargs="+", help=f"service name(s) {_services()}, or 'all'")

    down = sub.add_parser("stop", help="stop service(s) or 'all'")
    down.add_argument("target", nargs="+", help=f"service name(s) {_services()}, or 'all'")

    args = p.parse_args()

    if args.cmd == "start":
        return cmd_up(args.target)
    if args.cmd == "stop":
        return cmd_down(args.target)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
