# Documentation catalog

Guides and reference material, grouped by audience.

Generated from each file's title and opening prose. Run `python tools/doc_catalog.py`
after adding, removing or editing a guide; `python tools/doc_catalog.py --check`
checks the committed index without changing it.

Includes tracked and unignored Markdown. Excludes generated release notes and this
catalog, vendored dependencies, build output and test fixtures (including expected data).
Runnable regression guides are included under Contributing and internals.

## Start here

| Title | File | Description |
|---|---|---|
| [striim-testing-framework-utility](../README.md) | `README.md` | Test your Striim apps against a real Striim. |
| [Run your first test](RUN-YOUR-FIRST-TEST.md) | `docs/RUN-YOUR-FIRST-TEST.md` | At the end of this page, samples/live/01-plain-replication passes on your machine and you can read its result. |
| [Start here](START-HERE.md) | `docs/START-HERE.md` | Visual reference: how the framework works, the Acme Retail walkthrough, and the documentation library (open in a browser). |

## Writing tests

| Title | File | Description |
|---|---|---|
| [Integration tests: an Open Processor or UDF without a Striim app](INTEGRATION-TESTS.md) | `docs/INTEGRATION-TESTS.md` | The integration tier drives your module's code directly: it builds the jar, loads it into a small Java harness, feeds it events from a JSON file, and compares what it emits with the events you expect. |
| [Testing your own Java: Open Processors and UDFs](TESTING-YOUR-JAVA.md) | `docs/TESTING-YOUR-JAVA.md` | A live test can build your Maven module, load the jar into Striim, and then deploy the app that uses it. |
| [Using an AI coding assistant with this repo](USING-AI.md) | `docs/USING-AI.md` | An AI coding assistant can do most of the typing in this repo: copy the right sample, rewrite your TQL with the framework's tokens, write the DDL and seed files, fill in test.yaml, run doctor, read a failure. |
| [Writing tests](WRITING-TESTS.md) | `docs/WRITING-TESTS.md` | This walks through one test from an empty folder to a run. |
| [Writing a TQL application](tql/AUTHORING.md) | `docs/tql/AUTHORING.md` | The steps from a requirement to a TQL file that deploys. |
| [TQL application patterns](tql/PATTERNS.md) | `docs/tql/PATTERNS.md` | Complete applications for the common shapes. |
| [TQL guides](tql/README.md) | `docs/tql/README.md` | How to write, tune and review Striim TQL applications. |
| [Reviewing TQL](tql/REVIEW.md) | `docs/tql/REVIEW.md` | A checklist for reviewing a TQL file, yours or someone else's, and the form a review takes. |
| [TQL design and performance rules](tql/RULES.md) | `docs/tql/RULES.md` | The rules a TQL application should follow, each with the reason and how to check it. |
| [Instructions for AI coding assistants](../examples/acme-retail/AGENTS.md) | `examples/acme-retail/AGENTS.md` | This repository holds Striim tests that the Striim testing framework runs. |
| [Acme Retail: prompts for an AI coding agent](../examples/acme-retail/PROMPTS.md) | `examples/acme-retail/PROMPTS.md` | These are the prompt templates, filled in for Acme Retail. |
| [Acme Retail Striim tests](../examples/acme-retail/README.md) | `examples/acme-retail/README.md` | This example is complete. It holds three apps and three tests, each with its expected result, and all three passed on a live Striim. |
| [Code samples](../samples/code/README.md) | `samples/code/README.md` | Two live cases that build and load your own Java into Striim before the app runs: |
| [Open Processor sample: referenceop-copy-adds-userdata](../samples/code/op/README.md) | `samples/code/op/README.md` | An Open Processor, ReferenceOpV1, that copies every event through unchanged and sets userdata.processed=true on the copy. |
| [ReferenceOp — Open Processor Adapter](../samples/code/op/java/OpenProcessors/ReferenceOp/README.md) | `samples/code/op/java/OpenProcessors/ReferenceOp/README.md` | The smallest complete Open Processor: it copies every inbound WAEvent through unchanged and stamps one userdata key onto the copy. |
| [copy-adds-userdata](../samples/code/op/java/OpenProcessors/ReferenceOp/examples/copy-adds-userdata/README.md) | `samples/code/op/java/OpenProcessors/ReferenceOp/examples/copy-adds-userdata/README.md` | The operator in its smallest form: every row read from a Postgres table is copied through unchanged and stamped with processed=true, then fanned out to two targets so both halves of that claim can be checked independently. |
| [UDF sample: referenceudf-mark-processed](../samples/code/udf/README.md) | `samples/code/udf/README.md` | A Striim UDF library with one function, ReferenceUdfMarkProcessed. |
| [ReferenceUdf: a minimal UDF library](../samples/code/udf/java/UserDefinedFunctions/ReferenceUdf/README.md) | `samples/code/udf/java/UserDefinedFunctions/ReferenceUdf/README.md` | A Striim UDF library with one WAEvent → WAEvent function that copies its input and stamps userdata.processed=true on the copy. |
| [ReferenceUdf — Runnable examples](../samples/code/udf/java/UserDefinedFunctions/ReferenceUdf/examples/README.md) | `samples/code/udf/java/UserDefinedFunctions/ReferenceUdf/examples/README.md` | One folder per function. Each is self-verifying: the real function is asserted to turn the fixture input into the fixture output — so these examples are documentation and a regression net at once, and cannot rot. |
| [01 Plain replication](../samples/live/01-plain-replication/README.md) | `samples/live/01-plain-replication/README.md` | The simplest live test: copy three rows from a Postgres source table to a target table and check that the target holds exactly the rows you expect. |
| [02 Transform](../samples/live/02-transform/README.md) | `samples/live/02-transform/README.md` | Like 01-plain-replication, with a transformation in the middle: a CQ doubles the amount of each event before DatabaseWriter writes it. |
| [03 File output](../samples/live/03-file-output/README.md) | `samples/live/03-file-output/README.md` | The app writes to a file instead of a table. |
| [04 Lifecycle check](../samples/live/04-lifecycle-check/README.md) | `samples/live/04-lifecycle-check/README.md` | A change-data-capture (CDC) test whose correct result is an empty target. |
| [05 MySQL initial load](../samples/live/05-mysql-initial-load/README.md) | `samples/live/05-mysql-initial-load/README.md` | The same shape as 01-plain-replication, on MySQL: DatabaseReader reads five rows that exist before the app starts, and DatabaseWriter copies them to a target table. |
| [06 MySQL CDC](../samples/live/06-mysql-cdc/README.md) | `samples/live/06-mysql-cdc/README.md` | The MySQL counterpart of 04-lifecycle-check's CDC pipeline: MySQLReader reads the binary log and DatabaseWriter applies every insert, update and delete to a copy. |
| [Striim TQL authoring](../skills/tql-authoring/SKILL.md) | `skills/tql-authoring/SKILL.md` | Sends TQL work to the guide in docs/tql/ it needs. |

