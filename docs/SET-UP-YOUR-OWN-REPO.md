# Set up your own test repo

Keep your tests in a repo of your own, next to your apps, and run them with the framework at one
version you choose. At the end of this page you have:

- a repo holding your tests, a `framework.pin` naming the framework version, and a script that
  puts the framework at that version;
- one command that sets the framework up the same way on any machine or CI runner;
- a working test to start from.

You use the framework as it is: you never change its code. If you find a bug in it, report it as
an issue on the framework's repository.

## How it fits together

```
work/
  my-striim-tests/                     your repo
    framework.pin                      the framework version: a tag or a commit
    scripts/sync-framework.py          puts the checkout below at that version, makes .venv and .env
    gold-targets.yaml                  where your tests are
    .env                               your settings (not in git)
    .venv/                             Python, with the framework installed (not in git)
    tests/live/<case>/                 your tests
  striim-testing-framework-utility/    the framework, at the pinned version, read-only
```

The framework checkout sits next to your repo by default (`--home` puts it elsewhere). Your venv
has the framework installed from it, so `striim-test` runs from your repo and finds your tests
through `gold-targets.yaml`.

## 1. Start from the template

Copy `templates/consumer-repo/` from a framework clone (or from the framework's repository page)
into a new folder, and make it a repo:

```
cp -r striim-testing-framework-utility/templates/consumer-repo my-striim-tests
cd my-striim-tests
git init
```

| File | What it is |
|---|---|
| `framework.pin` | the framework version |
| `scripts/sync-framework.py` | the sync script; standard library only |
| `gold-targets.yaml` | your project manifest: `suites.live: tests/live` |
| `.env.example` | your settings; the sync script copies it to `.env` when there is none |
| `.gitignore` | keeps `.env`, `.venv/` and run state out of git |
| `tests/live/orders-cdc/` | a working CDC test, the one [WRITING-TESTS.md](WRITING-TESTS.md) builds |
| `README.md` | the commands below, for the next person |

## 2. Choose the framework version

`framework.pin` holds one line: a **release tag** (preferred), or a **full 40-character commit**:

```
v0.1.0
```

- A tag names a release, so the pin says something a person can read and compare.
- Pinning to tags assumes the framework's maintainers tag releases. If the repository you use has
  no release tags yet, pin a commit from its default branch.
- Never a branch name: a branch moves, so two machines could run different code under the same pin.
  The script refuses one.

The template ships pinned to the version it came from.

## 3. Set up the framework

```
python3 scripts/sync-framework.py
```

Any `python3` can run it: the script uses the standard library only. It:

1. clones the framework next to your repo, if it is not there yet;
2. fetches the pinned tag or commit, if the checkout does not have it;
3. checks it out as a **detached HEAD**, so no branch is created or moved (a checkout found on a
   branch is detached too);
4. makes `.venv` in your repo if there is none, with a Python the pinned framework supports (the
   `requires-python` in its `pyproject.toml`, 3.12 or later). It uses the Python running the
   script when that is new enough, else the first `python3.12`, `python3.13`, ... on your `PATH`,
   else one `uv` finds; `--python PATH` chooses one yourself. Run from inside a venv (activated,
   or as `.venv/bin/python scripts/sync-framework.py`), it uses that venv instead;
5. installs the framework into the venv (`pip install -e`), unless the venv records that commit as
   installed and its `striim-test` is there. The record lives in the venv, so a recreated venv is
   installed again;
6. copies `.env.example` to `.env`, readable only by you, if there is no `.env` yet. It never
   changes an existing `.env`;
7. prints what it did:

```
python: made /path/to/work/my-striim-tests/.venv with /usr/bin/python3.12 (Python 3.12.3)
framework: /path/to/work/striim-testing-framework-utility at v0.1.0 (0123456789ab), installed into /path/to/work/my-striim-tests/.venv
settings: made /path/to/work/my-striim-tests/.env from .env.example. Open it and set where Striim comes from before you run a test (`ls -a` shows these dot files)
```

It stops after the checkout, before installing anything (exit 2), when:

- the venv's Python is older than the framework needs. For your repo's `.venv`, delete it
  (`rm -rf .venv`) and run the script again: it makes a new one with a Python that qualifies;
- no Python new enough is installed: install Python 3.12 or later (python.org, Homebrew, your
  package manager, or `uv python install 3.12`), or pass `--python` with the path of one.

It refuses, and changes nothing, when:

- the framework folder exists but is not a git checkout (exit 3);
- the checkout has local changes: a changed file, or a file git does not ignore that is not part of
  the framework (exit 3). It is a read-only copy: move or discard the changes, and report the bug
  they were fixing as an issue;
- the pin is a branch, a short commit, or a tag the repository does not have (exit 2).

Exit 4 is a git or network failure, 5 a failed install. Options:

| Option | Does |
|---|---|
| `--home DIR` (or `FRAMEWORK_CHECKOUT`) | put the checkout somewhere else |
| `--remote URL` (or `FRAMEWORK_REMOTE`) | clone from another URL, such as a mirror |
| `--check` | change nothing; exit 0 only when the checkout is at the pin, detached, with no local changes |
| `--dry-run` | change nothing; say what it would do |
| `--no-install` | do not make a venv or install into one |
| `--python PATH` | make `.venv` with this Python, when there is no venv yet |
| `--find-links DIR` | install from a folder of wheels, with no package index (below) |
| `--offline` | install with no package index, using only what the venv already has |

Run the script again whenever `framework.pin` changes (after a `git pull`, for example). When
nothing changed it does nothing.

**What it downloads.** Besides cloning the framework, the install step downloads the framework's
Python dependencies (database drivers, Google Cloud and Kafka clients, pytest) and its build backend
(`setuptools`) from your package index, as any `pip install` does. On a machine without access to
a package index, build a folder of wheels once on a machine that has it, copy it over, and install
from it:

```
# on a connected machine, next to a framework checkout at the same pin, with the same Python
# version as the offline machine's .venv
python3.12 -m pip download -d wheelhouse "setuptools>=68" ./striim-testing-framework-utility

# on the offline machine
python3 scripts/sync-framework.py --remote <a reachable clone or mirror> --find-links wheelhouse
```

Use `--offline` instead when the venv already holds every dependency and `setuptools` (for example,
a prepared image). Running tests later downloads more: Docker images for the services a test
requires and, in Docker mode, the Striim packages ([RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md)).

## 4. Your settings

The sync script made `.env` from `.env.example`. Both start with a dot, so `ls` and Finder hide
them; `ls -a` shows them. Open `.env` and choose where Striim comes from, exactly as in [RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md),
step 3: your own Striim server in `.env`, or Docker mode with your own license in your shell. The `.env` in
your repo's root is the one that is read. Keep license keys and passwords out of git: in `.env`
(which `.gitignore` excludes), your shell, or a machine-wide settings file (below).

## 5. Check and run

```
.venv/bin/striim-test doctor --targets gold-targets.yaml --case tests/live/orders-cdc
.venv/bin/striim-test list --targets gold-targets.yaml
.venv/bin/striim-test run --targets gold-targets.yaml
```

`list` prints `live:tests/live/orders-cdc::orders-cdc`. Run directories go to `.state/runs/`
(`stateDir:` in `gold-targets.yaml`). With the venv activated, `striim-test` alone is enough.

## 6. Add your tests

One folder per test under `tests/live/`. [WRITING-TESTS.md](WRITING-TESTS.md) walks through one;
[TEST-YAML.md](TEST-YAML.md) is the reference. In your repo:

- `example:` and `jar:` paths are relative to your repo's root, so your Open Processor and UDF
  modules live here too ([TESTING-YOUR-JAVA.md](TESTING-YOUR-JAVA.md)).
- Integration cases go under a second suite, `suites.integration: tests/integration`
  ([INTEGRATION-TESTS.md](INTEGRATION-TESTS.md)).
- Services the framework does not ship go under `services/`, listed in `servicesRoots:`
  ([YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md)).

**Several case roots.** To run another tree's cases alongside your own (for example the framework's
stock service cases), give `suites.live` a list. The first entry is your own case root, relative to
the manifest; further entries may be relative or absolute and may use `$VAR`/`${VAR}`, and an entry
whose variable is unset is left out. `SLT_FRAMEWORK_HOME` is the exception: when it is not set at all, it names
the framework checkout `striim-test` runs from, so the example below works without setting it (set
to an empty value, it leaves the entry out):

