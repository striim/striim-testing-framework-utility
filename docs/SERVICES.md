# Services

A service is a database, broker or emulator a test lists under `requires:`. The framework starts it
in Docker with test-only accounts, or reuses it if it already runs, or connects to your own instance
when you set its host. This page has, for each shipped service: how to reach it, the tokens your
TQL and SQL use, the route names for `ddl:`, `seed:` and assertions, and what catches people out.

- What each service can do, and what has been tested: [WHAT-IS-SUPPORTED.md](WHAT-IS-SUPPORTED.md).
- Adding a service, or replacing a shipped one with your own image:
  [YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md).

## The model

Every database has the same three accounts:

- an **admin** account, used only by the framework, never by your app;
- **`qasource`**, which owns the `qasource` schema, for source tables and the app's reader;
- **`qatarget`**, which owns the `qatarget` schema, for target tables and the app's writer.

**Every password is `striim`.** A source table `T` and a target table `T` coexist as `qasource.T`
and `qatarget.T`. The schemas are shared by every test, so each test keeps apart by putting
`${TID}` at the start of every object name.

**Two places reach a service, and only the host differs:**

| From | Host | Port |
|---|---|---|
| This machine: the framework itself, a SQL client, or a native Striim | `localhost` | the published port |
| Striim running in Docker | `host.docker.internal` | the same published port |

Your TQL never names either: it uses the tokens below, which the framework fills in for the Striim
it found. To move a service to another host port (the default is taken), set its `*_HOST_PORT`
setting in `.env`; the container and every client move together, and `striim-test doctor` names
the setting when the port is busy.

## Quick reference

| Service (`requires:`) | Runs as | CDC | Host port | Routes | Use your own: set |
|---|---|---|---|---|---|
| `postgres` | `postgres:16` + `wal2json` | yes | 5432 | `postgres-source`, `postgres-target` | `SLT_PG_HOST` |
| `oracle` | Oracle Free 23ai, ARCHIVELOG + LogMiner user | yes | 1521 | `oracle-source`, `oracle-target` | `SLT_ORA_HOST` |
| `mssql` | SQL Server 2022 Developer, CDC + Agent | yes | 1433 | `mssql-source`, `mssql-target` | `SLT_MSSQL_HOST` |
| `mysql` | `mysql:8.0`, binlog `ROW` | yes | 3306 | `mysql-source`, `mysql-target` | `SLT_MYSQL_HOST` |
| `vertica` | Vertica 25.3 Community Edition, one node | no | 5433 | `vertica-source`, `vertica-target`, `vertica-admin` | `SLT_VERTICA_HOST` |
| `kafka` | Confluent 7.6.1: Kafka, ZooKeeper, Schema Registry | n/a | 9092, 19092, 8081 | `kafka` | `SLT_KAFKA_HOST` |
| `spanner` | Cloud Spanner emulator 1.5.55 | change streams | 9010, 9020 | `spanner-google`, `spanner-postgres` | `SLT_SPANNER_HOST` |
| `gcs` | `fake-gcs-server` 1.54.0 + a token server | n/a | 4443, 4444 | `gcs` | `SLT_GCS_HOST` |
| `teradata` | **your instance only** | n/a | 1025 | `teradata-source`, `teradata-target`, `teradata-admin` | `SLT_TERADATA_HOST` (required) |
| `servicenow` | **your instance only** | n/a | 443 | none | `SLT_SERVICENOW_HOST` (required) |

Each service's full list of settings is under `live_env` in
`scripts/live/services/<name>/service.yaml`, and its tokens under `provides`.

## Using your own instance

Set the service's host, and the other settings default to the Docker values ([SETTINGS.md](SETTINGS.md) lists every
service's settings with those values):

```
SLT_PG_HOST=db.example.com
SLT_PG_PORT=5432
SLT_PG_ADMIN_USER=postgres
SLT_PG_ADMIN_PASSWORD=...
```

- Put them in `.env`, or export them (the shell wins). `striim-test doctor` lists the ones that are
  set and where each comes from.
- **Use a disposable instance.** The framework creates the `qasource` and `qatarget` roles and
  schemas if they are missing, and drops the objects each test creates. It does not empty the
  schemas, so keep other data out of them.
