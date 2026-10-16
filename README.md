# striim-testing-framework-utility

Test your Striim apps against a real Striim. You write a test as a folder: the app's TQL, the DDL
and seed data, the rows you expect, and a `test.yaml` that ties them together. `striim-test` creates
the tables, deploys and starts the app, waits until it has finished, compares the target with your
expected rows exactly, and cleans up. It runs against your own Striim server, or against a Striim
cluster it builds and starts in Docker.

It is for teams who build Striim apps, Open Processors or UDFs and want checks they can repeat on
every change and run in CI.

## Run your first test

You need Python 3.12 or later, git and Docker, plus either a Striim server of your own or your own
Striim license for the Docker one. `python3 -V` shows which Python `python3` is; if it is older than 3.12
(as it often is on a Mac), use `python3.12` or newer in its place below. `.env.example` starts with a
dot, so `ls` hides it; `ls -a` shows it.

```
git clone https://github.com/striim/striim-testing-framework-utility.git striim-testing-framework-utility
cd striim-testing-framework-utility
python3 -m venv .venv && .venv/bin/pip install -e .
. .venv/bin/activate
cp .env.example .env
```

In `.env`, set `STRIIM_URL`, `STRIIM_USER` and `STRIIM_PASS` for your own Striim server, or leave
them unset for Docker mode and export a license in your shell
([RUN-YOUR-FIRST-TEST.md](docs/RUN-YOUR-FIRST-TEST.md), "Docker mode"). Then:

```
striim-test doctor --case samples/live/01-plain-replication
striim-test run samples/live/01-plain-replication
```

Exit code 0 means it passed. [docs/RUN-YOUR-FIRST-TEST.md](docs/RUN-YOUR-FIRST-TEST.md) has every
step in detail, with the output to expect and what to do when something fails.

## The samples

| Sample | Shows | Prerequisites |
|---|---|---|
| `01-plain-replication` | an initial load, compared exactly with a hand-written golden | Postgres |
| `02-transform` | a CQ that changes each event before it is written | Postgres |
| `03-file-output` | an app that writes a file, and checking the file's contents (Docker mode only) | Postgres; Docker file access |
| `04-lifecycle-check` | a CDC app whose correct result is an empty target, and how `lifecycle:` proves it ran | Postgres CDC |
| `05-mysql-initial-load` | an initial load on MySQL | MySQL |
| `06-mysql-cdc` | binlog CDC on MySQL: inserts, an update and a delete | MySQL binlog CDC |
| `07-spanner-exact` | typed and counted Spanner rows; requires the Spanner service | Spanner service |
| `08-strict-json` | preserve JSON token types and reject undeclared fields; Docker file access | Postgres; Docker file access |
| `09-raw-file` | byte hashes, lengths and an exact file inventory; Docker file access | Postgres; Docker file access |
| `10-bounded-nothing` | no output in a bounded window after a file sentinel | Postgres CDC; Docker file access |
| `11-transaction-seeds` | overlapping sessions, savepoints and a transaction across recovery | Postgres CDC; STOP/restore |
| `12-snapshot-observer` | consistent snapshots of related target tables | Postgres CDC; transaction preservation; STOP/restore |
| `13-append-only-audit` | immutable delivery rows with counted multiset comparison | Postgres |
| `14-progress-recovery` | interrupt CDC inside a declared target progress window | Postgres CDC; STOP/restore |
| `15-fault-evidence` | fault-time progress evidence and missed-window failure | Postgres CDC; STOP/restore |
| `16-recovery-sentinel` | fresh route proof after recovery and before assertions | Postgres CDC; STOP/restore |
| `17-one-node-failover` | kill one flow owner and prove survivor placement and complete data | Postgres CDC; Docker cluster, two app nodes and agent; published survivor API; **kills one shared app node** |
| `18-kafka-nothing` | no output in a bounded window after a Kafka sentinel; fresh owned topics | Postgres CDC; Kafka |

They are in `samples/live/`, each with a README. `samples/code/` holds two more that build and load
your own Java: an Open Processor and a UDF.

## Where to go next

[Visual reference](docs/reference.html): how the framework works, the Acme Retail walkthrough, and the documentation library (open in a browser).

[Documentation catalog](docs/CATALOG.md): every guide, grouped by audience.

**Start**
- [docs/START-HERE.md](docs/START-HERE.md): the ideas, in one page, and which doc to read for what.
- [docs/RUN-YOUR-FIRST-TEST.md](docs/RUN-YOUR-FIRST-TEST.md): from nothing to a passing sample.

**Write tests**
- [docs/WRITING-TESTS.md](docs/WRITING-TESTS.md): one test for your own app, step by step.
- [docs/TEST-YAML.md](docs/TEST-YAML.md): every `test.yaml` key.
- [docs/TESTING-YOUR-JAVA.md](docs/TESTING-YOUR-JAVA.md): tests that build and load an Open Processor or UDF.
- [docs/INTEGRATION-TESTS.md](docs/INTEGRATION-TESTS.md): an Open Processor or UDF tested without a Striim app.
- [docs/USING-AI.md](docs/USING-AI.md): working with any AI coding agent, with fill-in-the-blank prompt templates.

**Your setup**
- [docs/SET-UP-YOUR-OWN-REPO.md](docs/SET-UP-YOUR-OWN-REPO.md): your tests in your own repo, on a pinned framework version.
- [docs/SERVICES.md](docs/SERVICES.md): every database and emulator, how to reach it, and how to use your own.
- [docs/YOUR-OWN-SERVICES.md](docs/YOUR-OWN-SERVICES.md): adding a service, or your own image of a shipped one.
- [docs/SERVICENOW.md](docs/SERVICENOW.md): testing against your own ServiceNow instance.
- [docs/RUN-A-DOWNLOADED-EXAMPLE.md](docs/RUN-A-DOWNLOADED-EXAMPLE.md): running a component's published example.

**Reference**
- [docs/WHAT-IS-SUPPORTED.md](docs/WHAT-IS-SUPPORTED.md): run modes, platforms, releases, services and features, and what has been tested.
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): what you see, why, and the fix.

**Working on the framework itself:** [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/internals/](docs/internals/ENGINE.md).

## What is in this repo

- `samples/`: the samples above.
- `templates/consumer-repo/`: a starter for your own test repo.
- `scripts/cli`: the `striim-test` command (`run`, `list`, `doctor`, `fetch`).
- `scripts/live`: the live test engine (`livetest`), its services and its own regression tests.
- `scripts/integration`: the integration test engine (`inttest`).
- `tools/python/striim_api.py`: the Striim REST client both engines use.
- `docs/`: these docs.

The optional Teradata driver is Teradata's own, under Teradata's licence: see
[docs/SERVICES.md](docs/SERVICES.md), "Teradata". The drivers that Docker mode downloads are
likewise under their vendors' licenses.

## License

The [LICENSE](LICENSE) contains two parts: Striim software is subject to Striim's End User
License Agreement or Striim Cloud's Terms of Service, depending on deployment; code provided
in this repository falls under Elastic License 2.0 (ELv2).