<!-- snippet: shape a suites.live list, not a whole gold-targets.yaml -->
```yaml
suites:
  live:
    - tests/live
    - ${SLT_FRAMEWORK_HOME}/scripts/live/regression/services
```

With path settings instead of a manifest, list the roots in `SLT_LIVE_CASES`, separated by `:` (`;`
on Windows). Case names must be unique across the roots, two roots may not share a folder name, and
a root may not lie inside another; [internals/ENGINE.md](internals/ENGINE.md), "Several live case
roots", has the rules.

## Change the framework version

```
echo v0.2.0 > framework.pin
python3 scripts/sync-framework.py
.venv/bin/striim-test run --targets gold-targets.yaml
git commit -am "Framework v0.2.0"
```

Everyone who pulls that commit runs the sync script once.

## Found a framework bug?

Report it as an issue on the framework's repository, with:

- your `framework.pin`;
- the output of `striim-test doctor --targets gold-targets.yaml --case <the case>`;
- the failing run's `evidence.json` and `stdout.log` from its run directory, with anything you
  cannot share (host names, data) removed.

Do not patch the framework checkout: the next sync refuses to touch a changed checkout, and your
tests would be running code nobody else has.

## Run it in CI

```
python3 scripts/sync-framework.py
.venv/bin/striim-test run --targets gold-targets.yaml
```

