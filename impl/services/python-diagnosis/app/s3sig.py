"""AWS SigV4 签名工具（纯标准库，无 boto3 依赖）。

用于 MinIO 的建桶、列举、删除等操作。
放在 app 包内，便于 backup_prune 等模块复用（tools/s3_mkbucket.py 是同源脚本）。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import urllib.error
import urllib.request

REGION = "us-east-1"


def _cfg() -> tuple[str, str, str]:
    endpoint = os.getenv("S3_ENDPOINT", "http://milvus-minio.infra.svc.cluster.local:9000")
    host = endpoint.split("//", 1)[1].rstrip("/")
    ak = os.getenv("S3_ACCESS_KEY", "minioadmin")
    sk = os.getenv("S3_SECRET_KEY", "minioadmin123")
    return endpoint, host, ak, sk


def _canonical_query(query: str) -> str:
    """规范化查询串：按键排序、URI 编码（SigV4 要求）。"""
    from urllib.parse import quote

    if not query:
        return ""
    pairs = []
    for part in query.lstrip("?").split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((quote(k, safe="-_.~"), quote(v, safe="-_.~")))
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


def headers(method: str, canonical_uri: str, payload: bytes = b"", query: str = "",
            endpoint: str | None = None, host: str | None = None) -> dict[str, str]:
    """生成 SigV4 请求头。query 必须参与签名，否则 MinIO 返回 SignatureDoesNotMatch。"""
    endpoint = endpoint or _cfg()[0]
    host = host or endpoint.split("//", 1)[1].rstrip("/")
    ak, sk = _cfg()[2], _cfg()[3]

    now = datetime.datetime.utcnow()
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amzdate}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = (f"{method}\n{canonical_uri}\n{_canonical_query(query)}\n"
                         f"{canonical_headers}\n{signed_headers}\n{payload_hash}")
    scope = f"{datestamp}/{REGION}/s3/aws4_request"
    string_to_sign = (f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n"
                      + hashlib.sha256(canonical_request.encode()).hexdigest())

    def sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = sign(("AWS4" + sk).encode(), datestamp)
    k = sign(k, REGION)
    k = sign(k, "s3")
    k = sign(k, "aws4_request")
    sig = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={sig}"),
    }


def call(method: str, path: str, query: str = "", payload: bytes = b"",
         extra_headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    """执行 S3 请求，返回 (status, body)。"""
    req = urllib.request.Request(_cfg()[0] + path + query, data=payload or None, method=method)
    if payload:
        req.add_header("Content-Type", "application/xml")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    for k, v in headers(method, path, payload, query).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
