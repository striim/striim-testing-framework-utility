# Oracle service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- `images/oracle/Dockerfile` builds on `gvenzl/oracle-free` and runs `bake.sql` at build time:
  ARCHIVELOG, database-level supplemental logging (all columns), the common LogMiner user
  `c##striim` in the container root (a local user in the pluggable database cannot run LogMiner),
  and the `QASOURCE` and `QATARGET` schemas. Nothing is configured at runtime.
- The image follows the host's architecture (amd64 or arm64); no platform is pinned.
- The healthcheck runs the base image's own `/opt/oracle/healthcheck.sh` (startup finished,
  `FREEPDB1` open read-write) and then connects as `qasource`, so `docker compose up --wait` returns
  only when the data user can log in.

Observer cases use direct CREATE VIEW / SYS.V_$SESSION / SYS.V_$DATABASE grants
on the data users, plus the optional `oracle-admin` PDB route for logon triggers.
These grants change the image: rebuild this recipe and create a fresh Oracle
service, or have the database administrator grant them on an existing instance.
[Observer setup, SQL and teardown](../../../../docs/SERVICES.md#oracle-observers)
describe the program filter, privileged route and required direct grants.
