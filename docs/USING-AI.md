# Using an AI coding assistant with this repo

An AI coding assistant can do most of the typing in this repo: copy the right sample, rewrite your
TQL with the framework's tokens, write the DDL and seed files, fill in `test.yaml`, run `doctor`,
read a failure. It cannot know what your app is supposed to produce. That part is yours, and this
page is mostly about keeping it yours.

This page covers:

- what to give the assistant;
- the guard rails to set;
- the loop that works;
- twelve prompt templates you can fill in for your own pipelines;
- the mistakes assistants make here, and how to review what one wrote.

Before authorizing a live run, finish [RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md): check that `striim-test doctor`
passes and `samples/live/01-plain-replication` runs green on your machine. An assistant cannot fix a
setup that has never worked, and it will waste your time trying.

**When authoring on a machine without test infrastructure**, stop at offline checks and hand the
live command to a prepared test machine. `list` and `--dry-run` collect cases without provisioning;
they do not prove that an app deploys or its assertions pass. `doctor` also probes infrastructure,
so it is not an offline lint command. The [Acme Retail example](../examples/acme-retail/README.md)
includes a repeatable offline validator that loads manifests, checks their files and assertion
specs, and checks fixture/golden consistency. A live run is still required before claiming that
the apps pass.

## Set up

**`AGENTS.md` is already there.** The repo root has an `AGENTS.md` with the rules a test here
follows and the commands to check one. Most assistants read it on their own when they start in this
repo. If yours reads instructions from a different file, create that file with one line telling it
to read `AGENTS.md`. If yours reads neither, paste `AGENTS.md` into the conversation first.

**Open the assistant in the framework clone**, or in your own test repo with the clone next to it
(see [SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md)). It needs to read the samples and the docs,
and to run `striim-test`.

**Give it, for each task:**

| Give it | Why |
|---|---|
| Your app's TQL, or the path to it | It turns hosts, users and table names into tokens; it cannot guess your sources and targets |
| What the app should produce from a given input, in your words | It writes the expected rows from this. Without it, it will be tempted to copy them from a run |
| The doc for the task (named in each prompt below) | The docs are the contract; the assistant's training data predates them |
| The full error, or the path of the run directory | `evidence.json` says what failed far better than a summary |
| Whether you use Docker mode or your own Striim | Some features work only in Docker mode ([WHAT-IS-SUPPORTED.md](WHAT-IS-SUPPORTED.md)) |

**Do not give it** license keys, passwords for real systems, or production data. Tests use the
framework's test-only accounts and rows you make up. If a golden needs realistic data, describe
its shape and let the assistant invent values.

## Guard rails

Tell the assistant these up front, or check that your `AGENTS.md` says them:

1. **You write the expected result, or you check every row of it.** An expected file copied from
   what the app actually wrote proves only that the app did the same thing twice. Every golden is
   written from the input data and what the app is meant to do.
2. **Safe to run any time:** `striim-test doctor`, `striim-test list`, `striim-test run … --dry-run`.
   These deploy nothing and touch no database; `list` and `--dry-run` only write a run record.
3. **Ask before running:** `striim-test run` without `--dry-run`. It deploys apps to Striim and
   creates and drops tables. On your own machine with the shipped `.env` that is fine. On a shared
   Striim server, decide first.
4. **Never, without you saying so:** set `SLT_INFRA_OWNERSHIP=exclusive`; run
   `python -m livetest.cli stop all`; run `docker compose down` or `docker rm` on `slt-*`
   containers; point a `SLT_*_HOST` setting at a database that holds anything you care about. Each
   of these can take down infrastructure other people or other checkouts are using.
5. **A pass is an exit code, not a sentence.** The assistant shows you the command it ran and its
   exit code. 0 is a pass. 3 means something it needed was not available and the test was
   **skipped**, which is not a pass. 1 is a failed test; 2 is a mistake in settings or in a
   `test.yaml`.
