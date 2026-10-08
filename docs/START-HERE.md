# Start here

[Visual reference](reference.html): how the framework works, the Acme Retail walkthrough, and the documentation library (open in a browser).

This framework tests Striim apps against a real Striim. You describe a test as a folder; the
framework sets up the data, deploys your app, waits until it has finished, compares what it wrote
with what you expected, and cleans up. This page explains the ideas once, so the other pages can
be short.

## What a test is

A test is a folder with a `test.yaml` and the files it names:

```
orders-cdc/
  test.yaml            what to set up, what to run, what to check
  app.tql              your app, with tokens instead of hosts and passwords
  ddl_source.sql       the source table
  ddl_target.sql       the target table
  changes.sql          the data
  expected/rows.csv    what the target must hold at the end, written by you
```

A run goes through the same steps every time:

```
create tables → seed data → deploy and start the app → wait until it is ready
  → write the changes → wait until it has finished and the target is still
  → compare → clean up what this run created
```

## Two tiers

- **Live tests** deploy a whole TQL app to a real Striim and check its effect on real databases,
  files or topics. Most of these docs are about them.
- **Integration tests** drive an Open Processor or UDF's code directly, event in and event out,
  with no Striim server. They take seconds. [INTEGRATION-TESTS.md](INTEGRATION-TESTS.md).

## Where Striim comes from

| | Your own Striim server | Docker mode |
|---|---|---|
| How | set `STRIIM_URL`, `STRIIM_USER`, `STRIIM_PASS` | leave `STRIIM_URL` unset |
| First run | immediate | downloads 6.3 GB, builds a 22 GB image, starts a two-node cluster and an agent |
| Needs | a user that can create and drop apps | Docker with 35 GB free, and your own Striim license |
| Cannot | read files on the Striim nodes, kill a node, read JMX | — |

Both run the same tests. [WHAT-IS-SUPPORTED.md](WHAT-IS-SUPPORTED.md) has the details.

## Services

A test lists the databases and systems it needs under `requires:` (`postgres`, `oracle`, `mssql`,
`mysql`, `vertica`, `kafka`, `spanner`, `gcs`). The framework starts each one in Docker with
test-only accounts, or reuses it if it is running. Set a service's host and it uses your instance
instead. Teradata and ServiceNow are always your own instance. [SERVICES.md](SERVICES.md).

## Shared or exclusive

Every run says whether it owns the infrastructure it uses (`SLT_INFRA_OWNERSHIP`):

- **`shared`** (the default in `.env.example`): reuse whatever is running, start what is missing,
  never tear anything down. Safe on a machine or Striim server other people use.
- **`exclusive`**: a fresh stack of its own, refused if a Striim already answers on the endpoint,
  torn down at the end. Only where nothing else uses that endpoint.

## Why a passing test means something

- **`exact:`** compares every row by typed value: a missing, extra, duplicated or changed row fails,
  and `118.0` equals `118.00` when the column is declared `decimal:2`.
- **`lifecycle:`** makes the run wait for proof that the app has finished (the target count matches
  the source, or a marker row made it through) before it compares. Without it, an empty or
  half-written target can match too early.
- **The expected result is written by you**, from the input. The framework never writes one.

## Read in this order

Browse the [documentation catalog](CATALOG.md) for every guide, grouped by audience.

| You want to | Read |
|---|---|
| see it work | [RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md) |
| test your own app | [WRITING-TESTS.md](WRITING-TESTS.md), then [TEST-YAML.md](TEST-YAML.md) as a reference |
| write, tune or review the TQL itself | [tql/README.md](tql/README.md) |
| keep tests in your own repo | [SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md) |
| use your own databases, or add one | [SERVICES.md](SERVICES.md), [YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md) |
| test an Open Processor or UDF | [TESTING-YOUR-JAVA.md](TESTING-YOUR-JAVA.md), [INTEGRATION-TESTS.md](INTEGRATION-TESTS.md) |
| work with an AI coding assistant | [USING-AI.md](USING-AI.md#prompt-templates): generic prompt templates and review rules |
| fix something | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

## Words used in these docs

- **Case**: one test, a folder with a `test.yaml`. Its id is its path.
- **Case root**: a folder the framework collects cases from (`SLT_LIVE_CASES`, or `suites:` in a
  `gold-targets.yaml`). `striim-test run` only runs cases under one.
- **Golden**: the expected result, a CSV (or JSON) file under `expected/`.
- **Token**: a `${NAME}` the framework fills in for each run: hosts, users, names.
- **`${TID}`**: a per-test prefix for every object name, so two tests, or two runs, never collide.
  Empty in a serial run.
- **Route**: which database and account a `ddl:`, `seed:` or assertion uses, such as
  `postgres-source` (the `qasource` schema) or `postgres-target` (`qatarget`).
- **Run directory**: where a run writes its logs, JUnit XML and evidence, printed as `run-dir:`.
- **Evidence**: `evidence.json`, the full record of one case's run.
- **Ownership ledger**: the run's record of what it created, so it removes exactly that.
