#!/usr/bin/python3
"""Create an S3 bucket on a plain-HTTP endpoint (MinIO) if absent. SigV4, stdlib only.
Usage: s3-mkbucket.py host:port bucket   (credentials from EON_S3_ACCESS_KEY/EON_S3_SECRET_KEY)
curl 7.76 in the base image signs SigV4 wrongly for MinIO, and the image has no mc."""
import datetime, hashlib, hmac, http.client, os, sys

endpoint, bucket = sys.argv[1], sys.argv[2]
key, secret = os.environ["EON_S3_ACCESS_KEY"], os.environ["EON_S3_SECRET_KEY"]
region, empty = "us-east-1", hashlib.sha256(b"").hexdigest()


def call(method):
    now = datetime.datetime.now(datetime.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"host": endpoint, "x-amz-content-sha256": empty, "x-amz-date": amz}
    signed = ";".join(sorted(headers))
    canon = "\n".join([method, "/" + bucket, "",
                       "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, empty])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = ("AWS4" + secret).encode()
    for part in (day, region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={key}/{scope}, "
                                f"SignedHeaders={signed}, Signature={sig}")
    conn = http.client.HTTPConnection(endpoint, timeout=10)
    conn.request(method, "/" + bucket, headers=headers)
    r = conn.getresponse()
    return r.status, r.read()


status, _ = call("HEAD")
if status == 200:
    print("exists")
    sys.exit(0)
status, body = call("PUT")
if status != 200:
    sys.exit(f"PUT /{bucket}: HTTP {status} {body[:300]!r}")
print("created")