6. **Do not weaken a test to make it green.** Removing `exact:`, removing `lifecycle:`, raising
   `timeout`, adding `xfail:` or `disabled:`, or deleting a row from the golden are all changes you
   approve, with a reason.

## The loop that works

1. **Describe the behaviour** you want to prove, in one sentence. That sentence becomes `purpose:`.
2. **Let the assistant pick and copy the closest sample**, then adapt it.
3. **On a machine with test infrastructure**, run `striim-test doctor --case <dir>` until
   every line is `ok`. On an authoring-only machine, use offline validation (template 6) instead.
4. **`striim-test run <dir> --dry-run`** to see that the case is selected.
5. **Review the golden yourself** (guard rail 1).
6. **`striim-test run <dir>`**, and read the exit code.
7. **On a failure, give the assistant the run directory**, and ask it to read `evidence.json`
   before it changes anything.

Small steps beat one big prompt. An assistant that writes ten files and then runs them for the first
time has ten places to be wrong at once.

## Prompt templates

Copy a template, replace the `<angle brackets>` with your situation, and paste it into any
AI coding agent. Paths to framework docs and samples are relative to the framework checkout;
paths to your apps, cases and project manifest are relative to your test repository. Keep the
framework checkout read-only. The [worked prompts](../examples/acme-retail/PROMPTS.md) fill in
these templates for the example's five steps.

### 1. Set up my test repository

```text
I want a test repository for <project> at <repository path>.
Read the framework's AGENTS.md, docs/START-HERE.md, docs/SET-UP-YOUR-OWN-REPO.md
and docs/USING-AI.md. Copy templates/consumer-repo and follow the setup guide.
Keep its manifest, sync script, framework.pin and ignore rules; install the pinned
framework in a separate checkout and leave it read-only. Use fictional test data
and documented tokens. Keep credentials and machine-specific paths out of git.
Do not run infrastructure probes or live tests yet.
```

**Check the result:** the repository keeps the template's pin, manifest and sync script;
the framework checkout stays separate and read-only, and secrets remain uncommitted.

### 2. Test my Striim application

```text
I have a Striim application that reads <source system and reader> (<tables>)
and writes <target>;
its TQL is at <path>. Write a test that proves it moves the data correctly.
Read the framework's AGENTS.md, docs/WRITING-TESTS.md, docs/TEST-YAML.md,
docs/SERVICES.md and docs/WHAT-IS-SUPPORTED.md. Start from <closest sample>.
Use <initial load, CDC, or initial load + CDC> and put the test in <case folder>.
My input and expected behavior are:
<fixture and expected-result details>
Use documented tokens and supported assertions. Derive the golden from the input
and explain each expected row. Do not run infrastructure probes or live tests yet.
```

Choose the nearest sample after reading the support matrix:

- **Initial load:** `samples/live/01-plain-replication` or `samples/live/05-mysql-initial-load`.
  Describe the baseline rows and when the load should be complete.
- **CDC:** `samples/live/04-lifecycle-check` or `samples/live/06-mysql-cdc`.
  Describe inserts, updates and deletes after startup (`when: post_start`). For PostgreSQL,
  use the sentinel lifecycle in sample 04 to prove catch-up. For Oracle or SQL Server,
  read the source's section in `docs/SERVICES.md` for connection tokens and CDC prerequisites.
- **Initial load + CDC:** use the initial-load and CDC samples together. Specify the quiet
  baseline, slot creation order and when changes may begin; do not assume a concurrent snapshot
  handoff. Use supported readiness and completion checks for both phases.
- **Kafka target:** `scripts/live/regression/services/kafka/kafka-cdc-diff`.
  Specify message fields, encoding, expected records and whether the source is append-only;
  do not assume global ordering or update/delete semantics.
- **File target:** `samples/live/03-file-output`. Specify expected file content and formatting;
  check the support matrix for access to files on the Striim nodes.

