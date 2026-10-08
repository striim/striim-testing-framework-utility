"""Unit tests for inttest.manifest's `udf:` block normalization -- pure parsing
tests, no Docker/Striim/pytest-plugin collection involved.
"""
from __future__ import annotations

import pytest

from inttest.manifest import ManifestError, load_manifest, udf_to_wire


def _write(tmp_path, text: str):
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    manifest_path = test_dir / "test.yaml"
    manifest_path.write_text(text)
    return manifest_path, test_dir


BASE = """
name: udf-case
assert:
  smoke: true
"""


def test_absent_udf_normalizes_to_none(tmp_path):
    path, _ = _write(tmp_path, BASE + """
op:
  jar: java/OpenProcessors/ExampleTransformOp
""")
    manifest = load_manifest(path)
    assert manifest.udf is None


def test_minimal_udf_normalizes_and_wires(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: ReferenceUdfMarkProcessed
      args: ['$']
""")
    manifest = load_manifest(path)
    assert manifest.op is None
    assert manifest.udf.jar == "java/UserDefinedFunctions/ReferenceUdf"
    assert manifest.udf.class_name == "com.example.ReferenceUdf"
    assert manifest.udf.kind == "waevent"
    assert manifest.udf.source is None
    assert manifest.udf.target is None
    assert len(manifest.udf.pipeline) == 1
    step = manifest.udf.pipeline[0]
    assert step.function == "ReferenceUdfMarkProcessed"
    assert step.args == [{"reg": True}]
    assert step.as_ is None
    assert manifest.module_ref == "java/UserDefinedFunctions/ReferenceUdf"

    wire = udf_to_wire(manifest.udf)
    assert wire == {
        "className": "com.example.ReferenceUdf",
        "kind": "waevent",
        "source": None,
        "target": None,
        "pipeline": [{"function": "ReferenceUdfMarkProcessed", "args": [{"reg": True}], "as": None}],
    }


def test_udf_to_wire_none_roundtrips():
    assert udf_to_wire(None) is None


def test_udf_missing_jar_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  class: com.example.ReferenceUdf
  pipeline:
    - function: f
      args: ['$']
""")
    with pytest.raises(ManifestError, match="udf.jar"):
        load_manifest(path)


def test_udf_jar_failing_family_check_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/OpenProcessors/ExampleTransformOp
  class: com.example.ReferenceUdf
  pipeline:
    - function: f
      args: ['$']
""")
    with pytest.raises(ManifestError, match="OpenProcessors"):
        load_manifest(path)


def test_op_jar_failing_family_check_raises(tmp_path):
    """Mirror of test_udf_jar_failing_family_check_raises: an un-migrated UDF fixture
    that kept its jar under `op:` must fail loudly, not load as an OP."""
    path, _ = _write(tmp_path, BASE + """
op:
  jar: java/UserDefinedFunctions/ExampleEventUdf
""")
    with pytest.raises(ManifestError, match="UserDefinedFunctions"):
        load_manifest(path)


@pytest.mark.parametrize("class_yaml", ["class: ExampleEventUdf", "class: ''", "", "class: 123"])
def test_malformed_class_name_raises(tmp_path, class_yaml):
    path, _ = _write(tmp_path, BASE + f"""
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  {class_yaml}
  pipeline:
    - function: f
      args: ['$']
""")
    with pytest.raises(ManifestError, match="udf.class"):
        load_manifest(path)


def test_unknown_top_level_udf_key_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  bogus: true
  pipeline:
    - function: f
      args: ['$']
""")
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


def test_empty_pipeline_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline: []
""")
    with pytest.raises(ManifestError, match="udf.pipeline"):
        load_manifest(path)


def test_pipeline_step_missing_function_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - args: ['$']
""")
    with pytest.raises(ManifestError, match="udf.pipeline\\[0\\].function"):
        load_manifest(path)


