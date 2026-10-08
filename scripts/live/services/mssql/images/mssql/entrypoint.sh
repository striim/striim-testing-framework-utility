#!/bin/bash
# Custom slt-mssql entrypoint: once SQL Server is accepting connections, provision the
# qasource/qatarget accounts + relax sa (init.sql), then hand off to the real server —
# so a bare `docker compose up` leaves the users present (the live-test harness also
# ensures them idempotently at resolve). Runs the init in the background so it can't
# block/​break `docker compose up --wait` (which waits only on the healthcheck).
#
# THIS is the single authoritative sa-password migration. The healthcheck (compose.yaml)
# only checks for the DONE_MARKER below, rather than independently guessing a password —
# a login-probing healthcheck used to run concurrently with this loop AND with
# MssqlAdmin._ensure_sa_password() (mssqladmin.py), three separate actors racing to read
# or ALTER LOGIN the same sa account right after boot. A connection landing mid-ALTER
# LOGIN gets a spurious "Login failed... An error occurred while evaluating the password"
# (SQL Server error state 7) or "...Password did not match" (state 8) — intermittent, and
# once triggered it can keep failing past the framework's own retry budget. Gating
# `--wait`/ensure_setup on this marker instead means nothing else touches sa until this
# loop's ALTER LOGIN has already committed.
set -u
DONE_MARKER=/var/opt/mssql/.slt-init-done
# /var/opt/mssql has no VOLUME/bind-mount (compose.yaml declares none), so it's the
# container's writable layer: it survives `docker stop`/`start` (only `down -v` wipes it).
# A stale marker from a PRIOR boot would make the healthcheck report healthy before THIS
# boot's migration below has run, racing it exactly as before this fix existed — clear it
# unconditionally so the healthcheck can only ever see a marker this boot actually wrote.
rm -f "$DONE_MARKER"
(
  SQLCMD=/opt/mssql-tools18/bin/sqlcmd
  for _ in $(seq 1 90); do
    for pw in "${MSSQL_SA_PASSWORD:-}" striim; do
      [ -n "$pw" ] || continue
      if "$SQLCMD" -C -S localhost -U sa -P "$pw" -Q "SELECT 1" >/dev/null 2>&1; then
        "$SQLCMD" -C -S localhost -U sa -P "$pw" -b -i /usr/config/init.sql \
          && { touch "$DONE_MARKER" \
                 && { echo "[mssql-init] provisioned qasource/qatarget + relaxed sa->striim" >&2; exit 0; } \
                 || { echo "[mssql-init] ERROR: init.sql succeeded but could not write $DONE_MARKER" >&2; exit 1; }; }
      fi
    done
    sleep 2
  done
  echo "[mssql-init] WARNING: could not connect to provision (harness ensure_setup will retry)" >&2
) &
exec /opt/mssql/bin/launch_sqlservr.sh "$@"