## Services

| Title | File | Description |
|---|---|---|
| [Use your own ServiceNow instance in your tests](SERVICENOW.md) | `docs/SERVICENOW.md` | ServiceNow is a hosted platform: no container runs a ServiceNow instance. |
| [Services](SERVICES.md) | `docs/SERVICES.md` | A service is a database, broker or emulator a test lists under requires:. |
| [Your own services](YOUR-OWN-SERVICES.md) | `docs/YOUR-OWN-SERVICES.md` | A service is a container a test can requires:: a database, a message broker, an emulator. |
| [Integration-tier GCS emulator recipe](../scripts/integration/services/gcs/README.md) | `scripts/integration/services/gcs/README.md` | The integration tier's fake-gcs-server, on port 14443, with one fixed bucket and per-test isolation by ${TID}-prefixed object paths. |
| [GCS emulator service recipe](../scripts/live/services/gcs/README.md) | `scripts/live/services/gcs/README.md` | Using this service in a test (accounts, tokens, routes, using your own instance): docs/SERVICES.md. |
| [Kafka service recipe](../scripts/live/services/kafka/README.md) | `scripts/live/services/kafka/README.md` | Using this service in a test (accounts, tokens, routes, using your own instance): docs/SERVICES.md. |
| [SQL Server service recipe](../scripts/live/services/mssql/README.md) | `scripts/live/services/mssql/README.md` | Using this service in a test (accounts, tokens, routes, using your own instance): docs/SERVICES.md. |
| [MySQL service recipe](../scripts/live/services/mysql/README.md) | `scripts/live/services/mysql/README.md` | Using this service in a test (accounts, tokens, routes, using your own instance): docs/SERVICES.md. |
| [Oracle service recipe](../scripts/live/services/oracle/README.md) | `scripts/live/services/oracle/README.md` | Using this service in a test (accounts, tokens, routes, using your own instance): docs/SERVICES.md. |
| [Postgres service recipe](../scripts/live/services/postgres/README.md) | `scripts/live/services/postgres/README.md` | Using this service in a test (accounts, tokens, routes, using your own instance): docs/SERVICES.md. |
| [Spanner emulator service recipe](../scripts/live/services/spanner/README.md) | `scripts/live/services/spanner/README.md` | Using this service in a test (accounts, tokens, routes, using your own instance): docs/SERVICES.md. |
| [Striim image recipe](../scripts/live/services/striim/README.md) | `scripts/live/services/striim/README.md` | The Docker cluster the framework starts when STRIIM_URL is unset: slt-striim (primary, web UI and REST on 9080, login admin/striim), slt-node (a second node) and slt-agent. |
| [Extra JDBC drivers](../scripts/live/services/striim/images/striim/deps/extra-lib/README.md) | `scripts/live/services/striim/images/striim/deps/extra-lib/README.md` | Put a JDBC driver jar here to add it to the framework's Striim image: the build copies every .jar in this folder into Striim's lib/. |
| [Vertica service recipe](../scripts/live/services/vertica/README.md) | `scripts/live/services/vertica/README.md` | Using this service in a test (accounts, tokens, routes, using your own instance): docs/SERVICES.md. |

