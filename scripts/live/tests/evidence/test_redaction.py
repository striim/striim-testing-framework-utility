"""Redaction (C4 1.9.0, secret masking). Known secrets, URL userinfo, the home prefix and the
host name never reach evidence bytes; hashes are computed on the raw bytes first; identity fields are kept."""
from __future__ import annotations

import csv
import io
import json
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import canon, evidence

from .test_envelope import doc_of, good, record, report, write  # noqa: F401 - good is a fixture

SECRET = "hunter2-xyz-planted"


def _all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def test_secret_env_values_absent_from_every_evidence_byte(good, monkeypatch):
    monkeypatch.setenv("SLT_PG_TARGET_PASSWORD", SECRET)
    monkeypatch.setenv("STRIIM_PASS", "striim-pass-planted")
    comp = good._slt_data[0]
    decl = canon.declaration({"columns": {"id": "integer"}}, db_route=True)
    raw = canon.compare(canon.parse_golden(b"id,note\n1,a\n", db_route=True),
                        [{"id": 1, "note": f"row carrying {SECRET}"}], decl)
    good._slt_data = [comp, {**raw, "index": 1, "type": "data", "target": "qatarget.t", "route": "postgres-target",
                             "expected": {**raw["expected"], "source": "expected/tgt.csv", "templateSha256": comp["expected"]["templateSha256"]},
                             "actual": {**raw["actual"], "owned": True}}]
    good._slt_records = [record(), record(target="qatarget.t", status="failed")]
    item_report = report("call", "failed", f"E   AssertionError: saw striim-pass-planted and {SECRET}")
    doc = evidence.case_envelope(good, item_report, "failed")
    path = evidence.write_case_envelope(good.config.option.xmlpath, good.name, doc["run"]["runId"], doc)
    data = _all_bytes(Path(good.config.option.xmlpath).parent)
    assert SECRET.encode() not in data and b"striim-pass-planted" not in data
    written = json.loads(path.read_text())
    assert evidence.REDACTED in written["run"]["failure"] and evidence.REDACTED in json.dumps(written["data"]["comparisons"][1]["samples"])


def test_hashes_computed_before_redaction(good, monkeypatch):
    monkeypatch.setenv("SLT_PG_TARGET_PASSWORD", SECRET)
    decl = canon.declaration({}, db_route=False)
    raw = canon.compare([{"note": SECRET}], [{"note": SECRET}], decl)
    comp = good._slt_data[0]
    good._slt_data = [comp, {**raw, "index": 1, "type": "file", "target": "/opt/striim/x", "route": None,
                             "expected": {**raw["expected"], "source": "expected/tgt.csv", "templateSha256": comp["expected"]["templateSha256"]},
                             "actual": {**raw["actual"], "owned": True}}]
    good._slt_records = [record(), record(type="file", target="/opt/striim/x", db=None)]
    path = write(good)
    written = json.loads(path.read_text())["data"]["comparisons"][1]
    assert written["actual"]["sha256"] == raw["actual"]["sha256"] == canon.compare([{"note": SECRET}], [], decl)["expected"]["sha256"]
    assert written["actual"]["sha256"] != canon.compare([{"note": evidence.REDACTED}], [], decl)["expected"]["sha256"]


def test_url_userinfo_stripped(good):
    good.config._slt_striim = SimpleNamespace(url="http://admin:s3cret-userinfo@localhost:9080", mode="docker")
    rep = report("call", "failed", "E   ConnectionError: https://svc:another-pass@db.example.com:5432/x refused")
    doc = evidence.case_envelope(good, rep, "failed")
    path = evidence.write_case_envelope(good.config.option.xmlpath, good.name, doc["run"]["runId"], doc)
    text = path.read_text()
    assert "s3cret-userinfo" not in text and "another-pass" not in text
    written = json.loads(text)
    assert written["runtime"]["striim"]["observed"]["url"] == "http://localhost:9080"
    assert "https://db.example.com:5432/x" in written["run"]["failure"]