def test_dollar_arg_becomes_register_tag(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: f
      args: ['$']
""")
    manifest = load_manifest(path)
    assert manifest.udf.pipeline[0].args == [{"reg": True}]


def test_scalar_arg_wraps_as_val(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleEventUdf
  class: com.example.ExampleEventUdf
  pipeline:
    - function: WAPipeline
      args: ['$', CUSTOMER, 'restoreBefore', 100, true]
""")
    manifest = load_manifest(path)
    args = manifest.udf.pipeline[0].args
    assert args == [
        {"reg": True},
        {"val": "CUSTOMER"},
        {"val": "restoreBefore"},
        {"val": 100},
        {"val": True},
    ]


def test_str_tag_forces_literal_val(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: f
      args:
        - str: '$'
""")
    manifest = load_manifest(path)
    assert manifest.udf.pipeline[0].args == [{"val": "$"}]


def test_json_tag_parses_string_value(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONParse
      args:
        - json: '{"a": 1}'
""")
    manifest = load_manifest(path)
    assert manifest.udf.pipeline[0].args == [{"json": '{"a": 1}'}]


def test_json_tag_rejects_invalid_json_string(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONParse
      args:
        - json: 'not json'
""")
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_manifest(path)


def test_json_tag_accepts_a_structured_yaml_value(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONParse
      args:
        - json: {a: 1, b: [true, false, null]}
""")
    manifest = load_manifest(path)
    assert manifest.udf.pipeline[0].args == [{"json": {"a": 1, "b": [True, False, None]}}]


def test_json_tag_rejects_a_non_json_representable_yaml_type(tmp_path):
    # PyYAML parses an unquoted ISO date as a datetime.date -- json.dumps() can't
    # serialize it. Before this guard this reached json.dumps() only at RUN time
    # (inside harness.drive/build_perf_request), as an opaque, unnamed TypeError;
    # this must instead fail at COLLECTION time, naming the file and key.
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONParse
      args:
        - json: 2020-01-01
""")
    with pytest.raises(ManifestError, match="JSON-representable"):
        load_manifest(path)


def test_json_tag_rejects_a_non_json_representable_yaml_type_nested(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONParse
      args:
        - json: {when: 2020-01-01}
""")
    with pytest.raises(ManifestError, match="JSON-representable"):
        load_manifest(path)


def test_ref_to_prior_as_binding_resolves(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONBuild
      args: [id, 100]
      as: row1
    - function: JSONAddArray
      args: ['$', answers, {ref: row1}]
""")
    manifest = load_manifest(path)
    steps = manifest.udf.pipeline
    assert steps[0].as_ == "row1"
    assert steps[1].args[2] == {"ref": "row1"}


def test_ref_inside_nested_list_is_collected(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONBuild
      args: [id, 100]
      as: row1
    - function: JSONBuildArrayFromList
      args:
        - [{ref: row1}]
""")
    manifest = load_manifest(path)
    assert manifest.udf.pipeline[1].args == [{"val": [{"ref": "row1"}]}]


def test_forward_reference_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONAddArray
      args: ['$', answers, {ref: row1}]
    - function: JSONBuild
      args: [id, 100]
      as: row1
""")
    with pytest.raises(ManifestError, match="not bound by any earlier step"):
        load_manifest(path)


def test_self_reference_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONAddArray
      args: ['$', answers, {ref: row1}]
      as: row1
""")
    with pytest.raises(ManifestError, match="not bound by any earlier step"):
        load_manifest(path)


def test_duplicate_as_name_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONBuild
      args: [id, 100]
      as: row1
    - function: JSONBuild
      args: [id, 200]
      as: row1
""")
    with pytest.raises(ManifestError, match="already bound"):
        load_manifest(path)


def test_pipeline_where_every_step_binds_as_is_a_noop_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleEventUdf
  class: com.example.ExampleEventUdf
  pipeline:
    - function: WASetLogging
      args: [false]
      as: _log
""")
    with pytest.raises(ManifestError, match="no-op"):
        load_manifest(path)


