# Spanner emulator service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- Stock `gcr.io/cloud-spanner-emulator/emulator:1.5.55`: gRPC on 9010, REST on 9020. `SpannerAdmin`
  (`livetest`) creates the instance and the `gsql` (GoogleSQL) and `pgdb` (PostgreSQL dialect)
  databases.
- Striim's Spanner adapters reach an emulator only through the JVM-wide `SPANNER_EMULATOR_HOST`. The
  Docker cluster the framework starts applies `../striim/compose.spanner-emulator.yaml`, which sets
  it; it stays out of the base compose file so a cluster started by hand still reaches real
  Spanner.
- The adapters still parse a service-account key. The framework generates a throwaway one and
  uploads it as `UploadedFiles/fake-gcp-key.json`; `make-fake-key.sh` makes one by hand.