def test_host_hashed_home_prefix_replaced(good):
    host, home = socket.gethostname(), str(Path.home())
    rep = report("call", "failed", f"E   OSError: {home}/secret-dir unreadable on {host}")
    doc = evidence.case_envelope(good, rep, "failed")
    path = evidence.write_case_envelope(good.config.option.xmlpath, good.name, doc["run"]["runId"], doc)
    written = json.loads(path.read_text())
    assert written["runtime"]["hostId"] == evidence.host_id(host) and len(written["runtime"]["hostId"]) == 16
    assert written["run"]["failure"] == f"E   OSError: ~/secret-dir unreadable on {evidence.host_id(host)}"
    if len(host) >= 3:
        assert host not in path.read_text()
    assert written["runtime"]["interpreter"].startswith("~") or not written["runtime"]["interpreter"].startswith(home)


def test_identity_allowlist_kept(good, monkeypatch):
    ns = good._slt_ident.ns
    monkeypatch.setenv("SLT_SOME_TOKEN", ns)                               # an identity value that equals a secret
    rep = report("call", "failed", f"E   RuntimeError: namespace {ns} busy")
    doc = evidence.case_envelope(good, rep, "failed")
    redacted = evidence.redact(doc, evidence.known_secrets())
    assert redacted["inputs"]["bindings"]["NS"] == ns and redacted["run"]["runId"] == doc["run"]["runId"]
    assert redacted["run"]["nodeid"] == doc["run"]["nodeid"] and redacted["lifecycle"]["identity"]["namespace"] == evidence.REDACTED
    assert ns not in redacted["run"]["failure"]
    assert all(s.startswith("sha256:") for s in [redacted["inputs"]["logicalInputsSha256"]])


def _written_failure(good, text: str) -> str:
    doc = evidence.case_envelope(good, report("call", "failed", text), "failed")
    path = evidence.write_case_envelope(good.config.option.xmlpath, good.name, doc["run"]["runId"], doc)
    return json.loads(path.read_text())["run"]["failure"]


def test_short_and_resolved_credentials_redacted_at_the_writer(good, monkeypatch):
    """A short secret (the stack default ``striim``) and the credentials a service resolution
    returned are redacted as whole values and whole tokens, without mangling identity text that contains them --
    through the real envelope builder and writer, not the redactor helper alone."""
    monkeypatch.setattr(evidence, "_REGISTERED_SECRETS", set())
    evidence.register_secrets({"admin_password": "pgpw42", "host": "localhost", "port": 5432})
    monkeypatch.setenv("ADMIN_PASSWORD", "striim")
    failure = _written_failure(good, "E   OperationalError: password=striim for pgpw42 on slt-striim")
    assert failure == f"E   OperationalError: password={evidence.REDACTED} for {evidence.REDACTED} on slt-striim"
    assert "localhost" not in evidence.known_secrets()


def test_port_under_a_secret_named_key_is_not_a_secret(good, monkeypatch):
    """gcs resolves ``token_port: 4444``; a port (or any number) is not a credential, so 4444 in
    evidence text stays as it is."""
    monkeypatch.setattr(evidence, "_REGISTERED_SECRETS", set())
    evidence.register_secrets({"token_port": 4444, "api_token_port": "4445", "host": "localhost"})
    assert evidence.known_secrets(env={}) == []
    assert _written_failure(good, "E   AssertionError: expected 4444 rows, got 4443 (id=4444)") == \
        "E   AssertionError: expected 4444 rows, got 4443 (id=4444)"


@pytest.mark.parametrize("failure,secret", [("E   OperationalError: password=ab", "ab"),
                                            ("E   OperationalError: password:striim", "striim"),
                                            ("E   dsn host=db PGPASSWORD = 'x9' user=qa", "x9")],
                         ids=["password=ab", "password:striim", "PGPASSWORD-spaced-quoted"])
def test_short_credentials_redacted_through_the_real_writer(good, monkeypatch, failure, secret):
    """A known secret of any length in an explicit credential assignment, with = or :
    and optional spaces or quotes, is redacted in the written envelope."""
    monkeypatch.setenv("REVIEW_PASSWORD", secret)
    written = _written_failure(good, failure)
    assert secret not in written and evidence.REDACTED in written, written


def test_credential_delimiters_redacted_identifiers_kept_through_the_real_writer(good, monkeypatch):
    """A colon delimits a credential pair (admin:striim); an identifier that contains the
    secret (slt-striim) is kept."""
    monkeypatch.setenv("STRIIM_PASSWORD", "striim")
    written = _written_failure(good, "E   RuntimeError: slt-striim refused admin:striim (password: striim)")
    assert written == (f"E   RuntimeError: slt-striim refused admin:{evidence.REDACTED} "
                       f"(password: {evidence.REDACTED})"), written


