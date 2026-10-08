# Acme Retail Striim tests

**This example is complete.** It holds three apps and three tests, each with its expected result,
and all three passed on a live Striim. You do not create anything: copy it out beside the framework
and run it, as below, exactly as you will run a repository of your own tests.

A fictional retailer's consumer repository, copied from `templates/consumer-repo` following
[START-HERE](../../docs/START-HERE.md), [SET-UP-YOUR-OWN-REPO](../../docs/SET-UP-YOUR-OWN-REPO.md)
and [USING-AI](../../docs/USING-AI.md). Apps live under `apps/`; tests reference them through
`example:` rather than keeping a second copy. Each app directory also holds disposable SQL
fixtures, because `tql:`, `ddl:` and `seed:` resolve from that directory. Goldens stay with tests.
[PROMPTS.md](PROMPTS.md) fills in the generic [prompt templates](../../docs/USING-AI.md#prompt-templates)
for the five steps used to create this example.

## Run it as your own repository

Your tests live in a repository of their own, beside a framework checkout that the sync script keeps
at the version in `framework.pin`. Start in a new, empty folder:

```
striim-work/
  striim-testing-framework-utility/   the framework, kept at the version in framework.pin
  acme-retail-tests/                  your repository: apps/, tests/, framework.pin, .env, .venv/
```

```bash
mkdir striim-work && cd striim-work
git clone https://github.com/striim/striim-testing-framework-utility.git striim-testing-framework-utility
cp -r striim-testing-framework-utility/examples/acme-retail acme-retail-tests
cd acme-retail-tests
git init
python3 scripts/sync-framework.py       # makes .venv with Python 3.12+, installs the pin, makes .env
.venv/bin/python scripts/validate-example.py
.venv/bin/striim-test list --targets gold-targets.yaml
.venv/bin/striim-test run --targets gold-targets.yaml --dry-run
```

Any `python3` runs the sync script; it makes `.venv` with Python 3.12 or later and installs the
framework into it, so every command here starts with `.venv/bin/` and runs from your repository.
The sync script keeps the framework checkout beside your repository read-only at the pin, so do not
run it beside a checkout you develop in. `.env` and `.env.example` start with a dot, so `ls` and
Finder hide them; `ls -a` shows them.

The validator loads every manifest, checks assertion specs, files and tokens, and checks golden
consistency by executing the fixture SQL in SQLite; it ends with `PASS: 3 manifests and all offline
contract checks`. `list` and `--dry-run` print the three cases a run selects without provisioning
or deploying. None of this needs Striim or Docker, and none of it tests an adapter.

## Run it live

Set where Striim comes from in your repository's `.env` (see [SERVICES](../../docs/SERVICES.md),
[RUN-YOUR-FIRST-TEST](../../docs/RUN-YOUR-FIRST-TEST.md) and [SETTINGS](../../docs/SETTINGS.md)):

- your own Striim server: `STRIIM_URL`, `STRIIM_USER` and `STRIIM_PASS`;
- or Docker mode: leave `STRIIM_URL` unset and export a license in your shell, `STRIIM_HOME` (a
  licensed Striim install) or `COMPANY_NAME`, `CLUSTER_NAME`, `PRODUCT_KEY` and `LICENCE_KEY`.

Use only disposable databases. Services can be provisioned by the framework or connected through
`SLT_PG_HOST`, `SLT_MYSQL_HOST` and `SLT_KAFKA_HOST` and their documented settings. Keep
`SLT_INFRA_OWNERSHIP=shared` and `SLT_KEEP_SERVICES=1`; shell settings override `.env`.
If your Striim is an existing Docker container outside the configured stack prefix, set
`SLT_STRIIM_VIEW_HOST` to its service-facing host (locally, `host.docker.internal`). A stack
prefix scopes container names and cleanup; it does not select the Striim REST endpoint. Then, from
your repository (paths are relative to it):

```bash
.venv/bin/striim-test doctor --targets gold-targets.yaml --case tests/live
.venv/bin/striim-test run --targets gold-targets.yaml
.venv/bin/striim-test run tests/live/orders-cdc --targets gold-targets.yaml   # one app
```

`--targets gold-targets.yaml` makes your repository the project: its `.env` is the one read, and
`doctor` names that file. A live run creates tables, slots, topics and apps and attempts cleanup
afterward. Inspect the printed run directory under `.state/runs/`, including `live/stdout.log`,
`live/junit.xml` and each case's `evidence.json`. Exit 0 means passed; exit 3 means nothing
executed because prerequisites were missing, which is a skip, not a pass. See
[WRITING-TESTS](../../docs/WRITING-TESTS.md).

## Run it in place (framework contributors)

