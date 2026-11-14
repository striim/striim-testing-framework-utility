# Bigtable emulator service recipe

Using this service in a test (tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

**Assertions.** The service, the framework's `BigtableAdmin`, and `assert: bigtable:` (each spec
is `{table, expected}`, the table read whole and compared; see docs/TEST-YAML.md). Not supported
yet: manifest setup, a BigtableWriter or reader case, real Bigtable, and change streams (the
emulator crashes on them, on both architectures). Development runs natively on arm64; the x86 build
runs with `DOCKER_DEFAULT_PLATFORM=linux/amd64` (emulated on arm64).

How it is built:

- Google ships no standalone Bigtable emulator image. The emulator (`cbtemulator`) is a component
  of the gcloud CLI's `:emulators` image, pinned here by its index digest (arm64 and amd64 manifests of one release); the
  service starts it with `gcloud beta emulators bigtable start`. One gRPC port, 9470, serves data
  and admin.
- Striim's Bigtable clients reach an emulator only through the JVM-wide `BIGTABLE_EMULATOR_HOST`.
  The Docker cluster the framework starts applies `../striim/compose.bigtable-emulator.yaml`, which
  sets it, as it applies the Spanner overlay; it stays out of the base compose file so a cluster
  started by hand still reaches real Bigtable. No service-account key is needed: the emulator
  redirect skips auth.
- `BigtableAdmin` (`livetest/bigtableadmin.py`) dials `{host}:{port}` from the host. Before a test
  that requires `bigtable`, the live tier runs its readiness gate, `ensure()`: it creates and
  deletes an `_slt_readiness` table until the emulator accepts admin RPCs (once across pytest-xdist
  workers). The compose healthcheck only proves the port is listening.
- `BigtableAdmin` refuses a connection that carries credentials or names a Google API endpoint.
- An emulator you run yourself (`SLT_BIGTABLE_HOST`) serves the framework's admin only; the
  cluster's redirect still points at `host.docker.internal:${SLT_BIGTABLE_GRPC_HOST_PORT:-9470}`.
  Spanner has the same limit.
