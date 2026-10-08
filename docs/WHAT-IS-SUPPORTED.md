# What live testing supports

Each row says what the framework does, marked one of three ways:

- **tested**: it passed in a run, named in the row;
- **supported, not exercised here**: the code does it, but no run on record covers it;
- **not supported**: with the reason, and the workaround where there is one.

"the Linux host" is a Linux x86-64 test host, and "the Mac" an Apple Silicon Mac with Docker Desktop. Both ran
this framework against Striim 5.4.2. "Every sample" below means the Postgres samples (01 to 04) and
the two code samples; the MySQL samples (05, 06) are supported, not exercised here.

## Run modes

| | Status | Notes |
|---|---|---|
| **Docker mode**: the framework builds a Striim image and starts a cluster (`STRIIM_URL` unset) | **tested**: every sample on the Linux host, serially and with `--parallel`; sample 01 on the Mac | two Striim nodes and an agent. The first run downloads 6.3 GB and builds a 22 GB image |
| **Native mode**: your own Striim at `STRIIM_URL` | **tested**: every sample but 03 (which needs Docker mode), on the Linux host, against a native 5.4.2 install | the differences are below |

What differs in native mode:

| | Docker mode | Native mode |
|---|---|---|
| file assertions with `exact:`, and `lifecycle:` with `sink: file` | yes | **not supported**: the framework reads output files on the cluster's nodes. `samples/live/03-file-output` is Docker-only for this reason |
| plain `assert.file` (without `exact:`) | yes | yes, when Striim runs on the same machine as `striim-test`: the file is read from the local disk. The two code samples do this |
| `op:` / `udf:` jars | copied into the cluster | `STRIIM_HOME` must be the running server's own install, on this machine: the jars are copied into `$STRIIM_HOME/UploadedFiles`. `striim-test doctor --case` checks it |
| `recover: {mode: kill}` | yes | **not supported**: there is no container to kill. The test is skipped, never downgraded to `stop` |
| `expect_halt_contains` on a halt while running | checked against the server log | the test passes without the check, and its record says `halt reason not checked (native mode)` |
| `assert.jmx` | yes | **not supported**: it fails at once, since there are no exporters to read |
| `topology: cluster` | two nodes and an agent | needs a server with at least two nodes in `default` and an agent in `Agents`; otherwise the test is skipped |
| how Striim reaches the databases | `host.docker.internal` | `localhost`, or `SLT_<SERVICE>_VIEW_HOST` |

## Platforms

| Platform | Status | Notes |
|---|---|---|
| Linux x86-64 | **tested** (the Linux host: every sample, both modes) | |
| macOS on Apple Silicon | **tested** (the Mac: sample 01, Docker mode) | the Striim image is amd64 and runs under emulation. Sample 01 took 6 min 7 s on the Mac, including an image rebuild from cache, against about 95 s on the Linux host including cluster start. The first image build takes longer. Docker Desktop's disk is inside its VM: it needs 35 GB free for the first build and 5 GB to run ([RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md), "Docker mode"). SQL Server also runs under emulation; Oracle runs natively on arm64 |
| Linux on arm64 | **supported, not exercised here** | the Striim and SQL Server images are amd64, so they need emulation as on the Mac |
| Windows | **not supported** | the framework assumes a POSIX shell and filesystem (its lock directory defaults to `/tmp/slt-locks`, the Striim recipe runs bash scripts). Run it inside WSL 2 or a Linux VM; that is untested |

## Striim releases

| Release | Status |
|---|---|
| 5.4.2 | **tested**: Docker mode on the Linux host and the Mac, native mode on the Linux host |
| other 5.4 releases | **supported, not exercised here** |
| 5.0 and 5.2 | **supported, not exercised here**: the framework builds jars for them with Java 11 (5.4 uses Java 17) |

**How to choose one:**
- Docker mode builds the image from the release of the install `STRIIM_HOME` points at (the `<ver>`
  in its `lib/Platform-<ver>.jar`), or 5.4.2 when `STRIIM_HOME` is unset.
- That release must be published for download, or the first run stops with "not available for
  download". Or supply the installers yourself with `SLT_STRIIM_DEPS_MANIFEST`.