Database `exact:` and `lifecycle:` are supported on PostgreSQL routes only. For other routes,
choose the documented `data`, `diff` or `file` assertions. If the TQL is not written yet, say
“I want a Striim application” in the first line and add “Create the TQL there first” after
the TQL path. Describe the adapters and transformations you want before the fixture details.
For apps using your own Open Processor or UDF, also read `docs/TESTING-YOUR-JAVA.md` and
use template 7 for the module loading and observable effects.

**Check the result:**

- The golden follows from the input row by row; NULL cells use `<null>`, never a copied run output.
- Every object carries its documented prefix, and connections use service tokens.
- PostgreSQL `exact.columns` types every non-text column, and lifecycle checks prove completion.
- CDC changes run after startup; other routes use supported assertions without PostgreSQL-only keys.

### 3. Add or change a service

```text
My tests need <service and version or change>, used by <case folder>.
Read the framework's docs/YOUR-OWN-SERVICES.md, docs/SERVICES.md and
docs/WHAT-IS-SUPPORTED.md before adding or changing it in my test repository.
Keep the framework checkout read-only. Configure servicesRoots in gold-targets.yaml,
separate container names and ports, the stack-prefix pattern and a healthcheck.
Preserve shipped docker_defaults for a replacement; use variables without SLT_
for a new service. Explain which test routes and assertions are supported.
Run `docker compose -f <service compose path> config --quiet` and show the exit code.
Give me the doctor command to confirm the service or override on a prepared machine.
Do not start services or run live tests yet.
```

**Check the result:** the compose configuration passes; for a replacement, doctor on a
prepared machine shows the `[services] … overrides …` line described in YOUR-OWN-SERVICES.md.

### 4. Run my tests and explain a failure

```text
My tests are in <case folder>, with gold-targets.yaml in my repository.
I authorize a live run on my prepared test infrastructure using <Docker or native> mode.
Read the framework's docs/RUN-YOUR-FIRST-TEST.md, docs/TEST-YAML.md and
docs/TROUBLESHOOTING.md. Run `striim-test doctor --targets gold-targets.yaml --case <case folder>`;
if it passes, run `striim-test run <case folder> --targets gold-targets.yaml`.
Show both exit codes and the run directory; report a skip as a skip.
If it fails, read <run directory>/live/evidence/<case>/<run>/evidence.json
and <run directory>/live/stdout.log.
Explain the failed phase, evidence, missing/extra rows, and likely cause before editing.
Propose one fix; do not weaken assertions or copy output into the golden.
```

For an existing failure, replace the authorization and run instructions with
“I already ran the tests; the failing run directory is `<run directory>`.”
To inspect tables and the app after a failure, authorize a rerun with `--keep-resources`;
use the cleanup command named by `doctor` when you finish.

**Check the result:**

- The reported command and exit code distinguish a failure from a skip (exit 3).
- The diagnosis cites `run.failure`, lifecycle evidence or `data.comparisons`, as applicable.
- Missing and extra rows explain whether the app, the test or the environment is wrong.
- A proposed fix follows the evidence; the agent never copies actual output into the golden.

### 5. Review my tests against the docs

```text
I want to know whether my tests in <case folder> prove <intended behavior>.
Read the framework's AGENTS.md, docs/WRITING-TESTS.md, docs/TEST-YAML.md and
docs/WHAT-IS-SUPPORTED.md. Review without editing; list findings by severity.
Check supported assertion keys, seed timing, completion, tokens and object prefixes.
Derive the goldens from the fixtures. Could the tests pass with no output, missing
rows or duplicates? Explain any gaps and the smallest fixes, with file references.
State what remains unverified without a live run.
```

**Check the result:**

- Findings concern missing checks and behavior, with file references, rather than only style.
- Plain database `match:` compares a distinct set; it cannot detect duplicates without another check.
- `min_rows` alone does not verify every value; missing completion checks may allow partial output.
- The review checks CDC timing, golden derivation and support for every assertion tier.

### 6. Verify offline and document my tests

