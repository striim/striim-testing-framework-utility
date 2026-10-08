"""Unit tests for inttest.manifest -- test.yaml parsing/normalization (docs/INTEGRATION-TESTS.md).

Pure parsing tests: no Docker, no Striim, no pytest-plugin collection involved. Each
test writes a small inline test.yaml into tmp_path and calls load_manifest() directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from inttest.manifest import (
    AssertSpec,
    DataAssertion,
    FileSpec,
    ManifestError,
    OpRef,
    load_manifest,
)


def _write(tmp_path, text: str):
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    manifest_path = test_dir / "test.yaml"
    manifest_path.write_text(text)
    return manifest_path, test_dir


FULL_YAML = """
name: transform-regex-strip-non-ascii
purpose: >
  regex mode strips every non-ASCII character from text columns

op:
  jar: java/OpenProcessors/ExampleTransformOp

properties:
  ConfigFile: '${TEST_DIR}/config.json'
  ReplaceFrom: '[^\\x20-\\x7E]'
  ReplaceTo: ''
  EnableRegex: true
  Username: '${ORACLE_SOURCE_USER}'
  Password: '${ORACLE_SOURCE_PASSWORD}'
  BootstrapPassword: '${ORACLE_SOURCE_PASSWORD}'

requires: [oracle]

password_properties: [Password, BootstrapPassword]

ddl:
  - file: ddl_source.sql
    db: oracle-source

seed:
  - file: dml_source.sql
    db: oracle-source

assert:
  data:
    - input: input/customers.json
      match: expected/customers.json

timeout: 60
disabled: "TICKET-123: reason"
"""

MINIMAL_YAML = """
name: minimal-test
op:
  jar: java/OpenProcessors/ReferenceOp
properties:
  Foo: bar
assert:
  smoke: true
"""


def test_full_manifest_all_keys(tmp_path):
    manifest_path, test_dir = _write(tmp_path, FULL_YAML)
    m = load_manifest(manifest_path)

    assert m.name == "transform-regex-strip-non-ascii"
    assert m.purpose.strip() == "regex mode strips every non-ASCII character from text columns"
    assert m.op == OpRef(jar="java/OpenProcessors/ExampleTransformOp")
    assert m.properties == {
        "ConfigFile": "${TEST_DIR}/config.json",
        "ReplaceFrom": "[^\\x20-\\x7E]",
        "ReplaceTo": "",
        "EnableRegex": "true",           # YAML bool -> Java-style string
        "Username": "${ORACLE_SOURCE_USER}",
        "Password": "${ORACLE_SOURCE_PASSWORD}",
        "BootstrapPassword": "${ORACLE_SOURCE_PASSWORD}",
    }
    assert m.requires == ["oracle"]
    assert m.password_properties == ["Password", "BootstrapPassword"]
    # db_explicit is True because this manifest NAMES the route -- it is what lets a variant
    # honour a second schema of its own engine instead of silently overwriting it (§84).
    assert m.ddl == [FileSpec(file="ddl_source.sql", db="oracle-source",
                               path=(test_dir / "ddl_source.sql").resolve(), db_explicit=True)]
    assert m.seed == [FileSpec(file="dml_source.sql", db="oracle-source",
                                path=(test_dir / "dml_source.sql").resolve(), db_explicit=True)]
    assert m.assert_ == AssertSpec(
        data=[DataAssertion(
            input="input/customers.json", match="expected/customers.json",
            input_path=(test_dir / "input/customers.json").resolve(),
            match_path=(test_dir / "expected/customers.json").resolve(),
        )],
        smoke=False,
    )
    assert m.timeout == 60
    assert m.disabled == "TICKET-123: reason"
    assert m.dir == test_dir


def test_minimal_manifest(tmp_path):
    manifest_path, test_dir = _write(tmp_path, MINIMAL_YAML)
    m = load_manifest(manifest_path)

    assert m.name == "minimal-test"
    assert m.purpose is None
    assert m.op == OpRef(jar="java/OpenProcessors/ReferenceOp")
    assert m.properties == {"Foo": "bar"}
    assert m.requires == []
    assert m.password_properties == []
    assert m.ddl == []
    assert m.seed == []
    assert m.assert_ == AssertSpec(data=[], smoke=True)
    assert m.timeout == 120
    assert m.disabled is None


@pytest.mark.parametrize("ddl_yaml,expected", [
    # bare string -> default db route
    ("ddl: ddl_source.sql\n", [("ddl_source.sql", "postgres-source")]),
    # bare list of strings -> default db route
    ("ddl:\n  - a.sql\n  - b.sql\n", [("a.sql", "postgres-source"), ("b.sql", "postgres-source")]),
    # {file, db} mapping -> explicit route kept
    ("ddl:\n  - file: c.sql\n    db: oracle-target\n", [("c.sql", "oracle-target")]),
    # {file} mapping with no db -> default route
    ("ddl:\n  - file: d.sql\n", [("d.sql", "postgres-source")]),
    # mixed bare string + mapping in the same list
    ("ddl:\n  - e.sql\n  - file: f.sql\n    db: oracle-source\n",
     [("e.sql", "postgres-source"), ("f.sql", "oracle-source")]),
])
def test_ddl_normalization_forms(tmp_path, ddl_yaml, expected):
    text = f"""