- Native mode tests whatever release your server runs.
- `op:`/`udf:` jars are built against the release in `STRIIM_HOME`.

## Services

The framework starts each service a test `requires:` in Docker, with test-only accounts (every
password `striim`), or uses your own instance when you set its host. [SERVICES.md](SERVICES.md)
has every account, port, URL and token. To add a service or replace one, see
[YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md).

**Teradata is bring-your-own.** Its VM image is not public, so the framework ships no Teradata
container, only the connection, and its driver is the optional `teradata` extra under Teradata's own
licence ([SERVICES.md](SERVICES.md), "Teradata"). Point it at your instance the way you would your own Postgres, in
`.env` or the shell:

```
SLT_TERADATA_HOST=td.example.com
SLT_TERADATA_PORT=1025            # default 1025
SLT_TERADATA_USER=dbc             # the admin; default dbc
SLT_TERADATA_PASSWORD=...
SLT_TERADATA_SOURCE_USER=qasource   # default qasource; SOURCE_PASSWORD, SOURCE_SCHEMA likewise
SLT_TERADATA_TARGET_USER=qatarget   # default qatarget; TARGET_PASSWORD, TARGET_SCHEMA likewise
```

- The admin user creates the source and target users when they cannot log in. In Teradata a user
  is also a database, so each schema defaults to its user's name.
- `SLT_TERADATA_VIEW_HOST` sets the host Striim reaches it by, when that differs.
- The integration tier reads the same settings with the `INT_TERADATA_` prefix (`INT_TERADATA_HOST`,
  `INT_TERADATA_ADMIN_USER` and so on).
- `striim-test doctor --case` checks that the host answers.

**ServiceNow is bring-your-own** too, for a different reason: it is hosted, so no container runs an
instance. Set `SLT_SERVICENOW_HOST` and the integration user (and the OAuth client, if your test
uses OAuth) to a sub-production instance you own. [SERVICENOW.md](SERVICENOW.md) covers preparing
the instance, the settings and tokens, and how to assert, since the framework does not read
ServiceNow.

### Live tier

