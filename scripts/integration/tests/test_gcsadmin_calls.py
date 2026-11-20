"""The calls `inttest.gcsadmin` makes on the storage client, against a recording fake.

`test_services_gcs.py` drives these against the GCS emulator and is `docker`-marked, so the
release gate (`-m "not docker"`) never ran them. `_client` is replaced by a fake that records
each call; nothing reaches an endpoint.
"""
from __future__ import annotations

import pytest
from google.api_core.exceptions import NotFound

from inttest import gcsadmin

TOKENS = {"GCS_PROJECT": "test-project", "GCS_ENDPOINT": "http://gcs.local:4443", "GCS_BUCKET": "slt-bucket"}


class _Client:
    def __init__(self, present=(), bucket_exists=True, create_error=None):
        self.log, self.present = [], set(present)
        self.bucket_exists, self.create_error = bucket_exists, create_error

    def bucket(self, name):
        return _Bucket(self, name)

    def create_bucket(self, name):
        self.log.append(("create_bucket", name))
        if self.create_error:
            raise self.create_error


class _Bucket:
    def __init__(self, client, name):
        self.c, self.name = client, name

    def exists(self):
        self.c.log.append(("bucket_exists", self.name))
        return self.c.bucket_exists

    def blob(self, key):
        return _Blob(self.c, self.name, key)

    def list_blobs(self, prefix):
        self.c.log.append(("list_blobs", self.name, prefix))
        return [_Blob(self.c, self.name, k) for k in sorted(self.c.present) if k.startswith(prefix)]


class _Blob:
    def __init__(self, client, bucket, key):
        self.c, self.bucket, self.key = client, bucket, key

    def exists(self):
        self.c.log.append(("exists", self.bucket, self.key))
        return self.key in self.c.present

    def delete(self):
        self.c.log.append(("delete", self.bucket, self.key))
        if self.key not in self.c.present:
            raise NotFound(f"No such object: {self.bucket}/{self.key}")
        self.c.present.discard(self.key)


@pytest.fixture
def client(monkeypatch):
    holder = {}

    def use(**kw):
        holder["c"] = _Client(**kw)
        monkeypatch.setattr(gcsadmin, "_client", lambda tokens: holder["c"])
        return holder["c"]
    return use


def test_ensure_bucket_creates_a_missing_bucket(client):
    c = client(bucket_exists=False)
    gcsadmin.ensure_bucket(TOKENS)
    assert c.log == [("bucket_exists", "slt-bucket"), ("create_bucket", "slt-bucket")]


def test_ensure_bucket_leaves_an_existing_bucket_alone(client):
    c = client(bucket_exists=True)
    gcsadmin.ensure_bucket(TOKENS)
    assert c.log == [("bucket_exists", "slt-bucket")]


def test_ensure_bucket_treats_a_concurrent_create_as_success_and_raises_anything_else(client):
    client(bucket_exists=False, create_error=RuntimeError("409 Conflict: bucket already exists"))
    gcsadmin.ensure_bucket(TOKENS)
    client(bucket_exists=False, create_error=RuntimeError("403 Forbidden"))
    with pytest.raises(RuntimeError, match="403"):
        gcsadmin.ensure_bucket(TOKENS)


def test_an_unsafe_bucket_name_is_refused_before_any_call(client):
    c = client()
    with pytest.raises(ValueError, match="unsafe bucket name"):
        gcsadmin.ensure_bucket({**TOKENS, "GCS_BUCKET": "../other"})
    assert c.log == []


def test_delete_objects_deletes_exactly_the_named_keys_and_absent_ones_are_fine(client):
    c = client(present={"run/a.txt", "run/b.txt", "other/c.txt"})
    gcsadmin.delete_objects(TOKENS, ["run/a.txt", "run/missing.txt"])
    assert c.log == [("delete", "slt-bucket", "run/a.txt"), ("delete", "slt-bucket", "run/missing.txt")]
    assert c.present == {"run/b.txt", "other/c.txt"}


def test_missing_objects_reports_absent_keys_in_the_callers_order(client):
    client(present={"run/b.txt"})
    assert gcsadmin.missing_objects(TOKENS, ["run/c.txt", "run/b.txt", "run/a.txt"]) == ["run/c.txt", "run/a.txt"]


def test_delete_prefix_deletes_only_under_the_prefix_and_counts(client):
    c = client(present={"perf/1/a", "perf/1/b", "perf/10/c", "keep/d"})
    assert gcsadmin.delete_prefix(TOKENS, "perf/1/") == 2
    assert c.present == {"perf/10/c", "keep/d"}
    assert c.log[0] == ("list_blobs", "slt-bucket", "perf/1/")


@pytest.mark.parametrize("prefix", ["", "/", "//"])
def test_delete_prefix_refuses_an_empty_prefix_before_listing(client, prefix):
    c = client(present={"keep/d"})
    with pytest.raises(ValueError, match="empty prefix"):
        gcsadmin.delete_prefix(TOKENS, prefix)
    assert c.log == [] and c.present == {"keep/d"}
