# Bigtable emulator service recipe

Using services in a test (tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).
It has no Bigtable section yet; the tokens this service provides are listed in `service.yaml`.

How it is built:

- **External image, never built by the framework (mirrors the BigQuery service / `SLT_BIGQUERY_IMAGE`):**
  the CBTEMULATOR image is the field's own "dockerized Bigtable" artifact, like BigQuery's `
  SLT_BIGQUERY_IMAGE`. The framework owns the *connection* (`livetest/bigtableadmin.py`) and the
  orchestration, and it **never builds or distributes the image** -- it supplies a pinned default
  image and `SLT_BIGTABLE_IMAGE` overrides to the field's own CBTEMULATOR image. See "Who owns the
  image" below. As the default pin, `gcr.io/.../google-cloud-cli@sha256:f6d5061a…` (`:emulators`,
  pinned by the linux/amd64 manifest digest) bundles `cbtemulator`, the official Cloud Bigtable
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
- Change streams are not supported: the admin offers no change-stream surface, and
  `enable_change_stream` / `disable_change_stream` raise rather than fake one.

Health gate: the compose healthcheck only proves the port is listening; the admin-table
create+delete that the live tier runs before a bigtable test is the readiness gate (a half-booted
CBTEMULATOR opens its listener before it accepts admin RPCs). See "Health gate" below.

## Who owns the image

Mirrors the BigQuery service's split: the framework is the **connect + orchestration** layer
(`livetest/bigtableadmin.py`, the `services/bigtable` recipe, the Striim overlay) and **never builds
or distributes the CBTEMULATOR image**. The image is an **external artifact the field supplies** --
its own "dockerized Bigtable" -- the same boundary as BigQuery's `SLT_BIGQUERY_IMAGE`.

- **Supplied, not built.** `compose.yaml` resolves the emulator image from `SLT_BIGTABLE_IMAGE`,
  defaulting to the pinned gcloud image. `docker compose up` *pulls* that ref; the framework never
  runs `build` for it (BigQuery's driver returns `build_required=False`; the Bigtable service carries
  no build of its own, so nothing to build).
- **The boundary is the pin, not a build-owned label.** Unlike BigQuery -- a field-*built* image the
  framework gates on its `org.opencontainers.image.version` / `.revision` OCI label -- CBTEMULATOR is
  a *public* gcloud image, so the boundary here is the **manifest digest** the
  default pins: re-pin deliberately when the gcloud image moves, then re-run this service's tests
  against it (the emulator binary changes across gcloud versions). A `build_required` / OCI-label
  gate is *not* wired for this service on purpose -- there is no field build to gate; the pin is the
  boundary. (Explanatory note, not a TODO: if a real, field-built CBTEMULATOR image lands, the
  BQ-style label gate + a `driver:` is the natural seam.)
- **Point at your own.** `SLT_BIGTABLE_HOST` (`live_override_env`) reuses an emulator you run
  yourself and skips `docker compose up` -- a separate intent from the `SLT_BIGTABLE_IMAGE` /
  `SLT_BIGTABLE_GRPC_HOST_PORT` knobs, which move *the framework's* container.

## Health gate

`docker compose up --wait` waits on `slt-bigtable`'s healthcheck, a TCP probe of the published
port that only keeps `--wait` honest. The readiness proof is `BigtableAdmin(dsn).ensure()`, which
creates and deletes a throwaway `.__slt_readiness` table, retrying within its time budget until the
emulator accepts admin RPCs. On the live tier, `LiveItem.runtest` in `livetest/plugin.py` calls it
when a test requires the bigtable service (once across pytest-xdist workers). The integration tier
does not call it.

To use an emulator you run yourself (not via Docker), set `SLT_BIGTABLE_HOST`/`SLT_BIGTABLE_
GRPC_HOST_PORT`/`SLT_BIGTABLE_PROJECT`/`SLT_BIGTABLE_INSTANCE` (the `live_override_env` path).
Real Bigtable is not supported: `BigtableAdmin` refuses a connection that carries credentials or
names no emulator endpoint.