| Service | Image | CDC | Host ports | Your own instance | Status |
|---|---|---|---|---|---|
| **`postgres`** | recipe: `postgres:16` plus `wal2json` | yes: `wal_level=logical`, `wal2json` | 5432 (`SLT_PG_HOST_PORT`) | `SLT_PG_HOST` and `SLT_PG_*` | **tested**: every sample, the Linux host and the Mac |
| **`spanner`** | **Google's official Cloud Spanner emulator**, `gcr.io/cloud-spanner-emulator/emulator:1.5.55` (stock, amd64) | change streams, as the emulator provides them | 9010 gRPC, 9020 REST | `SLT_SPANNER_HOST` and `SLT_SPANNER_*` (another emulator) | **supported, not exercised here**. On by default, no opt-in, in both tiers. No credentials: the framework uploads a throwaway service-account key |
| `oracle` | recipe: `gvenzl/oracle-free:23.26.2-slim-faststart`, ARCHIVELOG and the LogMiner user baked in | yes: LogMiner (`c##striim`) | 1521 (`SLT_ORA_HOST_PORT`) | `SLT_ORA_HOST` and `SLT_ORA_*` | **supported, not exercised here**. The image follows the host's architecture: amd64 on Linux, arm64 on the Mac |
| `mssql` (SQL Server) | recipe: SQL Server 2022 Developer (amd64) | enabled: CDC and SQL Server Agent | 1433 (`SLT_MSSQL_HOST_PORT`) | `SLT_MSSQL_HOST` and `SLT_MSSQL_*` | **supported, not exercised here** |
| `kafka` | stock: Confluent 7.6.1 (Kafka, ZooKeeper, Schema Registry) | not applicable | 9092 for this machine, 19092 for Striim in Docker, 8081 registry, 2181 ZooKeeper | `SLT_KAFKA_HOST` and `SLT_KAFKA_*` | **supported, not exercised here** |
| `mysql` | stock: `mysql:8.0`, **past its upstream end of life (April 2026)** | yes: binlog in `ROW` format | 3306 (`SLT_MYSQL_HOST_PORT`) | `SLT_MYSQL_HOST` and `SLT_MYSQL_*` | **supported, not exercised here** (`samples/live/05-mysql-initial-load`, `06-mysql-cdc`, and the regression cases in `scripts/live/regression/services/mysql`) |
| `vertica` | recipe: `opentext/vertica-k8s:25.3.0-8-multiarch`, one node, unlicensed (Community Edition: 1 TB, 3 nodes). 25.3 is the newest release that still runs as CE | not applicable | 5433 (`SLT_VERTICA_HOST_PORT`) | `SLT_VERTICA_HOST` and `SLT_VERTICA_*` | **tested**: `scripts/live/regression/services/vertica/vertica-diff` (DatabaseReader and DatabaseWriter through `vertica-jdbc`) and `vertica-cdc-diff` (IncrementalBatchReader) on the Mac. Native on arm64 |
| `gcs` | stock: `fsouza/fake-gcs-server:1.54.0`, plus a fake token server | not applicable | 4443, 4444 token | `SLT_GCS_HOST` and `SLT_GCS_*` | **supported, not exercised here** (`scripts/live/regression/services/gcs`) |
| `teradata` | **none: bring your own instance.** The framework ships no Teradata container; its definition holds only the connection settings | not applicable | 1025 (`SLT_TERADATA_PORT`) | `SLT_TERADATA_HOST` and `SLT_TERADATA_*`, in `.env` or the shell (below) | **supported, not exercised here**. With `SLT_TERADATA_HOST` unset, its tests skip, naming the setting. A service of your own named `teradata` with a container (`servicesRoots`, [YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md)) replaces the definition |
| `servicenow` | **none: bring your own instance.** ServiceNow is hosted: no container runs an instance. Its definition holds only the connection settings | not applicable | 443 (`SLT_SERVICENOW_PORT`) | `SLT_SERVICENOW_HOST` and `SLT_SERVICENOW_*`, in `.env` or the shell | **supported, not exercised here**. With `SLT_SERVICENOW_HOST` unset, its tests skip, naming the setting. The framework does not read or write ServiceNow: assert on where a reader's data lands, or on Striim's own figures ([SERVICENOW.md](SERVICENOW.md)) |
| BigQuery | **not shipped**: Google publishes no official emulator | | | | **not supported as a service.** Test against a real GCP project: create a dataset for tests, put the service-account key on the Striim server with `server_files:`, pass the project and dataset to the TQL through `tokens:`, and assert on what the app writes elsewhere, since the framework cannot read BigQuery |

"Recipe" means the framework builds the image from a public base; "stock" means it uses a public image
as it is. Any service can move to another host port through the variable shown, when the default is
taken; `striim-test doctor` checks this and names the variable.

ADLS Gen2 cannot be emulated (Azurite does not support it): test it against a real account. Any other
system: add it as your own service ([YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md)), or test against a
real instance.

### Integration tier

The integration tier (`scripts/integration`) tests an Open Processor or UDF against databases without a
whole Striim app. It uses its own containers, on other ports, so it can run beside the live tier.
This repo ships no integration cases (`SLT_INT_CASES` points at yours).

| Service | Image | Host port | Your own instance | Status |
|---|---|---|---|---|
| `postgres` | stock: `postgres:16` | 15432 | `INT_PG_HOST` and `INT_PG_*` | **supported, not exercised here** |
| `mysql` | stock: `mysql:8.0` | 13306 | `INT_MYSQL_HOST` and `INT_MYSQL_*` | **supported, not exercised here** |
| `oracle` | stock: `gvenzl/oracle-free:23.26.2-slim-faststart` (amd64) | 11521 | `INT_ORA_HOST` and `INT_ORA_*` | **supported, not exercised here** |
| `sqlserver` | recipe: SQL Server 2022 (amd64) | 11433 | `INT_MSSQL_HOST` and `INT_MSSQL_*` | **supported, not exercised here** |
| **`spanner`** | **Google's official Cloud Spanner emulator** `1.5.55` (amd64), on by default | 19010, 19020 | `INT_SPANNER_HOST` and `INT_SPANNER_*` | **supported, not exercised here** |
| `gcs` | stock: `fsouza/fake-gcs-server:1.54.0` | 14443 | `INT_GCS_HOST` and `INT_GCS_*` | **supported, not exercised here** |
| `vertica` | recipe: `opentext/vertica-k8s:25.3.0-8-multiarch`, as the live tier | 15433 | `INT_VERTICA_HOST` and `INT_VERTICA_*` | **supported, not exercised here** (`scripts/integration/tests/test_vertica_live.py` covers the routes) |
| `teradata` | **none: bring your own instance** (connection settings only) | 1025 (`INT_TERADATA_PORT`) | `INT_TERADATA_HOST` and `INT_TERADATA_*` | **supported, not exercised here**. With `INT_TERADATA_HOST` unset, its cases skip, naming the setting |
| `servicenow` | **none: bring your own instance** (connection settings only) | 443 (`INT_SERVICENOW_PORT`) | `INT_SERVICENOW_HOST` and `INT_SERVICENOW_*` | **supported, not exercised here**. With `INT_SERVICENOW_HOST` unset, its cases skip, naming the setting ([SERVICENOW.md](SERVICENOW.md)) |

