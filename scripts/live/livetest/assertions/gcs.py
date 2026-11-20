from __future__ import annotations

import hashlib

from livetest.assertions import AssertionFailed, poll_holding, refuse_unknown_keys
from livetest.resultschema import build_assertion_result

# Assert a GCS object's CONTENT (for a binary writer): the
# uploaded object exists and its bytes match the source blob, by exact hex, sha256, or
# size. Polls until satisfied (the upload is asynchronous) or the timeout expires. A spec that
# expects an empty object matches an object created before its bytes arrive, so it is re-read
# until the timeout and fails as soon as the object stops matching.

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class GcsSpecError(Exception):
    pass


#: Every key an `assert.gcs` spec may carry (what _evaluate reads).
GCS_SPEC_KEYS = frozenset({"bucket", "object", "content_hex", "sha256", "size"})


def parse_gcs_specs(raw: list) -> list[dict]:
    if not isinstance(raw, list):
        raise GcsSpecError("'assert.gcs' must be a list of specs")
    for spec in raw:
        if isinstance(spec, dict):
            refuse_unknown_keys(spec, GCS_SPEC_KEYS, f"gcs spec {spec.get('object')!r}", GcsSpecError)
        if not isinstance(spec, dict) or not spec.get("bucket") or not spec.get("object"):
            raise GcsSpecError(f"gcs spec needs 'bucket' and 'object': {spec!r}")
        if not any(k in spec for k in ("content_hex", "sha256", "size")):
            raise GcsSpecError(
                f"gcs spec {spec['object']!r} needs one of content_hex/sha256/size")
    return raw


def _sha256_hex(value) -> str:
    # test.yaml may write the digest bare or as evidence does, sha256:<hex>
    text = str(value).strip().lower()
    return text[len("sha256:"):] if text.startswith("sha256:") else text


def _recorded(spec: dict) -> dict:
    # Evidence requires every sha256 key to be sha256:<hex>, so the record carries that form.
    return {**spec, "sha256": "sha256:" + _sha256_hex(spec["sha256"])} if "sha256" in spec else spec


def _evaluate(admin, spec: dict):
    # Mirrors _check_spec's pass/fail decision but also returns the expected/actual
    # bytes snapshot for the sidecar record. When the spec is content_hex/sha256 (no
    # independently-known expected size), `expected.size` falls back to the actual byte
    # length -- the detail string is what actually explains the hash/hex mismatch.
    data = admin.read_object_bytes(spec["bucket"], spec["object"])
    ref = f"{spec['bucket']}/{spec['object']}"

    if data is None:
        return False, f"{ref}: object not present", None, None

    actual_size = len(data)
    want_size = int(spec["size"]) if "size" in spec else actual_size
    expected = {"kind": "bytes", "size": want_size}
    actual = {"kind": "bytes", "size": actual_size}

    if "content_hex" in spec:
        got = data.hex().lower()
        want = str(spec["content_hex"]).replace(" ", "").lower()
        if got != want:
            return (False, f"{ref}: content_hex mismatch (len={len(data)}; got {got[:48]}… want {want[:48]}…)",
                     expected, actual)
    if "sha256" in spec:
        got = hashlib.sha256(data).hexdigest().lower()
        if got != _sha256_hex(spec["sha256"]):
            return False, f"{ref}: sha256 {got} != {spec['sha256']}", expected, actual
    if "size" in spec and actual_size != int(spec["size"]):
        return False, f"{ref}: size {actual_size} != {spec['size']}", expected, actual

    return True, None, expected, actual


def _check_spec(admin, spec: dict):
    ok, detail, _, _ = _evaluate(admin, spec)
    return None if ok else detail


def _expects_empty(spec: dict) -> bool:
    # True when every check in the spec is satisfied by zero bytes.
    return (("size" not in spec or int(spec["size"]) == 0)
            and ("content_hex" not in spec or not str(spec["content_hex"]).replace(" ", ""))
            and ("sha256" not in spec or _sha256_hex(spec["sha256"]) == _EMPTY_SHA256))


def assert_gcs(admin, specs: list[dict], timeout: int, poll: float = 2.0,
               status_probe=None, *, db: str | None = None, progress=None) -> list[dict]:
    holds = [_expects_empty(s) for s in specs]
    results, outcome = poll_holding(lambda idx: [_evaluate(admin, specs[i]) for i in idx], holds,
                                    timeout, poll, status_probe, progress)
    records = [
        build_assertion_result(type="gcs", status="passed" if ok else "failed", spec=_recorded(s),
                                target=f"{s['bucket']}/{s['object']}", db=db,
                                detail="ok" if ok else d, expected=exp, actual=act)
        for s, (ok, d, exp, act) in zip(specs, results)
    ]
    if outcome == "passed":
        return records
    detail = next(d for ok, d, _, _ in results if not ok)
    if outcome == "broke":
        raise AssertionFailed(
            f"gcs assertion: {detail}. The spec expects an empty object, so it must hold until the "
            f"{timeout}s timeout; it matched, then stopped matching.", records)
    raise AssertionFailed(f"gcs assertion not satisfied within {timeout}s: {detail}", records)
