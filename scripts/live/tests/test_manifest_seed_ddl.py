import warnings

import pytest
from livetest.manifest import load_manifest, ManifestError

def _write(tmp_path, body):
    p = tmp_path / "test.yaml"
    p.write_text(body)
    return p

def _load_no_warn(path):
    # Canonical forms must parse silently — a DeprecationWarning here is a bug.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        return load_manifest(path)

_BASE = "name: x\ntql: app.tql\nassert:\n  smoke: true\n"

def test_seed_entry_when_defaults_to_pre_deploy(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + "seed:\n  - {file: s.sql, db: oracle-source}\n"))
    assert m.seed_files == [("oracle-source", "s.sql", "pre_deploy", 0.0)]

def test_seed_entry_when_post_start_accepted(tmp_path):
    m = load_manifest(_write(
        tmp_path, _BASE + "seed:\n  - {file: s.sql, db: oracle-source, when: post_start}\n"))
    assert m.seed_files == [("oracle-source", "s.sql", "post_start", 0.0)]

def test_seed_entries_can_choose_different_phases(tmp_path):
    # The point of moving `when` onto the entry: one manifest, two lifecycle points.
    m = load_manifest(_write(tmp_path, _BASE + (
        "seed:\n"
        "  - {file: a.sql, db: oracle-source}\n"
        "  - {file: b.sql, db: oracle-source, when: post_start, after: 30s}\n")))
    assert m.seed_files == [("oracle-source", "a.sql", "pre_deploy", 0.0),
                            ("oracle-source", "b.sql", "post_start", 30.0)]

def test_scalar_seed_form_keeps_the_four_tuple_arity(tmp_path):
    # `seed: s.sql` and `seed: [s.sql]` are still legal shorthand. They must produce the same
    # arity as the mapping form -- the runner unpacks (db, file, when, after), so a 2-tuple
    # here is a ValueError at seed time, on the scalar-form tests only.
    for body in ("seed: s.sql\n", "seed:\n  - s.sql\n"):
        m = load_manifest(_write(tmp_path, _BASE + body))
        assert m.seed_files == [("postgres-source", "s.sql", "pre_deploy", 0.0)]

def test_bad_seed_entry_when_raises(tmp_path):
    with pytest.raises(ManifestError, match="when"):
        load_manifest(_write(
            tmp_path, _BASE + "seed:\n  - {file: s.sql, when: whenever}\n"))

def test_removed_manifest_wide_seed_when_is_rejected(tmp_path):
    # `seed_when:` was the manifest-wide predecessor of the per-entry `when:`. It is no longer
    # read, and an unread key must not load silently: a stale manifest would fall back to the
    # pre_deploy default, which for a CDC test seeds before the reader starts. Caught by the
    # generic unknown-key rule rather than a special case, so typos are caught the same way.
    with pytest.raises(ManifestError, match=r"unknown manifest key\(s\) \['seed_when'\]"):
        load_manifest(_write(tmp_path, _BASE + "seed_when: post_start\n"))

@pytest.mark.parametrize("key", ["timout: 30", "tag: [x]", "asserts:\n  smoke: true"])
def test_unknown_manifest_keys_are_rejected(tmp_path, key):
    with pytest.raises(ManifestError, match="unknown manifest key"):
        load_manifest(_write(tmp_path, _BASE + key + "\n"))

def test_every_documented_key_is_accepted(tmp_path):
    # The guard is only safe if the allow-list is complete -- a key the loader reads but the set
    # omits would start rejecting valid manifests.
    body = _BASE + (
        "purpose: p\ntopology: single\nrequires: [postgres]\n"
        "ddl: d.sql\nseed: s.sql\ntimeout: 60\ntags: [x]\n"
        "disabled: reason\ndisabled_parallel: reason\n"
        "expect_halt: true\nexpect_halt_contains: [boom]\n")
    m = load_manifest(_write(tmp_path, body))
    assert m.name == "x" and m.timeout == 60

@pytest.mark.parametrize("text,seconds", [
    ("30s", 30.0), ("500ms", 0.5), ("2m", 120.0), ("45", 45.0), (45, 45.0), (1.5, 1.5)])
def test_after_duration_forms(tmp_path, text, seconds):
    m = load_manifest(_write(tmp_path, _BASE + (
        f"seed:\n  - {{file: s.sql, db: oracle-source, when: post_start, after: {text!r}}}\n")))
    assert m.seed_files[0][3] == seconds

