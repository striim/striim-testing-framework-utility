# Acme Retail: prompts for an AI coding agent

These are the [prompt templates](../../docs/USING-AI.md#prompt-templates), filled in for
Acme Retail. They describe the five steps used to build this example: setup, three pipelines,
then offline verification and documentation. Paste them in order into any AI coding agent.
Read framework paths from its separate checkout and application paths from your test repository.
The pipeline prompts request the TQL as well as the test because these apps were not written yet.

Prompts 2 to 4 built the apps and tests that are already in the example, so in a copy of it
they rebuild tests you have. Prompt 6 adds a new test to the orders app; use it to watch an
assistant write a test of its own.

## 1. Create the consumer repository

Filled in from [template 1](../../docs/USING-AI.md#1-set-up-my-test-repository).

```text
I want a test repository for Acme Retail at acme-retail/.
Read the framework's AGENTS.md, docs/START-HERE.md, docs/SET-UP-YOUR-OWN-REPO.md
and docs/USING-AI.md. Copy templates/consumer-repo and follow the setup guide.
Keep its manifest, sync script, framework.pin and ignore rules; install the pinned
framework in a separate checkout and leave it read-only. Use fictional test data
and documented tokens. Keep credentials and machine-specific paths out of git.
Do not run infrastructure probes or live tests yet.
```

## 2. Add the orders application and test

Filled in from [template 2](../../docs/USING-AI.md#2-test-my-striim-application).

```text
I want a Striim application that reads PostgreSQLReader CDC (orders) and writes MySQL
fulfillment orders_copy using DatabaseWriter.
Put its TQL at apps/orders/app.tql. Create the TQL there first.
Write a test that proves it moves the data correctly.
Read the framework's AGENTS.md, docs/WRITING-TESTS.md, docs/TEST-YAML.md,
docs/SERVICES.md and docs/WHAT-IS-SUPPORTED.md. Start from
samples/live/04-lifecycle-check and samples/live/06-mysql-cdc.
Use CDC and put the test in tests/live/orders-cdc.
Use PG_SLOT and a separate slot setup file.
Keep SQL fixtures under apps/orders/fixtures and use example: apps/orders in
tests/live/orders-cdc/test.yaml; keep expected/rows.csv with the test.
My input and expected behavior are:
Insert orders (1001,2001,25.00,placed), (1002,2002,7.50,placed),
(1003,2003,120.25,placed), then update 1003 to 118.00/shipped and delete 1002.
Changes run post_start with after: 10s. Use depth: gate, requires: [postgres, mysql],
smoke, rows: 2 with a hand-written golden, and a cross-database diff. Do not use
exact or lifecycle against MySQL, since the documented support is PostgreSQL only.
Use documented tokens and supported assertions. Derive the golden from the input
and explain each expected row. Do not run infrastructure probes or live tests yet.
```

## 3. Add the customer initial load and CDC

Filled in from [template 2](../../docs/USING-AI.md#2-test-my-striim-application).

```text
I want a Striim application that reads DatabaseReader initial load and
PostgreSQLReader CDC (customers) and writes PostgreSQL customers_copy using
DatabaseWriter.
Put its TQL at apps/customers/app.tql. Create the TQL there first.
Write a test that proves it moves the data correctly.
Read the framework's AGENTS.md, docs/WRITING-TESTS.md, docs/TEST-YAML.md,
docs/SERVICES.md and docs/WHAT-IS-SUPPORTED.md. Start from
samples/live/01-plain-replication and samples/live/04-lifecycle-check.
Use initial load + CDC and put the test in tests/live/customers-load-cdc.
Use a controlled, quiescent baseline: create both tables, commit baseline rows,
then create PG_SLOT in a separate setup file, so CDC does not replay the baseline.
List these ordered setup files under ddl. Hold post_start changes until
baseline-landed readiness; use the documented initial-load lifecycle with row-count
completion, stability and typed exact comparison. Document this startup constraint
in my README; do not invent a seamless concurrent-snapshot handoff.
My input and expected behavior are:
Seed customer 2001 with shopper-2001@example.test/standard/100.00 and 2002 with
NULL/standard/50.25. After baseline readiness, update 2001 to gold/250.50, delete
2002, insert 2003 with NULL/standard/75.00 and 2004 with
shopper-2004@example.test/silver/150.25, in one transaction.
Use example: apps/customers, depth: regression, requires: [postgres], smoke,
an exact hand-written three-row golden with integer and decimal:2 types, and diff.
Explain why the final count differs from the baseline.
Use documented tokens and supported assertions. Derive the golden from the input
and explain each expected row. Do not run infrastructure probes or live tests yet.
```

## 4. Add the event pipeline

Filled in from [template 2](../../docs/USING-AI.md#2-test-my-striim-application).

```text
I want a Striim application that reads MySQLReader CDC (append-only retail_events)
and writes Kafka.
Put its TQL at apps/events/app.tql. Create the TQL there first.
Write a test that proves it moves the data correctly.
Read the framework's AGENTS.md, docs/WRITING-TESTS.md, docs/TEST-YAML.md,
docs/SERVICES.md and docs/WHAT-IS-SUPPORTED.md. Start from samples/live/06-mysql-cdc
and scripts/live/regression/services/kafka/kafka-cdc-diff.
Use CDC and put the test in tests/live/events-kafka.
Use a CQ projecting event_id, event_type and order_id as strings,
and KafkaWriter 2.1.0 with AvroFormatter Default and the registry token.
Use KAFKA_TGT_TOPIC, never a literal topic.
My input and expected behavior are:
Insert events (3001,order_placed,1001),
(3002,payment_captured,1001), (3003,order_shipped,1001), then the terminal fixture
(3004,batch_complete,1001), post_start with after: 10s.
Use example: apps/events, depth: gate, requires: [mysql, kafka], smoke and a Kafka
data assertion with db: kafka, rows: 4, the three projected keys, and a hand-written
expected/messages.csv. Explain the terminal row and the append-only scope.
Do not claim stream ordering or add unsupported lifecycle/exact keys.
Use documented tokens and supported assertions. Derive the golden from the input
and explain each expected row. Do not run infrastructure probes or live tests yet.
```

## 5. Verify and document the finished example

Filled in from [template 6](../../docs/USING-AI.md#6-verify-offline-and-document-my-tests).

```text
I have finished writing apps/orders, apps/customers and apps/events with their
tests/live cases in my test repository.
Read the framework's docs/SET-UP-YOUR-OWN-REPO.md, docs/USING-AI.md,
docs/WRITING-TESTS.md and docs/TEST-YAML.md. Keep its checkout read-only.
Write my README with setup, run commands, behavior, golden derivations
and limitations.
Add scripts/validate-example.py to load manifests and assertion specs,
check files and tokens, and verify goldens against fixtures offline.
Explain the limits of those checks. Run the validator with my installed framework,
then run these commands:
`striim-test list --targets gold-targets.yaml`
`striim-test run --targets gold-targets.yaml --dry-run`
Report results and give me doctor and live-run commands for a prepared machine.
Do not probe infrastructure or run live tests; offline validation does not prove
deployment or adapter behavior.
Use SQLite for fixture/golden consistency only; it does not test adapters.
```

## 6. Add a new test to the orders application

Filled in from [template 2](../../docs/USING-AI.md#2-test-my-striim-application), for an app
that already exists: the assistant writes only the test and its fixtures.

```text
The orders application at apps/orders/app.tql already exists, and tests/live/orders-cdc
tests it. Write a NEW test for it in tests/live/orders-cancel. Do not change
apps/orders/app.tql or tests/live/orders-cdc.
Read the framework's AGENTS.md, docs/WRITING-TESTS.md, docs/TEST-YAML.md and
docs/SERVICES.md. Start from tests/live/orders-cdc.
Use CDC and example: apps/orders. Reuse its DDL and slot files; put the new change
SQL in apps/orders/fixtures/cancel_changes.sql and keep expected/rows.csv with the test.
My input and expected behavior are:
Insert orders (2001,3001,40.00,placed) and (2002,3002,15.00,placed), then update
2001 to cancelled and 2002 to shipped.
Changes run post_start with after: 10s. Use depth: gate, requires: [postgres, mysql],
smoke, rows: 2 with a hand-written golden, and a cross-database diff.
Use documented tokens and supported assertions. Derive the golden from the input
and explain each expected row. Do not run infrastructure probes or live tests yet.
```

Check its golden against the input yourself (2001 cancelled, 2002 shipped), then run only that
test, from your repository:

```bash
.venv/bin/striim-test list --targets gold-targets.yaml        # the new case is listed
.venv/bin/striim-test run tests/live/orders-cancel --targets gold-targets.yaml --dry-run
.venv/bin/striim-test run tests/live/orders-cancel --targets gold-targets.yaml
```
