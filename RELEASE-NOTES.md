# Release notes

One entry per release, newest first: the pull requests it contains.

## v0.1.0 (2026-10-08)

First public release.

`striim-test` runs tests of Striim applications against a real Striim. A test is a folder: the
app's TQL, its DDL and seed data, the expected rows, and a `test.yaml`. The framework creates the
tables, deploys and starts the app, waits for it to finish, compares the target with the expected
rows, and cleans up.

- **Where Striim comes from:** your own server (`STRIIM_URL`), or a two-node cluster with an agent
  that the framework builds and starts in Docker. Docker mode needs your own Striim license.
- **Services:** PostgreSQL, MySQL, SQL Server, Oracle, Kafka, Vertica, the Cloud Spanner emulator
  and a GCS emulator, started in Docker or pointed at your own instances; ServiceNow through your
  own sub-production instance. See `docs/SERVICES.md`.
- **Checks:** row counts, golden files, source-to-target diffs, typed exact comparison and
  lifecycle witnesses (PostgreSQL routes), file output and JMX (Docker mode).
- **Your own Java:** Open Processors and UDFs are built and loaded into the app under test
  (`docs/TESTING-YOUR-JAVA.md`).
- **Your own repository:** keep tests beside your apps, pin the framework with `framework.pin`, and
  start from `templates/consumer-repo/` or the worked example in `examples/acme-retail/`.
- **Results:** exit codes 0 to 5, JUnit XML for CI, and an evidence record per case.

Tested on Striim 5.4.2: Docker mode on Linux x86-64 and macOS on Apple Silicon, native mode on Linux.
Support details are in `docs/WHAT-IS-SUPPORTED.md`. Start with `README.md` or `docs/reference.html`.
