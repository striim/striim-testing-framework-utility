from __future__ import annotations

import hashlib
import time

from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result

# Assert a GCS object's CONTENT (for a binary writer): the
# uploaded object exists and its bytes match the source blob, by exact hex, sha256, or
# size. Polls until satisfied (the upload is asynchronous) or the timeout expires.


class GcsSpecError(Exception):
    pass


def parse_gcs_specs(raw: list) -> list[dict]:
    if not isinstance(raw, list):
        raise GcsSpecError("'assert.gcs' must be a list of specs")
    for spec in raw:
        if not isinstance(spec, dict) or not spec.get("bucket") or not spec.get("object"):
            raise GcsSpecError(f"gcs spec needs 'bucket' and 'object': {spec!r}")
        if not any(k in spec for k in ("content_hex", "sha256", "size")):
            raise GcsSpecError(
                f"gcs spec {spec['object']!r} needs one of content_hex/sha256/size")
    return raw


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
        if got != str(spec["sha256"]).lower():
            return False, f"{ref}: sha256 {got} != {spec['sha256']}", expected, actual
    if "size" in spec and actual_size != int(spec["size"]):
        return False, f"{ref}: size {actual_size} != {spec['size']}", expected, actual

    return True, None, expected, actual


def _check_spec(admin, spec: dict):
    ok, detail, _, _ = _evaluate(admin, spec)
    return None if ok else detail


def assert_gcs(admin, specs: list[dict], timeout: int, poll: float = 2.0,
               status_probe=None, *, db: str | None = None, progress=None) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        if status_probe:
            status_probe()
        results = [_evaluate(admin, s) for s in specs]
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is None:
            return [
                build_assertion_result(type="gcs", status="passed", spec=s,
                                        target=f"{s['bucket']}/{s['object']}", db=db,
                                        detail="ok", expected=exp, actual=act)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
        if time.monotonic() >= deadline:
            records = [
                build_assertion_result(type="gcs", status="passed" if ok else "failed", spec=s,
                                        target=f"{s['bucket']}/{s['object']}", db=db,
                                        detail="ok" if ok else d, expected=exp, actual=act)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
            raise AssertionFailed(f"gcs assertion not satisfied within {timeout}s: {detail}", records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)