name: ddl-forms
op:
  jar: some/module
properties:
  Foo: bar
{ddl_yaml}
assert:
  smoke: true
"""
    manifest_path, test_dir = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    got = [(spec.file, spec.db) for spec in m.ddl]
    assert got == expected
    for spec, (file_, _db) in zip(m.ddl, expected):
        assert spec.path == (test_dir / file_).resolve()


def test_seed_normalization_same_shape_as_ddl(tmp_path):
    text = """
name: seed-forms
op:
  jar: some/module
properties:
  Foo: bar
seed:
  - dml_a.sql
  - file: dml_b.sql
    db: oracle-source
assert:
  smoke: true
"""
    manifest_path, test_dir = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert [(s.file, s.db) for s in m.seed] == [
        ("dml_a.sql", "postgres-source"),
        ("dml_b.sql", "oracle-source"),
    ]


@pytest.mark.parametrize("missing_key,text", [
    ("name", """
op:
  jar: some/module
properties:
  Foo: bar
assert:
  smoke: true
"""),
    ("op", """
name: no-op
properties:
  Foo: bar
assert:
  smoke: true
"""),
    ("assert", """
name: no-assert
op:
  jar: some/module
properties:
  Foo: bar
"""),
])
def test_missing_required_key_raises(tmp_path, missing_key, text):
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_properties_absent_defaults_to_empty(tmp_path):
    # An operator with no configuration of its own (no properties: block at
    # all) is a real case -- properties: is optional, not required.
    text = """
name: no-properties
op:
  jar: some/module
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.properties == {}


def test_properties_explicit_empty_mapping_allowed(tmp_path):
    text = """
name: empty-properties
op:
  jar: some/module
properties: {}
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.properties == {}


@pytest.mark.parametrize("assert_block", [
    # empty assert mapping
    "assert: {}\n",
    # assert with neither data nor smoke
    "assert:\n  timeout: 5\n",
    # assert.data entry missing 'match'
    "assert:\n  data:\n    - input: input/a.json\n",
    # assert.data entry missing 'input'
    "assert:\n  data:\n    - match: expected/a.json\n",
    # assert.data not a list
    "assert:\n  data: input/a.json\n",
    # assert.smoke not a bool
    "assert:\n  smoke: yes-please\n",
])
def test_malformed_assert_raises(tmp_path, assert_block):
    text = f"""
name: malformed-assert
op:
  jar: some/module
properties:
  Foo: bar
{assert_block}
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_disabled_truthy_string(tmp_path):
    text = """
name: disabled-test
op:
  jar: some/module
properties:
  Foo: bar
assert:
  smoke: true
disabled: "TICKET-999: flaky"
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.disabled == "TICKET-999: flaky"
    assert bool(m.disabled) is True


def test_disabled_truthy_bool(tmp_path):
    text = """
name: disabled-bool-test
op:
  jar: some/module
properties:
  Foo: bar
assert:
  smoke: true
disabled: true
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.disabled is True


def test_disabled_empty_string_rejected(tmp_path):
    text = """
name: disabled-empty
op:
  jar: some/module
properties:
  Foo: bar
assert:
  smoke: true
disabled: "   "
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_requires_scalar_coerced_to_list(tmp_path):
    text = """
name: requires-scalar
op:
  jar: some/module
properties:
  Foo: bar
requires: oracle
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.requires == ["oracle"]


