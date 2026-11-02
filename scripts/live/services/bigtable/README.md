# Bigtable emulator service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- Stock `gcr.io/google.com/cloudsdktool/google-cloud-cli@sha256:f6d5061a…` (`:emulators`, pinned by
  the linux/amd64 manifest digest): the image bundles `cbtemulator`, the official Cloud Bigtable
  emulator. The service starts it with `gcloud beta emulators bigtable start` and publishes one gRPC
  port (`SLT_BIGTABLE_GRPC_HOST_PORT`, default 9470) to the host. There is no separate REST/admin
  port -- unlike Spanner, CBTEMULATOR is a single gRPC listener.
- No fake GCP key: the Bigtable client (and the framework's admin) reach the emulator via
  `BIGTABLE_EMULATOR_HOST`, which skips auth, unlike Spanner/GCS. So the cluster override
  (`../striim/compose.bigtable-emulator.yaml`) only injects `BIGTABLE_EMULATOR_HOST` -- nothing is
  uploaded to UploadedFiles.
- The framework's own admin client (`livetest/bigtableadmin.py`, via `inttest.dbroutes`'
  `bigtable` route) reads/writes the table directly from the host at `{host}:{port}`
  (localhost:9470), with `google-cloud-bigtable`. The Striim app (`BigtableWriter`) reaches the same
  emulator through `BIGTABLE_EMULATOR_HOST`, set by `compose.bigtable-emulator.yaml`.
- Change streams are NOT supported by the CBTEMULATOR (`ReadChangeStream` /
  `GenerateInitialChangeStreamPartitions` SIGSEGV the `bttest` server) -- see
  [docs/BIGTABLE_EMULATOR.md](../../../../docs/BIGTABLE_EMULATOR.md) for the full probe record.

Health gate: the healthcheck below / `services.ensure_up` runs an admin-table create+delete; it is
the "really prove readiness" gate (a half-booted CBTEMULATOR opens its listener before it accepts
admin RPCs). See README `Health gate` section below.

## Health gate

The `docker compose up --wait` block below waits on `slt-bigtable`'s healthcheck, AND the
framework's `livetest.services.ensure_up` runs `bigtableadmin.BigtableAdmin(dsn).ensure()` -- it
creates and deletes a throwaway `.__slt_readiness` table, retrying until the listener is ready to
accept admin RPCs. The healthcheck (a TCP probe of the published port) only keeps `--wait` honest;
the admin `ensure()` is the real "ready for admin RPCs" proof.

To run the smoke against a reused emulator (not via Docker), set `SLT_BIGTABLE_HOST`/`SLT_BIGTABLE_
GRPC_HOST_PORT`/`SLT_BIGTABLE_PROJECT`/`SLT_BIGTABLE_INSTANCE` (the `live_override_env` path), or
point a test at real Bigtable via `SLT_BIGTABLE_*` + credentials.
