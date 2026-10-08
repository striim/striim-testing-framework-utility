# Your own services

A service is a container a test can `requires:`: a database, a message broker, an emulator. The
framework ships eight (Postgres, Oracle, SQL Server, MySQL, Vertica, Kafka, Spanner and GCS), and a
connection-only definition each for Teradata and ServiceNow, which run against your own instance
(see [WHAT-IS-SUPPORTED.md](WHAT-IS-SUPPORTED.md)). You can add your own, or replace a shipped one with a
customized image, without changing the framework: the service lives in your repo, and your project
manifest points at it.

This page builds one real example end to end: **Postgres 17 in place of the shipped Postgres 16**, so
your tests run against the Postgres version you use in production. Then it shows what changes for a
system the framework does not ship at all, and how to use an existing instance instead of a
container.

## What a service is

A folder named after the service, holding:

| File | Required | What it does |
|---|---|---|
| `service.yaml` | yes | how the framework starts the service, reaches it, and what it hands to tests |
| `compose.yaml` | yes | the Docker Compose file that runs it |
| `Dockerfile` and other files | no | anything the compose file builds or mounts |

Your repo, for this example:

```
my-striim-tests/
  gold-targets.yaml          # the project manifest: your suites and servicesRoots
  services/
    postgres/                # replaces the shipped postgres
      service.yaml
      compose.yaml
      Dockerfile
  tests/live/
    orders-cdc/              # the test from WRITING-TESTS.md
```

## 1. `service.yaml`

<!-- snippet: service postgres -->
```yaml
name: postgres                   # must equal the folder name
compose: compose.yaml            # relative to this file
container: slt-postgres17        # the container compose.yaml starts
isolation: none
live_override_env: SLT_PG_HOST   # set it to use an existing Postgres instead (step 7)
docker_defaults:
  host: localhost
  port: 55432
  dbname: sltdb
  admin_user: postgres
  admin_password: striim
  source_user: qasource
  source_password: striim
  source_schema: qasource
  target_user: qatarget
  target_password: striim
  target_schema: qatarget
docker_env:
  port: SLT_PG_HOST_PORT
live_env:
  host: SLT_PG_HOST
  port: SLT_PG_PORT
  dbname: SLT_PG_DB
  admin_user: SLT_PG_ADMIN_USER
  admin_password: SLT_PG_ADMIN_PASSWORD
  source_user: SLT_PG_SOURCE_USER
  source_password: SLT_PG_SOURCE_PASSWORD
  source_schema: SLT_PG_SOURCE_SCHEMA
  target_user: SLT_PG_TARGET_USER
  target_password: SLT_PG_TARGET_PASSWORD
  target_schema: SLT_PG_TARGET_SCHEMA
live_defaults:                   # an existing instance's fallbacks, ahead of docker_defaults (step 7)
  port: 5432
provides:
  PG_HOST: "{view_host}"
  PG_PORT: "{port}"
  PG_DB: "{dbname}"
  PG_SOURCE_USER: "{source_user}"
  PG_SOURCE_PASSWORD: "{source_password}"
  PG_SOURCE_SCHEMA: "{source_schema}"
  PG_TARGET_USER: "{target_user}"
  PG_TARGET_PASSWORD: "{target_password}"
  PG_TARGET_SCHEMA: "{target_schema}"
  PG_SLOT: "{schema}"
  PG_URL: "jdbc:postgresql://{view_host}:{port}/{dbname}"
```