def test_password_properties_scalar_coerced_to_list(tmp_path):
    text = """
name: password-properties-scalar
op:
  jar: some/module
properties:
  Foo: bar
  Password: '${PG_PASS}'
password_properties: Password
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.password_properties == ["Password"]


def test_password_properties_empty_list_normalizes_to_empty(tmp_path):
    text = """
name: password-properties-empty-list
op:
  jar: some/module
properties:
  Foo: bar
password_properties: []
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.password_properties == []


def test_password_properties_unknown_key_raises(tmp_path):
    text = """
name: password-properties-unknown-key
op:
  jar: some/module
properties:
  Foo: bar
password_properties: [Passwrod]
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="Passwrod"):
        load_manifest(manifest_path)


def test_password_properties_with_no_properties_block_raises(tmp_path):
    # The new interaction properties: being optional creates: password_properties:
    # naming a key when properties: is absent entirely (not just present-and-empty)
    # must still be a hard error -- no key can ever match against nothing.
    text = """
name: password-properties-no-properties-block
op:
  jar: some/module
password_properties: [DbPassword]
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="DbPassword"):
        load_manifest(manifest_path)


@pytest.mark.parametrize("password_properties_yaml", [
    "password_properties: ''\n",
    "password_properties:\n  - Foo\n  - ''\n",
    "password_properties:\n  - 3\n",
])
def test_malformed_password_properties_raises(tmp_path, password_properties_yaml):
    text = f"""
name: malformed-password-properties
op:
  jar: some/module
properties:
  Foo: bar
{password_properties_yaml}
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_invalid_yaml_raises_manifest_error(tmp_path):
    manifest_path, _ = _write(tmp_path, "name: [unclosed\n")
    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_top_level_not_a_mapping_raises(tmp_path):
    manifest_path, _ = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_neither_op_nor_udf_raises(tmp_path):
    text = """
name: neither-op-nor-udf
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="exactly one of 'op'/'udf'"):
        load_manifest(manifest_path)


def test_both_op_and_udf_raises(tmp_path):
    text = """
name: both-op-and-udf
op:
  jar: java/OpenProcessors/ExampleTransformOp
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: f
      args: ['$']
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="exactly one of 'op'/'udf'"):
        load_manifest(manifest_path)


def test_op_list_form_rejected(tmp_path):
    # scripts/integration/ (unlike scripts/live/) drives exactly one module per
    # subprocess -- a list under `op:` is scripts/live/-only.
    text = """
name: op-list-form
op:
  - jar: some/module
  - jar: some/other-module
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="single mapping, not a list"):
        load_manifest(manifest_path)


def test_integration_drives_exactly_one_op_or_udf(tmp_path):
    """Proof that scripts/integration/ drives exactly ONE
    module per subprocess -- a single op:, or a single udf:, and nothing else. The two
    accepted shapes both load; every other combination (a list under either key, both
    keys present, neither key present) raises. Each case is also covered individually
    elsewhere (test_op_list_form_rejected, test_udf_list_form_rejected,
    test_both_op_and_udf_raises, test_neither_op_nor_udf_raises) -- this test exists to
    make the overall invariant checkable in one place."""
    udf_block = """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: f
      args: ['$']
"""
    def _write_case(case_name: str, block: str) -> Path:
        text = f"name: {case_name.replace(' ', '-')}\n" + block + "assert:\n  smoke: true\n"
        test_dir = tmp_path / case_name.replace(" ", "-")
        test_dir.mkdir()
        manifest_path = test_dir / "test.yaml"
        manifest_path.write_text(text)
        return manifest_path

    accepted = {
        "single op": "op:\n  jar: java/OpenProcessors/ExampleTransformOp\n",
        "single udf": udf_block,
    }
    for case_name, block in accepted.items():
        manifest = load_manifest(_write_case(case_name, block))
        assert (manifest.op is not None) != (manifest.udf is not None), case_name

    rejected = {
        "op list": "op:\n  - jar: some/module\n  - jar: some/other-module\n",
        "udf list": "udf:\n  - jar: java/UserDefinedFunctions/ExampleEventUdf\n    class: com.example.ExampleEventUdf\n  - jar: java/UserDefinedFunctions/ExampleJsonUdf\n    class: com.example.ExampleJsonUdf\n",
        "both": "op:\n  jar: java/OpenProcessors/ExampleTransformOp\n" + udf_block,
        "neither": "",
    }
    for case_name, block in rejected.items():
        with pytest.raises(ManifestError):
            load_manifest(_write_case(case_name, block))


