# Run your first test

At the end of this page, `samples/live/01-plain-replication` passes on your machine and you can read
its result. That proves your Python, Docker, Striim and settings all work, which every later step
depends on.

**You need:** Python 3.12 or later (`python3`), git, and Docker. Docker runs the test databases, and
Striim itself if you do not have a Striim server of your own. Budget about 10 minutes with your own
Striim server; the first Docker-mode run downloads and builds Striim, which takes 30 minutes or more.

## 1. Install

```
git clone https://github.com/striim/striim-testing-framework-utility.git striim-testing-framework-utility
cd striim-testing-framework-utility
python3 -m venv .venv && .venv/bin/pip install -e .
. .venv/bin/activate
```

`python3 -V` shows which Python `python3` is. If it is older than 3.12 (a Mac often has 3.9 from
Apple's developer tools, or an older python.org install), name a newer one instead, such as
`python3.12 -m venv .venv`; otherwise `pip` stops with `requires a different Python`. If the venv was
already made with the old one, delete it first (`rm -rf .venv`).

Activating the environment puts `striim-test` on your `PATH`. Without it, call
`.venv/bin/striim-test`. Every command on this page runs from the clone root.

## 2. Create your settings file

```
cp .env.example .env
```

`.env` holds your settings; `.env.example` explains each one. Both start with a dot, so `ls` and
Finder hide them; `ls -a` shows them. A setting exported in your shell wins
over the same key in `.env`. Leave three lines as they are:

- `SLT_LIVE_CASES=samples`: `striim-test run` only runs cases under this folder.
- `SLT_INFRA_OWNERSHIP=shared` and `SLT_KEEP_SERVICES=1`: the run reuses whatever Striim cluster and
  databases are already running, starts what is missing, and never tears anything down. That is
  safe on a machine or Striim server other people use. A run refuses to start when
  `SLT_INFRA_OWNERSHIP` is unset.

Then choose where Striim comes from: step 3a or 3b.

## 3a. Your own Striim server

Set its root URL and a user allowed to create and drop apps:

```
STRIIM_URL=http://striim.example.com:9080
STRIIM_USER=admin
STRIIM_PASS=...
```

`STRIIM_PASSWORD` is accepted as another name for `STRIIM_PASS`. This is called **native mode**.
A few features need the Docker cluster instead (reading files on the Striim nodes, killing a node,
JMX): [WHAT-IS-SUPPORTED.md](WHAT-IS-SUPPORTED.md) lists them. Sample 03 is one of them.

## 3b. Docker mode

Leave `STRIIM_URL` unset. The first run builds a Striim image and starts a two-node cluster with an
agent; later runs reuse it.

**License.** Striim needs a license to boot, and you need your own: the framework does not include
one. The settings below are read from your shell, not from `.env`. Either:

- `export STRIIM_HOME=<a Striim install>`: the license is read from its `conf/startUp.properties`;
- or export `COMPANY_NAME`, `CLUSTER_NAME`, `PRODUCT_KEY` and `LICENCE_KEY` yourself.

`STRIIM_HOME` also sets the Striim release the image is built from: the `<ver>` in its
`lib/Platform-<ver>.jar`. That release must be published for download, or the first run stops with
"not available for download". Without `STRIIM_HOME`, the image is Striim 5.4.2. So one install may
not do both jobs:

- An install has a license only if one was entered in it: its `conf/startUp.properties` has
  `ProductKey=` and `LicenceKey=` lines with values. An install that was unpacked but never
  licensed has none, and `striim-test doctor` names the settings that are missing.
- If your licensed install is a different release from the one you want to test, do not point
  `STRIIM_HOME` at it. Leave `STRIIM_HOME` unset and export the four settings, copied from that
  install's `conf/startUp.properties` (`WAClusterName` is `CLUSTER_NAME`, `CompanyName` is
  `COMPANY_NAME`, `ProductKey` is `PRODUCT_KEY`, `LicenceKey` is `LICENCE_KEY`).

