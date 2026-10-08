"""Run + case + worker resource identity (C7.5).

Two simultaneous executions of the same case name get distinct identities, a serial execution
gets non-empty run isolation, and every length budget holds. Pure: ``livetest.runident`` plus
``livetest.services.derive_per_test_base``.
"""
from __future__ import annotations

import re

import pytest

from livetest import runident

RUN = "20260914T230000Z-abcd1234"
CASE = "postgres-cdc-diff"


def _env(**kv):
    return {"SLT_RUN_EPOCH": RUN, **kv}


def _names(i):
    return (i.per_test, i.tid, i.tid_upper, i.tid_oracle, i.ns, i.app, i.app_bare, i.pg_slot, i.owned_dir)


def test_same_case_different_run_epoch_differs():
    a = runident.derive(CASE, _env())
    b = runident.derive(CASE, _env(SLT_RUN_EPOCH="20260914T230001Z-0badc0de"))
    assert a.per_test != b.per_test and a.ns != b.ns and a.tid != b.tid and a.pg_slot != b.pg_slot


def test_same_run_different_worker_differs():
    a = runident.derive(CASE, _env(PYTEST_XDIST_WORKER="gw0"))
    b = runident.derive(CASE, _env(PYTEST_XDIST_WORKER="gw1"))
    c = runident.derive(CASE, _env())
    assert len({a.per_test, b.per_test, c.per_test}) == 3
    assert (a.worker, b.worker, c.worker) == ("gw0", "gw1", "w0")


def test_same_run_same_worker_same_case_is_deterministic():
    a = runident.derive(CASE, _env(PYTEST_XDIST_WORKER="gw2"))
    b = runident.derive(CASE, _env(PYTEST_XDIST_WORKER="gw2"))
    assert _names(a) == _names(b)          # teardown recomputes the same names; attempt excluded


def test_tid_always_non_empty_serial():
    i = runident.derive(CASE, _env())       # no PYTEST_XDIST_WORKER, no SLT_PARALLEL
    assert i.tid and i.tid_upper and i.tid_oracle
    t = runident.tokens(i)
    assert t["TID"] == i.tid != "" and t["TID_ORACLE"] == i.tid_oracle != ""


def test_tid_oracle_is_eleven_chars_upper_letter_first():
    for case in (CASE, "1-starts-with-digit", "spanner-json-feedback-submission-very-long-name"):
        o = runident.derive(case, _env()).tid_oracle
        assert len(o) == 11 and re.fullmatch(r"T[0-9A-F]{9}_", o), o


def test_tid_is_lowercase_with_trailing_underscore():
    i = runident.derive(CASE, _env())
    assert re.fullmatch(r"t[0-9a-f]{9}_", i.tid) and i.per_test == i.tid[:-1]


def test_tid_and_tid_oracle_share_body():
    i = runident.derive(CASE, _env())
    assert i.tid.upper() == i.tid_oracle == i.tid_upper


def test_ns_and_app_shapes():
    i = runident.derive(CASE, _env())
    assert i.ns == f"SLT_postgres_cdc_diff_{i.per_test}"
    assert i.app == f"{i.ns}.postgres_cdc_diffApp" and i.app_bare == "postgres_cdc_diffApp"
    assert i.ns_truncated is False and i.slug == "postgres_cdc_diff"
    d = runident.derive("2pc-case", _env())
    assert d.slug == "t2pc_case" and d.ns.startswith("SLT_2pc_case_")   # NS keeps the plugin _slug shape
    t = runident.tokens(i)
    assert (t["NS"], t["APP"], t["APP_BARE"]) == (i.ns, i.app, i.app_bare)


def test_ns_is_at_most_40_chars_for_a_50_char_slug_and_records_truncation():
    case = "a" * 25 + "-" + "b" * 24                   # slug of exactly 50 characters
    i = runident.derive(case, _env())
    assert len(runident._case_slug(case)) == 50
    assert len(i.ns) == 40 and i.ns == f"SLT_{runident._case_slug(case)[:25]}_{i.per_test}"
    assert i.ns_truncated is True
    assert re.fullmatch(r"[A-Za-z0-9_-]+", i.ns)       # striimfile._SAFE_NAME for checkpoint cleanup
    two = runident.derive("a" * 25 + "-" + "b" * 30, _env())
    assert two.ns[:29] == i.ns[:29] and two.ns != i.ns  # same truncated slug, distinct per_test


