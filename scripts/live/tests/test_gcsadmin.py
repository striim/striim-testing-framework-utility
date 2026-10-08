import pytest
from livetest.gcsadmin import GcsAdmin, _parse_dsv, _check_bucket

# ---- pure helpers -----------------------------------------------------------

def test_parse_dsv_positional_and_skips_blanks():
    assert _parse_dsv("1,alpha\n2,bravo\n\n") == [
        {"c0": "1", "c1": "alpha"}, {"c0": "2", "c1": "bravo"}]

def test_check_bucket_rejects_bad():
    assert _check_bucket("slt-src") == "slt-src"
    with pytest.raises(ValueError):
        _check_bucket("bad/bucket")

# ---- admin with an injected fake storage client -----------------------------

class _Blob:
    def __init__(self, store, name): self._store = store; self.name = name
    def upload_from_string(self, content, content_type=None): self._store[self.name] = content
    def download_as_text(self): return self._store[self.name]
    def delete(self): self._store.pop(self.name, None)

class _Bucket:
    def __init__(self, storage, name): self._s = storage; self.name = name
    def exists(self): return self.name in self._s.store
    def blob(self, name): return _Blob(self._s.store.setdefault(self.name, {}), name)
    def delete(self): self._s.store.pop(self.name, None)

class _FakeStorage:
    def __init__(self): self.store = {}   # {bucket: {object: content}}
    def bucket(self, name): return _Bucket(self, name)
    def create_bucket(self, name): self.store.setdefault(name, {})
    def list_blobs(self, bucket): return [_Blob(self.store.get(bucket, {}), n)
                                          for n in self.store.get(bucket, {})]

def _admin(fake):
    return GcsAdmin({"endpoint": "http://localhost:4443", "project": "test-project",
                     "seed_bucket": "slt-src"}, client=fake)

def test_ensure_bucket_creates_when_absent():
    fake = _FakeStorage()
    _admin(fake).ensure_bucket("slt-src")
    assert "slt-src" in fake.store

def test_ensure_bucket_tolerates_conflict_race():
    # TOCTOU: exists()->False then a concurrent creator wins -> create raises Conflict.
    # ensure_bucket must swallow it as success (mirrors kafkaadmin.ensure_topic).
    class _ConflictStorage(_FakeStorage):
        def create_bucket(self, name):
            raise Exception("409 Conflict: bucket already exists")
    _admin(_ConflictStorage()).ensure_bucket("slt-src")   # must not raise


def test_ensure_bucket_reraises_non_conflict_error():
    class _BoomStorage(_FakeStorage):
        def create_bucket(self, name):
            raise Exception("403 permission denied")
    with pytest.raises(Exception, match="permission denied"):
        _admin(_BoomStorage()).ensure_bucket("slt-src")


def test_run_sql_uploads_seed_to_source_bucket():
    fake = _FakeStorage(); fake.create_bucket("slt-src")
    _admin(fake).run_sql("1,alpha\n2,bravo\n")
    assert fake.store["slt-src"]  # one object written
    rows = _admin(fake).select_rows("slt-src")
    assert rows == [{"c0": "1", "c1": "alpha"}, {"c0": "2", "c1": "bravo"}]

def test_select_rows_reads_all_objects_in_bucket():
    fake = _FakeStorage(); fake.create_bucket("slt-tgt")
    fake.store["slt-tgt"] = {"a.csv": "1,x\n", "b.csv": "2,y\n"}
    rows = _admin(fake).select_rows("slt-tgt")
    assert {"c0": "1", "c1": "x"} in rows and {"c0": "2", "c1": "y"} in rows

def test_count_rows():
    fake = _FakeStorage(); fake.store["slt-tgt"] = {"a.csv": "1,x\n2,y\n3,z\n"}
    assert _admin(fake).count_rows("slt-tgt") == 3

def test_clear_bucket_empties_objects():
    fake = _FakeStorage(); fake.store["slt-tgt"] = {"a.csv": "1,x\n", "b.csv": "2,y\n"}
    _admin(fake).clear_bucket("slt-tgt")
    assert fake.store["slt-tgt"] == {}

def test_clear_bucket_noop_when_absent():
    fake = _FakeStorage()
    _admin(fake).clear_bucket("slt-tgt")   # must not raise when the bucket doesn't exist
    assert "slt-tgt" not in fake.store


def test_delete_bucket_removes_objects_then_bucket():
    fake = _FakeStorage()
    fake.create_bucket("slt-t1-src")
    fake.store["slt-t1-src"]["obj.csv"] = "1,a\n"
    _admin(fake).delete_bucket("slt-t1-src")
    assert "slt-t1-src" not in fake.store


def test_delete_bucket_is_a_noop_when_absent():
    fake = _FakeStorage()
    _admin(fake).delete_bucket("slt-nonexistent-src")   # must not raise