## Reference

| Title | File | Description |
|---|---|---|
| [Settings](SETTINGS.md) | `docs/SETTINGS.md` | Every setting the framework reads: where you can set it, its default, and what it does. tests/test_settings_reference.py fails when the code reads a setting this page does not list. |
| [test.yaml schema reference](TEST-YAML.md) | `docs/TEST-YAML.md` | Authoritative reference for every key a live-test manifest accepts. |
| [What live testing supports](WHAT-IS-SUPPORTED.md) | `docs/WHAT-IS-SUPPORTED.md` | Each row says what the framework does, marked one of three ways: |
| [TQL syntax reference](tql/REFERENCE.md) | `docs/tql/REFERENCE.md` | The TQL you need to write and read a Striim application: statements, components, CQ expressions, the WAEvent functions, and writer table mapping. |

## Running and troubleshooting

| Title | File | Description |
|---|---|---|
| [Run a downloaded example](RUN-A-DOWNLOADED-EXAMPLE.md) | `docs/RUN-A-DOWNLOADED-EXAMPLE.md` | Some Striim components are published together with a runnable example: a bundle holding the component's jar, one test case, its data and a README. striim-test fetch downloads a bundle, checks it, and unpacks it into a folder you then run like any other test. |
| [Set up your own test repo](SET-UP-YOUR-OWN-REPO.md) | `docs/SET-UP-YOUR-OWN-REPO.md` | Keep your tests in a repo of your own, next to your apps, and run them with the framework at one version you choose. |
| [Troubleshooting](TROUBLESHOOTING.md) | `docs/TROUBLESHOOTING.md` | Find what you see in the left column. Start with striim-test doctor --case : it checks your settings, Striim, the services a case requires and the case itself, and names what to change. |
| [Instructions for AI coding assistants](../templates/consumer-repo/AGENTS.md) | `templates/consumer-repo/AGENTS.md` | This repository holds Striim tests that the Striim testing framework runs. |
| [Your Striim tests](../templates/consumer-repo/README.md) | `templates/consumer-repo/README.md` | A starter for a repo of your own Striim tests, run by the Striim testing framework at the version in framework.pin. |

