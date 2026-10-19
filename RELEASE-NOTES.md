# Release notes

One entry per release, newest first: the pull requests it contains.

## Unreleased

Typed exact comparisons now cover Spanner and Kafka records. Strict JSON, raw
file inventories, sentinel-qualified absence, controlled transaction seeds,
consistent snapshot histories and append-only audit comparisons add stricter
checks. Progress-triggered recovery records fault boundaries and can prove a
fresh marker after restore. Named-node Docker faults also check live placement.

**Node faults are opt-in:** `recover.node` cases skip before provisioning unless
`SLT_ENABLE_NODE_FAULTS=1` is set for a dedicated disposable stack. Only opted-in
stacks publish the secondary API (`SLT_STRIIM_NODE_HTTP_HOST_PORT`, default 9082).
See [UPGRADING.md](docs/UPGRADING.md) before changing an older stack.

**Tags select tests:** each manifest tag becomes a pytest marker, `tag_<name>` (`-m tag_p1`).
`-k` now also matches tags, so a `-k` expression can select more cases than before.

**Breaking change — timed recovery:** timed recovery now requires the app to be
RUNNING at the fault boundary. A completed or halted app fails `fault-missed`
instead of counting as an interruption. Consumers updating their framework pin
must ensure work remains in flight at `recover.after`, or use a progress trigger
with a bounded window. Progress-triggered recovery also checks again after the
interruption takes effect, refusing a target count that reached its upper bound.

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