def test_udf_list_form_rejected(tmp_path):
    # Same constraint as test_op_list_form_rejected, mirrored for udf: -- the list
    # form (multiple modules in one test) is scripts/live/-only.
    text = """
name: udf-list-form
udf:
  - jar: java/UserDefinedFunctions/ExampleEventUdf
    class: com.example.ExampleEventUdf
  - jar: java/UserDefinedFunctions/ExampleJsonUdf
    class: com.example.ExampleJsonUdf
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="single mapping, not a list"):
        load_manifest(manifest_path)


def test_op_unknown_key_raises(tmp_path):
    text = """
name: op-unknown-key
op:
  jar: some/module
  bogus: true
assert:
  smoke: true
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(manifest_path)


def test_gcs_objects_scalar_coerced_to_list(tmp_path):
    text = """
name: gcs-objects-scalar
op:
  jar: some/module
requires: [gcs]
assert:
  data:
    - input: input/events.json
      match: expected/events.json
  gcs_objects: 'folder/table/COL_1.bin'
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.assert_.gcs_objects == ["folder/table/COL_1.bin"]


def test_gcs_objects_list_form_preserved(tmp_path):
    text = """
name: gcs-objects-list
op:
  jar: some/module
requires: [gcs]
assert:
  data:
    - input: input/events.json
      match: expected/events.json
  gcs_objects:
    - 'folder/table/COL_1.bin'
    - 'folder/table/COL_2.bin'
"""
    manifest_path, _ = _write(tmp_path, text)
    m = load_manifest(manifest_path)
    assert m.assert_.gcs_objects == ["folder/table/COL_1.bin", "folder/table/COL_2.bin"]


def test_gcs_objects_empty_list_raises(tmp_path):
    text = """
name: gcs-objects-empty-list
op:
  jar: some/module
requires: [gcs]
assert:
  data:
    - input: input/events.json
      match: expected/events.json
  gcs_objects: []
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="gcs_objects"):
        load_manifest(manifest_path)


def test_gcs_objects_non_string_entry_raises(tmp_path):
    text = """
name: gcs-objects-non-string
op:
  jar: some/module
requires: [gcs]
assert:
  data:
    - input: input/events.json
      match: expected/events.json
  gcs_objects:
    - 3
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match=r"gcs_objects\[0\]"):
        load_manifest(manifest_path)


def test_gcs_objects_uri_form_rejected(tmp_path):
    text = """
name: gcs-objects-uri-form
op:
  jar: some/module
requires: [gcs]
assert:
  data:
    - input: input/events.json
      match: expected/events.json
  gcs_objects:
    - 'gs://some-bucket/folder/table/COL_1.bin'
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="gs://"):
        load_manifest(manifest_path)


def test_gcs_objects_without_assert_data_raises(tmp_path):
    text = """
name: gcs-objects-no-data
op:
  jar: some/module
requires: [gcs]
assert:
  smoke: true
  gcs_objects: 'folder/table/COL_1.bin'
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="assert.gcs_objects"):
        load_manifest(manifest_path)


def test_gcs_objects_without_requires_gcs_raises(tmp_path):
    text = """
name: gcs-objects-no-requires
op:
  jar: some/module
assert:
  data:
    - input: input/events.json
      match: expected/events.json
  gcs_objects: 'folder/table/COL_1.bin'
"""
    manifest_path, _ = _write(tmp_path, text)
    with pytest.raises(ManifestError, match="requires: \\[gcs\\]"):
        load_manifest(manifest_path)


# --- assert.data ignore_fields/project ---------------------------

_PROJECTION_YAML = """
name: projection-case
op:
  jar: java/OpenProcessors/ReferenceOp
properties:
  EnableLogging: "false"
assert:
  data:
    - input: input/in.json
      match: expected/out.json
      {key}: {value}
"""


def test_ignore_fields_and_project_are_parsed(tmp_path):
    manifest_path, _ = _write(tmp_path, _PROJECTION_YAML.format(
        key="ignore_fields", value='["metadata.ReadTime", "data[2]"]'))
    m = load_manifest(manifest_path)
    assert m.assert_.data[0].ignore_fields == ("metadata.ReadTime", "data[2]")
    assert m.assert_.data[0].project == ()


def test_a_scalar_field_path_is_accepted(tmp_path):
    manifest_path, _ = _write(tmp_path, _PROJECTION_YAML.format(
        key="project", value='"data[0]"'))
    m = load_manifest(manifest_path)
    assert m.assert_.data[0].project == ("data[0]",)


def test_a_case_declaring_neither_key_is_unchanged(tmp_path):
    manifest_path, _ = _write(tmp_path, """
