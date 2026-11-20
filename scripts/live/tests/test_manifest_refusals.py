"""Manifest refusals, one row per `raise ManifestError` in livetest/manifest.py that no other test
reaches: each malformed key fails the load with its own message instead of loading as something
else (a misspelt action type as no action, a boolean duration as one second, a bare string as a
one-entry list). The id of each row names the refusal it reaches."""
from __future__ import annotations

import pytest

from livetest.manifest import ManifestError, load_manifest

BASE = "name: t\ntql: app.tql\nassert:\n  smoke: true\n"
PRIMARY, SECONDARY, AGENT = "${STRIIM_PRIMARY_SERVER}", "${STRIIM_SECONDARY_SERVER}", "${STRIIM_AGENT_SERVER}"
OP = "op:\n  jar: java/OpenProcessors/StubOp\n"


def _load(tmp_path, extra, *, base=BASE):
    case = tmp_path / "case"
    case.mkdir(parents=True)
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    (case / "alter.tql").write_text("CREATE CQ ${TID}cq INSERT INTO ${TID}out SELECT * FROM ${TID}in;\n")
    (case / "test.yaml").write_text(base + extra)
    return load_manifest(case / "test.yaml")


def _group(**item):
    body = ", ".join(f"{k}: {v}" for k, v in {"name": '"${TID}g"', "flow": '"${NS}.f"', **item}.items())
    return f"deployment_groups:\n  - {{{body}}}\n"


REFUSED = [
    # deployment groups
    pytest.param(_group(servers=f'["{PRIMARY}", "{PRIMARY}"]'),
                 r"invalid-deployment-group: servers requires distinct server tokens", id="group-servers-repeated"),
    pytest.param(_group(servers="[]"),
                 r"invalid-deployment-group: servers requires distinct server tokens", id="group-servers-empty"),
    pytest.param(_group(servers=f'["{PRIMARY}"]', add_after_deploy='"x"'),
                 r"invalid-deployment-group: add_after_deploy requires distinct server tokens",
                 id="group-add-after-deploy-not-a-list"),
    pytest.param(_group(servers=f'["{PRIMARY}"]', add_after_deploy=f'["{PRIMARY}"]'),
                 r"invalid-deployment-group: cannot mix agents and app servers or repeat members",
                 id="group-member-repeated"),
    pytest.param(_group(servers=f'["{AGENT}"]', add_after_deploy=f'["{SECONDARY}"]'),
                 r"invalid-deployment-group: cannot mix agents and app servers or repeat members",
                 id="group-agent-mixed"),
    # durations: a seed's `after:` and the zeroable recover durations
    pytest.param("seed:\n  - {file: s.sql, when: post_start, after: true}\n",
                 r"'seed' entry 'after' must be a duration, got True", id="seed-after-bool"),
    pytest.param("seed:\n  - {file: s.sql, when: post_start, after: [30]}\n",
                 r"'seed' entry 'after' must be a duration, got \[30\]", id="seed-after-list"),
    pytest.param("recover: {mode: stop, settle: true}\n",
                 r"'recover.settle' must be a duration, got True", id="recover-settle-bool"),
    pytest.param("recover: {mode: stop, settle: [5]}\n",
                 r"'recover.settle' must be a duration, got \[5\]", id="recover-settle-list"),
    # ddl/seed file specs
    pytest.param("ddl: 3\n", r"'ddl' must be a string or a list of \{file, db\}", id="ddl-not-a-list"),
    # server_files
    pytest.param("server_files: lib.jar\n", r"'server_files' must be a list of \{file, dest, when, load\} entries",
                 id="server-files-not-a-list"),
    pytest.param("server_files:\n  - {file: [lib.jar], dest: lib.jar}\n", r"'server_files' file must be a string",
                 id="server-files-file-not-a-string"),
    # op/udf module entries and their uploads
    pytest.param("op:\n  - 3\n", r"'op\[0\]' must be a mapping, got 3", id="module-entry-not-a-mapping"),
    pytest.param(OP + "  upload: lookup.json\n", r"'op.upload' must be a list of files or \{from, to\} entries",
                 id="upload-not-a-list"),
    pytest.param(OP + "  upload:\n    - {to: lookup.json}\n", r"'op.upload' entry needs a 'from' filename",
                 id="upload-without-from"),
    pytest.param(OP + "  upload:\n    - {from: lookup.json}\n", r"'op.upload' entry needs a 'to' filename",
                 id="upload-without-to"),
    pytest.param(OP + "  upload:\n    - 3\n", r"'op.upload' entries must be a filename string or \{from, to\}",
                 id="upload-entry-not-a-string-or-mapping"),
    # recover
    pytest.param("recover: {mode: stop, times: 2, expect_running: false}\n",
                 r"'recover.times: 2' requires 'recover.expect_running: true'", id="recover-times-left-stopped"),
    pytest.param("recover: {mode: restart}\n", r"invalid-node-fault: restart requires one node",
                 id="recover-restart-without-node"),
    # actions
    pytest.param("action:\n  - type: stop_strat_cycle\n", r"action\[0\] unknown type 'stop_strat_cycle'",
                 id="action-unknown-type"),
    pytest.param("action: stop_start_cycle\n", r"'action' must be a list of actions", id="action-not-a-list"),
    pytest.param("action:\n  - stop_start_cycle\n", r"action\[0\] must be a dict", id="action-not-a-mapping"),
    pytest.param("action:\n  - cycles: 2\n", r"action\[0\] must have a 'type' key", id="action-without-type"),
    pytest.param("action:\n  - type: stop_start_cycle\n    cycles: 0\n", r"'cycles' must be a positive integer",
                 id="stop-start-cycles-zero"),
    pytest.param("action:\n  - type: stop_start_cycle\n    cycles: -1\n", r"'cycles' must be a positive integer",
                 id="stop-start-cycles-negative"),
    pytest.param("action:\n  - type: stop_start_cycle\n    cycles: 1.5\n", r"'cycles' must be a positive integer",
                 id="stop-start-cycles-float"),
    pytest.param("action:\n  - type: stop_start_cycle\n    cycles: '2'\n", r"'cycles' must be a positive integer",
                 id="stop-start-cycles-string"),
    pytest.param("action:\n  - type: drop_recreate_app\n    app: ''\n",
                 r"action\[0\] 'drop_recreate_app' needs a non-empty 'app' key", id="drop-recreate-empty-app"),
    pytest.param('action:\n  - type: drop_recreate_app\n    app: "${APP}"\n    capture: [3]\n',
                 r"action\[0\] capture\[0\] must be a mapping, got 3", id="capture-not-a-mapping"),
    pytest.param('action:\n  - type: alter_recompile\n    app: "${APP}"\n    file: alter.tql\n'
                 '    server_files:\n      - {file: [trail], dest: trail}\n',
                 r"action\[0\] 'alter_recompile' server_files file and dest must be strings",
                 id="alter-server-files-not-strings"),
    pytest.param("requires: [nosuchservice]\naction:\n  - {type: service_outage, service: nosuchservice, signal: RESTART}\n",
                 r"action\[0\] 'service_outage': unknown service 'nosuchservice'", id="service-outage-unknown-service"),
    # `disabled:` and `example:`
    pytest.param("disabled: 3\n", r"'disabled' must be a string reason or a boolean", id="disabled-number"),
    pytest.param("disabled: [bug-1]\n", r"'disabled' must be a string reason or a boolean", id="disabled-list"),
    pytest.param('disabled: "   "\n', r"'disabled' string must be non-empty", id="disabled-blank"),
    pytest.param("example: 3\n", r"'example' must be a repo-relative directory string", id="example-not-a-string"),
    # kafka_cleanup_topics
    pytest.param('requires: [kafka]\nkafka_cleanup_topics: [""]\n',
                 r"'kafka_cleanup_topics' must be a list of non-empty topic-name strings", id="kafka-cleanup-blank"),
    pytest.param("kafka_cleanup_topics: [t_CHECKPOINT]\n", r"'kafka_cleanup_topics' needs service 'kafka' in 'requires'", id="kafka-cleanup-without-kafka"),
    # blocks validated by their own modules, refused as a ManifestError
    pytest.param("audit: 3\n", r"invalid-audit-spec: .*audit needs exactly version: 1", id="audit-not-version-1"),
    pytest.param("snapshot_observer: 3\n", r"invalid-snapshot-spec: .*unknown snapshot keys or invalid mapping",
                 id="snapshot-observer-not-a-mapping"),
    pytest.param("transactions: 3\n", r"invalid-transaction-seed: .*transactions must be a non-empty ordered list",
                 id="transactions-not-a-list"),
]