def _decimal_comparison(good, column: str) -> None:
    comp = good._slt_data[0]
    decl = canon.declaration({"columns": {"id": column}}, db_route=True)
    cell = Decimal("1.00") if column.startswith("decimal") else 1
    raw = canon.compare([{"id": str(cell)}], [{"id": cell}], decl)
    good._slt_data = [{**raw, "index": 0, "type": "data", "target": comp["target"], "route": comp["route"],
                       "expected": {**raw["expected"], "source": comp["expected"]["source"],
                                    "templateSha256": comp["expected"]["templateSha256"]},
                       "actual": {**raw["actual"], "owned": True}}]
    return decl


def test_colliding_secret_does_not_rewrite_the_canonical_declaration(good, monkeypatch):
    """The known password ``decimal`` collides with the canonical type ``decimal:2``, which
    both semantic validation and ``declarationSha256`` read. The real writer leaves the declaration alone, so the
    passing case still validates, is written and qualifies."""
    monkeypatch.setenv("REVIEW_PASSWORD", "decimal")
    decl = _decimal_comparison(good, "decimal:2")
    doc = evidence.case_envelope(good, report(), "passed")
    evidence.validate_case(doc)
    path = evidence.write_case_envelope(good.config.option.xmlpath, good.name, doc["run"]["runId"], doc)
    written = json.loads(path.read_text())["data"]["comparisons"][0]
    assert written["declaration"]["columns"] == {"id": "decimal:2"} and written["declarationSha256"] == decl.sha256
    assert evidence.read(path).qualifies


_COLLISIONS = {"decimal-type": ("decimal", "decimal:2"), "integer-type": ("integer", None),
               "declaration-order": ("any", None), "assertion-status": ("passed", None),
               "witness-reason": ("satisfied", None), "envelope-kind": ("case", None)}


@pytest.mark.parametrize("collision", list(_COLLISIONS), ids=list(_COLLISIONS))
def test_secret_colliding_with_the_envelope_grammar_keeps_it_and_still_redacts_credentials(good, monkeypatch, collision):
    """A known secret equal to a token the schema fixes -- a column type, the declaration
    order, an assertion status, a satisfied witness, the envelope kind -- does not rewrite that token, because
    validation and the digests read it; the same secret in credential text is still redacted by the same writer."""
    secret, column = _COLLISIONS[collision]
    monkeypatch.setenv("REVIEW_PASSWORD", secret)
    if column:
        _decimal_comparison(good, column)
    doc = evidence.case_envelope(good, report(), "passed")
    path = evidence.write_case_envelope(good.config.option.xmlpath, good.name, doc["run"]["runId"], doc)
    written = json.loads(path.read_text())
    assert written["kind"] == "case" and written["run"]["status"] == "passed"
    assert written["data"]["comparisons"][0]["declaration"] == doc["data"]["comparisons"][0]["declaration"]
    assert written["data"]["comparisons"][0]["order"] == "any" and written["assertions"][0]["status"] == "passed"
    assert written["lifecycle"]["ready"]["reason"] == "satisfied"
    assert evidence.read(path).qualifies
    good.config.option.xmlpath = str(Path(good.config.option.xmlpath).parent.parent / "leak" / "junit.xml")
    failure = _written_failure(good, f"E   RuntimeError: slt-{secret} refused admin:{secret} (password={secret})")
    assert f"admin:{secret}" not in failure and f"password={secret}" not in failure and evidence.REDACTED in failure
    if len(secret) < evidence.SECRET_MIN_LEN:
        assert f"slt-{secret}" in failure, failure


@pytest.mark.parametrize("length", [1, 2, 3, 7, 8], ids=["len1", "len2", "len3", "len7", "len8"])
def test_secret_of_any_length_redacted_in_assignments_and_colon_pairs(good, monkeypatch, length):
    """Leak control: keeping the envelope's fixed grammar does not reopen an earlier leak. A
    known secret of any length is redacted as the value of an explicit assignment; from three characters also as a
    colon-delimited pair; and an identifier that merely contains a short secret is still kept."""
    secret = "z" * length
    monkeypatch.setenv("REVIEW_PASSWORD", secret)
    failure = _written_failure(good, f"E   RuntimeError: slt-{secret} refused admin:{secret} (password={secret})")
    assert f"password={secret}" not in failure and evidence.REDACTED in failure
    if length >= 3:
        assert f"admin:{secret}" not in failure, failure
    assert (f"slt-{secret}" in failure) is (length < evidence.SECRET_MIN_LEN), failure


