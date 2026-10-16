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

| Sample | Shows |
|---|---|
| `01-plain-replication` | an initial load, compared exactly with a hand-written golden |
| `02-transform` | a CQ that changes each event before it is written |
| `03-file-output` | an app that writes a file, and checking the file's contents (Docker mode only) |
| `04-lifecycle-check` | a CDC app whose correct result is an empty target, and how `lifecycle:` proves it ran |
| `05-mysql-initial-load` | an initial load on MySQL |
| `06-mysql-cdc` | binlog CDC on MySQL: inserts, an update and a delete |
| `07-spanner-exact` | typed and counted Spanner rows; requires the Spanner service |
| `08-strict-json` | preserve JSON token types and reject undeclared fields; Docker file access |
| `09-raw-file` | byte hashes, lengths and an exact file inventory; Docker file access |
| `10-bounded-nothing` | no output in a bounded window after a file sentinel |
| `18-kafka-nothing` | no output in a bounded window after a Kafka sentinel; fresh owned topics |