ASSERT_REFUSED = [
    pytest.param("  monitor: 3\n", r"'assert.monitor' must be a non-empty list of specs", id="assert-monitor-not-a-list"),
    pytest.param("  placement: 3\n", r"invalid-placement-spec: nonempty list required", id="assert-placement-not-a-list"),
    pytest.param("  data:\n    - {target: t, db: postgres-target, nothing: {window: 5s}}\n",
                 r"invalid-nothing-spec: .*nothing needs window and where", id="assert-nothing-without-where"),
    pytest.param("  file:\n    - {path: /tmp/out.json, raw: {}}\n",
                 r"invalid-raw-file-spec: path must be a file under \$\{OWNED_DIR\}", id="assert-raw-file-outside-owned-dir"),
]


@pytest.mark.parametrize("extra, message", REFUSED)
def test_malformed_key_is_refused_at_load(tmp_path, extra, message):
    with pytest.raises(ManifestError, match=message):
        _load(tmp_path, extra)


@pytest.mark.parametrize("assert_lines, message", ASSERT_REFUSED)
def test_malformed_assert_tier_is_refused_at_load(tmp_path, assert_lines, message):
    with pytest.raises(ManifestError, match=message):
        _load(tmp_path, "", base=BASE + assert_lines)


def test_stop_duration_defaults_to_two_seconds_and_accepts_zero(tmp_path):
    """`action.stop_start_cycle.stop_duration` (TEST-YAML.md): how long the app stays stopped."""
    spec = _load(tmp_path, "action:\n  - type: stop_start_cycle\n").action_specs[0]
    assert (spec["stop_duration"], spec["cycles"]) == (2, 1)
    spec = _load(tmp_path / "zero", "action:\n  - type: stop_start_cycle\n    stop_duration: 0\n").action_specs[0]
    assert spec["stop_duration"] == 0
    spec = _load(tmp_path / "ms", "action:\n  - type: stop_start_cycle\n    stop_duration: 500ms\n").action_specs[0]
    assert spec["stop_duration"] == 0.5


def test_stop_duration_that_is_not_a_duration_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="'stop_duration' must be a duration"):
        _load(tmp_path, "action:\n  - type: stop_start_cycle\n    stop_duration: soon\n")