Every key a `service.yaml` can hold:

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | the service's name, as tests write it in `requires:`; equal to the folder name |
| `isolation` | yes | `none` for every shipped service: tests keep apart by `${TID}` in their object names |
| `compose` | yes, to start it in Docker | the compose file, relative to `service.yaml` |
| `container` | yes, to start it in Docker | the name of the container that compose starts. The framework checks whether it runs, and reuses it when it does |
| `docker_defaults` | no | the settings the framework and the test use when the framework runs the container: `host` and `port` (and any `*_port`), accounts, database names. Each value is available to `provides:` as `{key}` |
| `docker_env` | no | `key: VARIABLE`: when `VARIABLE` is set, it replaces that `docker_defaults` key. Use it for every published port, with the same variable compose publishes it by, so moving the port moves both |
| `live_override_env` | no | a variable that, when set, means "use an existing instance, do not start a container" |
| `live_env` | no | `key: VARIABLE` for that existing instance; a key whose variable is unset falls back to `live_defaults`, then `docker_defaults` |
| `required_env` | no | List of environment variable names that must be nonblank before connection settings are resolved. No default satisfies this requirement; errors list names only. Use this for credentials on external services. |
| `live_defaults` | no | `key: value` fallbacks for `live_env` keys that apply only to the existing instance (the override set and the key's variable unset), ahead of `docker_defaults`. For a container that listens on another port or scheme than a real instance does. Each key must be a `live_env` key |
| `provides` | no | the tokens a test's TQL and SQL can use: `TOKEN: "template"`, with `{key}` from the settings above. `{view_host}` is the host the Striim server reaches the service by |
| `opt_in_env` | no | a variable for heavy services; no shipped service sets it. In the live tier, until it (or `SLT_EMULATORS`) is set, `start all` and other bring-ups not tied to a selected test leave the service out; a live test that requires it still starts it. In the integration tier (umbrella `INT_EMULATORS`), tests that require it are skipped until it is set |
| `post_up` | no | a script path inside the container, run with `bash` after it is healthy |
| `required_files` | no | files the container needs that git does not carry (VM disks, licensed installers), relative to the service's folder. While one is missing and no existing instance is set, tests that require the service skip, naming the files, and `start all` leaves it out. To fetch them, give the service a `pre_up` script |
| `python_module` | no | a Python client module the framework's own connection needs that is not installed by default (a separately licensed driver, say). Without it, tests that require the service skip with `python_module_hint`, which says how to install it. The shipped `teradata` definitions declare `teradatasql` |
| `driver` | no | a Python module in the service's folder with the setup compose cannot do (hooks the framework calls). See "Code a service needs" |
| `pre_up` | no | a script, relative to the service's folder, that the framework runs on the host before it starts the service: fetching the `required_files`, say. See "Services that fetch their own files" |
| `graceful_stop` | no | a shell command run in the container (`docker exec <container> sh -c`) when a `service_outage` action stops it with `signal: TERM`, in place of `docker stop`. For an image whose PID 1 is a wrapper that ignores SIGTERM, so the server itself gets the signal: mssql uses `kill -TERM $(ps -C sqlservr -o pid=)`. The container must have no restart policy: the server exits on its own, which docker would restart during `down_for` |
| `graceful_stop_timeout` | no | seconds to wait for the container to exit after `graceful_stop` (default 60). A stop that takes longer is KILLed and fails the test |
| `restart_in_place` | no | a shell command run in the container (`docker exec <container> sh -c`) when a `service_outage` action uses `signal: RESTART`. It restarts the server without stopping the container and exits 0 once the server accepts work again; a non-zero exit fails the test |
| `restart_in_place_timeout` | no | seconds to wait for `restart_in_place` to return (default 900). A hook that takes longer fails the test as a hang; the container is left running, never KILLed |

**A definition with neither `compose` nor `container` is connection-only**, as the shipped
`teradata` is: nothing is started, and a test runs against the instance its `live_override_env`
and `live_env` settings name, or skips until they are set.

**Readiness is the compose file's `healthcheck:`.** The framework starts a service with
`docker compose up -d --wait --build`, which returns once every container is healthy. A service
without a healthcheck counts as ready as soon as it starts.

**Replacing a shipped service replaces its whole definition.** Start from the shipped
`scripts/live/services/<name>/service.yaml` and change only what differs. The framework's own code
for that service (for Postgres: creating the `qasource`/`qatarget` accounts, the `postgres-source` and
`postgres-target` routes, the data and diff assertions) reads the same `docker_defaults` keys, so
keep every one of them.

## 2. `compose.yaml` and the `Dockerfile`

<!-- snippet: compose postgres -->
```yaml
name: ${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-postgres17
services:
  slt-postgres17:
    build: .
    image: my-striim-tests/postgres:17
    container_name: ${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-postgres17
    command: ["postgres", "-c", "wal_level=logical", "-c", "max_replication_slots=10",
              "-c", "max_wal_senders=10",
              "-c", "output_plugin_libraries=pgoutput,test_decoding,wal2json"]
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: striim
      POSTGRES_DB: sltdb
    ports:
      - "${SLT_PG_HOST_PORT:-55432}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d sltdb"]
      interval: 2s
      timeout: 10s
      retries: 30
```

<!-- snippet: file services/postgres/Dockerfile -->
```dockerfile
FROM postgres:17
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-wal2json \
    && rm -rf /var/lib/apt/lists/*
```

The rules the framework relies on:

- **`container_name` and `name:` start with `${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}`.** An
  exclusive run (`SLT_INFRA_OWNERSHIP=exclusive`) gives its stack a prefix, and looks for
  `<prefix>-<container>`. Without it, an exclusive run cannot find your container.
- **A container name of its own.** Here `slt-postgres17`, not the shipped `slt-postgres`: a shared run
  reuses whatever container runs under the name it looks for, so a shared name could hand your tests
  the stock Postgres 16.
- **A host port of its own**, published through the `docker_env` variable with the same default as
  `docker_defaults.port` (55432 here). The shipped Postgres keeps 5432, so both can run on one
  machine.
- **CDC settings in the image and the command.** The `Dockerfile` installs `wal2json`, and the command
  turns on logical decoding and allows `wal2json` as an output plugin. Current Postgres 16 and 17
  releases refuse it otherwise (`library "wal2json" may not be used as an output plugin`), and a
  `PostgreSQLReader` cannot create its slot. The shipped Postgres recipe does the same.

**Files next to the compose file.** Before it starts anything, the framework checks that every file
the service uses is in the service's folder: the compose file, each build context and `Dockerfile`,
each `COPY`/`ADD` source, each `env_file`, and each relative bind mount. A missing one stops the run
with `preflight failed for <name>: ... missing <what> <path>`, naming it. Bind mounts have one
exception:

- a mount source that exists as a directory is fine, even if empty;
- an absent source whose name has no extension (`./init`) is fine: Docker creates it as an empty
  directory. `striim-test doctor` warns about it;
- an absent source that looks like a file (`./init.sql`) is refused, because Docker would create a
  directory where the container expects a file.

## 3. Point your project manifest at it

<!-- snippet: project -->
```yaml
schemaVersion: 1
targets: []
suites:
  live: tests/live
servicesRoots:
  - services
```

`servicesRoots` is a list. An entry is relative to `gold-targets.yaml`, or absolute, and may use
`${VARIABLE}` (an entry whose variable is unset is skipped). Each entry is a services directory, or a
directory holding `services/` or `scripts/live/services/`, which is then used.

- **A service** is a directory named after it, holding a `service.yaml` (`name:` equal to the
  directory name) and its compose file.
- **Paths:** a relative entry is relative to the manifest's directory and must stay inside it. An
  absolute one (usually from a variable) is used as it is.
- **Variables:** `$VAR` and `${VAR}` are expanded from the environment. An entry that uses an unset
  or empty variable is skipped, not an error.
- **Where it applies:** the live tier, wherever the manifest is named: `striim-test run` and
  `striim-test doctor` (`--targets` or `GOLD_TARGETS`), and `python -m livetest.cli start|stop`
  (`GOLD_TARGETS`), so `stop all` stops your services too. The integration tier reads the same
  entries (below).

## 4. The override rule

A service name is looked up in your `servicesRoots` entries in order, then in the shipped services.
**The first match wins.** So `services/postgres` here replaces the shipped `postgres` for every test in
this project, and an earlier entry beats a later one. Each run's header says so, one line per replaced
name:

```
[services] postgres: using <test-repo>/services/postgres, which overrides <framework>/scripts/live/services/postgres
```

A name the framework does not ship is simply added. Replacing a shipped connection-only
definition (no `compose`, no `container`, such as `teradata`) is not reported: that is its use.

## 5. Use it from a test

Nothing in a test changes: `requires: [postgres]`, the `${PG_*}` tokens and the `postgres-source` and
`postgres-target` routes now resolve to Postgres 17. Copy `orders-cdc` from
[WRITING-TESTS.md](WRITING-TESTS.md) into `tests/live/` and it runs against the new image.

## 6. Check it

From your repo, with this clone's `striim-test` (`/path/to/striim-testing-framework-utility/.venv/bin/striim-test`,
or on your `PATH` with its virtual environment active):