def test_jsonnode_kind_defaults_source_and_target_are_none_until_resolved_by_core(tmp_path):
    # manifest.py normalizes source/target ONLY when explicitly given; UdfCore.java
    # applies the data[0] default -- see UdfSpec.source/target docstrings.
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  pipeline:
    - function: JSONPipeline
      args: ['$']
""")
    manifest = load_manifest(path)
    assert manifest.udf.source is None
    assert manifest.udf.target is None


def test_jsonnode_kind_accepts_explicit_valid_slots(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  source: data[1]
  target: userdata.result
  pipeline:
    - function: JSONPipeline
      args: ['$']
""")
    manifest = load_manifest(path)
    assert manifest.udf.source == "data[1]"
    assert manifest.udf.target == "userdata.result"


def test_jsonnode_kind_rejects_invalid_slot(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  source: nonsense
  pipeline:
    - function: JSONPipeline
      args: ['$']
""")
    with pytest.raises(ManifestError, match="udf.source"):
        load_manifest(path)


def test_jsonnode_kind_rejects_a_slot_with_a_trailing_newline(tmp_path):
    # A block-scalar/quoted-string authoring slip that appends a trailing "\n" must be
    # rejected at collection -- Python's bare `$` (unlike Java's `\z`) tolerates exactly
    # one trailing newline in non-MULTILINE mode, which would otherwise let this slip
    # past the loader and fail only once UdfCore.java's own (stricter) SLOT_RE sees it.
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  source: "data[0]\\n"
  pipeline:
    - function: JSONPipeline
      args: ['$']
""")
    with pytest.raises(ManifestError, match="udf.source"):
        load_manifest(path)


def test_jsonnode_kind_rejects_an_index_too_large_for_a_java_int(tmp_path):
    # Bounded to 9 digits (max 999,999,999) on both sides so a match can never overflow
    # a Java `int` -- before this bound, a 10+-digit index passed Python's loader only
    # to raise a raw NumberFormatException out of UdfCore.java's Integer.parseInt.
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  source: data[9999999999]
  pipeline:
    - function: JSONPipeline
      args: ['$']
""")
    with pytest.raises(ManifestError, match="udf.source"):
        load_manifest(path)


def test_jsonnode_kind_accepts_a_nine_digit_index(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  source: data[999999999]
  pipeline:
    - function: JSONPipeline
      args: ['$']
""")
    manifest = load_manifest(path)
    assert manifest.udf.source == "data[999999999]"


def test_waevent_kind_rejects_source_or_target(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleEventUdf
  class: com.example.ExampleEventUdf
  source: data[0]
  pipeline:
    - function: WAReplaceDataFromBefore
      args: ['$']
""")
    with pytest.raises(ManifestError, match="only apply when"):
        load_manifest(path)


def test_unknown_kind_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ExampleEventUdf
  class: com.example.ExampleEventUdf
  kind: bogus
  pipeline:
    - function: f
      args: ['$']
""")
    with pytest.raises(ManifestError, match="udf.kind"):
        load_manifest(path)


def test_properties_with_udf_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
properties:
  Foo: bar
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: ReferenceUdfMarkProcessed
      args: ['$']
""")
    with pytest.raises(ManifestError, match="have no meaning"):
        load_manifest(path)


def test_properties_and_password_properties_with_udf_raises(tmp_path):
    # password_properties non-empty implies properties non-empty (the key must be
    # declared there) -- both are rejected together by one combined guard.
    path, _ = _write(tmp_path, BASE + """
properties:
  Password: secret
password_properties: [Password]
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: ReferenceUdfMarkProcessed
      args: ['$']
""")
    with pytest.raises(ManifestError, match="have no meaning"):
        load_manifest(path)


def test_unknown_arg_mapping_key_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: f
      args:
        - bogus: x
""")
    with pytest.raises(ManifestError, match="unknown key 'bogus'"):
        load_manifest(path)


def test_multi_key_arg_mapping_raises(tmp_path):
    path, _ = _write(tmp_path, BASE + """
udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  pipeline:
    - function: f
      args:
        - ref: a
          json: b
""")
    with pytest.raises(ManifestError, match="exactly one"):
        load_manifest(path)