def test_ns_leaves_room_for_a_component_name_spanner_accepts():
    # SpannerWriter fails "Insufficient Privilege to get Dialect" once
    # <NS>.<component> reaches 64 characters; 62 is the longest that passed. The longest PT case
    # name must still fit with SpannerTarget, and with a component of COMPONENT_MAX characters.
    i = runident.derive("spanner-json-array-nested-element-pk-update", _env())
    assert len(f"{i.ns}.SpannerTarget") <= 62
    assert runident.NS_MAX + 1 + runident.COMPONENT_MAX <= runident.FULL_NAME_MAX == 62
    assert runident.COMPONENT_MAX >= len("LookupOpStream")
    d = runident.derive("spanner-json-array-nested-element-pk-update", _env(PYTEST_XDIST_WORKER="gw1"))
    assert d.ns != i.ns and d.ns[:29] == i.ns[:29]    # truncated slug, per-test hash still unique


def test_pg_slot_is_slt_plus_per_test_and_within_63():
    i = runident.derive("x" * 80, _env())
    assert i.pg_slot == f"slt_{i.per_test}" and len(i.pg_slot) == 14 <= 63
    assert re.fullmatch(r"slt_t[0-9a-f]{9}", i.pg_slot)


def test_missing_run_epoch_named_error():
    for env in ({}, {"SLT_RUN_EPOCH": ""}):
        with pytest.raises(runident.IdentityError, match="SLT_RUN_EPOCH is not set"):
            runident.derive(CASE, env)


def test_tokens_include_run_id_worker_attempt():
    i = runident.derive(CASE, _env(PYTEST_XDIST_WORKER="gw3"), attempt="0a1b2c3d")
    t = runident.tokens(i)
    assert t["RUN_ID"] == RUN and t["WORKER"] == "gw3" and t["ATTEMPT"] == "0a1b2c3d"
    assert not any(k.startswith("SENTINEL") for k in t)   # sentinel tokens arrive with increment 2


def test_run_id_kept_in_full_not_suffix():
    a = runident.derive(CASE, _env())
    b = runident.derive(CASE, _env(SLT_RUN_EPOCH="20260915T000000Z-abcd1234"))   # same 8-hex suffix
    assert a.run_id == RUN and runident.record(a)["runId"] == RUN
    assert a.per_test != b.per_test, "the whole run id is hashed, not only its uuid suffix"


def test_attempt_differs_per_derive_call_and_is_not_in_any_name():
    a = runident.derive(CASE, _env())
    b = runident.derive(CASE, _env())
    assert re.fullmatch(r"[0-9a-f]{8}", a.attempt) and a.attempt != b.attempt
    assert _names(a) == _names(b)
    assert not any(a.attempt in n for n in _names(a))


def test_derive_per_test_base_receives_new_per_test():
    from livetest.services import derive_per_test_base
    i = runident.derive(CASE, _env())
    base = {"src_topic": "slt_src", "tgt_topic": "slt_tgt"}
    out = derive_per_test_base("kafka", base, i.per_test, env={})
    assert out["src_topic"] == f"slt_{i.per_test}_src" and out["tgt_topic"] == f"slt_{i.per_test}_tgt"
    assert re.fullmatch(r"slt_t[0-9a-f]{9}_src", out["src_topic"])
    other = runident.derive(CASE, _env(SLT_RUN_EPOCH="another-run"))
    assert derive_per_test_base("kafka", base, other.per_test, env={})["src_topic"] != out["src_topic"]


def test_owned_dir_token_and_shape():
    i = runident.derive(CASE, _env())
    assert i.owned_dir == f"/opt/striim/slt-runs/{i.ns}" and runident.tokens(i)["OWNED_DIR"] == i.owned_dir
    assert i.attempt not in i.owned_dir
    assert runident.derive(CASE, _env(SLT_RUN_EPOCH="other")).owned_dir != i.owned_dir
