#!/usr/bin/env python3
"""Write docs/SETTINGS.md: every setting the framework reads, where it may be set, and what it does.

Run it with the test environment: `.venv-test/bin/python tools/settings_reference.py`. `--check`
changes nothing and exits 1 when docs/SETTINGS.md is out of date. Which files may hold a setting
comes from livetest.paths and the service definitions. Each service's settings and Docker defaults
come from its service.yaml and compose file. Every other setting is described below; one with no
description stops the build, naming it.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("docs/SETTINGS.md")
for _sub in ("scripts/cli", "scripts/integration", "scripts/live"):
    sys.path.insert(0, str(ROOT / _sub))
from livetest import paths  # noqa: E402

# Core settings: (name, default, what it does). A default of None is read from a compose file.
SECTIONS = (
    ("Striim", (
        ("STRIIM_URL", "unset: Docker mode",
         "Root URL of your own Striim server, with no path. Unset, a run builds and starts a Striim "
         "cluster in Docker."),
        ("STRIIM_USER", "`admin`", "A Striim user allowed to create and drop apps."),
        ("STRIIM_PASS", "the Docker cluster's, `striim`", "That user's password."),
        ("STRIIM_PASSWORD", "", "Another name for `STRIIM_PASS`."),
        ("STRIIM_API_TIMEOUT", "`10,600`",
         "Timeout on Striim REST calls, in seconds: `600` (read), `10,600` (connect,read), or `0` for "
         "none. Login and read-only polls use `10,120`; jar LOAD and UNLOAD use none."),
        ("STRIIM_HOME", "unset: Striim 5.4.2",
         "A Striim install. Docker mode builds its image from this install's release and reads the "
         "license from its `conf/startUp.properties`; Open Processor and UDF jars compile against it."),
        ("COMPANY_NAME", "", "Docker mode's license, with the next three, when `STRIIM_HOME` is unset or "
                             "has no license: `CompanyName` in `startUp.properties`."),
        ("CLUSTER_NAME", "", "License: `WAClusterName`."),
        ("PRODUCT_KEY", "", "License: `ProductKey`."),
        ("LICENCE_KEY", "", "License: `LicenceKey`."),
        ("SLT_STRIIM_VIEW_HOST", "`localhost`; `host.docker.internal` for the Docker cluster",
         "The host Striim reaches every service by. Set it when your Striim reaches them under "
         "another name, such as a Striim in a Docker container of your own."),
        ("SLT_SERVICES_HOST", "`localhost`",
         "The host the Docker cluster is reached on. With `STRIIM_URL` unset, Striim's URL is "
         "`http://<this host>:9080`."),
        ("SLT_STRIIM_NATIVE_ONLY", "unset", "`1`: use only the Striim at `STRIIM_URL`; fail rather than "
                                            "start a Striim in Docker."),
        ("SLT_STRIIM_BOOT_WAIT", "`300`", "Seconds to wait for a starting Docker cluster to accept a "
                                          "login."),
        ("SLT_CLUSTER_SETTLE", "`20`", "Seconds to let a just-formed Docker cluster settle before the "
                                       "first deploy."),
    )),
    ("Ownership and cleanup", (
        ("SLT_INFRA_OWNERSHIP", "unset: a run refuses to start",
         "`shared`: reuse what is running, start what is missing, never tear it down (needs "
         "`SLT_KEEP_SERVICES=1`). `exclusive`: take a fresh stack, refuse if a Striim already answers, "
         "and tear everything down at the end."),
        ("SLT_KEEP_SERVICES", "unset", "`1`: keep the cluster and service containers after the run. "
                                       "Required with `shared`."),
        ("SLT_KEEP_RESOURCES", "unset", "`1`: keep each test's app, tables and slots whatever the result "
                                        "(`--keep-resources`)."),
        ("SLT_KEEP_RESOURCES_ON_ERROR", "unset",
         "`1`: the same, for failed tests only. Replication slots are dropped; the teardown prints the "
         "SQL that recreates them."),
        ("SLT_ALLOW_NO_CLUSTER", "unset", "`1`: do not fail a run in which every case skipped for missing "
                                          "infrastructure."),
    )),
    ("Where things are", (
        ("GOLD_TARGETS", "unset: the framework clone's own cases",
         "Your project manifest, `gold-targets.yaml`; the same as `--targets`."),
        ("SLT_PROJECT_ROOT", "the clone root",
         "The project: case ids, `example:` and `jar:` paths are relative to it, and its `.env` is "
         "the one read. Only the shell's value says where that `.env` is."),
        ("SLT_FRAMEWORK_HOME", "the clone's `scripts/`", "The framework checkout."),
        ("SLT_LIVE_CASES", "`scripts/live/regression`",
         "The live case root, or several separated by `:` (`;` on Windows). `striim-test run PATH` "
         "runs only cases under it."),
        ("SLT_INT_CASES", "the clone's integration cases", "The integration case root."),
        ("SLT_SERVICES_DIR", "`scripts/live/services`", "Live service definitions."),
        ("SLT_INT_SERVICES_DIR", "`scripts/integration/services`", "Integration service definitions."),
        ("SLT_STATE_DIR", "`scripts/live`", "Run directories and coordination files."),
        ("SLT_MACHINE_ENV", "`${XDG_CONFIG_HOME:-$HOME/.config}/striim-test/machine.env`",
         "The machine-wide settings file."),
        ("SLT_FRAMEWORK_DOTENV", "the clone's `.env`",
         "A file to read as the framework clone's `.env`, when `SLT_PROJECT_ROOT` is not set."),
        ("SLT_LOCK_DIR", "`/tmp/slt-locks`",
         "The coordination directory every run on the machine shares (mode 1777)."),
    )),
    ("Running", (
        ("SLT_PARALLEL", "unset", "`1`: allow pytest-xdist (`-n`) and give each test its own name tokens. "
                                  "`striim-test run --parallel` sets it."),
        ("SLT_RUN_DISABLED", "unset", "`1`: run tests marked `disabled:` or `disabled_parallel:`."),
        ("SLT_SKIP_VERIFY", "unset",
         "`1`: run each test until it is RUNNING with its data flowed, then skip every assertion and "
         "the `recover:` phase and report it skipped. For debugging by hand, with "
         "`SLT_KEEP_RESOURCES=1`."),
        ("SLT_EMULATORS", "unset", "`1`: opt in to every service that declares `opt_in_env`."),
        ("SLT_PRE_UP", "unset", "`0`: turn off every service's `pre_up` hook."),
        ("SLT_OP_RETRY_FAILED", "unset", "`1`: retry loading an Open Processor jar that already failed "
                                         "twice at the same bytes in this run."),
        ("SLT_RECOVER_RESTORE_TIMEOUT", "`600`", "Seconds a `recover:` phase allows for its restore."),
        ("SLT_STACK_PREFIX", "unset", "Run a second, independent stack on the same machine: its "
                                      "containers and cleanup carry this prefix."),
        ("SLT_JDK<release>_HOME", "on macOS, the JDK `java_home` finds",
         "The JDK that builds Open Processor and UDF jars for that Java release, for example "
         "`SLT_JDK17_HOME`. Off macOS the JDK comes only from here."),
    )),
    ("The Docker Striim cluster", (
        ("SLT_STRIIM_PRIMARY_CPUS", None,
         "CPUs the primary node may use; `0` is no limit. The license caps the CPUs a cluster counts "
         "(24 in the tested licenses), and each node counts every CPU it sees, so on a machine with "
         "more than 12 CPUs cap both nodes, for example 12 each."),
        ("SLT_STRIIM_NODE_CPUS", None, "CPUs the second node may use; `0` is no limit."),
        ("SLT_STRIIM_MEM_MAX", None, "Java heap of each node. Lower it on a modest machine or before "
                                     "running several stacks."),
        ("SLT_STRIIM_MEM_LIMIT", None, "Memory limit of each node's container; `0` is no limit."),
        ("SLT_STRIIM_JAVA_SYSTEM_PROPERTIES", None,
         "Extra Java system properties for the nodes, for example `-Djava.awt.headless=true`."),
        ("SLT_STRIIM_DEPS_MANIFEST", "unset",
         "A manifest of the Striim installers you already have, to skip the 6.3 GB download "
         "([RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md) has the format)."),
        ("SLT_STRIIM_MIN_FREE_GB", "`35`", "Free Docker disk, in GB, a first image build requires."),
        ("SLT_STRIIM_HTTP_HOST_PORT", None, "Host port of the console and REST API."),
        ("SLT_STRIIM_HTTPS_HOST_PORT", None, "Host port of HTTPS."),
        ("SLT_STRIIM_JMX_HOST_PORT", None, "Host port of the primary's JMX Prometheus exporter."),
        ("SLT_STRIIM_NODE_JMX_HOST_PORT", None, "Host port of the second node's JMX exporter."),
        ("SLT_STRIIM_AGENT_JMX_HOST_PORT", None, "Host port of the agent's JMX exporter."),
        ("SLT_STRIIM_DEBUG_HOST_PORT", None, "Host port of the primary's JDWP remote debugger."),
    )),
    ("Integration tier", (
        ("INT_STACK_PREFIX", "unset", "Run a second integration stack on the same machine, with its own "
                                      "containers and state."),
        ("INT_KEEP_SERVICES", "unset", "`1`: leave the service containers running after the session, for "
                                       "fast local iteration."),
        ("INT_KEEP_RESOURCES", "unset", "`1`: keep each case's resources after it runs."),
        ("INT_EMULATORS", "unset", "`1`: opt in to every integration service that declares "
                                   "`opt_in_env`."),
        ("INT_VARIANT", "unset: every variant",
         "Run only this variant of each case, to iterate on one engine without starting every "
         "database. A name no case defines runs nothing."),
        ("INT_EXTRA_CLASSPATH", "unset", "Jars added to the harness classpath, such as a JDBC driver kept "
                                         "elsewhere."),
        ("INT_PG_ALLOW_DESTRUCTIVE_RESET", "unset",
         "`1`: allow the Postgres reset, a cascading drop of the test schemas, on a database other than "
         "the sandbox `intdb`."),
        ("INT_ALLOW_CONCURRENT_SESSIONS", "unset", "`1`: allow a second integration session on the same "
                                                   "stack at the same time."),
        ("INT_SHARED_SERVICES", "unset", "Reserved: a broker owns the services, and the session does not "
                                         "tear them down."),
    )),
)
# Service settings no service.yaml field explains.
SERVICE_EXTRAS = {
    "SLT_MSSQL_BOOTSTRAP_PASSWORD": ("`QAtestuser1`",
                                     "The strong `sa` password the container boots with; the setup then "
                                     "sets the test password."),
    "SLT_GCS_EXTERNAL_URL": (None, "The URL the emulator gives out for its own objects."),
    "INT_GCS_EXTERNAL_URL": (None, "The URL the emulator gives out for its own objects."),
    "SLT_GCS_PUBLIC_HOST": (None, "The host name the emulator gives out for its own objects."),
}
# Set by the framework for its own processes: (names, what they carry).
FRAMEWORK_SET = (
    (("SLT_FRAMEWORK_MODE",), "how `striim-test` started the engine"),
    (("SLT_RUN_EPOCH", "SLT_RUN_IDENTITY"), "the run's id and its `identity.json`, shared by every engine "
                                            "process of one run"),
    (("SLT_INVOCATION_ID",), "the id of one engine process, recorded in its evidence"),
    (("STRIIM_TEST_GUARD",), "marks a pytest that `striim-test` started"),
    (("SLT_OPS_PRELOADED", "SLT_PREFLIGHT_ATTEMPT", "SLT_PREFLIGHT_CLEANUP_SERVICES",
      "SLT_PREFLIGHT_OUTCOMES_PATH"), "the pre-flight that loads shared Open Processors once before "
                                      "parallel runs"),
    (("SLT_SERVICE_DIR", "SLT_LIVE_SERVICE_DIR", "SLT_FRAMEWORK_PYTHON"),
     "given to a service's `pre_up` hook ([YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md))"),
    (("SLT_LIFECYCLE_FAULT",), "fault injection for the framework's own lifecycle tests"),
)
RETIRED = {"SLT_MODE": "SLT_FRAMEWORK_MODE", "SLT_FRAMEWORK": "SLT_FRAMEWORK_HOME"}
# Names the code holds that are not settings: tokens it fills in itself.
NOT_SETTINGS = {"STRIIM_RELEASE", "STRIIM_VERSION", "STRIIM_SERIES", "STRIIM_WEB_URL", "TID"}

NAMES = {"postgres": "PostgreSQL", "oracle": "Oracle", "mssql": "SQL Server", "sqlserver": "SQL Server",
         "mysql": "MySQL", "vertica": "Vertica", "kafka": "Kafka", "spanner": "Spanner emulator",
         "gcs": "GCS emulator", "teradata": "Teradata", "servicenow": "ServiceNow"}
FIELDS = {
    "host": "your own instance's host: set it to use that instead of the container",
    "port": "its port", "dbname": "the database", "database": "the database", "bucket": "the bucket",
    "admin_user": "the admin account, which makes the test accounts and schemas and cleans up",
    "admin_password": "the admin account's password",
    "user": "the account the tests connect as", "password": "that account's password",
    "source_user": "the account a test's source side uses", "source_password": "its password",
    "source_schema": "the schema source tables are made in",
    "target_user": "the account a test's target side uses", "target_password": "its password",
    "target_schema": "the schema target tables are made in",
    "cdc_user": "the account CDC reads with", "cdc_password": "its password",
    "service": "service name of the pluggable database", "cdb_service": "service name of the container "
                                                                          "database",
    "project": "the project id", "instance": "the instance", "gsql_db": "GoogleSQL-dialect database",
    "pg_db": "PostgreSQL-dialect database", "admin_port": "its REST admin port",
    "src_bucket": "the bucket source objects go in", "tgt_bucket": "the bucket target objects go in",
    "src_topic": "the source topic", "tgt_topic": "the target topic",
    "broker_port": "the broker port Striim connects to", "registry_port": "schema registry port",
    "zookeeper_port": "ZooKeeper client port", "token_port": "the fake OAuth token server's port",
    "scheme": "`http` or `https`", "client_id": "OAuth client id", "client_secret": "OAuth client secret",
}
SUFFIXES = (
    ("_VIEW_HOST", "The host Striim reaches it by, when that differs from this machine's."),
    ("_HOST_PORT", "Host port the container publishes; change it when the port is in use."),
    ("_CLIENT_PORT", "Host port the container publishes; change it when the port is in use."),
    ("_CPUS", "CPUs the container may use."),
    ("_MEM_LIMIT", "Memory limit of the container."),
)
LANES = "Paths, ports, the stack prefix and `STRIIM_URL`"
CORE = {name for _, rows in SECTIONS for name, _, _ in rows}


def compose_defaults(text: str) -> dict:
    """NAME -> default for each ``${NAME:-default}`` or ``${NAME-default}``; a default may hold
    another ``${...}``."""
    out = {}
    for m in re.finditer(r"\$\{([A-Z][A-Z0-9_]*):?-", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        out.setdefault(m.group(1), text[m.end():i - 1])
    return out


def code(value) -> str:
    """A value read from a service or compose file, as code; nothing for no value."""
    return "" if value is None or str(value) == "" else f"`{value}`"


def service_settings(services: Path) -> list:
    """[(title, [(name, default, text)])] for every service folder under ``services``."""
    from livetest.service_env import compose_keys
    out = []
    for file in sorted(services.glob("*/service.yaml")):
        raw = yaml.safe_load(file.read_text()) or {}
        compose = file.parent / raw["compose"] if raw.get("compose") else None
        text = compose.read_text() if compose and compose.is_file() else ""
        defaults, compose_vals = raw.get("docker_defaults") or {}, compose_defaults(text)
        rows, seen = [], set()

        def add(name, default, what):
            if name and name not in seen and name not in CORE:
                seen.add(name)
                rows.append((name, default, what))
        for field, name in (raw.get("live_env") or {}).items():
            what = FIELDS.get(field) or field.replace("_", " ")
            add(name, code(defaults.get(field)), "Your own instance: " + what + "." if field != "host"
                else what[0].upper() + what[1:] + ".")
        for field, name in (raw.get("docker_env") or {}).items():
            add(name, code(compose_vals.get(name, defaults.get(field))),
                "Host port the container publishes" + ("" if field == "port" else " for "
                + (FIELDS.get(field) or field).removeprefix("its ")) + "; change it when the port is in use.")
        for name in (raw.get("opt_in_env"), *sorted(compose_keys(text))):
            if (name and name not in seen and name not in CORE and name not in NOT_SETTINGS
                    and file.parent.name != "striim"):
                add(name, code(compose_vals.get(name)), describe(name)[1])
        out.append((file.parent.name, rows))
    return out


def describe(name: str) -> tuple:
    if name in SERVICE_EXTRAS:
        return SERVICE_EXTRAS[name]
    for suffix, what in SUFFIXES:
        if name.endswith(suffix):
            return None, what
    raise SystemExit(f"settings_reference: no description for {name}; add one to tools/settings_reference.py")


def where(name: str, allowed: set) -> str:
    if name not in allowed:
        return "shell"
    if name in paths.LICENCE_KEYS:
        return "shell, machine"
    return "shell, `.env`" + ("" if paths.lane_key(name) else ", machine")


def cell(value) -> str:
    return "" if value is None else str(value)


def table(rows, allowed, header="Default") -> list:
    lines = [f"| Setting | Where | {header} | What it does |", "|---|---|---|---|"]
    for name, default, what in rows:
        lines.append(f"| `{name}` | {where(name, allowed)} | {cell(default)} | {what} |")
    return lines


def build() -> str:
    from livetest.service_env import declarations
    declared = {k for k in declarations({"SLT_FRAMEWORK_DOTENV": os.devnull}) if k not in NOT_SETTINGS}
    allowed = set(paths.KEYS) | set(paths.SERVICE_KEYS) | set(paths.LICENCE_KEYS) | declared
    striim = compose_defaults((ROOT / "scripts/live/services/striim/compose.yaml").read_text())
    live = [s for s in service_settings(ROOT / "scripts/live/services") if s[0] != "striim"]
    integration = service_settings(ROOT / "scripts/integration/services")
    listed = {n for _, rows in SECTIONS for n, _, _ in rows}
    listed |= {n for _, rows in live + integration for n, _, _ in rows}
    # Service settings no service file names (view hosts, extras): file them under their service.
    prefixes = {"PG": "postgres", "POSTGRES": "postgres", "ORA": "oracle", "ORACLE": "oracle",
                "MSSQL": "mssql", "MYSQL": "mysql", "VERTICA": "vertica", "KAFKA": "kafka",
                "SCHEMA": "kafka", "ZOOKEEPER": "kafka", "SPANNER": "spanner", "GCS": "gcs",
                "TOKEN": "gcs", "TERADATA": "teradata", "SERVICENOW": "servicenow"}
    for name in sorted((allowed | set(SERVICE_EXTRAS)) - listed - set(paths.LICENCE_KEYS)):
        tier, _, rest = name.partition("_")
        service = prefixes.get(rest.split("_")[0])
        group = live if tier == "SLT" else integration
        match = [rows for svc, rows in group if svc == service or (svc, service) == ("sqlserver", "mssql")]
        if not service or not match:
            raise SystemExit(f"settings_reference: {name} belongs to no section; describe it in "
                             "tools/settings_reference.py")
        default, what = describe(name)
        match[0].append((name, default or "", what))
    out = [
        "# Settings", "",
        "<!-- Generated by tools/settings_reference.py; edit that file, not this one. -->", "",
        "Every setting the framework reads: where you can set it, its default, and what it does.",
        "`tests/test_settings_reference.py` fails when the code reads a setting this page does not list.", "",
        "## Where settings come from", "",
        "Highest first: your shell's environment, then `.env` in your project root (in a framework "
        "clone, its root), then the machine file `${XDG_CONFIG_HOME:-$HOME/.config}/striim-test/machine.env` "
        "(or the file `SLT_MACHINE_ENV` names). The **Where** column says which of them a setting is read from:", "",
        "- **shell**: only from the environment. In `.env` it is not read.",
        "- **`.env`**: from the environment or `.env`.",
        "- **machine**: from the machine file too. " + LANES + " are never read from it, so two "
        "checkouts on one machine stay apart; license keys are read from the shell or the machine file "
        "only, never `.env`.", "",
        "Start from your repository's `.env.example`. `striim-test doctor` lists the settings that are "
        "set and where each came from.", "",
    ]
    for title, rows in SECTIONS:
        if title == "Integration tier":
            continue
        out += [f"## {title}", ""]
        out += table([(n, code(striim.get(n)) if d is None else d, w) for n, d, w in rows], allowed)
        out.append("")
    out += ["## Services", "",
            "To use your own database instead of the container the framework starts, set its host (the "
            "first row); the other settings default to the container's values, shown here. "
            "[SERVICES.md](SERVICES.md) has each service's accounts, tokens and routes.", ""]
    for svc, rows in live:
        out += [f"### {NAMES.get(svc, svc)} (`{svc}`)", ""] + table(rows, allowed, "Docker default") + [""]
    title, rows = next(s for s in SECTIONS if s[0] == "Integration tier")
    out += [f"## {title}", "", "The integration engine's own settings "
            "([INTEGRATION-TESTS.md](INTEGRATION-TESTS.md)), then its services'.", ""]
    out += table(rows, allowed) + [""]
    for svc, rows in integration:
        out += [f"### {NAMES.get(svc, svc)}, integration (`{svc}`)", ""]
        out += table(rows, allowed, "Docker default") + [""]
    out += ["## Set by the framework", "",
            "The framework sets these for its own processes. Do not set them.", "",
            "| Names | What they carry |", "|---|---|"]
    out += [f"| {', '.join(f'`{n}`' for n in names)} | {what} |" for names, what in FRAMEWORK_SET]
    out += ["", "## Retired names", "",
            "A run refuses these and names the setting that replaced them.", "",
            "| Retired | Use |", "|---|---|"]
    out += [f"| `{old}` | `{new}` |" for old, new in sorted(RETIRED.items())]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="exit 1 when docs/SETTINGS.md is out of date")
    args = p.parse_args(argv)
    text, target = build(), ROOT / OUTPUT
    if args.check:
        if not target.is_file() or target.read_text() != text:
            print(f"{OUTPUT} is out of date: run tools/settings_reference.py", file=sys.stderr)
            return 1
        return 0
    target.write_text(text)
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
