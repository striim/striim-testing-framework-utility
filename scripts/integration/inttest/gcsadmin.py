"""GCS admin helper for the integration tier -- the object-store counterpart of
`dbroutes.py`'s per-service connection helpers, scoped to what a GCS-writing
OP's integration cases need.

Unlike `scripts/live/livetest/gcsadmin.py`'s `GcsAdmin` (a full DSV read/write
layer for the live tier's reader+writer diff scenarios), this tier drives the
operator core directly (`IntegrationProcessor`) and asserts primarily on the
WAEvent it emits -- specifically the `userdata` object-path string
the writer writes back. Two operations beyond that are needed, both
driven by a `test.yaml`'s `assert.gcs_objects:` block: `ensure_bucket` (a real
upload needs the bucket to exist first), and the `delete_objects`/
`missing_objects` pair that lets a case check the emulator genuinely RECEIVED an
object at the key `userdata` claims -- `userdata`'s value is computed from
`finalFilePath` independently of what was actually written, so the path string
alone cannot prove the write landed. Object CONTENTS are still never read back.
"""
from __future__ import annotations

import re

_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_bucket(bucket: str) -> str:
    if not _BUCKET.match(bucket):
        raise ValueError(f"unsafe bucket name: {bucket!r}")
    return bucket


def _client(tokens: dict):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    return storage.Client(
        project=tokens["GCS_PROJECT"],
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": tokens["GCS_ENDPOINT"]},
    )


def ensure_bucket(tokens: dict) -> None:
    """Create `${GCS_BUCKET}` against the emulator if it doesn't already exist.
    Idempotent, so callers (`plugin.py`'s `_ensure_gcs_bucket`) can call this on
    every test rather than once per session -- the common case is a cheap
    `exists()` check that finds the bucket already there. Mirrors
    `scripts/live/livetest/gcsadmin.py::GcsAdmin.ensure_bucket`'s TOCTOU handling:
    a concurrent xdist worker creating the same bucket between the `exists()`
    check and `create_bucket` is treated as success, not an error."""
    bucket = _check_bucket(tokens["GCS_BUCKET"])
    c = _client(tokens)
    if not c.bucket(bucket).exists():
        try:
            c.create_bucket(bucket)
        except Exception as e:  # noqa: BLE001 - see TOCTOU note above
            msg = str(e).lower()
            if "already" not in msg and "exist" not in msg and "conflict" not in msg:
                raise


def _bucket(tokens: dict):
    """The `${GCS_BUCKET}` handle, name-validated the same way `ensure_bucket` does."""
    return _client(tokens).bucket(_check_bucket(tokens["GCS_BUCKET"]))


def delete_objects(tokens: dict, keys) -> None:
    """Delete each key in `keys` from `${GCS_BUCKET}`, treating an absent object as
    success. Callers use this to PRUNE the exact keys they are about to assert on,
    before the operator runs -- see `plugin.py::_prune_gcs_objects` for why that is a
    correctness requirement rather than hygiene. Only the named keys are touched:
    never a prefix, never the bucket, so `services/gcs/README.md`'s one-fixed-bucket
    lifecycle still holds for every other test's objects.

    Unlike `ensure_bucket`'s substring-based "already exists" tolerance (a faithful
    port of the live tier's, kept in sync with it deliberately), this catches
    `google.api_core.exceptions.NotFound` precisely -- there is no live-tier twin to
    stay bug-compatible with, so the tighter form applies here from the start."""
    from google.api_core.exceptions import NotFound

    bucket = _bucket(tokens)
    for key in keys:
        try:
            bucket.blob(key).delete()
        except NotFound:
            pass  # nothing to prune -- the normal first-run case


def missing_objects(tokens: dict, keys) -> list:
    """Return the subset of `keys` that does NOT exist in `${GCS_BUCKET}`, preserving
    the caller's order. Existence only -- object bytes are never downloaded (this tier
    has no fixture format for expected binary contents; that stays a unit-tier claim,
    `GcsBinaryFileWriterTest`)."""
    bucket = _bucket(tokens)
    return [key for key in keys if not bucket.blob(key).exists()]


def delete_prefix(tokens: dict, prefix: str) -> int:
    """Delete every object in `${GCS_BUCKET}` whose key starts with `prefix`, returning how many.
    The perf tier's per-iteration reset (`performance.gcs_prune_prefix`, PERF_SPEC.md §9) uses
    it so each measured iteration writes into an empty folder. An empty prefix would empty the
    shared bucket, so it is refused rather than honoured."""
    if not prefix or not prefix.strip("/"):
        raise ValueError(f"refusing to prune GCS objects under an empty prefix ({prefix!r})")
    deleted = 0
    for blob in _bucket(tokens).list_blobs(prefix=prefix):
        blob.delete()
        deleted += 1
    return deleted
