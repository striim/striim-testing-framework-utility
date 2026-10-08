

# --- §85.3 / §127: the dataclass and the builder must agree ---------------------------------


def test_assertion_result_dataclass_round_trips_through_validation():
    """⚠ Found in review, where it did NOT.

    `AssertionResult` is the declared shape and `TestResult.assertions` is documented as a list of
    `AssertionResult.to_dict()`. Adding `elapsed_s` to the dataclass made `to_dict()` emit it
    unconditionally as null — and `elapsed_s` is pinned to `diff`, so EVERY non-diff record it
    produced failed validation. Nothing caught it because the dataclass has no live caller; the
    builder is what everything actually uses. This test makes the two agree by construction.
    """
    from livetest.resultschema import AssertionResult, _validate_assertion

    for a_type in ("data", "diff", "smoke", "halt"):
        record = AssertionResult(type=a_type, status="passed", spec={}).to_dict()
        assert "elapsed_s" not in record, f"{a_type}: unset elapsed must be OMITTED, not null"
        _validate_assertion(record, f"dataclass[{a_type}]")

    timed = AssertionResult(type="diff", status="passed", spec={}, elapsed_s=2.5).to_dict()
    assert timed["elapsed_s"] == 2.5
    _validate_assertion(timed, "dataclass[diff+elapsed]")


def test_builder_and_dataclass_produce_the_same_keys():
    """The two constructors must not drift: one is the schema, the other is what runs."""
    from livetest.resultschema import AssertionResult, build_assertion_result

    built = build_assertion_result(type="diff", status="passed", spec={})
    declared = AssertionResult(type="diff", status="passed", spec={}).to_dict()
    assert set(built) == set(declared), f"builder {sorted(built)} != dataclass {sorted(declared)}"

    built_t = build_assertion_result(type="diff", status="passed", spec={}, elapsed_s=1.0)
    declared_t = AssertionResult(type="diff", status="passed", spec={}, elapsed_s=1.0).to_dict()
    assert set(built_t) == set(declared_t)