```text
I have finished writing <applications and case folders> in my test repository.
Read the framework's docs/SET-UP-YOUR-OWN-REPO.md, docs/USING-AI.md,
docs/WRITING-TESTS.md and docs/TEST-YAML.md. Keep its checkout read-only.
Write my README with setup, run commands, behavior, golden derivations and limitations.
Add <validator path> to load manifests and assertion specs, check files and tokens,
and verify goldens against fixtures offline. Explain the limits of those checks.
Run the validator with my installed framework, then `striim-test list --targets gold-targets.yaml`
and `striim-test run --targets gold-targets.yaml --dry-run`. Report results and give me
doctor and live-run commands for a prepared machine. Do not probe infrastructure
or run live tests; offline validation does not prove deployment or adapter behavior.
```

**Check the result:** manifests, files, tokens and fixture/golden consistency are checked;
selection lists the intended cases. Offline success is never reported as a live pass.

### 7. Test my Open Processor or UDF

```text
I have <an Open Processor or a UDF> in <Maven module path> that <behavior>.
Read the framework's AGENTS.md, docs/TESTING-YOUR-JAVA.md, docs/TEST-YAML.md
and docs/WHAT-IS-SUPPORTED.md. Start from <samples/code/op or samples/code/udf>.
Write a test in <case folder> with the documented op: or udf: block that builds
and loads my module and checks <observable effect> from <test input>.
Use a file target if the effect is only visible in userdata. Derive the golden
from the input. Give me the doctor and run commands for a prepared machine;
do not run infrastructure probes or live tests yet.
```

**Check the result:** `jar:` names the Maven module directory in the documented Java tree;
an OP uses `Global.${OP_NAME}`; assertions observe the effect where it is visible.
For native mode, `STRIIM_HOME` must be the running server's own install on this machine.

### 8. Use my own test database

```text
I want my tests in <case folder> to use my disposable <database system> instance
at <host>:<port>, rather than a container.
Read the framework's docs/SERVICES.md ("Using your own instance" and my database's
section) and docs/WHAT-IS-SUPPORTED.md. Show the .env settings with placeholders
for credentials; I will fill them in. Explain CDC prerequisites and how Striim
reaches the database if its view differs from the runner's.
Give me `striim-test doctor --targets gold-targets.yaml --case <case folder>`
to check the connection on a prepared machine. Do not write passwords into files,
probe infrastructure or run live tests yet.
```

**Check the result:** settings come from the chosen service's docs. PostgreSQL CDC needs
`wal_level=logical`, `wal2json` and source-user `REPLICATION`; `SLT_POSTGRES_VIEW_HOST`
sets a different address for Striim when needed. The database is disposable: runs create and drop objects.

### 9. Move my tests into a repository and run them in CI

```text
My tests are in <current case folders>. I want them in <test repository path>
and running in <CI system> with <Docker or native> test infrastructure.
Read the framework's docs/SET-UP-YOUR-OWN-REPO.md, docs/RUN-YOUR-FIRST-TEST.md
and docs/WHAT-IS-SUPPORTED.md. Start from templates/consumer-repo; preserve the
cases and pin the framework in a separate read-only checkout.
Write a CI job that runs the sync script, uses --check for an existing prepared
checkout, and runs `striim-test run --targets gold-targets.yaml`.
Fail on every nonzero exit code, including skips. Keep the run directory's
live/junit.xml and evidence as artifacts; use the CI secret store for credentials
and license settings. Keep shared infrastructure ownership. Do not run the job yet.
```

**Check the result:** the pinned framework is installed before tests run, exit 3 fails the job,
and JUnit and failure evidence are retained. Secrets stay out of committed files.

### 10. Convert my manual test plan

```text
I test <application behavior> by hand today. My steps and expected outcome are:
<manual steps, test input and expected result>
Read the framework's docs/WRITING-TESTS.md, docs/TEST-YAML.md and
docs/WHAT-IS-SUPPORTED.md. Map each step to ddl, seed with its timing, action,
recover or assert, and show me the mapping before writing <case folder>.
List steps the framework cannot express; propose supported alternatives rather
than inventing keys. Derive the golden from the plan, not from a run.
Do not run infrastructure probes or live tests yet.
```

