"""`seed: when: post_recover`: seeds that run after the recover: restore."""

import pytest
from livetest.manifest import load_manifest, ManifestError


def _write(tmp_path, body):
    p = tmp_path / "test.yaml"
    p.write_text(body)
    return p


_BASE = "name: x\ntql: app.tql\nassert:\n  smoke: true\n"
_RECOVER = ("recover:\n  mode: kill\n  after: 5\n  settle: 30\n"
            "assert:\n  smoke: true\n  data:\n    - {target: s.t, min_rows: 1}\n")


def test_post_recover_seed_with_an_offset_is_accepted_under_recover(tmp_path):
    m = load_manifest(_write(tmp_path, "name: x\ntql: app.tql\n" + _RECOVER + (
        "seed:\n"
        "  - {file: a.sql, db: oracle-source, when: post_start, after: 10s}\n"
        "  - {file: late_parent.sql, db: oracle-source, when: post_recover, after: 15s}\n")))
    assert m.seed_files == [("oracle-source", "a.sql", "post_start", 10.0),
                            ("oracle-source", "late_parent.sql", "post_recover", 15.0)]


def test_post_recover_seed_without_recover_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="post_recover"):
        load_manifest(_write(tmp_path, _BASE + (
            "seed:\n  - {file: late.sql, db: oracle-source, when: post_recover}\n")))


def test_post_recover_seed_when_restore_stays_stopped_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="post_recover.*expect_running"):
        load_manifest(_write(tmp_path, (
            "name: x\ntql: app.tql\n"
            "recover:\n  mode: stop\n  after: 5\n  expect_running: false\n"
            "assert:\n  data:\n    - {target: s.t, min_rows: 1}\n"
            "seed:\n  - {file: late.sql, db: oracle-source, when: post_recover}\n")))


def test_post_recover_is_seed_only(tmp_path):
    with pytest.raises(ManifestError, match="server_files"):
        load_manifest(_write(tmp_path, "name: x\ntql: app.tql\n" + _RECOVER + (
            "server_files:\n  - {file: f.csv, dest: /tmp/x/f.csv, when: post_recover}\n")))
