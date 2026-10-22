"""Mock EAM 服务（联调用）。

生产环境由真实 EAM/MES（SAP PM、Maximo、鼎捷等）替换；
本服务实现同一契约，用于：
  1. 本地/CI 联调 integration.py 的 Outbox 投递
  2. 验证幂等（同一 external_id 重复提交只建一张工单）
  3. 模拟故障（503 / 超时 / 4xx）以验证重试与死信

契约：
  POST /api/v1/work-orders
    Header: X-External-Id（幂等键）、X-Signature（HMAC-SHA256）
    Body:   work order JSON
  GET  /api/v1/work-orders/{external_id}
  POST /api/v1/work-orders/{external_id}/status   （EAM 侧状态回写）

故障注入（通过环境变量）：
  MOCK_FAIL_RATE=0.5     随机返回 503
  MOCK_FAIL_STATUS=503   固定返回该状态码
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.getenv("EAM_HMAC_SECRET", "diting-eam-secret")
STORE: dict[str, dict] = {}
FAIL_RATE = float(os.getenv("MOCK_FAIL_RATE", "0"))
FAIL_STATUS = int(os.getenv("MOCK_FAIL_STATUS", "503"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 静音
        pass

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _verify(self, body: bytes) -> bool:
        sig = self.headers.get("X-Signature", "")
        expect = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expect)

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)

        if not self._verify(body):
            self._json(401, {"error": "bad signature"})
            return

        # 故障注入：仅当 FAIL_RATE > 0 时按概率触发；0 表示永不注入
        if FAIL_RATE > 0 and (time.time() % 1000 < FAIL_RATE * 1000):
            self._json(FAIL_STATUS, {"error": "injected failure"})
            return

        if self.path == "/api/v1/work-orders":
            ext = self.headers.get("X-External-Id", "")
            if not ext:
                self._json(400, {"error": "missing X-External-Id"})
                return
            if ext in STORE:  # 幂等：重复提交不建重复工单
                self._json(200, {"workorder_no": STORE[ext]["workorder_no"], "idempotent": True})
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            wo = f"WO-{int(time.time())}-{len(STORE) + 1:04d}"
            STORE[ext] = {
                "workorder_no": wo,
                "external_id": ext,
                "status": "OPEN",
                "payload": payload,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            print(f"[eam] created {wo} ext={ext} asset={payload.get('asset_id')}", flush=True)
            self._json(201, {"workorder_no": wo, "idempotent": False})
            return

        if self.path.endswith("/status"):
            ext = self.path.split("/")[-2]
            if ext not in STORE:
                self._json(404, {"error": "not found"})
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            STORE[ext]["status"] = payload.get("status", STORE[ext]["status"])
            print(f"[eam] {ext} status → {STORE[ext]['status']}", flush=True)
            self._json(200, {"external_id": ext, "status": STORE[ext]["status"]})
            return

        self._json(404, {"error": "not found"})

    def do_GET(self) -> None:
        if self.path.startswith("/api/v1/work-orders/"):
            ext = self.path.rsplit("/", 1)[-1]
            if ext in STORE:
                self._json(200, STORE[ext])
            else:
                self._json(404, {"error": "not found"})
            return
        if self.path == "/healthz":
            self._json(200, {"status": "ok", "service": "mock-eam", "orders": len(STORE)})
            return
        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8082"))
    print(f"mock-eam listening :{port} fail_rate={FAIL_RATE} fail_status={FAIL_STATUS}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
