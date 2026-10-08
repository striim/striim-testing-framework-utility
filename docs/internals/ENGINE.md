# Live engine (`scripts/live/`, `livetest`)

How the live tier works inside. It is a pytest plugin (`pyproject.toml` loads `livetest.plugin`) that
`striim-test run` drives; this page is for people changing the engine or running it with pytest
directly. Writing tests is covered in [../WRITING-TESTS.md](../WRITING-TESTS.md) and
[../TEST-YAML.md](../TEST-YAML.md).

## Running it with pytest

For hermetic engine development, first create and activate the pinned `.venv-test` environment
as described in [../../CONTRIBUTING.md](../../CONTRIBUTING.md#tests). Its commands keep Docker
unavailable and external Python packages off the path.

`striim-test run` builds the pytest command line, passes `.env` values to it and records the run
directory. A direct pytest run reads **only its environment**, never `.env`, and a live run with
`SLT_INFRA_OWNERSHIP` unset is refused before it starts anything. Export the ownership first:

```bash
export SLT_INFRA_OWNERSHIP=shared SLT_KEEP_SERVICES=1   # reuse and keep the stack; never tears it down
export SLT_INFRA_OWNERSHIP=exclusive                    # a fresh stack of its own, torn down at the end
```

```bash
cd scripts/live
python -m pytest -q -m 'not live' tests                  # the engine's hermetic tests (no Docker, no Striim)
python -m pytest -m live regression                      # the framework's own live cases
python -m pytest -m live -k hello-single                 # one live case by name
python -m pytest -m live --collect-only -q               # list them
python -m pytest -m 'live and depth_smoke'               # by depth
python -m pytest -m 'live and not spanner'               # by service: every live item is marked
                                                         # with the services its manifest requires
```

Use `-m` (markers), not `-k`, to select by service: `-k "not spanner"` matches names and paths,
and misses a test that reaches Spanner from a folder not named after it.

## A skipped run

Every live case skips when the cluster is not fully formed, and pytest reports that as `N skipped`
with exit status 0. `striim-test run` treats a selection where nothing executed as a failure (exit
3), and so does the live tier unless `SLT_ALLOW_NO_CLUSTER=1` is set. With direct pytest, read the
executed count from the JUnit XML, not the exit code.

A container that is up is not a server that is up: a Striim node can shut itself down (its log ends
`Striim Server Beginning Shutdown`) while `docker ps` still shows the container running. When a run
skips everything:

```bash
docker logs --tail 20 ${SLT_STACK_PREFIX:+$SLT_STACK_PREFIX-}slt-node   # look for "Beginning Shutdown"
python -m livetest.cli stop striim && python -m livetest.cli start striim
```

`SLT_KEEP_SERVICES=1` is what lets a cluster be reused across runs, and also what lets a node lost
in an earlier run go unnoticed.

## How Striim is resolved

`STRIIM_URL` (default `http://localhost:9080`) is probed:

- **Reachable**: reuse it. The mode is `docker` if the framework's `slt-striim` container runs, else
  `native`. Topology is read from `LIST DEPLOYMENTGROUPS;`.
  - In docker mode a reused cluster must be the release detected from `STRIIM_HOME` (or 5.4.2). A
    cluster of another release, read from its `slt-striim:<version>` image tag, is redeployed at
    the right one (exclusive ownership only; a shared run refuses to redeploy).
- **Not reachable** (and the port free): provision the Docker cluster. Download the Striim packages
  if missing, build the image if missing, `docker compose up slt-striim slt-node slt-agent`, and
  wait for the agent to register.

A test's `topology:` is matched against what is available; a mismatch skips:

- `single`: always runs.
- `cluster` (`agent` is an alias): needs at least two nodes in `default` and a registered agent in
  `Agents`. The Docker cluster has both; a native single-node server does not.

Placement uses tokens: `${APP_GROUP}` is `default`, `${SOURCE_GROUP}` is `Agents`, as in
`DEPLOY APPLICATION x WITH SourceFlow IN ${SOURCE_GROUP}, AppFlow IN ${APP_GROUP};`
(`regression/hello/hello-cluster/`).

Database tokens resolve to the host Striim reaches a service by: `localhost` for a native Striim,
`host.docker.internal` for the Docker cluster, or `SLT_STRIIM_VIEW_HOST` /
`SLT_<SERVICE>_VIEW_HOST`. The framework's own connections always use `localhost`.

## Environment variables

Server settings are `STRIIM_*`; framework settings are `SLT_*` (integration: `INT_*`).
[SETTINGS.md](../SETTINGS.md) lists every one, where it can be set and its default.

| Variable | Purpose |
|---|---|
| `STRIIM_URL`, `STRIIM_USER`, `STRIIM_PASS` (alias `STRIIM_PASSWORD`) | the Striim to test. Default `http://localhost:9080`, `admin`; the Docker cluster's password is `striim` |
| `STRIIM_API_TIMEOUT` | default timeout on Striim REST calls, seconds: `600` (read), `10,600` (connect,read) or `0` for none; default `10,600`. The login and read-only polls (status, `MON`, `DESCRIBE`, `SHOW … CHECKPOINT HISTORY`, `LIST LIBRARIES`, `LIST DEPLOYMENTGROUPS`) use `10,120`, re-login the rest of its deadline; `0` also turns these off; jar LOAD/UNLOAD use none. Also read from `machine.env` |
| `STRIIM_HOME` | a Striim install: the release the image is built from and jars are compiled against, and the license source |
| `SLT_INFRA_OWNERSHIP` | `shared` or `exclusive`; required for a live run ([design/lifecycle.md](design/lifecycle.md)) |
| `SLT_KEEP_SERVICES=1` | keep the cluster and service containers after the run; required with `shared` |
| `SLT_KEEP_RESOURCES=1` | keep the app, its tables and slots whatever the result; `--keep-resources` sets it |
| `SLT_KEEP_RESOURCES_ON_ERROR=1` | the same, for failed tests only. Replication slots are dropped; the teardown prints the SQL that recreates them |
| `SLT_SKIP_VERIFY=1` | run a test to RUNNING with its data flowed, then skip every assertion and the `recover:` phase and report it skipped. For debugging by hand, with `SLT_KEEP_RESOURCES=1` |
| `SLT_RUN_DISABLED=1` | run tests marked `disabled:` or `disabled_parallel:` |
| `SLT_ALLOW_NO_CLUSTER=1` | do not fail a run in which every case skipped for missing infrastructure |
| `SLT_EMULATORS=1` | opt in to every service that declares `opt_in_env` (none of the shipped ones do) |
| `SLT_STRIIM_VIEW_HOST`, `SLT_<SERVICE>_VIEW_HOST` | the host Striim reaches services by |
| `SLT_CLUSTER_SETTLE` | seconds to let a just-formed cluster settle before the first deploy (default 20) |
| `SLT_<SERVICE>_HOST` and the other `live_env` keys | use an existing instance instead of the container (`scripts/live/services/<name>/service.yaml`) |
| `SLT_<SERVICE>_HOST_PORT` | the host port a service container publishes on |
| `SLT_PARALLEL=1` | allow `pytest-xdist` (`-n`) and turn on per-test name tokens |
| `SLT_STACK_PREFIX` | run a second, independent stack on the same host |
| `SLT_LOCK_DIR` | the machine-wide coordination directory (default `/tmp/slt-locks`, mode 1777) |
| `SLT_STRIIM_PRIMARY_CPUS`, `SLT_STRIIM_NODE_CPUS`, `SLT_STRIIM_MEM_MAX`, `SLT_STRIIM_MEM_LIMIT` | CPU and memory caps for the Docker cluster |
| `SLT_STRIIM_DEPS_MANIFEST` | installers you already have, instead of the download |
| `SLT_STRIIM_MIN_FREE_GB` | the free Docker disk a first image build requires |

## Path and connection settings

Nothing needs setting inside a framework clone: every key defaults to the clone's layout. The keys
let a test repo drive the engine.

| Key | Default | What it points at |
|---|---|---|
| `SLT_PROJECT_ROOT` | the clone root | the project: `example:` and `jar:` paths, and its `.env` |
| `SLT_FRAMEWORK_HOME` | the clone's `scripts/` | the framework checkout |
| `SLT_LIVE_CASES` | `scripts/live/regression` (`.env.example` sets `samples`) | the live case root, or several (below) |
| `SLT_SERVICES_DIR` | `scripts/live/services` | live service definitions |
| `SLT_STATE_DIR` | `scripts/live` | run directories and coordination files |
| `SLT_INT_CASES`, `SLT_INT_SERVICES_DIR` | `scripts/integration/...` | the integration tier's ([INTEGRATION-ENGINE.md](INTEGRATION-ENGINE.md)) |

**Precedence, per key:** the process environment, then `<project root>/.env`, then the default.
Settings shared by several checkouts can also come from a machine file
(`${XDG_CONFIG_HOME:-$HOME/.config}/striim-test/machine.env`, or `SLT_MACHINE_ENV`), below `.env`.

**`.env` rules:**
- It is read from `SLT_PROJECT_ROOT` as set in the environment, else from the clone. An
  `SLT_PROJECT_ROOT` line inside `.env` moves modules but not the `.env` itself.
- The clone's `.env` is the file `SLT_FRAMEWORK_DOTENV` names, when that is set in the
  environment (never from a `.env` or the machine file); otherwise the framework checkout's own.
  A file that does not exist reads as empty, as for `SLT_MACHINE_ENV`, so a mistyped path
  silently drops that layer. Tests use it to keep a developer's `.env` from their child runs
  (`scripts/live/tests/_hermetic_child.py`).
- Only known keys are read; other lines are ignored, and nothing is exported.
- `KEY=VALUE`, with comments, blank lines, an `export` prefix, one level of quotes, CRLF and a BOM
  accepted. No interpolation. An empty value means unset at that layer.
- A relative path resolves against the `.env` file's directory; in the environment, against the
  current directory. `~` is expanded.
- The live engine's pytest header names every key taken from `.env`, never its value.
- A key that is set but names a missing path stops the run with `PathConfigError`, naming the key
  and where it was set.

**Several live case roots.** `SLT_LIVE_CASES` may list several case trees, separated by the
platform's path separator (`:` on Linux and macOS, `;` on Windows); a project manifest may list them
as `suites.live` ([../SET-UP-YOUR-OWN-REPO.md](../SET-UP-YOUR-OWN-REPO.md)). One root behaves
exactly as before.
- The first root is the primary root: the default collection path, and what a single-root caller sees.
- A run that collects the whole primary root (pytest's `regression`, `striim-test run` or `list` with
  no path, `-k <name>`) collects every root. A run naming a case or folder collects only that.
- Depth and service markers, `--depth`/`-m` filtering, pre-flight's service and OP/UDF unions and
  `known_test_ids` cover every root.
- Ids stay unambiguous: a manifest `name:` must be unique across all roots (collection refuses a
  duplicate, as within one root). A case under the project root keeps its project-relative
  `striim-test` id; with several roots, a case in a root outside the project is
  `<root directory name>/<path in the root>`, e.g. `live:services/kafka/kafka-diff::kafka-diff`, the
  same whether the run lists everything or narrows with `--suite` or a path (one root keeps its
  absolute id). So two roots may not share a directory name, and a root may not lie inside
  another; either stops the run with `PathConfigError`.
- A relative entry resolves like a single value: against the current directory, or the `.env`'s.
  Empty entries are ignored, so `root:` is the same as `root`.

## Depth

Every case may carry `depth:`, one of `smoke`, `gate`, `regression` (the default), `fault`,
`customer`, `measure`, `canary`. It becomes the pytest marker `depth_<value>`, so a direct pytest
run selects with `-m 'live and depth_smoke'`. `striim-test run` has no marker option; select with a
path or `--case` there.

## Parallel runs (`SLT_PARALLEL=1`)

The suite runs serially by default: it shares one Striim cluster and one set of service
containers. `striim-test run --parallel` (or `SLT_PARALLEL=1 python -m pytest -n N`) runs tests at
once:

- **Names.** `${TID}` and `${TID_ORACLE}` are empty in a serial run and a per-test prefix in a
  parallel one: `"t"` plus 9 hex digits of a hash of the test name, with a trailing underscore
  (`t1a2b3c4d_users`, `T1A2B3C4D_USERS`). Templates carry no separator of their own.
- **Provisioning happens once.** The first worker to need the cluster or a service provisions it
  under a lock while the others wait, then reuse it.
- **Open Processor and UDF jars load once** per cluster, across workers, checkouts and worktrees:
  the record lives in the machine-wide lock directory, keyed by the cluster, and is checked against
  `LIST LIBRARIES`. A jar whose contents did not change is not reloaded; in docker mode a changed
  jar set is staged, the app nodes restart once, and the set is registered again. A load that fails
  twice at the same bytes is not retried for the rest of the run (`SLT_OP_RETRY_FAILED=1` retries).
- **No teardown.** No worker tears down the shared cluster or services at the end; always run
  parallel with `SLT_KEEP_SERVICES=1`.
- **Spanner** serializes read-write transactions in its emulator. Run it apart:
  `-m "live and not spanner"` in parallel, then `-m "live and spanner"` serially.

## Parallel stacks (`SLT_STACK_PREFIX`)

`SLT_STACK_PREFIX` stands up a second, independent stack (cluster and services) beside the default
one, for example to compare two Striim releases. Export it (lowercase letters, digits, dashes) for
every command that touches that stack.

```bash
export SLT_STACK_PREFIX=alt
python -m pytest -m live -k hello             # provisions and uses alt-slt-striim and the rest
python -m livetest.cli stop all               # stops the alt-* stack only
```

The prefix scopes container names (`<prefix>-slt-*`), compose projects (and so networks and
volumes), coordination files, and the Striim image tag (`<prefix>-slt-striim:<version>`). Compose
service names and in-network host names stay unprefixed; each stack has its own network.

**Ports.** Two stacks running at once must not publish the same host ports: give the second its own
`SLT_*_HOST_PORT` values and point `STRIIM_URL` at its Striim port. Kafka's ZooKeeper publishes
2181; move it with `SLT_ZOOKEEPER_CLIENT_PORT`.

**Memory and CPUs.** Two clusters are twice the JVM heaps; set `SLT_STRIIM_MEM_MAX` per stack, and
`SLT_STRIIM_NODE_CPUS` so each cluster stays within the license's CPU count.

## Services

A test declares what it needs with `requires:`. Each service resolves once per session: an existing
instance when its `live_override_env` variable is set, else its compose file is brought up, or the
running container is reused. Service definitions are `scripts/live/services/<name>/service.yaml`;
the keys are documented in [../YOUR-OWN-SERVICES.md](../YOUR-OWN-SERVICES.md). The schemas are fixed
(`qasource`, `qatarget`) and shared, so tests keep apart by `${TID}` in object names; the framework
creates missing roles and schemas, and each run drops only the objects its ownership ledger
recorded ([design/lifecycle.md](design/lifecycle.md)).

Before a test resets Postgres it terminates any backend that is idle in a transaction holding a
lock on the objects it is about to drop (a writer whose last batch never reached its commit
policy), and prints one line per backend. A backend that is mid-statement is never killed; the DDL
waits 60 s and then fails naming the open transactions.

## Design notes

- [design/lifecycle.md](design/lifecycle.md): infrastructure ownership, run identity, the
  `lifecycle:` block, the ownership ledger.
- [design/c8-exact-data.md](design/c8-exact-data.md): exact comparison.
- [PERF_SPEC.md](PERF_SPEC.md): the integration tier's performance extension.
- [RELEASING.md](RELEASING.md): cutting a tagged release.
