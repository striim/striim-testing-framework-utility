# Bigtable emulator service recipe

Using this service in a test (tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

**Groundwork.** The service and the framework's `BigtableAdmin` (table and row helpers) only. Not
supported yet: manifest setup or assertions, a BigtableWriter or reader case, real Bigtable,
change streams, and Apple silicon (the image is linux/amd64 and not qualified under emulation).

How it is built:

- Google ships no standalone Bigtable emulator image. The emulator (`cbtemulator`) is a component
  of the gcloud CLI's `:emulators` image, pinned here by its linux/amd64 manifest digest; the
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