```
striim-test doctor --targets gold-targets.yaml --case tests/live/orders-cdc
striim-test list --targets gold-targets.yaml
docker compose -f services/postgres/compose.yaml config --quiet
```

`doctor` prints the override line above, and one line per service the case requires:

```
[ ok ] case orders-cdc: test.yaml loads (orders-cdc)
[ ok ] service postgres: Docker (slt-postgres17) not running; Docker 29.4.0 answers, and the run starts it
```

For a service, doctor checks:
- the definition loads, and the name is not a typo of a known one;
- the preflight above: every file present, with a warning for each absent directory mount;
- in Docker mode: that Docker answers, and that no other process holds the service's host port. It
  names the `docker_env` variable to change if one does;
- once the container runs, or for an existing instance (step 7): that `host:port` accepts a
  connection. For Postgres it also logs in as the admin user;
- for an `opt_in_env` service: a warning that `start all` leaves it out until the variable is set.

`list` shows the cases your manifest selects, and `docker compose config` checks the compose file
itself.

## 7. Use an existing instance instead of a container

Set the service's `live_override_env` variable. The framework then starts no container, and reads
the settings from `live_env`:

```
SLT_PG_HOST=pg17.test.example.com
SLT_PG_PORT=5432
SLT_PG_ADMIN_PASSWORD=...
```