**CPUs.** The license caps how many CPUs the cluster may count, 24 in the licenses this was tested
with. Each of the two nodes counts every CPU it can see, so on a machine with more than 12 CPUs the
second node refuses to join ("would bring the total number of CPUs to 48 … Striim Server Beginning
Shutdown") and the run fails. Cap each node, in your shell:

```
export SLT_STRIIM_PRIMARY_CPUS=12 SLT_STRIIM_NODE_CPUS=12
```

**Download and disk.** The first run downloads about 6.3 GB of Striim packages and drivers into
`scripts/live/services/striim/images/striim/deps/`, then builds an image of about 22 GB. Docker
needs at least **35 GB free** for that first build, and 5 GB to run afterwards. On Docker Desktop
that space is inside Docker's virtual machine, not on your disk: `docker system df` shows what uses
it, and its size is under Settings, Resources. The run refuses to build with less; if you know you
need less, export `SLT_STRIIM_MIN_FREE_GB`. A rebuild after a framework update comes from Docker's
build cache and needs only the 5 GB.

**Vendor drivers.** The JDBC drivers and other third-party files the first run downloads remain under
their vendors' licenses, which apply to your use of them.

**If you already have the installers,** skip the download: list them in a manifest and export its
path from your shell.

```
export SLT_STRIIM_DEPS_MANIFEST=/path/to/deps-manifest.json
```

The manifest is JSON: `schemaVersion` 1, the `directory` holding the files (relative to the
manifest), and each file's sha256 (`sha256sum <file>`, or `shasum -a 256 <file>` on a Mac). The
files must be in the manifest's folder or below it.

```json
{"schemaVersion": 1, "directory": ".", "sha256": {
  "striim-node-5.4.2-Linux.deb": "<64 hex digits>",
  "…": "…"}}
```

It must name all nine files: `striim-dbms-5.4.2-Linux.deb`, `striim-node-5.4.2-Linux.deb`,
`striim-agent-5.4.2-Linux.deb`, `striim-samples-5.4.2-Linux.deb`,
`sqljdbc_6.0.8112.200_enu.tar.gz`, `mysql-connector-java-8.0.30.zip`, `vertica-jdbc-25.3.0-0.jar`,
`jmx_prometheus_javaagent-0.16.1.jar` and `instantclient-basic-linux.x64-21.6.0.0.0dbru.zip`. The
run checks every digest before it builds, and stops at the first file that is missing or does not
match, naming it. It copies the files into the clone (about 5.2 GB) and never changes the originals.

**Apple Silicon.** The Striim image is amd64 and runs under emulation: sample 01 takes about six
minutes there, against about a minute and a half on a Linux x86-64 host.

## 4. Check your setup

```
striim-test doctor --case samples/live/01-plain-replication
```

Each line is one check. A clean result ends without a problem count and exits 0:

```
[ ok ] env: /path/to/striim-testing-framework-utility/.env (3 keys)
[ ok ] service settings: none set; each required service runs in Docker with its defaults
[ ok ] ownership: shared (SLT_INFRA_OWNERSHIP, set in /path/to/.env; ...); SLT_KEEP_SERVICES=1 (...)
[ ok ] ledgers: no leftover ownership ledgers
[ ok ] striim: ...
[ ok ] case 01-plain-replication: test.yaml loads (plain-replication)
[ ok ] service postgres: ...
```

A failing line says what is wrong and what to set. Doctor then exits 2 when any problem is in
your settings or a test (as below), or 3 when everything else is right but something is not
reachable or not running, such as Docker. Treat any status but 0 as "fix first":

```
[FAIL] striim: Docker mode (STRIIM_URL unset): Striim needs a license to boot, and CLUSTER_NAME, COMPANY_NAME, PRODUCT_KEY, LICENCE_KEY are not set; export STRIIM_HOME=<a Striim install> (read from its conf/startUp.properties), or export COMPANY_NAME, CLUSTER_NAME, PRODUCT_KEY and LICENCE_KEY
[FAIL] service postgres: Docker (slt-postgres) is not running, but localhost:5432 already answers: something else holds the host port the run publishes postgres on; set SLT_PG_HOST_PORT to a free port in .env
doctor: 2 problem(s)
```

Fix each `[FAIL]` line before you run.

## 5. Run it

```
striim-test run samples/live/01-plain-replication
```

The first line of output names the run directory (`striim-test: run-dir: ...`). The run creates the
tables, loads the seed rows, deploys and starts the app, waits until the target has caught up and
stayed still, compares the target with `expected/rows.csv` row by row, and cleans up.

| Exit code | Meaning |
|---|---|
| 0 | passed |
| 1 | a test failed |
| 2 | a mistake in the settings or in a `test.yaml` |
| 3 | something a test needed was not available, so it was **skipped**: not a pass |
| 4 | cancelled, interrupted or timed out |
| 5 | nothing was selected |

## 6. Read the result

The run directory is `scripts/live/runs/<time>-<id>/`. Under `live/`:

| File | What is in it |
|---|---|
| `stdout.log` | the test output: each phase as it ran, and the failure message |
| `junit.xml` | one result per case, for CI |
| `junit.slt.json` | each assertion's result and detail, or the skip reason |
| `evidence/<case>/<run>/evidence.json` | the full record of the case: what it created, what it waited for, every comparison |
| `cluster-logs/` | each Striim node's log, when the Docker cluster never answered |

[WRITING-TESTS.md](WRITING-TESTS.md), "Read a failure", walks through `evidence.json`.

## 7. Run the other samples

```
striim-test list                                   # every case a run would select
striim-test run samples/live                       # every live sample
striim-test run samples/live --parallel            # the same, three at a time
striim-test run samples/live/04-lifecycle-check --dry-run   # the selection, without running
```

The [README](../README.md) lists what each sample shows. `samples/code/` holds two more that build
and load Java ([TESTING-YOUR-JAVA.md](TESTING-YOUR-JAVA.md)).

## 8. Stop what the runs left up

The Striim cluster (Docker mode) and the test databases stay up so the next run is fast. To stop
the cluster and Postgres:

```
(cd scripts/live && ../../.venv/bin/python -m livetest.cli stop striim postgres)
```

This removes those containers and their data; the next run starts them again. It stops the stack
your settings name: the one with no prefix, or the one `SLT_STACK_PREFIX` names in your shell or in
this clone's `.env` ([SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md), "Several checkouts or
people on one machine"). Run it from the checkout whose stack you started, with the same settings.
Everyone who uses that stack shares it, so run it only when no one else is using it.
`stop all` stops **every** service the framework knows, including ones other people or other
checkouts started. Use it only when this Docker is yours alone.

## If it did not work

- **`Bind for 0.0.0.0:5432 failed: port is already allocated`**: something else on this machine uses
  the port. Set another in `.env`, for Postgres `SLT_PG_HOST_PORT=55432`; the run and Striim both
  follow it. Each service's setting is in [SERVICES.md](SERVICES.md).
- **`PermissionError: … /tmp/slt-locks/…`, or "the shared lock dir … belongs to …"**: every user on a
  machine shares one lock directory, and an older version created it writable only by its owner.
  The named user can run `chmod 1777 /tmp/slt-locks`; or export a directory of your own (shell only,
  not `.env`): `export SLT_LOCK_DIR=$HOME/.slt-locks`. In Docker mode you then share containers
  with that user without coordinating, so do not run at the same time.
- **Skipped: `provisioned cluster did not become reachable in time`** (exit 3): read
  `cluster-logs/` in the run directory; check the CPU cap and free disk above.

Everything else: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