Each service's full list of settings is under `live_env` in `scripts/integration/services/<name>/service.yaml`.

## Test features

The reference for each is [TEST-YAML.md](TEST-YAML.md).

### Assertions (`assert:`)

| Tier | Status |
|---|---|
| `smoke`: the app reaches RUNNING and stays there | **tested**: every sample |
| `data`: a target table's rows, count or golden (Postgres, Oracle, SQL Server, MySQL, Teradata, Vertica, Spanner, Kafka) | **tested**: samples 01, 02 and 04 (Postgres), the code samples |
| `diff`: the target's rows equal the source's | **supported, not exercised here** (`scripts/live/regression/services/postgres/postgres-diff`) |
| `file`: what a `FileWriter` wrote | **tested**: sample 03 (with `exact:`), the code samples (without) |
| `gcs`: an object's bytes, size or hash in the GCS emulator | **supported, not exercised here** |
| `json`: a JSON column, compared by meaning | **supported, not exercised here** |
| `monitor`: what Striim's monitor shows for a target | **supported, not exercised here** (`scripts/live/regression/framework/framework-assert-monitor`) |
| `monitor` `component:` (any component) and the `present` / `absent` / `matches` matchers | **supported, not run on a cluster yet**; hermetic tests only |
| `checkpoint_history`: whether Striim recorded a recovery checkpoint | **supported, not exercised here** |
| `expect_halt` / `expect_halt_contains`: the app must halt | **supported, not exercised here** (`framework-expect-halt`) |
| `jmx`: an Open Processor's MBean attributes, read from the Striim nodes' JMX exporters | **supported, not exercised here**. Docker mode only; each spec names the MBean domain its bean is registered under. No shipped case uses it |

**An `assert:` key that is not one of these tiers is not rejected, and never runs.** A typo there
(`rowcount:` for `data:` with `rows:`) leaves the test checking less than it says, and neither the
run nor `striim-test doctor` reports it. Check the tier names against this table.

### Everything else