## Contributing and internals

| Title | File | Description |
|---|---|---|
| [Instructions for AI coding assistants](../AGENTS.md) | `AGENTS.md` | This repository is a test framework for Striim apps, Open Processors and UDFs. |
| [Contributing](../CONTRIBUTING.md) | `CONTRIBUTING.md` | This repository is gated: its maintainers review and merge every change. |
| [Security policy](../SECURITY.md) | `SECURITY.md` | Report suspected security vulnerabilities privately through this repository's Security tab, using Report a vulnerability. |
| [Upgrading the Java harness](UPGRADING.md) | `docs/UPGRADING.md` | The integration harness now uses the com.striim.testing.inttest package and the com.striim.testing Maven group. |
| [Live engine (scripts/live/, livetest)](internals/ENGINE.md) | `docs/internals/ENGINE.md` | How the live tier works inside. It is a pytest plugin (pyproject.toml loads livetest.plugin) that striim-test run drives; this page is for people changing the engine or running it with pytest directly. |
| [Integration engine (scripts/integration/, inttest)](internals/INTEGRATION-ENGINE.md) | `docs/internals/INTEGRATION-ENGINE.md` | How the integration tier works inside: its services, isolation, clean-up, fixtures and settings. |
| [Performance Testing Extension for the Integration Framework](internals/PERF_SPEC.md) | `docs/internals/PERF_SPEC.md` | Where this lives. This specification lives with the harness it specifies, in striim-testing-framework-utility: scripts/integration/inttest/perfmanifest.py (Section 3 loader), inttest/perf.py (pre-flight, reset, launch, sampling, correctness), inttest/perfreport.py (Sections… |
| [Releasing the framework](internals/RELEASING.md) | `docs/internals/RELEASING.md` | For maintainers. Users pin a release by its tag (vX.Y.Z) or by the commit it names; this page is how a tag is made. |
| [C8 — Exact data semantics slt-canon/1 (design note)](internals/design/c8-exact-data.md) | `docs/internals/design/c8-exact-data.md` | The design of exact comparison. The model is livetest.canon; the manifest dispatch, readers and ownership check are livetest.exactdata; the plugin's input-snapshot and evidence hooks call them. |
| [Lifecycle, run identity and ownership (design notes)](internals/design/lifecycle.md) | `docs/internals/design/lifecycle.md` | Modules: livetest/runident.py (identity), livetest/infra.py (infrastructure ownership), livetest/lifecycle.py (the lifecycle: block, witnesses, probes), livetest/ownership.py (the ownership ledger and cleanup). |
| [Integration engine (scripts/integration/)](../scripts/integration/README.md) | `scripts/integration/README.md` | The integration tier: inttest, which drives an Open Processor's or UDF's code directly, event in and event out, with no Striim server. striim-test run --tier integration drives it. |
| [Live engine (scripts/live/)](../scripts/live/README.md) | `scripts/live/README.md` | The live tier: livetest, a pytest plugin that deploys whole Striim apps and checks what they do. striim-test run drives it. |
| [framework/ — the harness testing itself](../scripts/live/regression/framework/README.md) | `scripts/live/regression/framework/README.md` | Other tests check an app, an Open Processor or a writer. |
| [hello-cluster](../scripts/live/regression/hello/hello-cluster/README.md) | `scripts/live/regression/hello/hello-cluster/README.md` | Exercises topology-aware placement: the reader flow (SourceFlow, DatabaseReader) is deployed to the agent group (${SOURCE_GROUP}) while the writer flow (AppFlow, DatabaseWriter) is deployed to the cluster group (${APP_GROUP}), via DEPLOY APPLICATION ... |
| [hello-single](../scripts/live/regression/hello/hello-single/README.md) | `scripts/live/regression/hello/hello-single/README.md` | The single-topology twin of hello-cluster. |
| [gcs-cdc-diff](../scripts/live/regression/services/gcs/gcs-cdc-diff/README.md) | `scripts/live/regression/services/gcs/gcs-cdc-diff/README.md` | gcs-diff with the object uploaded after the app is RUNNING (seed … when: post_start), so GCSReader has to find a new object while it polls, as a CDC reader captures changes made after start. |
| [gcs-diff](../scripts/live/regression/services/gcs/gcs-diff/README.md) | `scripts/live/regression/services/gcs/gcs-diff/README.md` | A round-trip through the Cloud Storage emulator (fake-gcs-server): GCSReader (DSV) reads a seeded object from the source bucket into a WAEvent stream, a CQ projects just the two parsed data columns (the reader also attaches GCS metadata — offset/path/object/VALID_RECORD —… |
| [kafka-cdc-diff](../scripts/live/regression/services/kafka/kafka-cdc-diff/README.md) | `scripts/live/regression/services/kafka/kafka-cdc-diff/README.md` | kafka-diff with the messages produced after the app is RUNNING (seed … when: post_start), so KafkaReader has to consume new messages from a topic that was empty at start, as a CDC reader captures changes made after start. |
| [kafka-diff](../scripts/live/regression/services/kafka/kafka-diff/README.md) | `scripts/live/regression/services/kafka/kafka-diff/README.md` | A round-trip through Kafka + Schema Registry with Avro: KafkaReader (AvroParser) reads the seeded Avro messages from the source topic into an AvroEvent stream; a CQ projects the two record fields (AvroEvent.data is a map, accessed as data.get('c0')) into a typed stream so the… |
| [mssql-cdc-diff](../scripts/live/regression/services/mssql/mssql-cdc-diff/README.md) | `scripts/live/regression/services/mssql/mssql-cdc-diff/README.md` | The full SQL Server CDC path: MSSqlReader (native CDC, via the SQL Server Agent capture job) → DatabaseWriter, source and target tables in their own qasource/qatarget schemas of the shared qauser database (like postgres-cdc-diff/oracle-cdc-diff). qasource.SRC is CDC-enabled… |
| [mssql-diff](../scripts/live/regression/services/mssql/mssql-diff/README.md) | `scripts/live/regression/services/mssql/mssql-diff/README.md` | The SQL Server analogue of postgres-diff/oracle-diff: DatabaseReader → DatabaseWriter within the shared qauser database, a non-CDC initial load, source and target tables in their own qasource/qatarget schemas (qasource.SRC → qatarget.TGT). |
| [mysql-batch-commit-policy](../scripts/live/regression/services/mysql/mysql-batch-commit-policy/README.md) | `scripts/live/regression/services/mysql/mysql-batch-commit-policy/README.md` | DatabaseReader → DatabaseWriter on MySQL with BatchPolicy and CommitPolicy set, over twelve seeded rows inserted in four statements. |
| [MySQL CDC Diff Test](../scripts/live/regression/services/mysql/mysql-cdc-diff/README.md) | `scripts/live/regression/services/mysql/mysql-cdc-diff/README.md` | Standard multi-database regression test: MySQL CDC via binlog (MySQLReader). |
| [MySQL Diff Test](../scripts/live/regression/services/mysql/mysql-diff/README.md) | `scripts/live/regression/services/mysql/mysql-diff/README.md` | Standard multi-database regression test: MySQL initial load via DatabaseReader/Writer. |
| [mysql-multi-table-cdc](../scripts/live/regression/services/mysql/mysql-multi-table-cdc/README.md) | `scripts/live/regression/services/mysql/mysql-multi-table-cdc/README.md` | One DatabaseReader reads three related MySQL tables (customers, their orders, the orders' items) and one DatabaseWriter writes all three. |
| [mysql-parallel-cdc](../scripts/live/regression/services/mysql/mysql-parallel-cdc/README.md) | `scripts/live/regression/services/mysql/mysql-parallel-cdc/README.md` | Verifies that MySQL tests isolate from one another under concurrent workers. |
| [oracle-cdc-diff](../scripts/live/regression/services/oracle/oracle-cdc-diff/README.md) | `scripts/live/regression/services/oracle/oracle-cdc-diff/README.md` | The full Oracle CDC path: OracleReader (LogMiner) → DatabaseWriter, source QASOURCE.SRC → target QATARGET.TGT. |
| [oracle-diff](../scripts/live/regression/services/oracle/oracle-diff/README.md) | `scripts/live/regression/services/oracle/oracle-diff/README.md` | The Oracle analogue of postgres-diff: DatabaseReader → DatabaseWriter across the source (QASOURCE.SRC, read as qasource) and target (QATARGET.TGT, written as qatarget) schemas, a non-CDC initial load. |
| [postgres-cdc-diff](../scripts/live/regression/services/postgres/postgres-cdc-diff/README.md) | `scripts/live/regression/services/postgres/postgres-cdc-diff/README.md` | Exercises PostgreSQLReader (CDC / logical decoding) → DatabaseWriter within one Postgres database. |
| [postgres-diff](../scripts/live/regression/services/postgres/postgres-diff/README.md) | `scripts/live/regression/services/postgres/postgres-diff/README.md` | Exercises the diff tier with an initial-load lifecycle: the framework creates src+tgt in separately owned source and target schemas, seeds src before deployment with the three fixed text rows in seed.sql, then deploys DatabaseReader(src) → DatabaseWriter(tgt). |
| [spanner-cdc-diff](../scripts/live/regression/services/spanner/spanner-cdc-diff/README.md) | `scripts/live/regression/services/spanner/spanner-cdc-diff/README.md` | spanner-googlesql-diff with the rows inserted after the app is RUNNING (seed … when: post_start). |
| [spanner-googlesql-diff](../scripts/live/regression/services/spanner/spanner-googlesql-diff/README.md) | `scripts/live/regression/services/spanner/spanner-googlesql-diff/README.md` | Cloud Spanner emulator, GoogleSQL dialect, same-backend source → target: SpannerBatchReader(src) → SpannerWriter(tgt). |
| [spanner-pg-diff](../scripts/live/regression/services/spanner/spanner-pg-diff/README.md) | `scripts/live/regression/services/spanner/spanner-pg-diff/README.md` | Cloud Spanner emulator, PostgreSQL dialect, same-backend source → target: SpannerBatchReader(src) → SpannerPGDialectWriter(tgt). |
| [vertica-cdc-diff](../scripts/live/regression/services/vertica/vertica-cdc-diff/README.md) | `scripts/live/regression/services/vertica/vertica-cdc-diff/README.md` | Rows inserted into Vertica after the app is RUNNING (seed … when: post_start). |
| [vertica-diff](../scripts/live/regression/services/vertica/vertica-diff/README.md) | `scripts/live/regression/services/vertica/vertica-diff/README.md` | The Vertica analogue of mssql-diff/mysql-diff: DatabaseReader → DatabaseWriter within the shared sltdb database, a non-CDC initial load, source and target tables in their own qasource/qatarget schemas (qasource.src → qatarget.tgt). |