- If Striim reaches the instance under another name than this machine does, set
  `SLT_<SERVICE>_VIEW_HOST` (for example `SLT_POSTGRES_VIEW_HOST`), or `SLT_STRIIM_VIEW_HOST` for
  every service.
- **An existing Striim container outside your stack prefix** is treated as your own server.
  `SLT_STACK_PREFIX` selects the framework's Docker container names; it does not change
  `STRIIM_URL` or identify an external container. Set `SLT_STRIIM_VIEW_HOST` to the host that
  server uses to reach the services (for a local Docker container, `host.docker.internal`).
  Otherwise the external/native default is `localhost`, which points inside that container.
  To provision a separate cluster instead, select an unused HTTP host port and matching
  `STRIIM_URL`, plus unused ports for its other published endpoints, as described in
  [SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md#several-checkouts-or-people-on-one-machine).
- On SIGTERM or SIGINT a run still cleans up its apps and objects (within 90 seconds) before it
  exits non-zero.

## Postgres (`postgres`)

The default service; the samples use it. `postgres:16` with the `wal2json` plugin, started with
`wal_level=logical`.

| | |
|---|---|
| JDBC | `jdbc:postgresql://HOST:5432/sltdb` |
| Accounts | admin `postgres`; `qasource`, `qatarget` (both with `REPLICATION`); password `striim` |
| Tokens | `PG_URL`, `PG_HOST`, `PG_PORT`, `PG_DB`, `PG_SOURCE_USER`, `PG_SOURCE_PASSWORD`, `PG_SOURCE_SCHEMA`, `PG_TARGET_USER`, `PG_TARGET_PASSWORD`, `PG_TARGET_SCHEMA`, `PG_SLOT` |
| Settings | `SLT_PG_HOST`, `SLT_PG_PORT`, `SLT_PG_DB`, `SLT_PG_ADMIN_USER`/`_PASSWORD`, `SLT_PG_SOURCE_USER`/`_PASSWORD`/`_SCHEMA`, `SLT_PG_TARGET_USER`/`_PASSWORD`/`_SCHEMA`; port `SLT_PG_HOST_PORT`; memory `SLT_POSTGRES_MEM_LIMIT` (default 4g) |

- **CDC:** `PostgreSQLReader` needs a replication slot it can read. Create it in its own DDL file
  with `${PG_SLOT}` as its name (`samples/live/04-lifecycle-check/slot.sql`): Postgres cannot
  create a slot after a write in the same transaction. The framework drops the slot at the end of
  the test; an inactive slot left behind holds WAL and fills the disk.
- **Your own Postgres for CDC** needs `wal_level=logical`, the `wal2json` plugin installed and
  allowed as an output plugin, and the `REPLICATION` attribute on the source user.
- `exact:` comparisons and `lifecycle:` blocks read Postgres routes only.

## Oracle (`oracle`)

Oracle Free 23ai, built from `gvenzl/oracle-free` with ARCHIVELOG, supplemental logging and a
LogMiner user baked in. It runs natively on Linux x86-64 and Apple Silicon.

| | |
|---|---|
| JDBC, data | `jdbc:oracle:thin:@//HOST:1521/FREEPDB1` (the pluggable database) |
| JDBC, CDC | `jdbc:oracle:thin:@//HOST:1521/FREE` (the container database root) |
| Accounts | admin `system`; `qasource`, `qatarget`; CDC user `c##striim`; password `striim` |
| Tokens | `ORACLE_URL`, `ORACLE_CDC_URL`, `ORACLE_CDC_OCI_URL`, `ORACLE_HOST`, `ORACLE_PORT`, `ORACLE_SERVICE`, `ORACLE_SOURCE_USER`, `ORACLE_SOURCE_PASSWORD`, `ORACLE_SOURCE_SCHEMA`, `ORACLE_TARGET_USER`, `ORACLE_TARGET_PASSWORD`, `ORACLE_TARGET_SCHEMA`, `ORACLE_CDC_USER`, `ORACLE_CDC_PASSWORD` |
| Settings | `SLT_ORA_HOST` and the other `SLT_ORA_*` keys; port `SLT_ORA_HOST_PORT` |

- **CDC:** `OracleReader` connects to the container root (`${ORACLE_CDC_URL}`) as
  `${ORACLE_CDC_USER}`, and names each table with the pluggable database in front:
  `FREEPDB1.${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC`. LogMiner mines the whole container database, so an
  unqualified name matches nothing. Writers and `DatabaseReader` connect to the pluggable database
  (`${ORACLE_URL}`). See `scripts/live/regression/services/oracle/oracle-cdc-diff`.
- **Table names:** use `${TID_ORACLE}` (upper case) instead of `${TID}`. It takes 11 characters of
  Oracle's identifier limit.
- Oracle is slow to start the first time; `SLT_KEEP_SERVICES=1` (the default in `.env.example`)
  keeps it up between runs.

## SQL Server (`mssql`)

SQL Server 2022 Developer edition with CDC and the SQL Server Agent enabled. The image is amd64
only, so it runs under emulation on Apple Silicon.

| | |
|---|---|
| JDBC | `jdbc:sqlserver://HOST:1433;databaseName=qauser;encrypt=true;trustServerCertificate=true` |
| Accounts | admin `sa`; `qasource`, `qatarget` (members of `db_owner` in `qauser`); password `striim` |
| Tokens | `MSSQL_URL`, `MSSQL_HOST`, `MSSQL_PORT`, `MSSQL_HOSTPORT` (`host:port`), `MSSQL_DB`, `MSSQL_SOURCE_USER`, `MSSQL_SOURCE_PASSWORD`, `MSSQL_SOURCE_SCHEMA`, `MSSQL_TARGET_USER`, `MSSQL_TARGET_PASSWORD`, `MSSQL_TARGET_SCHEMA` |
| Settings | `SLT_MSSQL_HOST` and the other `SLT_MSSQL_*` keys; port `SLT_MSSQL_HOST_PORT` |

- **CDC: disable before you drop.** SQL Server ties a capture instance to a table, and a plain
  `DROP TABLE` leaves it behind, so the next run fails with "capture instance already exists".
  A CDC test's DDL disables CDC on the table before dropping it, and enables it after creating it:

  ```sql
  IF EXISTS (SELECT 1 FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id
             WHERE s.name = '${MSSQL_SOURCE_SCHEMA}' AND t.name = '${TID}SRC' AND t.is_tracked_by_cdc = 1)
      EXEC sys.sp_cdc_disable_table @source_schema = N'${MSSQL_SOURCE_SCHEMA}', @source_name = N'${TID}SRC', @capture_instance = N'all';
  DROP TABLE IF EXISTS ${MSSQL_SOURCE_SCHEMA}.${TID}SRC;
  CREATE TABLE ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (id INT PRIMARY KEY, msg VARCHAR(200));
  EXEC sys.sp_cdc_enable_table @source_schema = N'${MSSQL_SOURCE_SCHEMA}', @source_name = N'${TID}SRC', @role_name = NULL, @supports_net_changes = 0;
  ```

  `scripts/live/regression/services/mssql/mssql-cdc-diff` is a whole case. Seed the changes
  `when: post_start`, so the capture job sees them.

## MySQL (`mysql`)

`mysql:8.0` with binary logging in `ROW` format. MySQL 8.0 is past its upstream end of life.

| | |
|---|---|
| JDBC | `jdbc:mysql://HOST:3306/` |
| Accounts | admin `root`; `qasource` (with `REPLICATION SLAVE`, `REPLICATION CLIENT`), `qatarget`; password `striim` |
| Tokens | `MYSQL_URL`, `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_SOURCE_USER`, `MYSQL_SOURCE_PASSWORD`, `MYSQL_SOURCE_SCHEMA`, `MYSQL_TARGET_USER`, `MYSQL_TARGET_PASSWORD`, `MYSQL_TARGET_SCHEMA`; `MYSQL_USER`/`MYSQL_PASSWORD` are the source account |
| Settings | `SLT_MYSQL_HOST` and the other `SLT_MYSQL_*` keys; port `SLT_MYSQL_HOST_PORT` |

- `samples/live/05-mysql-initial-load` and `06-mysql-cdc` are complete tests.
- **CDC:** `MySQLReader` with `StartPositionByName: true` reads from the current binlog position,
  so seed the changes `when: post_start`.
- `DatabaseWriter`'s `Tables` is a source,target pair:
  `'${MYSQL_SOURCE_SCHEMA}.${TID}orders,${MYSQL_TARGET_SCHEMA}.${TID}orders'`.
- `${TID}` must be a **prefix**: clean-up drops the tables whose names start with it, so
  `orders_${TID}` is left behind in a parallel run.

## Vertica (`vertica`)

A one-node Vertica 25.3 Community Edition (unlicensed: 1 TB, three nodes). Multi-arch, so it runs
natively on Apple Silicon. 25.3 is the newest release that still runs as Community Edition.

| | |
|---|---|
| JDBC | `jdbc:vertica://HOST:5433/sltdb` (no TLS) |
| Accounts | admin `dbadmin`; `qasource`, `qatarget`; password `striim` |
| Tokens | `VERTICA_URL`, `VERTICA_HOST`, `VERTICA_PORT`, `VERTICA_DB`, `VERTICA_SOURCE_USER`, `VERTICA_SOURCE_PASSWORD`, `VERTICA_SOURCE_SCHEMA`, `VERTICA_TARGET_USER`, `VERTICA_TARGET_PASSWORD`, `VERTICA_TARGET_SCHEMA` |
| Settings | `SLT_VERTICA_HOST` and the other `SLT_VERTICA_*` keys; port `SLT_VERTICA_HOST_PORT` |

- `DatabaseReader` emits three-part names (`sltdb.qasource.<table>`), so `DatabaseWriter`'s
  `Tables` mapping uses all three parts.
- `vertica-admin` runs a `seed:` statement as `dbadmin`; nothing is dropped on it.
- The Striim image carries the Vertica JDBC driver.

## Kafka (`kafka`)

Kafka with ZooKeeper and a Confluent Schema Registry, for `KafkaReader` and `KafkaWriter` with Avro.

| | |
|---|---|
| Broker | `localhost:9092` from this machine; `host.docker.internal:19092` from Striim in Docker |
| Registry | `http://HOST:8081` |
| Accounts | none |
| Tokens | `KAFKA_BROKER`, `KAFKA_SCHEMA_REGISTRY_URL`, `KAFKA_ZOOKEEPER`, `KAFKA_SRC_TOPIC`, `KAFKA_TGT_TOPIC` (per-test topics `slt_<tid>_src`, `slt_<tid>_tgt`) |
| Settings | `SLT_KAFKA_HOST` and the other `SLT_KAFKA_*` keys; ports `SLT_KAFKA_HOST_PORT`, `SLT_KAFKA_DOCKER_HOST_PORT`, `SLT_SCHEMA_REGISTRY_HOST_PORT`, `SLT_ZOOKEEPER_CLIENT_PORT` |

- **Striim's Avro framing is not Confluent's.** With a schema registry, Striim writes
  length-delimited frames (`[int32 length][int32 schema id][Avro payload]`, several per message),
  read by `com.striim.avro.deserializer.LengthDelimitedAvroRecordDeserializer`. Standard Confluent
  deserializers cannot decode them. The framework reads and writes this framing when it seeds and
  compares topics.
- A topic your app creates that the framework does not know about (a persisted stream's topic, for
  example) goes in `kafka_cleanup_topics:` so it is removed at the end.

## Spanner (`spanner`)

Google's Cloud Spanner emulator, with an instance and two databases: `gsql` (GoogleSQL) and `pgdb`
(PostgreSQL dialect).

| | |
|---|---|
| JDBC | `jdbc:cloudspanner://HOST:9010/projects/test-project/instances/test-inst/databases/gsql?autoConfigEmulator=true` |
| Accounts | none |
| Tokens | `SPANNER_GSQL_URL`, `SPANNER_PG_URL`, `SPANNER_PROJECT`, `SPANNER_INSTANCE`, `SPANNER_GSQL_DB`, `SPANNER_PG_DB` |
| Settings | `SLT_SPANNER_HOST` and the other `SLT_SPANNER_*` keys; ports `SLT_SPANNER_GRPC_HOST_PORT`, `SLT_SPANNER_REST_HOST_PORT` |

- `SpannerWriter` uses Google's client, which reaches an emulator only through
  `SPANNER_EMULATOR_HOST` on the Striim server. The framework sets it on the Docker cluster it
  starts; a Striim server you provide needs it set yourself.
- The writer still parses a service-account key. The framework uploads a throwaway one; reference
  it as `ServiceAccountKey: 'UploadedFiles/fake-gcp-key.json'`.
- GoogleSQL targets are `<database>.<table>` (`gsql.emp`); PostgreSQL-dialect targets are
  `public.<table>`.
- The emulator runs one read-write transaction at a time: run Spanner tests serially.

## GCS (`gcs`)

`fake-gcs-server` as a Cloud Storage reader and writer target, plus a small fake token server.

| | |
|---|---|
| Endpoint | `http://HOST:4443`; tokens on 4444 |
| Accounts | none |
| Tokens | `GCS_ENDPOINT`, `GCS_PROJECT`, `GCS_SRC_BUCKET`, `GCS_TGT_BUCKET` (the framework derives a per-test bucket name from them) |
| Settings | `SLT_GCS_HOST` and the other `SLT_GCS_*` keys; ports `SLT_GCS_HOST_PORT`, `SLT_GCS_TOKEN_HOST_PORT` |

- **The endpoint must be an IP address.** Striim's GCS adapters reject `host.docker.internal` and
  `localhost` as an endpoint host, so `${GCS_ENDPOINT}` carries the IP Striim reaches the emulator
  at. Always use the token.
- The adapters fetch an OAuth token before any call; the framework's throwaway key points them at
  the fake token server.
- `GCSReader` against a fresh emulator: set `PollingInterval` (milliseconds) and
  `_h_GCSQueryCoolingTime: '0'`, or it skips objects newer than five minutes.

## Teradata (`teradata`)

No container: Teradata's own VM image is not public. A test that requires `teradata` runs against
your instance, or skips naming the setting to set.

1. Install the driver, an optional extra: `pip install -e ".[teradata]"` in the framework's
   environment, or `pip install teradatasql`.
2. Give Striim the Teradata JDBC driver. The framework's Striim image does not include it: put the
   jar in `scripts/live/services/striim/images/striim/deps/extra-lib/` (Docker mode), or in your
   Striim install's `lib/` (native mode).
3. Set your instance, in `.env` or the shell:

| Setting | Live tier | Integration tier | Default |
|---|---|---|---|
| host (required) | `SLT_TERADATA_HOST` | `INT_TERADATA_HOST` | none |
| port | `SLT_TERADATA_PORT` | `INT_TERADATA_PORT` | 1025 |
| admin user, password | `SLT_TERADATA_USER`, `SLT_TERADATA_PASSWORD` | `INT_TERADATA_ADMIN_USER`, `INT_TERADATA_ADMIN_PASSWORD` | none; both required |
| admin database | (not used) | `INT_TERADATA_DB` | `dbc` |
| source user, password, schema | `SLT_TERADATA_SOURCE_USER`, `_PASSWORD`, `_SCHEMA` | `INT_TERADATA_SOURCE_USER`, `_PASSWORD`, `_SCHEMA` | user and password required; schema `qasource` |
| target user, password, schema | `SLT_TERADATA_TARGET_USER`, `_PASSWORD`, `_SCHEMA` | `INT_TERADATA_TARGET_USER`, `_PASSWORD`, `_SCHEMA` | user and password required; schema `qatarget` |
| host Striim reaches it by | `SLT_TERADATA_VIEW_HOST` | (not used) | the host |

- Tokens: `TERADATA_URL` (`jdbc:teradata://HOST/DBS_PORT=1025`), `TERADATA_HOST`, `TERADATA_PORT`,
  `TERADATA_SOURCE_USER`, `TERADATA_SOURCE_PASSWORD`, `TERADATA_SOURCE_SCHEMA`, and the `TARGET`
  equivalents.
- The admin user creates the source and target users when they cannot log in. In Teradata a user
  is also a database: set each schema to match its configured user. Missing or blank
  credentials fail with the required environment variable names before connecting.
- `teradata-admin` runs a `seed:` statement as the admin user (for something a data user may not
  run, such as `SYSLIB.AbortSessions`); nothing is dropped on it, so do not use it for `ddl:`.
- **Licence.** `teradatasql` is Teradata's proprietary driver, not part of this framework.
  Installing it means accepting Teradata's licence for it (the `LICENSE` file it installs). That
  licence allows using the driver only with your own licensed Teradata platform, and forbids
  publishing test or benchmark results, including performance, without Teradata's written consent.

## ServiceNow (`servicenow`)

No container: ServiceNow is hosted. A test that requires `servicenow` runs against a sub-production
instance you own, or skips naming `SLT_SERVICENOW_HOST`. The framework does not read or write
ServiceNow itself, so tests assert on where a reader's data lands, or on Striim's own figures.

- Tokens: `SERVICENOW_URL`, `SERVICENOW_HOST`, `SERVICENOW_USER`, `SERVICENOW_PASSWORD`,
  `SERVICENOW_CLIENT_ID`, `SERVICENOW_CLIENT_SECRET`, `SERVICENOW_TOKEN_URL`.
- Preparing the instance, the settings and a whole test: [SERVICENOW.md](SERVICENOW.md).

## Striim itself

In Docker mode the framework builds a Striim image and runs a primary node (`slt-striim`, web UI and
REST on port 9080), a second node (`slt-node`) and an agent (`slt-agent`). The login is
`admin`/`striim`. Building it, its license and its resource limits are in
[RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md), "Docker mode".

## Not shipped

- **BigQuery** has no official emulator. Test against a real GCP project: a dataset for tests, the
  service-account key placed on the Striim server with `server_files:`, the project and dataset
  passed to the TQL with `tokens:`, and assertions on what the app writes elsewhere.
- **ADLS Gen2** cannot be emulated; test it against a real account.
- Anything else: add it yourself ([YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md)), or use a real
  instance through the service's settings.

## Integration tier

The integration tier ([INTEGRATION-TESTS.md](INTEGRATION-TESTS.md)) runs its own containers on
other ports, so both tiers can run at once. Same accounts and passwords.

| Service | Host port | Use your own: set |
|---|---|---|
| `postgres` (`postgres:16`) | 15432 | `INT_PG_HOST` and `INT_PG_*` |
| `mysql` (`mysql:8.0`) | 13306 | `INT_MYSQL_HOST` and `INT_MYSQL_*` |
| `oracle` (Oracle Free 23ai, amd64) | 11521 | `INT_ORA_HOST` and `INT_ORA_*` |
| `sqlserver` (SQL Server 2022) | 11433 | `INT_MSSQL_HOST` and `INT_MSSQL_*` |
| `spanner` (emulator 1.5.55) | 19010, 19020 | `INT_SPANNER_HOST` and `INT_SPANNER_*` |
| `gcs` (`fake-gcs-server` 1.54.0) | 14443 | `INT_GCS_HOST` and `INT_GCS_*` |
| `vertica` (as the live tier, database `intdb`) | 15433 | `INT_VERTICA_HOST` and `INT_VERTICA_*` |
| `teradata` | your instance | `INT_TERADATA_HOST` and `INT_TERADATA_*` |
| `servicenow` | your instance | `INT_SERVICENOW_HOST` and `INT_SERVICENOW_*` |

Each one's settings are under `live_env` in `scripts/integration/services/<name>/service.yaml`.

## Connecting a SQL client

With the hosts and ports above, from this machine:

- **Postgres**: host `localhost`, port `5432`, database `sltdb`, user `qasource` or `qatarget`,
  password `striim`.
- **Oracle**: host `localhost`, port `1521`, service name `FREEPDB1`, user `qasource`.
- **SQL Server**: host `localhost`, port `1433`, database `qauser`, user `qasource`; trust the
  server certificate.
- **Vertica**: host `localhost`, port `5433`, database `sltdb`, user `qasource`; SSL off.

**Spanner in DBeaver** needs a custom driver:

1. Database → Driver Manager → New. Driver name `Spanner Emulator`, type Generic, class
   `com.google.cloud.spanner.jdbc.JdbcDriver`, "No authentication" on, host and port fields blank.
2. Libraries tab: Add Artifact `com.google.cloud:google-cloud-spanner-jdbc:RELEASE` (or add the jars
   from Maven Central with Add File).
3. In the connection, set **Connect by: URL** (not Host) and paste
   `jdbc:cloudspanner://localhost:9010/projects/test-project/instances/test-inst/databases/gsql?autoConfigEmulator=true`
   (`pgdb` for the PostgreSQL dialect). With "Connect by: Host" and no URL template, DBeaver fails
   with "Cannot generate database URL with empty sample URL template".
4. Use port 9010 (gRPC), not 9020, and keep `autoConfigEmulator=true`: it points the driver at the
   emulator and turns off TLS and credentials.
