"""`action: capture`, `action: alter_recompile`, drop_recreate_app's `capture:` / `stopped_seed:` /
`tokens:`, and `server_files` `load:` sources outside the test dir: the load-time rules
(manifest.py) and the runtime pieces (appactions.py, opartifacts.jar_source, inputs). The same
features run through the real plugin in tests/lifecycle/test_appactions_exec.py."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from livetest import appactions, inputs, opartifacts
from livetest.manifest import ManifestError, load_manifest
from livetest.striim import StriimClient, StriimError
from livetest.substitute import SubstitutionError

POS = "{ContinuationToken[a1]-DocumentTimeStamp[1700000000]-InternalTs[2]-InternalTimeInc[3]}"


def _case(tmp_path: Path, body: str, files: dict | None = None) -> Path:
    d = tmp_path / "case"
    d.mkdir(parents=True)
    (d / "app.tql").write_text("CREATE OR REPLACE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    for name, text in (files or {}).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    (d / "test.yaml").write_text("name: c\ntql: app.tql\nassert: {smoke: true}\n" + textwrap.dedent(body))
    return d / "test.yaml"


SWAP = "CREATE OR REPLACE SOURCE Src USING Global.NewReader (Pos: '${RESTART}') OUTPUT TO S;\n"


# ---- capture: load ---------------------------------------------------------------------------

def test_capture_step_normalizes(tmp_path):
    m = load_manifest(_case(tmp_path, """
        action:
          - type: capture
            token: RESTART
            describe: Src
            field: Source Restart Position
          - {type: capture, token: SEEN, mon: "${APP}", field: input, timeout: 90s, delay_before: 2m}
    """))
    assert m.action_specs == [
        {"type": "capture", "token": "RESTART", "source": "describe", "component": "Src",
         "field": "Source Restart Position", "timeout": None, "delay_before": 0.0},
        {"type": "capture", "token": "SEEN", "source": "mon", "component": "${APP}",
         "field": "input", "timeout": 90.0, "delay_before": 120.0}]


@pytest.mark.parametrize("action, match", [
    ("{type: capture, token: X, field: f}", "exactly one of 'describe:' or 'mon:'"),
    ("{type: capture, token: X, describe: S, mon: S, field: f}", "exactly one of"),
    ("{type: capture, token: x, describe: S, field: f}", r"\[A-Z\]"),
    ("{type: capture, token: X, describe: S}", "non-empty 'field'"),
    ("{type: capture, token: X, describe: '', field: f}", "non-empty component"),
    ("{type: capture, token: X, describe: S, field: f, timeout: 0}", "greater than 0"),
    ("{type: capture, token: X, describe: S, field: f, when: later}", "unknown key"),
    ("{type: capture, token: X, describe: S, field: f, delay_before: soon}", "duration"),
    ("{type: capture, token: NS, describe: S, field: f}", "harness-provided"),
    ("{type: capture, token: PG_SLOT, describe: S, field: f}", "harness-provided"),
])
def test_capture_step_refusals(tmp_path, action, match):
    with pytest.raises(ManifestError, match=match):
        load_manifest(_case(tmp_path, f"action:\n  - {action}\n"))


def test_capture_token_may_not_shadow_tokens_or_repeat(tmp_path):
    with pytest.raises(ManifestError, match="collides with a tokens: token"):
        load_manifest(_case(tmp_path, """
            tokens: {RESTART: ""}
            action: [{type: capture, token: RESTART, describe: S, field: f}]
        """))
    with pytest.raises(ManifestError, match="captured twice"):
        load_manifest(_case(tmp_path / "b", """
            action:
              - {type: capture, token: RESTART, describe: S, field: f}
              - type: alter_recompile
                app: "${APP}"
                file: swap.tql
                capture: [{token: RESTART, describe: S, field: f}]
        """, {"swap.tql": SWAP}))


# ---- drop_recreate_app: load -------------------------------------------------------------------

def test_drop_recreate_app_defaults_are_unchanged(tmp_path):
    m = load_manifest(_case(tmp_path, "action: [{type: drop_recreate_app, app: '${APP}'}]\n"))
    assert m.action_specs == [{"type": "drop_recreate_app", "app": "${APP}", "delay_before_stop": 3.0,
                               "recreate_wait": 5.0, "seed": [], "capture": [], "stopped_seed": [],
                               "tokens": {}}]


def test_drop_recreate_app_stopped_seed_capture_and_tokens(tmp_path):
    m = load_manifest(_case(tmp_path, """
        tokens: {START_POSITION: ""}
        action:
          - type: drop_recreate_app
            app: "${APP}"
            capture: [{token: RESTART, describe: Src, field: Source Restart Position}]
            stopped_seed: [{file: batch2.sql, db: spanner-google}]
            tokens: {START_POSITION: "^ ${RESTART}"}
            seed: [{file: batch3.sql, db: spanner-google, after: 10s}]
    """))
    spec = m.action_specs[0]
    assert spec["stopped_seed"] == [("spanner-google", "batch2.sql")]
    assert spec["seed"] == [("spanner-google", "batch3.sql", 10.0)]
    assert spec["tokens"] == {"START_POSITION": "^ ${RESTART}"}
    assert spec["capture"][0]["token"] == "RESTART"


@pytest.mark.parametrize("extra, match", [
    ("tokens: {UNDECLARED: x}", "does not declare"),
    ("tokens: {START_POSITION: [1]}", "string or number"),
    ("tokens: {}", "non-empty mapping"),
    ("stopped_seed: [{file: a.sql, when: post_start}]", "only 'seed'"),
    ("stoped_seed: [a.sql]", "unknown key"),
    ("capture: []", "non-empty list"),
    ("seed: [{file: a.sql, when: post_start}]", "'when'"),
    ("seed: [{file: a.sql, after: soon}]", "duration"),
    ("stopped_seed: [{file: a.sql, after: 5s}]", "only 'seed'"),
    ("capture: [{token: R, describe: S, field: f, delay_before: 1}]", "unknown key"),
])
def test_drop_recreate_app_refusals(tmp_path, extra, match):
    with pytest.raises(ManifestError, match=match):
        load_manifest(_case(tmp_path, f"""
            tokens: {{START_POSITION: ""}}
            action:
              - type: drop_recreate_app
                app: "${{APP}}"
                {extra}
        """))


# ---- alter_recompile: load ---------------------------------------------------------------------

def test_alter_recompile_normalizes(tmp_path):
    m = load_manifest(_case(tmp_path, """
        action:
          - type: alter_recompile
            app: "${APP}"
            file: swap.tql
            capture: [{token: RESTART, describe: Src, field: Source Restart Position}]
            stopped_seed: [batch2.sql]
            seed: [{file: batch3.sql, db: postgres-source}]
    """, {"swap.tql": "-- ALTER APPLICATION in a comment is fine\n" + SWAP}))
    assert m.action_specs == [{
        "type": "alter_recompile", "app": "${APP}", "file": "swap.tql", "delay_before_stop": 3.0,
        "capture": [{"token": "RESTART", "source": "describe", "component": "Src",
                     "field": "Source Restart Position", "timeout": None}],
        "stopped_seed": [("postgres-source", "batch2.sql")],
        "seed": [("postgres-source", "batch3.sql", 0.0)]}]


@pytest.mark.parametrize("body, files, match", [
    ("{type: alter_recompile, file: swap.tql}", {"swap.tql": SWAP}, "non-empty 'app'"),
    ("{type: alter_recompile, app: A}", {}, "needs a 'file'"),
    ("{type: alter_recompile, app: A, file: nope.tql}", {}, "not found"),
    ("{type: alter_recompile, app: A, file: ../out.tql}", {}, "escapes the test dir"),
    ("{type: alter_recompile, app: A, file: e.tql}", {"e.tql": "-- nothing\n"}, "is empty"),
    ("{type: alter_recompile, app: A, file: x.tql}", {"x.tql": "ALTER APPLICATION A RECOMPILE;\n"},
     "ALTER APPLICATION"),
    ("{type: alter_recompile, app: A, file: x.tql}", {"x.tql": SWAP + "DEPLOY APPLICATION A;\n"},
     "DEPLOY APPLICATION"),
    ("{type: alter_recompile, app: A, file: x.tql}", {"x.tql": "USE ns;\n" + SWAP}, "USE"),
    ("{type: alter_recompile, app: A, file: x.tql, tokens: {X: y}}", {"x.tql": SWAP}, "unknown key"),
])
def test_alter_recompile_refusals(tmp_path, body, files, match):
    (tmp_path / "out.tql").write_text(SWAP)
    with pytest.raises(ManifestError, match=match):
        load_manifest(_case(tmp_path, f"action:\n  - {body}\n", files))


# ---- server_files load: from outside the test dir ------------------------------------------------

def test_server_files_load_accepts_absolute_env_and_gs_sources(tmp_path):
    jar = tmp_path / "ChangeReaderV3B-5.4.jar"
    jar.write_bytes(b"jar")
    m = load_manifest(_case(tmp_path, f"""
        server_files:
          - {{file: {jar}, dest: ChangeReaderV3B-5.4.jar, load: open_processor}}
          - {{file: "${{OLD_JARS}}/R-${{STRIIM_SERIES}}.jar", dest: R.jar, load: op}}
          - {{file: gs://bucket/jars/Old-5.4.jar, dest: Old-5.4.jar, load: open_processor}}
          - {{file: local.jar, dest: L.jar, load: udf}}
    """))
    assert [(f, load) for f, _d, _w, load in m.server_files] == [
        (str(jar), "open_processor"), ("${OLD_JARS}/R-${STRIIM_SERIES}.jar", "open_processor"),
        ("gs://bucket/jars/Old-5.4.jar", "open_processor"), ("local.jar", "udf")]


@pytest.mark.parametrize("entry, match", [
    ("{file: /abs/in.csv, dest: '/tmp/${NS}/in.csv'}", "only a 'load:' entry"),
    ("{file: '${DATA}/in.csv', dest: '/tmp/${NS}/in.csv'}", "only a 'load:' entry"),
    ("{file: gs://b/in.csv, dest: '/tmp/${NS}/in.csv'}", "only a 'load:' entry"),
    ("{file: gs://bucket-only, dest: X.jar, load: open_processor}", "gs://<bucket>/<object>"),
])
def test_server_files_external_source_refusals(tmp_path, entry, match):
    with pytest.raises(ManifestError, match=match):
        load_manifest(_case(tmp_path, f"server_files:\n  - {entry}\n"))


def test_input_snapshot_resolves_env_sources_and_leaves_runtime_ones_out(tmp_path, monkeypatch):
    jar = tmp_path / "jars" / "Old-5.4.jar"
    jar.parent.mkdir()
    jar.write_bytes(b"old")
    monkeypatch.setenv("OLD_JARS", str(jar.parent))
    monkeypatch.delenv("UNSET_JARS", raising=False)
    p = _case(tmp_path, """
        server_files:
          - {file: "${OLD_JARS}/Old-5.4.jar", dest: Old-5.4.jar, load: open_processor}
          - {file: "${UNSET_JARS}/X.jar", dest: X.jar, load: open_processor}
          - {file: gs://bucket/Old-5.4.jar, dest: G.jar, load: open_processor}
    """)
    m = load_manifest(p)
    files = [path for role, path in inputs.referenced(m, p) if role == "server-file"]
    assert files == [jar]


def test_jar_source_relative_absolute_gs_and_missing(tmp_path):
    (tmp_path / "rel.jar").write_bytes(b"r")
    absolute = tmp_path / "elsewhere" / "abs.jar"
    absolute.parent.mkdir()
    absolute.write_bytes(b"a")
    assert opartifacts.jar_source("rel.jar", tmp_path, tmp_path / "s") == tmp_path / "rel.jar"
    assert opartifacts.jar_source(str(absolute), tmp_path / "case", tmp_path / "s") == absolute
    fetched = []

    def fetch(url, dest):
        fetched.append(url)
        dest.write_bytes(b"g")
    got = opartifacts.jar_source("gs://b/dir/Old-5.4.jar", tmp_path, tmp_path / "s", fetch=fetch)
    assert fetched == ["gs://b/dir/Old-5.4.jar"] and got.read_bytes() == b"g" and got.name == "Old-5.4.jar"
    with pytest.raises(FileNotFoundError, match="jar not found"):
        opartifacts.jar_source("missing.jar", tmp_path, tmp_path / "s")

    def broken(url, dest):
        raise PermissionError("403")
    with pytest.raises(FileNotFoundError, match="could not fetch gs://b/x.jar: 403"):
        opartifacts.jar_source("gs://b/x.jar", tmp_path, tmp_path / "s", fetch=broken)


# ---- capture: runtime ----------------------------------------------------------------------------

def _describe(position):
    cp = [{"Source Restart Position": {"CheckpointText": position}}] if position else []
    return [{"name": "Src", "adapterName": "ChangeReaderV3C", "Checkpoint": cp}]


class FakeClient:
    def __init__(self, describes=(), mons=()):
        self.describes, self.mons, self.calls = list(describes), list(mons), []

    def describe(self, name):
        self.calls.append(("describe", name))
        return self.describes.pop(0) if len(self.describes) > 1 else self.describes[0]

    def mon(self, name):
        self.calls.append(("mon", name))
        return self.mons.pop(0) if len(self.mons) > 1 else self.mons[0]


def _spec(**kw):
    return {"token": "RESTART", "source": "describe", "component": "Src",
            "field": "Source Restart Position", "timeout": None, **kw}


def test_describe_values_reads_the_checkpoint_text():
    assert appactions.describe_values(_describe(POS), "Source Restart Position") == [POS]
    assert appactions.describe_values(_describe(POS), "adapterName") == ["ChangeReaderV3C"]
    assert appactions.describe_values(_describe(None), "Source Restart Position") == []
    assert appactions.describe_values([{"x": {"y": [{"f": 3}]}}], "f") == ["3"]


def test_capture_polls_until_the_field_appears_and_binds_the_token():
    client = FakeClient(describes=[_describe(None), _describe(None), _describe(POS)])
    tokens, said = {}, []
    got = appactions.capture(client, _spec(), "NS1", tokens, 30, report=said.append,
                             sleep=lambda s: None)
    assert got == POS and tokens == {"RESTART": POS}
    assert client.calls == [("describe", "NS1.Src")] * 3
    assert said and "${RESTART}" in said[0]


def test_capture_renders_and_qualifies_the_component():
    client = FakeClient(mons=[{"input": 7}])
    tokens = {"APP": "NS1.App"}
    appactions.capture(client, _spec(source="mon", component="${APP}", field="input", token="SEEN"),
                       "NS1", tokens, 30)
    assert client.calls == [("mon", "NS1.App")] and tokens["SEEN"] == "7"
    client = FakeClient(mons=[{"input": "1,234"}])
    appactions.capture(client, _spec(source="mon", component="Other.Src", field="input"), "NS1", tokens, 30)
    assert client.calls == [("mon", "Other.Src")] and tokens["RESTART"] == "1,234"


def test_capture_refuses_two_different_values():
    two = [{"Checkpoint": [{"Source Restart Position": {"CheckpointText": "a"}},
                           {"Source Restart Position": {"CheckpointText": "b"}}]}]
    with pytest.raises(appactions.CaptureError, match="2 different values"):
        appactions.capture(FakeClient(describes=[two]), _spec(), "NS", {}, 30)
    same = [{"Checkpoint": [{"Source Restart Position": {"CheckpointText": "a"}}] * 2}]
    assert appactions.capture(FakeClient(describes=[same]), _spec(), "NS", {}, 30) == "a"


def test_capture_times_out_naming_what_it_saw():
    now = [0.0]

    def clock():
        now[0] += 10
        return now[0]
    with pytest.raises(appactions.CaptureError, match=r"no value for 'lastCheckpointedPosition' "
                       r"within 25s; top-level fields seen: \[input, output\]"):
        appactions.capture(FakeClient(mons=[{"input": 1, "output": 1}]),
                           _spec(source="mon", field="lastCheckpointedPosition", timeout=25.0),
                           "NS", {}, 300, sleep=lambda s: None, clock=clock)


def test_override_tokens_render_against_the_run_tokens():
    tokens = {"RESTART": POS, "START_POSITION": ""}
    out = appactions.override_tokens({"START_POSITION": "^ ${RESTART}"}, tokens)
    assert out["START_POSITION"] == "^ " + POS and tokens["START_POSITION"] == ""
    with pytest.raises(SubstitutionError, match="NOPE"):
        appactions.override_tokens({"START_POSITION": "${NOPE}"}, tokens)


def test_alter_tql_and_deploy_statement():
    full = ("CREATE OR REPLACE APPLICATION NS.App;\nEND APPLICATION NS.App;\n"
            "DEPLOY APPLICATION NS.App ON ANY IN default;\n")
    dep = appactions.deploy_statement(full, "NS.App")
    assert dep == "DEPLOY APPLICATION NS.App ON ANY IN default;"
    assert appactions.deploy_statement("nothing", "NS.App") == "DEPLOY APPLICATION NS.App;"
    assert appactions.alter_tql("NS", "NS.App", "\n" + SWAP + "\n", dep) == (
        "USE NS;\nUNDEPLOY APPLICATION NS.App;\nALTER APPLICATION NS.App;\n" + SWAP
        + "ALTER APPLICATION NS.App RECOMPILE;\nDEPLOY APPLICATION NS.App ON ANY IN default;\n"
        "START APPLICATION NS.App;\n")


class _Api:
    def __init__(self, resp):
        self.resp, self.lines = resp, []

    def post_tungsten_line(self, line, timeout=None):
        self.lines.append(line)
        return self.resp


def test_striim_client_describe_returns_the_output_list():
    api = _Api([{"executionStatus": "Success", "output": _describe(POS)}])
    assert StriimClient(api).describe("NS.Src") == _describe(POS) and api.lines == ["DESCRIBE NS.Src;"]
    assert StriimClient(_Api([{"executionStatus": "Success", "output": {"a": 1}}])).describe("X") == [{"a": 1}]
    with pytest.raises(StriimError, match="DESCRIBE X failed: no such"):
        StriimClient(_Api([{"executionStatus": "Failure", "failureMessage": "no such"}])).describe("X")


# ---- capture expect: ----------------------------------------------------------------------------

def test_capture_expect_normalizes(tmp_path):
    m = load_manifest(_case(tmp_path, """
        action:
          - {type: capture, token: ADAPTER, describe: Src, field: adapterName, expect: ChangeReaderV3C}
          - {type: capture, token: POSN, mon: Src, field: lastEventPosition, expect: {matches: "^\\\\^ "}}
          - {type: capture, token: N, mon: Src, field: input, expect: 50}
    """))
    assert [a["expect"] for a in m.action_specs] == ["ChangeReaderV3C", {"matches": "^\\^ "}, "50"]


@pytest.mark.parametrize("expect, match", [
    ("{absent: true}", "can never hold"),
    ("{matches: '('}", "not a valid regex"),
    ("{min: 1}", "literal or one of"),
    ("''", "non-empty string"),
    ("[a]", "non-empty string"),
])
def test_capture_expect_refusals(tmp_path, expect, match):
    with pytest.raises(ManifestError, match=match):
        load_manifest(_case(tmp_path, f"action: [{{type: capture, token: A, describe: S, field: f, expect: {expect}}}]\n"))


def test_capture_expect_waits_for_the_expected_value_then_binds_it():
    v3b = [{"adapterName": "ChangeReaderV3B"}]
    v3c = [{"adapterName": "ChangeReaderV3C"}]
    client, tokens = FakeClient(describes=[v3b, v3c]), {"NEW": "ChangeReaderV3C"}
    got = appactions.capture(client, _spec(token="ADAPTER", field="adapterName", expect="${NEW}"),
                             "NS", tokens, 30, sleep=lambda s: None)
    assert got == "ChangeReaderV3C" and tokens["ADAPTER"] == got and len(client.calls) == 2


def test_capture_expect_mismatch_fails_naming_both_values():
    now = [0.0]

    def clock():
        now[0] += 10
        return now[0]
    with pytest.raises(appactions.CaptureError,
                       match=r"'adapterName' shows 'ChangeReaderV3B', expected 'ChangeReaderV3C'"):
        appactions.capture(FakeClient(describes=[[{"adapterName": "ChangeReaderV3B"}]]),
                           _spec(token="ADAPTER", field="adapterName", expect="ChangeReaderV3C",
                                 timeout=15.0), "NS", {}, 30, sleep=lambda s: None, clock=clock)
    with pytest.raises(appactions.CaptureError, match="does not match"):
        appactions.capture(FakeClient(mons=[{"p": "@ x"}]),
                           _spec(source="mon", field="p", expect={"matches": "^\\^ "}, timeout=15.0),
                           "NS", {}, 30, sleep=lambda s: None, clock=clock)


def test_expect_miss_escapes_tokens_in_patterns():
    assert appactions.expect_miss({"matches": "^\\^ ${P}$"}, "^ " + POS, {"P": POS}) is None
    assert appactions.expect_miss({"present": True}, "x", {}) is None
    assert appactions.expect_miss("a", "b", {}) == "expected 'a'"


# ---- inputs: action-time files ------------------------------------------------------------------

def test_input_snapshot_lists_action_fragments_and_seeds(tmp_path):
    p = _case(tmp_path, """
        action:
          - type: alter_recompile
            app: "${APP}"
            file: swap.tql
            stopped_seed: [b2.sql]
            seed: [{file: b3.sql, after: 5s}]
    """, {"swap.tql": SWAP, "b2.sql": "", "b3.sql": ""})
    refs = [(r, path.name) for r, path in inputs.referenced(load_manifest(p), p)]
    assert ("tql-fragment", "swap.tql") in refs and ("seed", "b2.sql") in refs and ("seed", "b3.sql") in refs


def test_apply_upload_renames():
    assert appactions.apply_upload_renames("ConfigFile: 'UploadedFiles/a.json'", {"a.json": "t_a.json"}) \
        == "ConfigFile: 'UploadedFiles/t_a.json'"


def test_monitor_recapture_is_validated_at_load(tmp_path):
    load_manifest(_case(tmp_path / "ok", """
        assert:
          monitor:
            - component: Src
              recapture: [{token: RESTART, describe: Src, field: Source Restart Position}]
              metrics: {lastCheckpointedPosition: "^ ${RESTART}"}
    """))
    with pytest.raises(ManifestError, match="exactly one of 'describe:' or 'mon:'"):
        load_manifest(_case(tmp_path / "bad", """
            assert:
              monitor: [{component: Src, recapture: [{token: R, field: f}], metrics: {p: x}}]
        """))
    with pytest.raises(ManifestError, match="harness-provided"):
        load_manifest(_case(tmp_path / "ns", """
            assert:
              monitor: [{component: Src, recapture: [{token: NS, mon: Src, field: f}], metrics: {p: x}}]
        """))


# ---- review r2: load dest is a bare name; recapture keys; staging cleanup ------------------------

@pytest.mark.parametrize("dest", ["/opt/striim/lib/X.jar", "sub/X.jar", "..\\\\X.jar", ".."])
def test_server_files_load_dest_must_be_a_bare_name(tmp_path, dest):
    with pytest.raises(ManifestError, match="bare jar name"):
        load_manifest(_case(tmp_path, f"server_files: [{{file: x.jar, dest: '{dest}', load: open_processor}}]\n"))


def test_server_files_plain_dest_may_still_be_a_path(tmp_path):
    m = load_manifest(_case(tmp_path, "server_files: [{file: in.csv, dest: '/tmp/${NS}/in.csv'}]\n"))
    assert m.server_files[0][1] == "/tmp/${NS}/in.csv"


def test_jar_staging_removes_exactly_its_own_root(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    sibling = tmp_path / "keep-me"
    sibling.mkdir()
    with opartifacts.jar_staging() as root:
        assert root.parent == tmp_path and root.name.startswith("slt-load-jar-")
        (root / "fetched").mkdir()
        (root / "fetched" / "Old.jar").write_bytes(b"x")
        (root / "Old.jar").write_bytes(b"x")
    assert not root.exists() and sibling.is_dir() and sorted(p.name for p in tmp_path.iterdir()) == ["keep-me"]
    with pytest.raises(RuntimeError):
        with opartifacts.jar_staging() as root2:
            raise RuntimeError("load failed")
    assert not root2.exists()


@pytest.mark.parametrize("extra", ["expect: NEVER", "timeout: 5s", "delay_before: 1s"])
def test_recapture_refuses_keys_it_would_ignore(tmp_path, extra):
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(_case(tmp_path, f"""
            assert:
              monitor:
                - component: Src
                  recapture: [{{token: R, describe: Src, field: f, {extra}}}]
                  metrics: {{p: "${{R}}"}}
        """))


def test_recapture_of_the_asserted_mon_figure_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="compare the figure with itself"):
        load_manifest(_case(tmp_path, """
            assert:
              monitor:
                - component: Src
                  recapture: [{token: R, mon: Src, field: lastEventPosition}]
                  metrics: {lastEventPosition: "${R}"}
        """))