- The runner needs Python 3.12 or later installed; the sync script finds it and makes `.venv`.
  When the runner has no `.env`, the script makes one from `.env.example`. Settings the CI system
  sets as environment variables win over it.
- Run the sync script, or `sync-framework.py --check` on a runner that keeps the checkout; either
  fails when the checkout is not exactly the pin.
- **Fail the job on any exit code but 0.** Exit 3 means tests were skipped because something they
  needed was missing: nothing was checked.
- Keep the run directory (`.state/runs/<time>-<id>/`) as a build artifact: `live/junit.xml` for the
  CI's test report, `evidence.json` for failures.
- License keys and passwords come from the CI system's secret store, as environment variables.
- Keep `SLT_INFRA_OWNERSHIP=shared` on a runner other jobs use.
- Docker mode builds a 22 GB Striim image on first use. On a runner that starts empty, keep the
  installers somewhere and use `SLT_STRIIM_DEPS_MANIFEST` to skip the 6.3 GB download, or run
  against a Striim server of your own.
- `--parallel` runs three tests at a time.

## Several checkouts or people on one machine

Settings come from three layers, highest first: your shell, your repo's `.env`, then a machine-wide
file, `${XDG_CONFIG_HOME:-$HOME/.config}/striim-test/machine.env` (or the path in `SLT_MACHINE_ENV`).
The machine file uses the same `KEY=VALUE` format. Put shared things there, and protect it with
`chmod 600`:

```
# machine.env
SLT_STRIIM_PRIMARY_CPUS=4
SLT_STRIIM_NODE_CPUS=4
SLT_STRIIM_MEM_MAX=3072m
SLT_STRIIM_MEM_LIMIT=5g
```

License keys (`COMPANY_NAME`, `CLUSTER_NAME`, `PRODUCT_KEY`, `LICENCE_KEY`) belong only in the
machine file or your shell; their values are never printed. Settings that tell one checkout from
another are refused in the machine file and belong in each checkout's `.env`: stack prefixes,
`*_HOST_PORT` ports, `STRIIM_URL` and paths. For a second, independent stack on the same machine:

```
SLT_STACK_PREFIX=checkout-a
INT_STACK_PREFIX=checkout-a
SLT_STRIIM_HTTP_HOST_PORT=30023
STRIIM_URL=http://localhost:30023
```

Every command run from that checkout then uses that stack: `striim-test`, and the
`livetest.cli start`/`stop` commands, which stop only the containers of the prefix they are given.

Size the CPU caps so every concurrent node fits the license's CPU count (three two-node clusters at
four CPUs per node request 24), and leave memory for every node, the service containers and the
host; the heap (`MEM_MAX`) must fit below the container limit (`MEM_LIMIT`).

## Without the template

**A worked consumer repo inside this checkout:** [examples/acme-retail](../examples/acme-retail/README.md)
keeps its own `gold-targets.yaml`, apps and tests. Run the installed `striim-test` from the framework
root with `--targets examples/acme-retail/gold-targets.yaml`; paths in that manifest resolve from
the example directory. Do not run its sync script here: its default destination would clone a second framework
checkout into `examples/`. Only an explicit `--home` or `FRAMEWORK_CHECKOUT` pointing at the
enclosing checkout would detach it at the pin (or refuse local changes). Use sync after copying
the example out into a separate repo, with its own framework checkout next to it.

To try the framework against your tests quickly, without a repo of your own:

- **From a framework clone**, set `SLT_PROJECT_ROOT=/path/to/your-repo` and
  `SLT_LIVE_CASES=/path/to/your-repo/tests/live` in the clone's `.env`, and run
  `striim-test run /path/to/your-repo/tests/live/<case>`. When you export `SLT_PROJECT_ROOT` in your
  shell, the `.env` is read from that folder instead of the clone.
- **At a tag, by hand:**
  `git clone --depth 1 --branch <tag> https://github.com/striim/striim-testing-framework-utility.git`
  next to your repo, then `pip install -e` it into your venv. This also assumes the maintainers tag
  releases. A plain `pip install` of the package is not enough: the services, compose files and
  Java harness are not part of the Python package.