def test_after_with_pre_deploy_raises(tmp_path):
    # An `after` that silently did nothing would hide the author's intent.
    with pytest.raises(ManifestError, match="only meaningful with 'when: post_start'"):
        load_manifest(_write(
            tmp_path, _BASE + "seed:\n  - {file: s.sql, when: pre_deploy, after: 30s}\n"))

def test_after_without_when_raises_because_default_is_pre_deploy(tmp_path):
    with pytest.raises(ManifestError, match="only meaningful with 'when: post_start'"):
        load_manifest(_write(tmp_path, _BASE + "seed:\n  - {file: s.sql, after: 30s}\n"))

@pytest.mark.parametrize("bad", ["soon", "-5s", "0s", "30 sec", "true"])
def test_bad_after_values_raise(tmp_path, bad):
    with pytest.raises(ManifestError, match="after"):
        load_manifest(_write(
            tmp_path, _BASE + f"seed:\n  - {{file: s.sql, when: post_start, after: {bad!r}}}\n"))

def test_ddl_entry_rejects_when_and_after(tmp_path):
    # ddl has exactly one lifecycle point; accepting the keys would imply otherwise.
    for key in ("when: post_start", "after: 30s"):
        with pytest.raises(ManifestError, match="only 'seed' entries accept"):
            load_manifest(_write(tmp_path, _BASE + f"ddl:\n  - {{file: a.sql, {key}}}\n"))

def test_ddl_string_normalizes_to_postgres(tmp_path):
    m = _load_no_warn(_write(tmp_path, _BASE + "ddl: ddl.sql\nseed: seed.sql\n"))
    assert m.ddl_files == [("postgres-source", "ddl.sql")]
    assert m.seed_files == [("postgres-source", "seed.sql", "pre_deploy", 0.0)]

def test_ddl_list_canonical_file_db_form(tmp_path):
    # The canonical list entry: `- file: <name>` + optional `db: <route>` (the same
    # style upload:/server_files: entries use). No deprecation warning.
    body = _BASE + (
        "ddl:\n"
        "  - file: source_oracle_ddl.sql\n"
        "    db: oracle-source\n"
        "  - file: target_postgres_ddl.sql\n"
        "    db: postgres-target\n"
    )
    m = _load_no_warn(_write(tmp_path, body))
    assert m.ddl_files == [("oracle-source", "source_oracle_ddl.sql"),
                           ("postgres-target", "target_postgres_ddl.sql")]

def test_ddl_list_bare_string_item_defaults_route(tmp_path):
    # A bare filename item routes to postgres-source, like upload:'s plain-string form.
    m = _load_no_warn(_write(tmp_path, _BASE + "ddl:\n  - ddl.sql\n"))
    assert m.ddl_files == [("postgres-source", "ddl.sql")]

def test_ddl_entry_without_db_defaults_route(tmp_path):
    m = _load_no_warn(_write(tmp_path, _BASE + "ddl:\n  - file: ddl.sql\n"))
    assert m.ddl_files == [("postgres-source", "ddl.sql")]

def test_ddl_deprecated_source_db_key_parses_with_warning(tmp_path):
    p = _write(tmp_path, _BASE + "ddl:\n  - {source_db: oracle-source, file: a.sql}\n")
    with pytest.warns(DeprecationWarning, match=r"deprecated 'source_db:' route key"):
        m = load_manifest(p)
    assert m.ddl_files == [("oracle-source", "a.sql")]   # still routed identically

def test_seed_deprecated_target_db_key_parses_with_warning_naming_file(tmp_path):
    p = _write(tmp_path, _BASE + "seed:\n  - {target_db: postgres-target, file: s.sql}\n")
    with pytest.warns(DeprecationWarning, match=r"test\.yaml.*'seed' entry") :
        m = load_manifest(p)
    assert m.seed_files == [("postgres-target", "s.sql", "pre_deploy", 0.0)]

def test_ddl_list_item_missing_file_raises(tmp_path):
    with pytest.raises(ManifestError, match="file"):
        load_manifest(_write(tmp_path, _BASE + "ddl:\n  - {db: oracle}\n"))

def test_no_ddl_gives_empty_list(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE))
    assert m.ddl_files == [] and m.seed_files == []
