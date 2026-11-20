"""Reference and getting-started passages that state a fact the code decides: each test reads the
fact from the code and checks the docs say it."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _doc(rel: str) -> str:
    return (REPO / rel).read_text()


def _flat(rel: str) -> str:
    return " ".join(_doc(rel).split())


def _section(text: str, heading: str) -> str:
    """From ``heading`` to the next heading of the same or a higher level."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    nxt = re.compile(rf"^#{{1,{level}}} ", re.M)
    m = nxt.search(text, start + len(heading))
    return text[start:m.start() if m else len(text)]


# ---- ${TID} ------------------------------------------------------------------------------

def test_live_tid_is_never_empty_and_the_docs_say_so_per_tier():
    from inttest import tokens as int_tokens
    from livetest import runident
    serial = runident.tokens(runident.derive("orders-cdc", {runident.RUN_EPOCH_ENV: "run-1"}))
    again = runident.tokens(runident.derive("orders-cdc", {runident.RUN_EPOCH_ENV: "run-2"}))
    assert re.fullmatch(r"t[0-9a-f]{9}_", serial["TID"]) and serial["TID"] != again["TID"]
    assert int_tokens.isolation_tokens(parallel=False) == {"TID": "", "TID_ORACLE": ""}
    assert re.fullmatch(r"t[0-9a-f]{9}_", int_tokens.isolation_tokens(parallel=True)["TID"])

    rules = " ".join(_section(_doc("docs/TEST-YAML.md"), "## Parallel-safety rules").split())
    engine = _flat("docs/internals/ENGINE.md")
    start = _flat("docs/START-HERE.md")
    for text in (rules, engine, start):
        assert "never empty" in text
        assert "Empty in a serial run" not in text and "are empty in a serial run" not in text
    assert 'as `""`' not in rules and "sha256 of the test name" not in rules
    assert "hash of the test name" not in engine
    assert "integration" in rules and "empty in a serial integration run" in rules


# ---- sample 17 ---------------------------------------------------------------------------

def test_sample_17_names_its_gating_setting_where_the_prerequisites_are_listed():
    m = yaml.safe_load(_doc("samples/live/17-one-node-failover/test.yaml"))
    assert m["recover"]["node"]
    plugin = _doc("scripts/live/livetest/plugin.py")
    assert 'not paths.flag("SLT_ENABLE_NODE_FAULTS")' in plugin
    sample = _flat("samples/live/17-one-node-failover/README.md")
    assert "does not require port 9082" in sample
    for rel in ("README.md", "docs/RUN-YOUR-FIRST-TEST.md"):
        text = _flat(rel)
        assert "SLT_ENABLE_NODE_FAULTS=1" in text, rel
        assert "dedicated disposable stack" in text, rel
        assert "survivor API" not in text, rel


# ---- building by hand --------------------------------------------------------------------

def test_by_hand_build_recipe_gives_the_engines_series_for_three_and_four_part_releases():
    from livetest import releases
    doc = _doc("docs/TESTING-YOUR-JAVA.md")
    assert "${ver%.*}" not in doc
    line = re.search(r"^series=.*$", doc, re.M).group(0)
    for ver in ("5.4.2", "5.4.0.6", "5.4.4.0", "5.2.0.8"):
        got = subprocess.run(["bash", "-c", f'ver=$1; {line}; echo "$series"', "_", ver],
                             capture_output=True, text=True, check=True).stdout.strip()
        assert got == releases._series_of(ver), ver
    assert "-DSTRIIM_SERIES=$series" in doc


# ---- Docker mode memory ------------------------------------------------------------------

def test_docker_mode_memory_is_in_the_getting_started_docs():
    compose = _doc("scripts/live/services/striim/compose.yaml")
    heap = re.search(r"MEM_MAX: \$\{SLT_STRIIM_MEM_MAX:-(\d+)m\}", compose).group(1)
    assert compose.count("mem_limit: ${SLT_STRIIM_MEM_LIMIT:-0}") == 3          # three JVMs, no limit
    step = " ".join(_section(_doc("docs/RUN-YOUR-FIRST-TEST.md"), "## 3b. Docker mode").split())
    assert "SLT_STRIIM_MEM_MAX" in step and "SLT_STRIIM_MEM_LIMIT" in step
    assert f"{heap}m" in step and "three" in step
    for rel in ("README.md", "docs/START-HERE.md"):
        assert re.search(r"memory", _doc(rel), re.I), rel