def test_validator_rejects_an_unredacted_assigned_short_credential(good, monkeypatch):
    """Validation shares the matching rules, so an envelope that still carries an assigned
    short credential is refused as evidence-secret."""
    monkeypatch.setenv("REVIEW_PASSWORD", "ab")
    doc = evidence.case_envelope(good, report("call", "failed", "E   OperationalError: password:ab"), "failed")
    with pytest.raises(evidence.EvidenceError) as ei:
        evidence.validate_case(doc, evidence.known_secrets())
    assert ei.value.code == "evidence-secret"


def test_runtime_row_value_spelling_a_column_type_is_redacted(good, monkeypatch):
    """The grammar exemption covers the two paths the schema itself fixes -- a canonical
    declaration's columns and the spec an exact record echoes -- never a runtime value. A legacy data assertion keeps
    its raw result row; a password there that happens to spell an accepted canonical type (``binary:base64``) is
    runtime data, so the real writer redacts it and the envelope still qualifies."""
    from livetest.assertions.data import assert_data
    secret = "binary:base64"
    monkeypatch.setenv("REVIEW_PASSWORD", secret)
    row = {"columns": {"password": secret}}
    case = Path(good.manifest_path).parent
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["columns"])
    writer.writerow([str(row["columns"])])
    (case / "row.csv").write_text(stream.getvalue())
    admin = SimpleNamespace(count_rows=lambda target: 1, select_rows=lambda target: [row])
    good._slt_records.extend(assert_data(admin, [{"target": "qatarget.payload", "match": "row.csv"}], case,
                                         timeout=0, db="postgres-target"))
    path = write(good)
    assert evidence.read(path).qualifies
    written = json.loads(path.read_text())
    assert written["assertions"][-1]["actual"]["rows"][0]["columns"]["password"] == evidence.REDACTED


@pytest.mark.parametrize("key", ["COMPANY_NAME", "CLUSTER_NAME"])
def test_a_striim_licence_name_is_not_a_secret(key):
    # A Striim licence holds COMPANY_NAME=Striim: the product and company name, not a secret.
    secrets = evidence.known_secrets({key: "Striim"})
    assert "Striim" not in secrets
    assert evidence.redact_text("the running Striim version 5.4.3", secrets, home="", hostname="") == \
        "the running Striim version 5.4.3"


def test_a_password_spelled_striim_is_still_a_secret_everywhere():
    # Only licence names are exempt; the Docker cluster's default password is the same word.
    secrets = evidence.known_secrets({"STRIIM_PASS": "striim", "COMPANY_NAME": "Striim"})
    assert secrets == ["striim"]
    for text in ("sqlcmd -U qasource -P striim", "login --password striim", "admin:striim", "password=striim"):
        assert "striim" not in evidence.redact_text(text, secrets, home="", hostname=""), text


def test_another_licence_company_name_is_still_a_secret():
    secrets = evidence.known_secrets({"COMPANY_NAME": "AcmeBank"})
    assert "AcmeBank" not in evidence.redact_text("licensed to AcmeBank", secrets, home="", hostname="")


def test_a_registered_striim_licence_name_is_not_a_secret(monkeypatch):
    monkeypatch.setattr(evidence, "_REGISTERED_SECRETS", set())
    evidence.register_secrets({"COMPANY_NAME": "Striim", "admin_password": "hunter2"})
    assert evidence._REGISTERED_SECRETS == {"hunter2"}


@pytest.mark.parametrize("key", ["CLUSTER_NAME_PASSWORD", "COMPANY_NAME_TOKEN", "cluster_name_password"])
def test_a_credential_key_that_contains_a_licence_name_is_still_a_secret(key, monkeypatch):
    assert evidence.known_secrets({key: "striim"}) == ["striim"]
    monkeypatch.setattr(evidence, "_REGISTERED_SECRETS", set())
    evidence.register_secrets({key: "striim"})
    assert evidence._REGISTERED_SECRETS == {"striim"}


def test_a_prefixed_licence_name_is_exempt_too():
    assert evidence.known_secrets({"LIC_COMPANY_NAME": "Striim"}) == []