- **Unset settings.** A `live_env` key whose variable is unset takes its `live_defaults` value, if
  the definition has one, and otherwise keeps its `docker_defaults` value. The example's
  `live_defaults` makes the port 5432 here, not the container's 55432. A definition without
  `live_defaults` needs the port set too.
- **Where to set them.** The shipped services' variables (`SLT_PG_*` and the rest) can go in `.env`
  or the shell. So can the variables of a service you add: the framework reads
  every variable a service definition it can see declares (in `docker_env`, `live_env`,
  `live_override_env`, `opt_in_env`, `pre_up_env`, or a `${...}` in its compose file).
- **How Striim reaches it.** `{view_host}` is the instance's host, unless it is `localhost` and Striim
  runs in Docker: then it is `SLT_STRIIM_VIEW_HOST`. Set `SLT_<NAME>_VIEW_HOST` (for example
  `SLT_POSTGRES_VIEW_HOST`) when Striim reaches the instance by another name.
- **Use a disposable instance.** The framework creates the source and target roles and schemas if
  they are missing, and drops the objects each test creates.

## A system the framework does not ship

A new name works the same way: a folder under `servicesRoots`, a `service.yaml` and a
`compose.yaml`. This one runs MongoDB:

<!-- snippet: service mongodb -->
```yaml
name: mongodb
compose: compose.yaml
container: slt-mongodb
isolation: none
live_override_env: MYTESTS_MONGODB_HOST
docker_defaults:
  host: localhost
  port: 57017
  user: root
  password: striim
docker_env:
  port: MYTESTS_MONGODB_HOST_PORT
live_env:
  host: MYTESTS_MONGODB_HOST
  port: MYTESTS_MONGODB_PORT
  user: MYTESTS_MONGODB_USER
  password: MYTESTS_MONGODB_PASSWORD
provides:
  MONGODB_URI: "mongodb://{user}:{password}@{view_host}:{port}/?authSource=admin"
```

<!-- snippet: compose mongodb -->
```yaml
name: ${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-mongodb
services:
  slt-mongodb:
    image: mongo:7.0
    container_name: ${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-mongodb
    environment:
      MONGO_INITDB_ROOT_USERNAME: root
      MONGO_INITDB_ROOT_PASSWORD: striim
    volumes:
      - ./init:/docker-entrypoint-initdb.d:ro
    ports:
      - "${MYTESTS_MONGODB_HOST_PORT:-57017}:27017"
    healthcheck:
      test: ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping')"]
      interval: 2s
      timeout: 10s
      retries: 30
```

<!-- snippet: file services/mongodb/init/seed.js -->
```js
db.getSiblingDB("shop").customers.insertMany([
  { _id: 1, name: "Record01" },
  { _id: 2, name: "Record02" }
]);
```

It publishes on 57017, not MongoDB's usual 27017, so it does not collide with a MongoDB already
running on the machine. A test lists it (`requires: [postgres, mongodb]`) and uses `${MONGODB_URI}`
in its TQL wherever the connection string goes. What a new name gets, and what it does not:

- **It gets** its container started, reused and stopped like any service (with `GOLD_TARGETS` set to your
  manifest, `python -m livetest.cli stop mongodb` stops it), its `provides:` tokens, doctor's checks, and an existing instance
  through `live_override_env`.
- **It does not get** framework code: there are no `mongodb-source`/`mongodb-target` routes for
  `ddl:` and `seed:`, and `data`, `diff` and `lifecycle:` cannot read it. Put its data in the image
  or an init script (as `init/seed.js` does above), and assert on what the app writes to a supported
  service or to a file, or on Striim's own figures (`assert.monitor`, `smoke`).
- **Name its variables without the `SLT_` prefix** (`MYTESTS_...` here). `striim-test doctor`
  reports an `SLT_` variable the framework does not know as a likely typo.
- **Set them in `.env` or the shell**, like any other setting.

## Services that fetch their own files (`pre_up`)

A service whose container needs large files git does not carry (a VM's disks) can fetch them itself:

<!-- snippet: shape service.yaml keys, not a whole definition -->
```yaml
required_files: [deps/disk1.qcow2, deps/disk2.qcow2, deps/disk3.qcow2]
pre_up: download-dependencies.sh
```

- **When it runs:** before the service starts, for a test that requires the service (a `striim-test
  run` that selects one, or a pre-flight of test ids), and for a start that names it
  (`python -m livetest.cli start teradata`). It never runs for `start all` or `start live`, which
  only lists the service, so an unrequested run never downloads gigabytes.
- **When it does not:** with the service's `live_override_env` set (an existing instance needs no
  files); with `SLT_PRE_UP=0` in the shell or `.env`, which turns off every service's hook; or when
  every `required_files` entry is already there, so a run that has its files never waits on the
  network.
- **How:** from the service's folder, with `SLT_SERVICE_DIR` set to it. One at a time per service name
  on the machine (a lock in the shared lock directory), so parallel workers never fetch into the same
  folder at once; the next one finds the work done. Make the script idempotent: skip what is present.
- **How long:** at most `pre_up_timeout` seconds (default 7200). Then its process group is killed.
- **If it fails** (a non-zero exit, the timeout, or files still missing after it), that is an
  error, not a skip: the test fails with the hook's exit code and the last lines it printed, and a
  named `start` exits non-zero. With the hook switched off and a file missing, the test skips,
  naming the file.

## Integration services

The integration tier reads the same `servicesRoots`. In each entry it looks for
`scripts/integration/services/` (a checkout laid out like the framework), else an `integration/`
folder inside the entry's services directory:

```
my-striim-tests/
  gold-targets.yaml          # servicesRoots: [services]
  services/
    teradata/                # live tier
    integration/
      teradata/              # integration tier
```

As in the live tier, the first match wins, and the override is reported once
(`[services] <name>: using …, which overrides …`), unless the shipped definition is
connection-only, as `teradata` is. The integration `service.yaml` takes the same
keys, `pre_up`, `pre_up_timeout` and `required_files` included; the hook runs through the same
runner, under the same lock, and its script also gets `SLT_LIVE_SERVICE_DIR`, the live tier's service
of the same name. An entry is read as the live tier reads it: the integration folder is the
`integration/` folder of the services directory the live tier takes for that entry. Three more rules:
- **`live_service_paths`** maps a variable the compose file reads to a path inside that live service,
  so an integration container can use files the live tier holds without a copy. A value in the shell
  wins. The live service's `required_files` under that path count as this service's too: missing,
  its cases skip naming them, and `start all` leaves it out.

  <!-- snippet: shape service.yaml keys, not a whole definition -->
  ```yaml
  live_service_paths:
    INT_TERADATA_DEPS_DIR: deps      # compose: ${INT_TERADATA_DEPS_DIR:-./deps}:/disks:ro
  ```
- **A connection-only definition** is never a `start`/`stop` target: `python -m inttest.cli start all`
  skips it, and naming it prints a note.

## Keeping private images private

- The service folder lives in your repo, not in the framework clone, so nothing private enters the
  framework.
- A compose `image:` from your registry, or a `Dockerfile` whose `FROM` is a private image, needs
  `docker login` to that registry on each machine that runs the tests. The framework never pushes
  an image.