# ---- op.on_agent -------------------------------------------------------------------------

def test_every_op_entry_key_is_in_the_reference_with_on_agent_explained():
    from livetest import manifest
    section = _section(_doc("docs/TEST-YAML.md"), "## `op:` / `udf:` — building/uploading OpenProcessor or UDF jars")
    for key in sorted(manifest._MODULE_ENTRY_KEYS):
        assert f"- `{key}` (" in section, key
    flat = " ".join(section.split())
    assert "restarts the agent" in flat and "disabled_parallel" in flat
    assert "`op:` only" in flat or "`op:` entries only" in flat


# ---- Teradata ----------------------------------------------------------------------------

def test_what_is_supported_gives_teradata_settings_as_the_service_requires_them():
    raw = yaml.safe_load(_doc("scripts/live/services/teradata/service.yaml"))
    doc = _doc("docs/WHAT-IS-SUPPORTED.md")
    block = doc[doc.index("**Teradata is bring-your-own.**"):]
    block = block[:block.index("`striim-test doctor --case` checks that the host answers.")]
    assert "default dbc" not in block and "defaults to its user's name" not in block
    assert "likewise" not in block
    for name in raw["required_env"]:
        assert re.search(rf"^{name}=\S*\s+# required", block, re.M), name
    for field, env in (("source_schema", "SLT_TERADATA_SOURCE_SCHEMA"),
                       ("target_schema", "SLT_TERADATA_TARGET_SCHEMA")):
        assert re.search(rf"^{env}=\S*\s+# default {raw['docker_defaults'][field]}\b", block, re.M), env
    assert re.search(rf"^SLT_TERADATA_PORT=\S*\s+# default {raw['docker_defaults']['port']}\b", block, re.M)


# ---- build matrix ------------------------------------------------------------------------

def test_the_build_matrix_settings_are_described():
    settings = _doc("docs/SETTINGS.md")
    for name in ("STRIIM_BUILD_<version>", "SLT_BUILD_MATRIX_ENV", "FTC_PUBLISHER_RELEASES_ENV"):
        assert f"| `{name}` |" in settings, name
    java = _flat("docs/TESTING-YOUR-JAVA.md")
    assert "SLT_BUILD_MATRIX_ENV" in java and "STRIIM_BUILD_5_4_2=" in java
    assert "existing `STRIIM_BUILD_<version>` build matrix" not in java
    for rel in ("scripts/live/livetest/opjarrelease.py", "scripts/integration/inttest/opjarrelease.py"):
        assert "console" not in _doc(rel), rel


# ---- keys the loaders accept ---------------------------------------------------------------

def test_fold_names_is_in_the_diff_reference():
    from livetest.assertions import diff
    assert diff.parse_diff_specs([{"source": "a", "target": "b", "fold_names": True}])
    section = " ".join(_section(_doc("docs/TEST-YAML.md"), "### `diff` — source → target propagation").split())
    assert "fold_names: true" in section and "lower-case" in section


def test_integration_assert_tiers_have_their_own_sections():
    from inttest import manifest as im
    guide = _doc("docs/INTEGRATION-TESTS.md")
    for tier in ("expect_log", "monitor", "exception_store"):
        assert tier in im._ASSERT_TIERS
        assert re.search(rf"^### `assert\.{tier}:`", guide, re.M), tier
    log = " ".join(_section(guide, "### `assert.expect_log:`").split())
    assert "1500" in log and "expect_error" in log and "too broad" in log
    src = _doc("scripts/integration/inttest/plugin.py")
    assert "logged[-1500:]" in src


def test_stop_start_cycle_and_loop_interval_defaults_are_in_the_reference():
    src = _doc("scripts/live/livetest/manifest.py")
    defaults = {k: re.search(rf'\.get\("{k}", (\d+)\)', src).group(1)
                for k in ("cycles", "delay_before_stop", "stop_duration", "loop_interval")}
    assert defaults == {"cycles": "1", "delay_before_stop": "3", "stop_duration": "2",
                        "loop_interval": "1"}
    row = next(line for line in _doc("docs/TEST-YAML.md").splitlines() if line.startswith("| `action` |"))
    cycle = row[row.index("**`stop_start_cycle`**"):row.index("**`drop_recreate_app`**")]
    for key, value in defaults.items():
        assert re.search(rf"`{key}`[^`;]{{0,30}}default {value}\b", cycle), key
