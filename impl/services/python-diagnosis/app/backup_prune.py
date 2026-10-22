"""备份清理（纯标准库 SigV4，与 s3_mkbucket.py 同源）。

删除 s3://pdm-backup/ 下早于保留期的 full-YYYYmmdd-HHMMSS 目录。

用法：
    python -m app.backup_prune --keep-days 7 [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import xml.etree.ElementTree as ET

from .s3sig import headers as _headers

ACCESS = os.getenv("S3_ACCESS_KEY", "minioadmin")
SECRET = os.getenv("S3_SECRET_KEY", "minioadmin123")
ENDPOINT = os.getenv("S3_ENDPOINT", "http://milvus-minio.infra.svc.cluster.local:9000")
BUCKET = os.getenv("S3_BUCKET", "pdm-backup")
REGION = "us-east-1"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _req(method: str, path: str, query: str = "") -> bytes:
    from .s3sig import call

    status, body = call(method, path, query)
    if status == 404:
        return b""
    return body


def list_prefixes(prefix: str) -> list[str]:
    """列出 bucket 下以 prefix 开头的"目录"。"""
    out: list[str] = []
    token = ""
    while True:
        q = f"?list-type=2&prefix={prefix}&delimiter=/" + (f"&continuation-token={token}" if token else "")
        root = ET.fromstring(_req("GET", f"/{BUCKET}", q))
        out += [e.text for e in root.iter(f"{NS}Prefix")]
        trunc = root.find(f"{NS}IsTruncated")
        if trunc is None or trunc.text != "true":
            break
        nt = root.find(f"{NS}NextContinuationToken")
        token = nt.text if nt is not None else ""
    return out


def list_objects(prefix: str) -> list[str]:
    out: list[str] = []
    token = ""
    while True:
        q = f"?list-type=2&prefix={prefix}" + (f"&continuation-token={token}" if token else "")
        root = ET.fromstring(_req("GET", f"/{BUCKET}", q))
        out += [e.text for e in root.iter(f"{NS}Key")]
        trunc = root.find(f"{NS}IsTruncated")
        if trunc is None or trunc.text != "true":
            break
        nt = root.find(f"{NS}NextContinuationToken")
        token = nt.text if nt is not None else ""
    return out


def delete_objects(keys: list[str]) -> None:
    import base64
    import hashlib

    body = "<Delete>" + "".join(f"<Object><Key>{k}</Key></Object>" for k in keys) + "</Delete>"
    md5 = base64.b64encode(hashlib.md5(body.encode()).digest()).decode()
    from .s3sig import call

    status, _ = call("POST", f"/{BUCKET}", f"?delete", body.encode(), extra_headers={"Content-MD5": md5})
    if status not in (200, 204):
        raise RuntimeError(f"delete failed: HTTP {status}")


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-days", type=int, default=int(os.getenv("KEEP_DAYS", "7")))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cutoff = datetime.datetime.now() - datetime.timedelta(days=a.keep_days)
    dirs = list_prefixes("full-")
    stale: list[tuple[str, datetime.datetime]] = []
    for d in dirs:
        stamp = d.rstrip("/")[len("full-"):]
        try:
            when = datetime.datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if when < cutoff:
            stale.append((d, when))

    if not stale:
        print(f"nothing to prune: dirs={len(dirs)} keep={a.keep_days}d")
        return 0

    total = 0
    for d, when in sorted(stale, key=lambda x: x[1]):
        if a.dry_run:
            print(f"[dry-run] {d} ({when:%Y-%m-%d %H:%M})")
            continue
        keys = list_objects(d)
        for i in range(0, len(keys), 500):
            delete_objects(keys[i:i + 500])
        total += len(keys)
        print(f"deleted {d} ({when:%Y-%m-%d %H:%M}) objects={len(keys)}")

    print(f"pruned {len(stale)} dirs / {total} objects (keep {a.keep_days}d)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