**Check the result:** every manual step is mapped or explicitly unsupported; CDC changes use
`post_start`; all manifest keys and assertion tiers exist in TEST-YAML.md.

### 11. Get oriented before writing

```text
I am new to this framework and want to understand <sample case folder> before
writing my tests. Read the framework's README.md, AGENTS.md, docs/START-HERE.md,
docs/WRITING-TESTS.md and every file in that sample.
Explain in under 15 lines what each file does, the run phases in order, how the
test proves completion, and how it decides pass or fail. Explain where the
expected result comes from. Do not edit files, probe infrastructure or run tests.
```

**Check the result:** the explanation follows setup, seed, deployment, readiness, completion,
comparison and cleanup where supported; the golden is derived by hand rather than recorded.

### 12. Write or review my TQL

```text
<Write a Striim application for | Review and fix> <app.tql path or requirement file>.
Read the framework's docs/tql/README.md and only the pages it names for this task.
Source: <database, reader, tables, DDL path>. Target: <system, writer, tables, DDL path>.
Mapping and transformations: <source column -> target column, filters, lookups>.
Load type: <initial load | CDC | both>. Related tables: <parent -> child>.
Ask before assuming anything not listed. Deliver the whole TQL file and every file it
reads, and list each choice or change with its rule ID from docs/tql/RULES.md.
Do not deploy or run anything.
```

**Check the result:** the file parses in the Striim console; no property, function or clause
is missing from docs/tql/REFERENCE.md or the adapter's docs.striim.com page; every stream has
a producer and a consumer; related tables share one writer.

## Mistakes assistants make here

| What it does | Why it is wrong | What to ask for instead |
|---|---|---|
| Copies the target's contents into `expected/` | The test then proves nothing | A golden derived from the seed, row by row |
| `localhost`, `5432`, `striim` in TQL or SQL | Breaks in Docker mode and with your own database | The service's tokens ([SERVICES.md](SERVICES.md)) |
| Table names without `${TID}`, or with it at the end | Runs collide; clean-up misses suffixed names | `${TID}` first in every name, in TQL, SQL and `test.yaml` alike |
| `assert: {rowcount: …}` or another made-up tier | Unknown tiers are ignored: the test checks less than it says | Tier names from [TEST-YAML.md](TEST-YAML.md) only |
| CDC data seeded before deploy | The reader never sees it | `when: post_start` |
| A numeric or date column without a type under `exact.columns` | The run stops with `undeclared-conversion` | Type every non-text column |
| `lifecycle:` or `exact:` on Oracle, SQL Server or MySQL routes | Postgres only; refused at load | `diff` or `data` with `rows:`, and `timeout` |
| Runs `pytest` directly instead of `striim-test` | Ignores `.env`; refused without `SLT_INFRA_OWNERSHIP` | `striim-test run` |
| Calls a skipped run "passed" | Exit 3 means nothing was checked | Report the exit code and the skip reason |
| Raises `timeout` to fix a flaky test | Hides an app that has not finished | A `lifecycle:` block, then find out why it is slow |
| Stops or removes containers to "start clean" | Other runs on the machine share them | `--keep-resources` and the replay command, or ask |

## Reviewing what it wrote

Before you commit a test an assistant wrote, check five things yourself. It takes about five minutes:

1. **`purpose:`** says one behaviour, and the test checks that behaviour.
2. **The golden**: work out two of its rows from the seed by hand.
3. **The TQL** contains no host, port, user, password or bare table name.
4. **`striim-test doctor --case <dir>`** is all `ok`, and you saw it yourself.
5. **The run's exit code is 0**, and `evidence.json` shows `equal: true` for every comparison.

If any of these fails, the test is not done, whoever wrote it.