- Keep secrets out of `service.yaml` and `compose.yaml`: use `${VARIABLE}` in the compose file and
  export it in the shell.
- A `servicesRoots` entry can come from a variable (`- ${MY_PRIVATE_SERVICES}`), so a repo can name a
  private services checkout that only some machines have. Where the variable is unset, the entry is
  skipped and the shipped service is used.


### Stricter preparation

Two more `service.yaml` keys change how `pre_up` and missing prerequisites behave:

- **`pre_up_check: always`** runs the `pre_up` script even when every `required_files` entry is
  there, so an idempotent script can check more than file presence. The default, `missing_files`,
  runs it only when a file is missing.
- **`unavailable_policy: fail`** makes a missing prerequisite fail a test that requires the service
  (and a `start` that names it) instead of skipping it. The default is `skip`. With the hook turned
  off (`SLT_PRE_UP=0`), such a service fails without running it.

Either way, an existing instance (the `live_override_env` setting) needs no hook, and `start all`
never runs one. A hook gets the resolved settings plus `SLT_SERVICE_DIR` and `SLT_FRAMEWORK_PYTHON`
(the framework's Python), runs under the machine-wide per-service lock, and fails on a non-zero
exit, a timeout, a missing script, or files still missing after it. A hook's own inputs are declared
with `pre_up_env: [{name: MY_INPUT, type: string}]` (or `type: path`, resolved against the `.env`
that set it). For files whose folder is a setting, declare
`required_files_env: {MY_CACHE_DIR: deps}` beside `required_files: [deps/file]`.

### Code a service needs (`driver`)

Some services need setup that `docker compose up` cannot express: a database setting applied
after start, a sidecar that must join the cluster, or a reader that must be confirmed as reading
before a test seeds data. Put that code in a Python module in the service folder and name it in
`service.yaml`:

<!-- snippet: shape service.yaml keys, not a whole definition -->
```yaml
driver: mydb_driver        # mydb_driver.py beside this service.yaml
```

The framework imports the module by that name, with the service folder on `sys.path` for the
import only, so it loads once per process and its own sibling imports work. Choose a name no
other module uses. Your tests import the same module the same way. Every hook is optional; the
framework calls the ones the module defines (`livetest/drivers.py` has the full contract):

| Hook | Called | Returns |
|---|---|---|
| `unsupported_mode(mode)` | first, before any `pre_up` hook | why the service cannot run in this run mode (`docker` / `native`), or `None` |
| `unavailable(env, mode)` | after the framework's own availability checks pass | why it cannot be brought up here, or `None` |
| `compose_env(env)` | before bring-up | settings to publish to compose and the test process |
| `admins(base, defn)` | for each test that requires the service | the connections `ddl:`/`data:`/`seed:` route to, keyed `<service>-<role>` |
| `provision(admins, client, progress)` | after `admins` (with `{}` when the driver has no `admins`); once across xdist workers, otherwise per test | nothing; must be idempotent |
| `reader_mode(tql)`, `reader_mark()`, `wait_reader_ready(mode, mark, timeout, progress)` | around deploy: `post_start` seeds wait until the reader is reading, and again after `drop_recreate_app` | the mode a rendered TQL selects (or `None`), a position in the reader's evidence, and the seconds waited |
| `ENV_PATH_KEYS` (a tuple) | by `striim-test doctor` | settings checked as existing paths when set |
| `ENV_KEYS` (a tuple) | by `striim-test doctor` | other settings the driver reads, so doctor does not report them as typos |

Each test that requires the service gets a pytest marker named after it (`-m mydb`); the framework
registers it when your configuration does not. `striim-test doctor` reports a driver that cannot be
imported, naming the service and module. A driver name must resolve to a module file in the service
folder; anything else (a built-in, a module already loaded from elsewhere) is refused.

### Where settings come from

Every tier, hook and compose file reads settings the same way, highest first: your shell, your
project's `.env`, the framework clone's `.env`, the machine settings file
([SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md)), then the defaults in `service.yaml`. Port
settings (`*_HOST_PORT`, `*_CLIENT_PORT`) are refused in the machine file, because they tell one
checkout from another. Secrets in a settings file that no service declares are never passed on.