Framework contributors can run the example inside the framework checkout instead, with the
contributor environment. `tools/make-test-venv.sh` creates `.venv-test` (not `.venv`) with the
pinned test dependencies; it needs Python 3.12, which it finds as `python3.12` on your `PATH` or
through `uv`, or set `PYTHON` to one. From the framework root, with the settings in
`examples/acme-retail/.env`:

```bash
bash tools/make-test-venv.sh
cp examples/acme-retail/.env.example examples/acme-retail/.env
.venv-test/bin/python examples/acme-retail/scripts/validate-example.py
.venv-test/bin/striim-test list --targets examples/acme-retail/gold-targets.yaml
.venv-test/bin/striim-test run --targets examples/acme-retail/gold-targets.yaml --dry-run
.venv-test/bin/striim-test doctor --targets examples/acme-retail/gold-targets.yaml --case examples/acme-retail/tests/live
.venv-test/bin/striim-test run --targets examples/acme-retail/gold-targets.yaml
```

The manifest sets `tests/live` as its only suite and `.state` as its run-state directory, relative
to this example; the framework's default suites do not include this directory.

## Apps and what the cases prove

| App | Case / depth | Behavior and assertions |
|---|---|---|
| `apps/orders/app.tql` | `orders-cdc` / `gate` | PostgreSQL CDC to MySQL fulfillment orders: insert three orders, update the shipped order, delete the cancelled order. Smoke, a two-row golden and a cross-database source/target diff. Requires PostgreSQL and MySQL. |
| `apps/customers/app.tql` | `customers-load-cdc` / `regression` | PostgreSQL customer profiles to PostgreSQL: load two profiles, then update one, delete one and insert two. Baseline readiness, a three-row completion witness, stability, typed exact golden and source/target diff. Requires PostgreSQL. |
| `apps/events/app.tql` | `events-kafka` / `gate` | MySQL CDC from an append-only retail event log to Kafka. A CQ creates flat string-valued Avro records; smoke plus four records matching the golden, including a terminal batch record. Requires MySQL and Kafka with Schema Registry. |

All connections, schemas, slots and topics use the documented service tokens; tables start with
`${TID}`. Component names fit the documented 21-character limit. No real data or credentials
belong in these files. The customer names and email addresses are fictional fixtures.

The customers app uses two documented adapters and two writers to one target. Its startup
requires a quiescent source: commit the baseline, create the replication slot, start the app,
wait for the baseline to land, then allow changes. In the fixture, the baseline insert file is
an ordered `ddl:` setup entry before `slot.sql`; changes are a `post_start` seed. The final count
is deliberately three rather than the baseline's two, and exact comparison verifies every value.
This is a controlled initial load followed by CDC, not a handoff under concurrent snapshot writes.
The baseline reader can finish while the CDC reader keeps the application running; the
readiness and completion checks exercise both phases in this single application.

Exact comparison and lifecycle witnesses support PostgreSQL routes only. The MySQL and Kafka
cases therefore poll documented assertions until `timeout: 180`, with a documented ten-second
`post_start` delay for reader startup. The event log is append-only; this case does not claim
update/delete envelopes or global Kafka ordering. The terminal record prevents an incomplete
prefix from matching. The string projection keeps the comparison portable and easy to inspect;
a production feed can retain numeric Avro fields and key records by order ID for per-order
partitioning. This case checks delivery and values, and makes no partition-ordering guarantee.
Kafka topics are framework-derived and cleaned up without custom names.

## How the goldens were derived

- Orders: 1001 stays at 25.00 and `placed`; 1002 is deleted; 1003 changes to 118.00 and `shipped`.
- Customers: 2001 becomes `gold` with credit 250.50; 2002 is deleted; new 2003 has a NULL email
  and credit 75.00; new 2004 is `silver` with credit 150.25. `<null>` means SQL NULL.
- Events: the CQ converts the three source fields to strings for each of the four inserted rows,
  preserving the values in `expected/messages.csv`. The fourth row is `batch_complete`.

No expected result is recorded from a Striim run.

## The framework pin

The sync script and manifest are the consumer template's originals; `framework.pin` names a
framework commit on the default branch that has the guidance used for these cases (a commit cannot
contain its own SHA, so it is an earlier one). At release, `tools/release.py` rewrites both this pin and the consumer template pin to the
release tag; bootstrap repairs carried-over pins in a fresh repository. See
[RELEASING](../../docs/internals/RELEASING.md). [SET-UP-YOUR-OWN-REPO](../../docs/SET-UP-YOUR-OWN-REPO.md)
explains the pin, the sync script and CI. Never run the sync script inside `examples/`: it would clone
another framework there. After copying, read the framework links above as paths under the framework
checkout beside your repository.

## License

This folder is part of the Striim testing framework and is covered by the framework's `LICENSE`:
Elastic License 2.0 (ELv2). Code you copy from it stays under ELv2: keep this notice and give
anyone you share the copy with a copy of that `LICENSE`.
