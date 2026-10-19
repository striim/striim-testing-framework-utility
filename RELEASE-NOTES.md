# Release notes

One entry per release, newest first: the pull requests it contains.

## v0.2.0 (2026-10-19)

**Breaking changes and changed behaviour**

- **Timed recovery requires RUNNING:** a completed or halted app at `recover.after`
  fails `fault-missed`. Keep work in flight at the boundary, or use a bounded
  progress trigger. Progress recovery also refuses a target that reaches its upper
  bound while the interruption takes effect. (#22)
- **Tags affect selection:** `-k` also matches manifest tags and can select more
  cases. Each tag has a `tag_<name>` pytest marker, for example `-m tag_p1`. (#27, #34)
- A DEPLOY/START refusal with `expect_halt: true` no longer passes while silently
  skipping declared post-start work, lifecycle or assertions. Use a matching
  `expect_halt_contains` for an intentional startup refusal; evidence records what
  was skipped. (#9)
- **Oracle resource retention:** case-prefixed observer views and owned logon
  triggers are removed even when apps/tables are retained. Existing cases relying
  on retained prefixed views must use the documented observer cleanup contract.
  Unprefixed ordinary views retain their previous behaviour. (#10)
- GCS/Spanner credential uploads now fail setup immediately if any eligible app
  node cannot receive the key, rather than continuing with missing credentials. (#11)
- Smoke settling is now one second instead of five; a smoke pass does not prove
  a slow asynchronous initialization completed. Post-start seeds without an
  explicit `after` wait at least five seconds after RUNNING when neither the reader
  nor lifecycle has proved readiness. (#3, #4, #5)
- Integration target events now enter through `receive` under the writer's
  `synchObject`, matching platform event delivery; `flush` remains outside that
  lock. Rebuild the integration harness when updating. (#32)

**Opt-in features and settings**

- **Node faults:** `recover.node` skips before provisioning unless
  `SLT_ENABLE_NODE_FAULTS=1` selects a dedicated disposable stack. The opt-in
  publishes the secondary API (`SLT_STRIIM_NODE_HTTP_HOST_PORT`, default 9082).
  Native mode skips named-node faults. See [UPGRADING.md](docs/UPGRADING.md) before
  reusing an older stack. (#22, #36)
- **Oracle observers:** `oracle-admin` supports scoped views and logon triggers
  in the data PDB, with `SLT_ORA_ADMIN_USER` and `SLT_ORA_ADMIN_PASSWORD`.
  Observer probes identify themselves at login; teardown removes observer hooks
  even when ordinary resources are kept. Existing Oracle databases need the
  documented direct grants; rebuilding an image alone does not update them. (#10, #36)
- **BigQuery emulator:** supply `SLT_BIGQUERY_IMAGE` for bounded scalar exact
  reads, legacy streaming, direct CSV batch loads and non-optimized batch MERGE
  sentinels with a Postgres source. Historical Linux amd64 qualification used
  earlier emulator images; the currently required managed image still needs
  matching fresh qualification. The Java exact-chunk boundary is excluded.
  UTF-8 CSV needs the documented JVM setting.
  Native ARM, Storage Write, recovery and cloud equivalence are unqualified. (#11)
- **Real Cloud Spanner:** the integration tier accepts `INT_SPANNER_CREDENTIALS`
  and pre-existing project/instance/databases. Unset emulator endpoints and use
  `SLT_PARALLEL=1`; SQL and teardown require case-prefixed objects. The framework
  creates no instance or database. (#33)
- **Postgres idle transactions:** `SLT_PG_IDLE_TRANSACTION_TIMEOUT_MS` and
  `INT_PG_IDLE_TRANSACTION_TIMEOUT_MS` optionally set positive millisecond budgets
  on disposable data roles, including new Striim connections. Unset retains no
  framework role policy; zero is refused. Allow for deliberate held transactions
  and recovery. External databases keep their policy. (#24)
- **Fixture deadlines:** `fixture_timeout` overrides `SLT_PG_FIXTURE_TIMEOUT` for
  Postgres DDL and seeds. Both unset preserve server statement/lock policy.
  Enabled budgets also bound lock waits; passive blocker diagnostics remain on
  without a budget. Concurrent action DML keeps its existing SQL path. (#23)
- **Teradata JDBC:** `SLT_TERADATA_JDBC=1` fetches the optional driver and builds
  it into the Striim image. Unsetting stops the fetch; without a dependency
  manifest, remove an already staged driver to omit it from a rebuild. Teradata
  cases skip or fail with instructions when it is missing. Driver licence terms
  apply.
  The settings example and service guide cover this choice. (#28, #29, #30)
- **Prebuilt jars:** select the exact OP/UDF artifact with
  `SLT_PREBUILT_JAR_<MODULE>`, `SLT_PREBUILT_JAR` or `SLT_PREBUILT_DIR`, in that
  order. Both engines validate the module, release series and Java level and
  report the selected bytes' SHA-256 instead of building the module. (#2)

**Checks and fixes**

- Typed exact comparisons cover both Spanner dialects and Kafka records.
  Strict JSON distinguishes invalid or duplicated keys; raw-file checks cover
  complete inventories and bytes. Bounded absence requires a qualifying completion
  sentinel. Postgres-source CDC sentinels also support Spanner, Kafka and Docker
  files; unsupported routes remain documented explicitly. (#25)
- Controlled Postgres transaction seeds can overlap, commit or roll back across
  recovery. Consistent multi-table snapshot histories check committed prefixes;
  append-only audit checks count duplicate deliveries rather than hiding them
  behind final state. New samples demonstrate each contract. (#26)
- Recovery can interrupt inside a bounded progress window, record observed fault
  boundaries, and require a fresh post-restore marker. Named-node Docker faults
  verify live placement and survivor APIs. When reusing a contributor environment,
  run `python3 tools/prepare-recovery-goldens.py` for the shared sample CSV copies;
  the test-environment helper also prepares them. (#22)
- SQL Server seeds can use `mssql-admin` for privileged statements. Nothing is
  dropped through this route. (#1)
- Service-token discovery is reused across manifest loads until its inputs change,
  reducing collection work without retaining stale service declarations. (#8)
- Integration mocks provide `RetriableWriter.getOwnerFlow`, its `Flow` return type,
  and the `BatchAcknowledgeable` marker, so compatible adapters load without missing
  platform types. The harness leaves the owner flow unset off-server. (#31, #35)

Tested on Striim 5.4.2: Postgres samples 01–04 and the two code samples in
Docker mode on Linux x86-64; samples 01, 02, 04 and the code samples in native
mode on Linux; sample 01 in Docker mode on Apple Silicon. Striim 5.4.4.0 was
qualified on Apple Silicon at framework `44ab472`
with 31 field live-gate passes and 202 regression passes (2 skips), excluding
Spanner, Teradata and MSJet. These are recorded qualifications, not a claim that
all new features or this release commit passed live. (#7)
See [WHAT-IS-SUPPORTED.md](docs/WHAT-IS-SUPPORTED.md) for route-specific qualifications.

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
