"""在 MinIO 上创建 S3 bucket（纯标准库 SigV4，无需 boto3）。

用法（集群内）：
    kubectl -n pdm-prod exec -i deploy/python-diagnosis -- python - < tools/s3_mkbucket.py
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.request

ACCESS = os.getenv("S3_ACCESS_KEY", "minioadmin")
SECRET = os.getenv("S3_SECRET_KEY", "minioadmin123")
ENDPOINT = os.getenv("S3_ENDPOINT", "http://milvus-minio.infra.svc.cluster.local:9000")
BUCKET = os.getenv("S3_BUCKET", "pdm-backup")
REGION = "us-east-1"

host = ENDPOINT.split("//", 1)[1].rstrip("/")


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _headers(method: str, canonical_uri: str, payload: bytes = b""):
    now = datetime.datetime.utcnow()
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amzdate}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = f"{method}\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

    scope = f"{datestamp}/{REGION}/s3/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )
    k = _sign(("AWS4" + SECRET).encode(), datestamp)
    k = _sign(k, REGION)
    k = _sign(k, "s3")
    k = _sign(k, "aws4_request")
    sig = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={ACCESS}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        ),
    }


def call(method: str, path: str) -> tuple[int, bytes]:
    req = urllib.request.Request(ENDPOINT + path, method=method)
    for k, v in _headers(method, path).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    status, body = call("PUT", f"/{BUCKET}")
    print(f"create bucket {BUCKET}: HTTP {status}")
    if status not in (200, 409):
        print("body:", body[:200])
        return 1
    status, body = call("GET", "/")
    print(f"list buckets: HTTP {status}")
    print(body.decode()[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