name: plain-case
op:
  jar: java/OpenProcessors/ReferenceOp
assert:
  data:
    - input: input/in.json
      match: expected/out.json
""")
    m = load_manifest(manifest_path)
    assert m.assert_.data[0].ignore_fields == ()
    assert m.assert_.data[0].project == ()


def test_declaring_both_keys_is_rejected(tmp_path):
    manifest_path, _ = _write(tmp_path, """
name: both-case
op:
  jar: java/OpenProcessors/ReferenceOp
assert:
  data:
    - input: input/in.json
      match: expected/out.json
      ignore_fields: ["metadata.ReadTime"]
      project: ["data[0]"]
""")
    with pytest.raises(ManifestError, match="inverses"):
        load_manifest(manifest_path)


def test_a_malformed_path_fails_at_LOAD_naming_the_grammar(tmp_path):
    # Fail before any container starts, and say what the legal forms are -- an author
    # should not have to reverse-engineer a regex from a stack trace.
    manifest_path, _ = _write(tmp_path, _PROJECTION_YAML.format(
        key="ignore_fields", value='["data.2"]'))
    with pytest.raises(ManifestError, match=r"metadata\.<key>"):
        load_manifest(manifest_path)


def test_a_typoed_key_is_rejected_rather_than_silently_dropped(tmp_path):
    # The singular form is the mistake this slice invites. Dropped in silence it would
    # leave the comparison at exact-match while the author believed a field was excluded.
    manifest_path, _ = _write(tmp_path, _PROJECTION_YAML.format(
        key="ignore_field", value='["metadata.ReadTime"]'))
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(manifest_path)


def test_an_empty_field_path_list_is_rejected(tmp_path):
    manifest_path, _ = _write(tmp_path, _PROJECTION_YAML.format(
        key="project", value="[]"))
    with pytest.raises(ManifestError, match="non-empty"):
        load_manifest(manifest_path)


# --- types: the mapping form (keys / aliases) ------------------------------

_TYPES_YAML = """
name: types-case
op:
  jar: java/OpenProcessors/ExampleChangeOp
types:
{block}
assert:
  data:
    - input: input/in.json
      match: expected/out.json
"""


def test_types_bare_list_form_is_unchanged(tmp_path):
    manifest_path, _ = _write(tmp_path, _TYPES_YAML.format(
        block="  SRC.T: [a, b, c]"))
    m = load_manifest(manifest_path)
    assert m.types == {"SRC.T": ["a", "b", "c"]}, "the original form must stay exactly as it was"


def test_types_mapping_form_carries_keys_and_aliases(tmp_path):
    manifest_path, _ = _write(tmp_path, _TYPES_YAML.format(
        block="  SRC.T:\n    columns: [a, b]\n    keys: [a]\n    aliases: {b: B_ALIAS}"))
    m = load_manifest(manifest_path)
    assert m.types["SRC.T"] == {"columns": ["a", "b"], "keys": ["a"], "aliases": {"b": "B_ALIAS"}}


def test_a_key_naming_an_undeclared_column_is_rejected(tmp_path):
    # Silent otherwise: it would simply never match, and the case would take the
    # no-primary-key branch while its author believed a key was declared.
    manifest_path, _ = _write(tmp_path, _TYPES_YAML.format(
        block="  SRC.T:\n    columns: [a, b]\n    keys: [not_a_column]"))
    with pytest.raises(ManifestError, match="not one of its columns"):
        load_manifest(manifest_path)


def test_an_alias_naming_an_undeclared_column_is_rejected(tmp_path):
    manifest_path, _ = _write(tmp_path, _TYPES_YAML.format(
        block="  SRC.T:\n    columns: [a]\n    aliases: {nope: X}"))
    with pytest.raises(ManifestError, match="not one of its columns"):
        load_manifest(manifest_path)


def test_an_unknown_key_in_the_mapping_form_is_rejected(tmp_path):
    manifest_path, _ = _write(tmp_path, _TYPES_YAML.format(
        block="  SRC.T:\n    columns: [a]\n    key: [a]"))
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(manifest_path)
