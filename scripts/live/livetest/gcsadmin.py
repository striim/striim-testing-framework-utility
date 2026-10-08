from __future__ import annotations
import csv
import io
import re

# GCS admin client — the object-store counterpart to PgAdmin/OraAdmin/SpannerAdmin,
# on google-cloud-storage against the fake-gcs-server emulator. Used to ensure the
# buckets exist, seed a source object, and read objects back for assertions.
#
# GCS has no SQL: "seed" (run_sql) uploads the rendered file content as an object in
# the source bucket, and select_rows(bucket) reads every object in a bucket and parses
# it as DSV/CSV into positional rows ({c0, c1, ...}) so the existing diff tier works.
#
# Connects with: explicit api_endpoint + anonymous credentials (the emulator needs no auth).

_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

def _check_bucket(bucket: str) -> str:
    if not _BUCKET.match(bucket):
        raise ValueError(f"unsafe bucket name: {bucket!r}")
    return bucket

def _parse_dsv(text: str) -> list[dict]:
    # DSV == CSV (comma-delimited). Positional columns -> {c0, c1, ...}; skip blanks.
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if rec and any(f.strip() for f in rec):
            rows.append({f"c{i}": v for i, v in enumerate(rec)})
    return rows

class GcsAdmin:
    def __init__(self, dsn: dict, client=None):
        # dsn: {endpoint, project, seed_bucket}
        self.dsn = dsn
        self._client = client
        self._seed_n = 0

    def _storage(self):
        if self._client is not None:
            return self._client
        from google.cloud import storage
        from google.auth.credentials import AnonymousCredentials
        return storage.Client(project=self.dsn["project"],
                              credentials=AnonymousCredentials(),
                              client_options={"api_endpoint": self.dsn["endpoint"]})

    def ensure_bucket(self, bucket: str) -> None:
        c = self._storage()
        if not c.bucket(_check_bucket(bucket)).exists():
            try:
                c.create_bucket(bucket)
            except Exception as e:                     # noqa: BLE001
                # TOCTOU: another concurrent resolve (or a retry) can create the bucket
                # between exists()->False and create_bucket. Treat an "already exists" /
                # Conflict race as success; re-raise anything else. (Mirrors
                # kafkaadmin.ensure_topic's "already exists" handling.)
                msg = str(e).lower()
                if "already" not in msg and "exist" not in msg and "conflict" not in msg:
                    raise

    def clear_bucket(self, bucket: str) -> None:
        # Delete every object so each test starts from a clean bucket. Otherwise a prior
        # run's objects (e.g. a matching target) satisfy the diff instantly -> false pass.
        c = self._storage()
        if c.bucket(_check_bucket(bucket)).exists():
            for blob in c.list_blobs(bucket):
                blob.delete()

    def delete_bucket(self, bucket: str) -> None:
        # DELETE without recreate (unlike clear_bucket, which empties-and-keeps for
        # per-test setup) -- used at per-test TEARDOWN now that buckets are per-test
        # (derive_per_test_base), so nothing else needs this bucket again.
        c = self._storage()
        b = c.bucket(_check_bucket(bucket))
        if b.exists():
            for blob in c.list_blobs(bucket):
                blob.delete()
            b.delete()

    def run_sql(self, content: str) -> None:
        # Seed: upload the content as a DSV object in the source bucket.
        name = f"seed-{self._seed_n}.csv"
        self._seed_n += 1
        self._storage().bucket(self.dsn["seed_bucket"]).blob(name).upload_from_string(
            content, content_type="text/csv")

    def read_object_bytes(self, bucket: str, name: str) -> bytes | None:
        # Raw bytes of one object (None if absent) — for asserting a binary writer's
        # output uploaded the exact source blob.
        c = self._storage()
        blob = c.bucket(_check_bucket(bucket)).get_blob(name)
        return None if blob is None else blob.download_as_bytes()

    def count_rows(self, bucket: str) -> int:
        return len(self.select_rows(bucket))

    def select_rows(self, bucket: str) -> list[dict]:
        c = self._storage()
        rows = []
        for blob in c.list_blobs(_check_bucket(bucket)):
            rows.extend(_parse_dsv(blob.download_as_text()))
        return rows