| Feature | Status |
|---|---|
| `exact:` on `data` (Postgres) and `file` (Docker mode) | **tested**: samples 01 to 04 |
| `exact:` on `diff`, and on other databases | `diff`: **supported, not exercised here**. Other databases: **not supported**; compare without `exact:`, or copy the rows to Postgres |
| `lifecycle:` `initial-load` with a table (`baseline-landed`, `source-count`) | **tested**: samples 01 and 02 |
| `lifecycle:` `initial-load` with a file (`baseline-landed`, `file-lines`) | **tested**: sample 03 (Docker mode) |
| `lifecycle:` `cdc` with `sentinel` | **tested**: sample 04 |
| `lifecycle:` `source-progress`, `row-count` | **supported, not exercised here** |
| `lifecycle:` on a database other than Postgres | **not supported**: witnesses read Postgres only. Use no `lifecycle:` block (the test then polls its assertions until `timeout`) |
| `seed:` `pre_deploy` and `post_start` | **tested**: samples 01 to 04 |
| `seed:` `after:` delays, `post_recover` | **supported, not exercised here** (`framework-seed-after`, `framework-seed-phases`) |
| `recover:` `stop`, `quiesce` | **supported, not exercised here** (`framework-recover`, `framework-recover-quiesce`) |
| `recover:` `kill` | **supported, not exercised here**, Docker mode only (`framework-recover-kill`). It kills every Striim node container, so every other test on that cluster fails with it |
| `xfail:` | **supported, not exercised here** |
| `disabled:` | **supported, not exercised here** |
| `action:` (`stop_start_cycle`, `drop_recreate_app`) | **supported, not exercised here** |
| `action:` `service_outage` | **supported, not exercised here**, Docker mode only. It restarts the shared service container (with `signal: RESTART`, the server inside it, through the service's `restart_in_place`), so pair it with `disabled_parallel:` |
| `server_files:` | **supported, not exercised here** (`framework-server-files`) |
| `action:` `capture`, `alter_recompile`; `drop_recreate_app` `capture` / `stopped_seed` / `tokens` | **supported, not run on a cluster yet**; hermetic tests only |
| `server_files:` `load:` from outside the test dir (absolute, `${...}`, `gs://`) | **supported, not run on a cluster yet**; hermetic tests only (`gs://` with a fake download) |
| `op:` / `udf:` (your jars, built with Maven) | **tested**: `samples/code/op` and `samples/code/udf`, both modes, on the Linux host |
| `tokens:`, `kafka_cleanup_topics:`, `generate:` (GoldenGate trail files) | **supported, not exercised here** |
| `topology: cluster` | **supported, not exercised here** |

### Running

| Feature | Status |
|---|---|
| `striim-test run --parallel` (3 workers) | **tested**: every sample on the Linux host |
| `SLT_INFRA_OWNERSHIP=shared` with `SLT_KEEP_SERVICES=1` | **tested**: every Linux-host and Mac run above |
| `SLT_INFRA_OWNERSHIP=exclusive` | **tested** on the Linux host: each run started its own prefixed stack and tore it down |
| leftover clean-up: `--keep-resources`, then `python -m livetest.ownership replay <ledger>` (doctor names it) | **tested**: two ledgers left by a failed clean-up were replayed on the Linux host |
| tests in your own repo (`SLT_PROJECT_ROOT`, or `--targets gold-targets.yaml`) | **supported, not exercised here** |
| your own services (`servicesRoots:`) | **tested** by the framework's own tests and `docker compose config`, not in a live run. The Postgres 17 example in [YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md) was built and its `wal2json` slot checked |

## Limits the framework enforces

| Limit | Value | Why |
|---|---|---|
| namespace (`${NS}`) | at most 40 characters | a component's full name is `<namespace>.<name>`; SpannerWriter fails at `START` ("Insufficient Privilege to get Dialect") once that reaches 64 |
| component names (source, CQ, target) | keep to 21 characters | so the full name stays within 62 |
| Oracle table names | `${TID_ORACLE}` plus your name within Oracle's identifier limit | the prefix takes 11 characters |
| Striim license CPUs, Docker mode | 24 in the tested licenses, counted by each of the two nodes | on a machine with more than 12 CPUs, set `SLT_STRIIM_PRIMARY_CPUS=12 SLT_STRIIM_NODE_CPUS=12`, or the second node refuses to join |
| Docker disk, Docker mode | 35 GB free for the first image build, 5 GB to run | `SLT_STRIIM_MIN_FREE_GB` lowers the build floor; doctor reports free space |
| download, first Docker run | about 6.3 GB, or none with `SLT_STRIIM_DEPS_MANIFEST` | |
| `exact:` | 1,000,000 rows and 512 MiB per side at most (defaults 100,000 and 64 MiB) | nothing is truncated; over the limit the comparison fails |
| `lifecycle:` and `exact:` routes | `postgres-source` and `postgres-target` | the other databases have no bounded reader yet |
| lifecycle zero | `expect: 0`, `lines: 0` refused; a zero source count fails | zero proves nothing; use the sentinel kinds for an empty result |
| `--parallel` | 3 workers | |
| `diff_poll` | above 0 | a zero poll would spin against the database |
| unknown `test.yaml` keys | refused at load, top level and inside `op:`/`udf:`, `exact:`, `lifecycle:`, `recover:`, `xfail:` | `striim-test doctor --case` reports them. `assert:` tier names are not checked (above) |
