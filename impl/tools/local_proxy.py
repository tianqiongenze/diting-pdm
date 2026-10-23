#!/usr/bin/env python3
"""本地反向代理：让无头浏览器能访问 pdm-api（Ingress 按 Host 头路由）。

背景：
  集群 Ingress 规则匹配 Host: pdm-api.10.167.2.175.nip.io，
  而浏览器无法自定义 Host 头；且 master 到 Pod 网段被隔离，
  ClusterIP 对 python-diagnosis 也失效（kube-proxy 规则问题）。
  唯一稳定路径是 NodePort 32231。

本代理监听 127.0.0.1:18080，把请求转发到
http://10.167.2.175:32231 并注入正确的 Host 头，
同时在响应里重写 Location，避免重定向跳出代理。

用法： python tools/local_proxy.py
"""

from __future__ import annotations

import http.server
import socketserver
import urllib.error
import urllib.request

UPSTREAM = "http://10.167.2.175:32231"
INGRESS_HOST = "pdm-api.10.167.2.175.nip.io"
LISTEN = ("127.0.0.1", 18080)
DEFAULT_AUTH = "cGRtYWRtaW46RGl0aW5nMjAyNiE="  # base64(pdmadmin:Diting2026!)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"[proxy] {self.address_string()} {fmt % args}", flush=True)

    def _proxy(self) -> None:
        # Ingress 的路径规则带 /pdm-api 前缀，本地访问不带，这里补上
        path = self.path
        if not path.startswith("/pdm-api"):
            path = "/pdm-api" + (path if path.startswith("/") else "/" + path)
        url = UPSTREAM + path
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)

        req = urllib.request.Request(url, data=body, method=self.command)
        # 强制 Host（Ingress 按 Host 路由），其余头原样透传。
        # 注意：X-Tenant-Id 是多租户必需头，早期版本只透传 Content-Type/
        # Authorization，导致所有需要租户头的接口报 missing header。
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection",
                             "transfer-encoding", "keep-alive"):
                continue
            req.add_header(k, v)
        req.add_header("Host", INGRESS_HOST)
        auth = self.headers.get("Authorization")
        if not auth:
            # 看板是 HTTP Basic Auth，浏览器不便预设凭据，代理侧注入默认值。
            # 鉴权本身（401/401/200）用 curl 直连 NodePort 验证。
            req.add_header("Authorization", "Basic " + DEFAULT_AUTH)

        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "text/plain"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001
            msg = f"proxy error: {exc}".encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy  # noqa: N815


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"proxy listening on http://{LISTEN[0]}:{LISTEN[1]} -> {UPSTREAM} "
          f"(Host: {INGRESS_HOST})", flush=True)
    with Server(LISTEN, Handler) as srv:
        srv.serve_forever()
