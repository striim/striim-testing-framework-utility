from pathlib import Path
import textwrap
import pytest
from livetest.manifest import XFAIL_TIERS_ALLOWED, load_manifest, ManifestError, xfail_for_release

def _write(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "hello-single"
    d.mkdir()
    (d / "test.yaml").write_text(textwrap.dedent(body))
    return d / "test.yaml"

def test_loads_minimal_manifest(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert m.name == "hello-single"
    assert m.tql == "app.tql"
    assert m.topology == "single"        # default
    assert m.requires == []              # default
    assert m.timeout == 120              # default
    assert m.assert_ == {"smoke": True}
    assert m.dir == p.parent

def test_missing_name_raises(tmp_path):
    p = _write(tmp_path, "tql: app.tql\n")
    with pytest.raises(ManifestError, match="name"):
        load_manifest(p)

def test_missing_tql_raises(tmp_path):
    p = _write(tmp_path, "name: x\n")
    with pytest.raises(ManifestError, match="tql"):
        load_manifest(p)

def test_bad_topology_raises(tmp_path):
    p = _write(tmp_path, "name: x\ntql: app.tql\ntopology: quantum\n")
    with pytest.raises(ManifestError, match="topology"):
        load_manifest(p)

def test_missing_assert_raises(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        topology: single
    """)
    with pytest.raises(ManifestError, match="assert"):
        load_manifest(p)


# --- expect_halt --------------------------------------------------------------------

def test_expect_halt_defaults_false(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
    """)
    assert load_manifest(p).expect_halt is False

def test_expect_halt_parses_true(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
        assert:
          smoke: true
    """)
    assert load_manifest(p).expect_halt is True

def test_expect_halt_alone_satisfies_assert_requirement(tmp_path):
    # expect_halt IS an assertion, so a halt-expecting test needs no assert block.
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
    """)
    m = load_manifest(p)
    assert m.expect_halt is True and m.assert_ == {}

def test_expect_halt_non_bool_raises(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: "yes"
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="expect_halt"):
        load_manifest(p)


# --- expect_halt_contains -----------------------------------------------------------

def test_expect_halt_contains_defaults_empty(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
    """)
    assert load_manifest(p).expect_halt_contains == ()

def test_expect_halt_contains_parses_string_to_tuple(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
        expect_halt_contains: "conflict detected"
    """)
    assert load_manifest(p).expect_halt_contains == ("conflict detected",)

def test_expect_halt_contains_parses_list_to_tuple(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
        expect_halt_contains:
          - "table USERS"
          - "conflict"
    """)
    assert load_manifest(p).expect_halt_contains == ("table USERS", "conflict")

def test_expect_halt_contains_non_string_non_list_raises(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
        expect_halt_contains: 7
    """)
    with pytest.raises(ManifestError, match="expect_halt_contains"):
        load_manifest(p)

def test_expect_halt_contains_empty_string_raises(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
        expect_halt_contains: ""
    """)
    with pytest.raises(ManifestError, match="must be non-empty"):
        load_manifest(p)

def test_expect_halt_contains_empty_list_raises(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
        expect_halt_contains: []
    """)
    with pytest.raises(ManifestError, match="list must be non-empty"):
        load_manifest(p)

def test_expect_halt_contains_list_with_bad_entry_raises(tmp_path):
    p_empty = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt: true
        expect_halt_contains: ["ok", ""]
    """)
    with pytest.raises(ManifestError, match=r"expect_halt_contains\[1\]"):
        load_manifest(p_empty)

    d = tmp_path / "hello-two"
    d.mkdir()
    p_nonstr = d / "test.yaml"
    p_nonstr.write_text(textwrap.dedent("""
        name: hello-two
        tql: app.tql
        expect_halt: true
        expect_halt_contains: ["ok", 7]
    """))
    with pytest.raises(ManifestError, match=r"expect_halt_contains\[1\]"):
        load_manifest(p_nonstr)

def test_expect_halt_contains_without_expect_halt_raises(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        expect_halt_contains: "conflict"
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="requires 'expect_halt: true'"):
        load_manifest(p)


# --- OP example pointer (Option C) + op:/udf: artifact block ---------------------

def test_example_and_op_fields_parse_and_resolve_source_dir(tmp_path):
    p = _write(tmp_path, """
        name: mapping-passthrough
        example: java/OpenProcessors/ExampleMapOp/examples/passthrough
        tql: app.tql
        op:
          jar: java/OpenProcessors/ExampleMapOp
          upload: [passthrough.json]
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert m.example == "java/OpenProcessors/ExampleMapOp/examples/passthrough"
    # single-mapping form normalizes to one module with default token "OP" ->
    # ${OP_JAR}/${OP_NAME}
    assert m.modules == [{
        "jar": "java/OpenProcessors/ExampleMapOp", "token": "OP", "kind": "op",
        "upload": [{"from": "passthrough.json", "to": None}], "on_agent": False,
    }]
    assert m.op_uploads == [{"from": "passthrough.json", "to": None}]
    # source_dir points at the shipped example dir (repo-relative), NOT the test dir
    assert m.source_dir.as_posix().endswith(
        "java/OpenProcessors/ExampleMapOp/examples/passthrough")
    assert m.source_dir != m.dir

def test_no_example_source_dir_is_test_dir(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert m.example is None
    assert m.modules == []
    assert m.op_uploads == []
    assert m.source_dir == m.dir     # self-contained regression style

def test_op_without_jar_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          upload: [x.json]
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="op.jar"):
        load_manifest(p)


# --- op:/udf: single-mapping and list forms ---------------------------------------

def test_udf_single_form_normalizes_with_default_token(tmp_path):
    p = _write(tmp_path, """
        name: udf-single
        tql: app.tql
        udf:
          jar: java/UserDefinedFunctions/ExampleEventUdf
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert m.modules == [{
        "jar": "java/UserDefinedFunctions/ExampleEventUdf", "token": "UDF", "kind": "udf",
        "upload": [], "on_agent": False,
    }]

def test_op_list_form_normalizes_each_module(tmp_path):
    p = _write(tmp_path, """
        name: multi-op
        tql: app.tql
        op:
          - {jar: java/OpenProcessors/FooOp, token: FOO}
          - {jar: java/OpenProcessors/BarOp, token: BAR, upload: [config.json]}
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert m.modules == [
        {"jar": "java/OpenProcessors/FooOp", "token": "FOO", "kind": "op", "upload": [],
         "on_agent": False},
        {"jar": "java/OpenProcessors/BarOp", "token": "BAR", "kind": "op",
         "upload": [{"from": "config.json", "to": None}], "on_agent": False},
    ]
    assert m.op_uploads == [{"from": "config.json", "to": None}]

def test_udf_list_form_normalizes_each_module(tmp_path):
    p = _write(tmp_path, """
        name: multi-udf
        tql: app.tql
        udf:
          - {jar: java/UserDefinedFunctions/FooUdf, token: FOO}
          - {jar: java/UserDefinedFunctions/BarUdf, token: BAR}
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert m.modules == [
        {"jar": "java/UserDefinedFunctions/FooUdf", "token": "FOO", "kind": "udf", "upload": [],
         "on_agent": False},
        {"jar": "java/UserDefinedFunctions/BarUdf", "token": "BAR", "kind": "udf", "upload": [],
         "on_agent": False},
    ]

def test_op_and_udf_together_concatenate_op_first(tmp_path):
    p = _write(tmp_path, """
        name: mixed
        tql: app.tql
        op:
          jar: java/OpenProcessors/FooOp
        udf:
          jar: java/UserDefinedFunctions/BarUdf
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert [mod["kind"] for mod in m.modules] == ["op", "udf"]
    assert [mod["token"] for mod in m.modules] == ["OP", "UDF"]

def test_multiple_ops_and_multiple_udfs_together(tmp_path):
    """Proof of scripts/live/'s multi-module support: unlike
    scripts/integration/, a live test can load two OPs AND two UDFs at once."""
    p = _write(tmp_path, """
        name: multi-op-multi-udf
        tql: app.tql
        op:
          - {jar: java/OpenProcessors/ExampleMapOp, token: MAP}
          - {jar: java/OpenProcessors/ExampleTransformOp,  token: CLEAN}
        udf:
          - {jar: java/UserDefinedFunctions/ExampleJsonUdf, token: JM}
          - {jar: java/UserDefinedFunctions/ExampleEventUdf,   token: WAM}
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert [mod["kind"] for mod in m.modules] == ["op", "op", "udf", "udf"]
    assert [mod["token"] for mod in m.modules] == ["MAP", "CLEAN", "JM", "WAM"]
    assert [mod["jar"] for mod in m.modules] == [
        "java/OpenProcessors/ExampleMapOp",
        "java/OpenProcessors/ExampleTransformOp",
        "java/UserDefinedFunctions/ExampleJsonUdf",
        "java/UserDefinedFunctions/ExampleEventUdf",
    ]

def test_module_entry_missing_token_defaults_to_OP(tmp_path):
    p = _write(tmp_path, """
        name: ok
        tql: app.tql
        op:
          - {jar: java/OpenProcessors/FooOp}
        assert:
          smoke: true
    """)
    assert load_manifest(p).modules[0]["token"] == "OP"

def test_module_entry_bad_token_pattern_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          - {jar: java/OpenProcessors/FooOp, token: foo}
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="token"):
        load_manifest(p)

def test_module_entry_leading_digit_token_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          - {jar: java/OpenProcessors/FooOp, token: "1FOO"}
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="token"):
        load_manifest(p)

def test_duplicate_token_same_key_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          - {jar: java/OpenProcessors/FooOp, token: FOO}
          - {jar: java/OpenProcessors/OtherOp, token: FOO}
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="duplicate"):
        load_manifest(p)

def test_duplicate_token_across_op_and_udf_raises(tmp_path):
    # Token uniqueness is enforced across the MERGED op+udf list, not per-key -- two
    # token-less entries of different kinds would otherwise silently clobber tokens.
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          - {jar: java/OpenProcessors/FooOp, token: SAME}
        udf:
          - {jar: java/UserDefinedFunctions/BarUdf, token: SAME}
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="duplicate token"):
        load_manifest(p)

def test_module_list_empty_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op: []
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="non-empty"):
        load_manifest(p)

def test_module_entry_missing_jar_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          - {token: FOO}
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="jar"):
        load_manifest(p)

def test_module_entry_unknown_key_raises(tmp_path):
    # A leftover `load:` key (the old spelling) must be rejected outright -- there is no
    # compatibility shim, so an un-migrated fixture fails loudly at load, not silently.
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          jar: java/OpenProcessors/FooOp
          load: true
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(p)

def test_op_jar_wrong_family_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          jar: java/UserDefinedFunctions/FooUdf
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="did you mean 'udf:'"):
        load_manifest(p)

def test_udf_jar_wrong_family_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        udf:
          jar: java/OpenProcessors/FooOp
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="did you mean 'op:'"):
        load_manifest(p)


# --- validation guards: unhashable/wrong-typed values must raise ManifestError,
#     never a raw TypeError/ValueError, and a bare string must not char-explode ------

def test_topology_as_list_raises_manifest_error(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        topology: [single, agent]
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="topology"):
        load_manifest(p)

def test_removed_seed_when_rejected_whatever_its_shape(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        seed_when: [pre_deploy, post_start]
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="unknown manifest key"):
        load_manifest(p)

def test_op_non_dict_raises(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op: not-a-mapping
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="op"):
        load_manifest(p)

def test_requires_string_raises_instead_of_char_exploding(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        requires: postgres
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="requires"):
        load_manifest(p)

def test_tags_string_raises_instead_of_char_exploding(tmp_path):
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        tags: smoke
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="tags"):
        load_manifest(p)


# --- purpose (one-line scenario description, R3) ----------------------------------

def _write_manifest(tmp_path, extra: str) -> "Path":
    p = tmp_path / "test.yaml"
    p.write_text("name: t\ntql: app.tql\nassert:\n  smoke: true\n" + extra)
    return p


def test_purpose_absent_is_none(tmp_path):
    man = load_manifest(_write_manifest(tmp_path, ""))
    assert man.purpose is None


def test_purpose_valid_string(tmp_path):
    man = load_manifest(_write_manifest(tmp_path, 'purpose: "does one thing"\n'))
    assert man.purpose == "does one thing"


def test_purpose_empty_rejected(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(_write_manifest(tmp_path, 'purpose: ""\n'))


def test_purpose_whitespace_rejected(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(_write_manifest(tmp_path, 'purpose: "   "\n'))


def test_purpose_non_string_rejected(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(_write_manifest(tmp_path, "purpose: 42\n"))


def test_purpose_multiline_rejected(tmp_path):
    # A YAML block scalar survives .strip() but violates the one-line rule (R3).
    with pytest.raises(ManifestError):
        load_manifest(_write_manifest(tmp_path, "purpose: |\n  line one\n  line two\n"))


# --- upload: (per op:/udf: entry) --------------------------------------------------

def test_op_upload_emits_no_deprecation_warning(tmp_path):
    p = _write(tmp_path, """
        name: canon
        tql: app.tql
        op:
          jar: java/OpenProcessors/FooOp
          upload: [cfg.json]
        assert:
          smoke: true
    """)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        m = load_manifest(p)
    assert m.op_uploads == [{"from": "cfg.json", "to": None}]

def test_udf_upload_normalizes(tmp_path):
    p = _write(tmp_path, """
        name: udf-upload
        tql: app.tql
        udf:
          jar: java/UserDefinedFunctions/FooUdf
          upload: [cfg.json]
        assert:
          smoke: true
    """)
    m = load_manifest(p)
    assert m.modules[0]["upload"] == [{"from": "cfg.json", "to": None}]
    assert m.op_uploads == [{"from": "cfg.json", "to": None}]

def test_op_uploads_plural_alias_rejected(tmp_path):
    # The deprecated plural `uploads:` alias is gone WITHOUT a compatibility shim (N4)
    # -- it is simply an unknown key now, same as any other leftover old-shape key.
    p = _write(tmp_path, """
        name: bad
        tql: app.tql
        op:
          jar: java/OpenProcessors/FooOp
          uploads: [cfg.json]
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(p)


# --- generate: (data generators) --------------------------------------------------

def _write_generate(tmp_path: Path, generate_block: str, *, workload: bool = True) -> Path:
    # A test dir carrying a test.yaml with the given `generate:` block plus (unless the
    # case is specifically about a missing file) the workload.yaml it points at.
    d = tmp_path / "gen-single"
    d.mkdir()
    (d / "test.yaml").write_text(textwrap.dedent("""
        name: gen-single
        tql: app.tql
        assert:
          smoke: true
    """) + textwrap.dedent(generate_block))
    if workload:
        (d / "workload.yaml").write_text("seed: 42\n")
    return d / "test.yaml"

def test_generate_absent_defaults_to_empty(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
    """)
    assert load_manifest(p).generate_specs == []

def test_generate_parses_and_applies_defaults(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: workload.yaml
            dest: '/tmp/${NS}-gg/'
    """)
    (spec,) = load_manifest(p).generate_specs
    assert spec["kind"] == "ggtrail"
    assert spec["dest"] == "/tmp/${NS}-gg/"          # rendered at execution time, not here
    assert spec["when"] == "post_start"               # default (CDC-style), unlike server_files
    # workload resolves to an absolute path under the test dir
    assert spec["workload"] == (p.parent / "workload.yaml").resolve()
    assert spec["workload"].is_absolute() and spec["workload"].is_file()

def test_generate_explicit_when_pre_deploy(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: workload.yaml
            dest: '/tmp/${NS}-gg/'
            when: pre_deploy
    """)
    assert load_manifest(p).generate_specs[0]["when"] == "pre_deploy"

def test_generate_bad_when_raises(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: workload.yaml
            dest: '/tmp/${NS}-gg/'
            when: whenever
    """)
    with pytest.raises(ManifestError, match="'generate' when must be one of"):
        load_manifest(p)

def test_generate_missing_workload_file_raises(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: workload.yaml
            dest: '/tmp/${NS}-gg/'
    """, workload=False)
    with pytest.raises(ManifestError, match="workload file not found"):
        load_manifest(p)

def test_generate_missing_workload_key_raises(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            dest: '/tmp/${NS}-gg/'
    """)
    with pytest.raises(ManifestError, match="needs a 'workload' file"):
        load_manifest(p)

def test_generate_workload_escape_rejected(tmp_path):
    # ../ escapes out of source_dir -- rejected in the same spirit as R2's tql confinement,
    # even when the target exists.
    (tmp_path / "outside.yaml").write_text("seed: 1\n")
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: ../outside.yaml
            dest: '/tmp/${NS}-gg/'
    """)
    with pytest.raises(ManifestError, match="escapes source_dir"):
        load_manifest(p)

def test_generate_absolute_workload_escape_rejected(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: /etc/hostname
            dest: '/tmp/${NS}-gg/'
    """)
    with pytest.raises(ManifestError, match="escapes source_dir"):
        load_manifest(p)

def test_generate_missing_dest_raises(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: workload.yaml
    """)
    with pytest.raises(ManifestError, match="needs a non-empty 'dest'"):
        load_manifest(p)

def test_generate_empty_dest_raises(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: workload.yaml
            dest: '   '
    """)
    with pytest.raises(ManifestError, match="needs a non-empty 'dest'"):
        load_manifest(p)

def test_generate_missing_kind_raises(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - workload: workload.yaml
            dest: '/tmp/${NS}-gg/'
    """)
    with pytest.raises(ManifestError, match="needs a non-empty 'kind'"):
        load_manifest(p)

def test_generate_unknown_kind_parses(tmp_path):
    # kind is NOT validated against the registry at load time -- that is an execution-time
    # concern (plugin.GENERATORS), so a manifest for a generator this checkout lacks parses.
    p = _write_generate(tmp_path, """
        generate:
          - kind: extgen
            workload: workload.yaml
            dest: '/tmp/${NS}-gg/'
    """)
    assert load_manifest(p).generate_specs[0]["kind"] == "extgen"

def test_generate_not_a_list_raises(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          kind: ggtrail
    """)
    with pytest.raises(ManifestError, match="'generate' must be a list"):
        load_manifest(p)

def test_generate_entry_not_a_mapping_raises(tmp_path):
    p = _write_generate(tmp_path, """
        generate:
          - workload.yaml
    """)
    with pytest.raises(ManifestError, match="entries must be mappings"):
        load_manifest(p)

def test_generate_multiple_specs_preserve_order(tmp_path):
    d = tmp_path / "gen-single"
    p = _write_generate(tmp_path, """
        generate:
          - kind: ggtrail
            workload: workload.yaml
            dest: '/tmp/${NS}-a/'
            when: pre_deploy
          - kind: ggtrail
            workload: second.yaml
            dest: '/tmp/${NS}-b/'
    """)
    (d / "second.yaml").write_text("seed: 7\n")
    specs = load_manifest(p).generate_specs
    assert [s["dest"] for s in specs] == ["/tmp/${NS}-a/", "/tmp/${NS}-b/"]
    assert [s["when"] for s in specs] == ["pre_deploy", "post_start"]

def test_disabled_parallel_absent_defaults_to_none(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
    """)
    assert load_manifest(p).disabled_parallel is None

def test_disabled_parallel_string_reason_parses(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
        disabled_parallel: "PLATFORM-1: reproducible only under xdist worker scheduling"
    """)
    m = load_manifest(p)
    assert m.disabled_parallel == "PLATFORM-1: reproducible only under xdist worker scheduling"
    assert m.disabled is None            # independent of disabled:

def test_disabled_parallel_bool_true_parses(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
        disabled_parallel: true
    """)
    assert load_manifest(p).disabled_parallel is True

def test_disabled_parallel_empty_string_raises(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
        disabled_parallel: "   "
    """)
    with pytest.raises(ManifestError, match="disabled_parallel"):
        load_manifest(p)

def test_disabled_parallel_non_string_non_bool_raises(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
        disabled_parallel: 42
    """)
    with pytest.raises(ManifestError, match="disabled_parallel"):
        load_manifest(p)


# ---------------------------------------------------------------------------
# xfail -- marks a test EXPECTED TO FAIL, reaching pytest as
# pytest.mark.xfail(reason, strict) in plugin.py.
#
# Added with no coverage on either side: no framework example (which is what
# test_framework_coverage caught) and none of the five rejections below. Every branch of
# the parser is exercised here, because a schema rule nothing tests is a schema rule that
# silently stops applying.
# ---------------------------------------------------------------------------

def _xf(tmp_path, line):
    return _write(tmp_path, f"""
        name: hello-single
        tql: app.tql
        assert:
          smoke: true
        {line}
    """)

def test_xfail_absent_defaults_to_empty(tmp_path):
    # Falsy, because plugin.py gates the marker on `if m.xfail:` -- None would crash it.
    assert load_manifest(_xf(tmp_path, "")).xfail == {}

def test_xfail_string_form_becomes_a_reason(tmp_path):
    m = load_manifest(_xf(tmp_path, 'xfail: "PLATFORM-1: known server bug"'))
    assert m.xfail["reason"] == "PLATFORM-1: known server bug"
    assert m.xfail["tiers"] == sorted(XFAIL_TIERS_ALLOWED), "the string form covers every assertion tier"

def test_xfail_string_form_is_not_strict(tmp_path):
    # The string branch sets no "strict" key at all, so the plugin's .get("strict", False)
    # decides. Pinned because a string-form xfail that turned strict would redden a suite
    # the day the underlying bug got fixed -- the opposite of what the marker is for.
    assert load_manifest(_xf(tmp_path, 'xfail: "reason"')).xfail.get("strict", False) is False

def test_xfail_dict_defaults_strict_to_false(tmp_path):
    m = load_manifest(_xf(tmp_path, 'xfail: {reason: "PLATFORM-1: flaky upstream"}'))
    assert m.xfail == {"reason": "PLATFORM-1: flaky upstream", "strict": False, "tiers": sorted(XFAIL_TIERS_ALLOWED)}

def test_xfail_dict_keeps_explicit_strict(tmp_path):
    m = load_manifest(_xf(tmp_path, 'xfail: {reason: "always fails", strict: true}'))
    assert m.xfail["strict"] is True

def test_xfail_empty_string_raises(tmp_path):
    with pytest.raises(ManifestError, match="xfail"):
        load_manifest(_xf(tmp_path, 'xfail: "   "'))

def test_xfail_dict_without_reason_raises(tmp_path):
    with pytest.raises(ManifestError, match="reason"):
        load_manifest(_xf(tmp_path, "xfail: {strict: true}"))

def test_xfail_dict_blank_reason_raises(tmp_path):
    with pytest.raises(ManifestError, match="reason"):
        load_manifest(_xf(tmp_path, 'xfail: {reason: "  ", strict: true}'))

def test_xfail_non_bool_strict_raises(tmp_path):
    with pytest.raises(ManifestError, match="strict"):
        load_manifest(_xf(tmp_path, 'xfail: {reason: "r", strict: "yes"}'))

def test_xfail_wrong_type_raises(tmp_path):
    with pytest.raises(ManifestError, match="xfail"):
        load_manifest(_xf(tmp_path, "xfail: 42"))

# xfail.tiers -- the narrowing (design §10.1). An xfail used to say green for a TQL typo, a missing
# jar or a DDL error; now only an assertion failure on a named tier is the expected failure.

def test_xfail_tiers_narrows_to_named_tiers(tmp_path):
    m = load_manifest(_xf(tmp_path, 'xfail: {reason: "r", tiers: [diff, data]}'))
    assert m.xfail["tiers"] == ["data", "diff"]

def test_xfail_tiers_dedupes_and_sorts(tmp_path):
    m = load_manifest(_xf(tmp_path, 'xfail: {reason: "r", tiers: [diff, diff, data]}'))
    assert m.xfail["tiers"] == ["data", "diff"]

def test_xfail_tiers_rejects_smoke(tmp_path):
    # An app that never reached RUNNING has not failed on its defect.
    with pytest.raises(ManifestError, match="smoke"):
        load_manifest(_xf(tmp_path, 'xfail: {reason: "r", tiers: [smoke]}'))

def test_xfail_tiers_rejects_unknown_tier(tmp_path):
    with pytest.raises(ManifestError, match="allowed tiers"):
        load_manifest(_xf(tmp_path, 'xfail: {reason: "r", tiers: [rows]}'))

@pytest.mark.parametrize("bad", ["tiers: []", "tiers: diff", "tiers: [1]"])
def test_xfail_tiers_must_be_a_non_empty_list_of_strings(tmp_path, bad):
    with pytest.raises(ManifestError, match="'tiers'"):
        load_manifest(_xf(tmp_path, "xfail: {reason: \"r\", " + bad + "}"))

def test_xfail_rejects_unknown_keys(tmp_path):
    # A typo'd key (`raises`, `on` -- which YAML reads as True anyway) must not silently widen
    # the marker back to "anything".
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(_xf(tmp_path, 'xfail: {reason: "r", raises: [diff]}'))

# xfail.releases -- the xfail applies only on the named Striim releases; on every other release
# the test must pass. Validated at load so a typo'd release cannot silently never match.

def test_xfail_releases_is_kept_when_valid(tmp_path):
    m = load_manifest(_xf(tmp_path, 'xfail: {reason: "r", releases: ["5.4.0", "5.4.0.6A-5.4.0.6F"]}'))
    assert m.xfail["releases"] == ["5.4.0", "5.4.0.6A-5.4.0.6F"]

def test_xfail_without_releases_has_no_releases_key(tmp_path):
    assert "releases" not in load_manifest(_xf(tmp_path, 'xfail: {reason: "r"}')).xfail

@pytest.mark.parametrize("bad", ["releases: []", "releases: 5.4.0", "releases: [5]"])
def test_xfail_releases_must_be_a_non_empty_list_of_strings(tmp_path, bad):
    with pytest.raises(ManifestError, match="'releases'"):
        load_manifest(_xf(tmp_path, "xfail: {reason: \"r\", " + bad + "}"))

def test_xfail_releases_rejects_a_range_across_patch_lines(tmp_path):
    with pytest.raises(ManifestError, match="one patch line"):
        load_manifest(_xf(tmp_path, 'xfail: {reason: "r", releases: ["5.4.0.2-5.4.0.6G"]}'))

@pytest.mark.parametrize("version,applies", [("5.4.0.6C", True), ("5.4.0.6G", False), ("5.4.0.2", False)])
def test_xfail_for_release(tmp_path, version, applies):
    xf = load_manifest(_xf(tmp_path, 'xfail: {reason: "r", strict: true, releases: ["5.4.0.6A-5.4.0.6F"]}')).xfail
    assert xfail_for_release(xf, version) == (xf if applies else {})

def test_xfail_for_release_without_releases_applies_everywhere(tmp_path):
    xf = load_manifest(_xf(tmp_path, 'xfail: {reason: "r"}')).xfail
    assert xfail_for_release(xf, "5.4.0.6G") is xf
    assert xfail_for_release({}, "5.4.0.6G") == {}


# ---------------------------------------------------------------------------
# op.on_agent -- places the jar on the AGENT's classpath and restarts it.
#
# It exists because `LOAD OPEN PROCESSOR` is server-side and cannot reach an agent, so an
# agent-deployed flow whose source is an OP fails at DEPLOY with ClassNotFoundException however
# correctly the servers loaded the same jar. See opartifacts.place_on_agent.
# ---------------------------------------------------------------------------

def test_op_on_agent_defaults_to_false(tmp_path):
    p = _write(tmp_path, """
        name: op-no-agent
        tql: app.tql
        op:
          jar: java/OpenProcessors/FooOp
        assert:
          smoke: true
    """)
    # Default false, not absent: an agent restart is disruptive enough that it must be asked
    # for, and a missing key must not read as "maybe".
    assert load_manifest(p).modules[0]["on_agent"] is False


def test_op_on_agent_true_is_carried_through(tmp_path):
    p = _write(tmp_path, """
        name: op-on-agent
        tql: app.tql
        op:
          jar: java/OpenProcessors/FooOp
          on_agent: true
        assert:
          smoke: true
    """)
    assert load_manifest(p).modules[0]["on_agent"] is True


def test_op_on_agent_must_be_a_boolean(tmp_path):
    p = _write(tmp_path, """
        name: op-on-agent-bad
        tql: app.tql
        op:
          jar: java/OpenProcessors/FooOp
          on_agent: "yes"
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError) as ei:
        load_manifest(p)
    assert "on_agent" in str(ei.value)


def test_udf_on_agent_is_rejected_rather_than_ignored(tmp_path):
    # A UDF jar reaches an agent through the same load path a server uses, so there is nothing
    # to place. Silently ignoring the flag would read as coverage the run does not have.
    p = _write(tmp_path, """
        name: udf-on-agent
        tql: app.tql
        udf:
          jar: java/UserDefinedFunctions/FooUdf
          on_agent: true
        assert:
          smoke: true
    """)
    with pytest.raises(ManifestError) as ei:
        load_manifest(p)
    assert "on_agent" in str(ei.value)


# --- §85.3 / §127: diff_poll ------------------------------------------------------------------


def _write(tmp_path, body: str):
    p = tmp_path / "test.yaml"
    p.write_text(body)
    return p


_MIN = """name: x
tql: app.tql
topology: single
assert:
  smoke: true
"""


def test_diff_poll_defaults_to_two_seconds(tmp_path):
    m = load_manifest(_write(tmp_path, _MIN))
    assert m.diff_poll == 2.0


def test_diff_poll_is_read(tmp_path):
    m = load_manifest(_write(tmp_path, _MIN + "diff_poll: 0.25\n"))
    assert m.diff_poll == 0.25


def test_diff_poll_refuses_zero(tmp_path):
    """⚠ Zero is not 'as fast as possible'. A zero-poll diff spins the CPU issuing SELECTs with
    no gap, which on a shared database changes the thing being measured."""
    with pytest.raises(ManifestError, match="greater than 0"):
        load_manifest(_write(tmp_path, _MIN + "diff_poll: 0\n"))


def test_diff_poll_refuses_a_negative_and_a_non_number(tmp_path):
    with pytest.raises(ManifestError, match="greater than 0"):
        load_manifest(_write(tmp_path, _MIN + "diff_poll: -1\n"))
    with pytest.raises(ManifestError, match="must be a number"):
        load_manifest(_write(tmp_path, _MIN + "diff_poll: fast\n"))


def test_diff_poll_refuses_a_bool(tmp_path):
    """`diff_poll: true` is a typo, not a poll interval -- and float(True) is 1.0, so without an
    explicit bool check it would load as a plausible-looking one-second interval."""
    with pytest.raises(ManifestError, match="must be a number"):
        load_manifest(_write(tmp_path, _MIN + "diff_poll: true\n"))


# --- tokens: (manifest-declared ${NAME} values) ------------------------------------

def test_tokens_absent_defaults_to_empty(tmp_path):
    assert load_manifest(_write(tmp_path, _MIN)).tokens == {}


def test_tokens_accepted_and_numbers_cast_to_str(tmp_path):
    m = load_manifest(_write(tmp_path, _MIN + "tokens:\n  SRC_TRAIL: 'k10*'\n  ROWS: 1000\n"))
    assert m.tokens == {"SRC_TRAIL": "k10*", "ROWS": "1000"}


def test_tokens_render_into_the_tql(tmp_path):
    from livetest.plugin import _rendered_tql
    p = _write(tmp_path, _MIN + "tokens:\n  SRC_TRAIL: 'k10*'\n")
    (p.parent / "app.tql").write_text("TrailFilePattern: '${SRC_TRAIL}', Ns: '${NS}'")
    m = load_manifest(p)
    text = _rendered_tql(m, {"NS": "SLT_x", **m.tokens}, {})
    assert "TrailFilePattern: 'k10*'" in text
    assert "Ns: 'SLT_x'" in text


@pytest.mark.parametrize("name", ["NS", "TID", "APP_BARE", "APP_GROUP", "STRIIM_WEB_URL",
                                  "MSSQL_URL", "PG_SLOT", "STRIIM_VERSION"])
def test_tokens_refuse_harness_and_service_names(tmp_path, name):
    with pytest.raises(ManifestError, match="collides"):
        load_manifest(_write(tmp_path, _MIN + f"tokens:\n  {name}: x\n"))


def test_tokens_refuse_module_jar_and_name_tokens(tmp_path):
    body = _MIN + "op:\n  jar: java/OpenProcessors/ExampleMapOp\n  token: TM\ntokens:\n  TM_JAR: x\n"
    with pytest.raises(ManifestError, match="collides"):
        load_manifest(_write(tmp_path, body))


@pytest.mark.parametrize("name", ["src_trail", "1TRAIL", "Trail", "TRAIL-1", "_X"])
def test_tokens_refuse_bad_names(tmp_path, name):
    with pytest.raises(ManifestError, match=r"\[A-Z\]\[A-Z0-9_\]\*"):
        load_manifest(_write(tmp_path, _MIN + f"tokens:\n  '{name}': x\n"))


def test_tokens_refuse_nested_token_values(tmp_path):
    with pytest.raises(ManifestError, match="may not contain"):
        load_manifest(_write(tmp_path, _MIN + "tokens:\n  TRAIL_DIR: '/tmp/${NS}-gg-trail'\n"))


def test_tokens_refuse_non_mapping_and_bool_values(tmp_path):
    with pytest.raises(ManifestError, match="must be a mapping"):
        load_manifest(_write(tmp_path, _MIN + "tokens: [A, B]\n"))
    with pytest.raises(ManifestError, match="must be a string or number"):
        load_manifest(_write(tmp_path, _MIN + "tokens:\n  FLAG: true\n"))

def test_jmx_specs_are_validated_at_load(tmp_path):
    # A clock attribute is refused before any stack boots for it.
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          jmx:
            - bean: {domain: com.example.cache, type: LookupOp, component: ProductEnrich}
              attributes: {ProbeMaxOverrunMillis: 0}
    """)
    with pytest.raises(ManifestError, match="CLOCK"):
        load_manifest(p)


def test_a_jmx_bean_without_a_domain_is_a_manifest_error(tmp_path):
    p = _write(tmp_path, """
        name: hello-single
        tql: app.tql
        assert:
          jmx:
            - bean: {type: LookupOp, component: ProductEnrich}
              attributes: {Hits: 3}
    """)
    with pytest.raises(ManifestError, match="domain"):
        load_manifest(p)

def test_xfail_tiers_may_name_jmx(tmp_path):
    m = load_manifest(_xf(tmp_path, 'xfail: {reason: "r", tiers: [jmx]}'))
    assert m.xfail["tiers"] == ["jmx"]
