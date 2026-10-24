#!/usr/bin/env python3
"""谛听 PdM 一键巡检（技术债 #2）。

替代 docs/38 §4.1 的手工 5 条 curl —— 那些命令容易漏项，且不检查
「Egress 策略是否漏了同命名空间」这类曾导致重大误判的配置。

用法：
    python tools/health_check.py            # 人类可读输出
    python tools/health_check.py --json     # 机器可读（接监控）
    python tools/health_check.py --quiet    # 只输出失败项

退出码：0 全部通过；1 有失败项；2 有告警级问题（不阻断但需关注）

检查项：
  H1  Pod 就绪          所有 pdm-prod Pod 1/1 Running
  H2  服务健康          /healthz status=ok
  H3  事件总线          failures=0 且 last_error 为空
  H4  集成 Outbox       PENDING 不堆积（< 50）
  H5  三个页面          200
  H6  Egress 策略       含同命名空间放行（历史事故点）
  H7  Ingress 端口      放行 8000 与 8081（历史事故点）
  H8  CPU 配额余量      master 请求 < 90%
  H9  CronJob 状态      最近一次执行成功
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

SERVER = "root@10.167.2.175"
NS = "pdm-prod"
INGRESS_HOST = "pdm-api.10.167.2.175.nip.io"
BASE = "http://10.167.2.175:32231/pdm-api"
AUTH = "pdmadmin:Diting2026!"

RESULTS: list[dict] = []


_SSH_NOISE = ("Warning: Permanently added", "post-quantum", "store now, decrypt later",
              "The server may need to be upgraded", "openssh.com/pq.html")


def sh(cmd: str, timeout: int = 40) -> tuple[int, str]:
    """在 master 上执行命令（本地无 kubeconfig，统一走 ssh）。

    只返回 stdout：ssh 自身会往 stderr 打 ED25519/post-quantum 警告，
    若与 stdout 合并会污染 curl 的 HTTP 码、job 名等解析（踩过一次）。
    """
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10", SERVER, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        out = p.stdout or ""
        if p.returncode != 0 and not out.strip():
            err = "\n".join(l for l in (p.stderr or "").splitlines()
                             if not any(n in l for n in _SSH_NOISE))
            out = err
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def rec(code: str, name: str, ok: bool, detail: str, warn: bool = False) -> None:
    RESULTS.append({"code": code, "name": name, "ok": ok,
                    "warn": warn and not ok, "detail": detail})


# ---------------------------------------------------------------------------
def check_pods() -> None:
    rc, out = sh(f"kubectl -n {NS} get pods --no-headers 2>/dev/null")
    if rc != 0:
        rec("H1", "Pod 就绪", False, f"kubectl 失败: {out.strip()[:80]}")
        return
    lines = [l for l in out.strip().splitlines() if l.strip()]
    bad = []
    for l in lines:
        f = l.split()
        if len(f) < 3:
            continue
        name, ready, status = f[0], f[1], f[2]
        if status == "Completed":
            continue
        if "/" in ready and ready.split("/")[0] != ready.split("/")[1]:
            bad.append(f"{name}({ready})")
        elif status not in ("Running", "Completed"):
            bad.append(f"{name}({status})")
    rec("H1", "Pod 就绪", not bad,
        f"{len(lines)} 个 Pod" + (f"，异常: {', '.join(bad[:3])}" if bad else "，全部就绪"))


def check_healthz() -> None:
    rc, out = sh(f"curl -s --max-time 10 -H 'Host: {INGRESS_HOST}' {BASE}/healthz")
    try:
        d = json.loads(out)
        ok = d.get("status") == "ok"
        rec("H2", "服务健康", ok, f"status={d.get('status')} version={d.get('version')}")
    except Exception:  # noqa: BLE001
        rec("H2", "服务健康", False, out.strip()[:100] or "无响应")


def check_events() -> None:
    rc, out = sh(f"curl -s --max-time 10 -H 'Host: {INGRESS_HOST}' {BASE}/events/stats")
    try:
        d = json.loads(out)
        fails = int(d.get("failures") or 0)
        err = d.get("last_error")
        ok = fails == 0 and not err
        rec("H3", "事件总线", ok,
            f"transport={d.get('transport')} emitted={d.get('emitted')} "
            f"failures={fails}" + (f" last_error={str(err)[:40]}" if err else ""))
    except Exception:  # noqa: BLE001
        rec("H3", "事件总线", False, out.strip()[:100] or "无响应")


def check_outbox() -> None:
    rc, out = sh(f"curl -s --max-time 10 -H 'Host: {INGRESS_HOST}' "
                 f"-H 'X-Tenant-Id: site-a' {BASE}/integration/stats")
    try:
        d = json.loads(out)
        pending = int(d.get("PENDING") or 0)
        rec("H4", "集成 Outbox", pending < 50,
            f"{d}" if pending else f"无积压（{d}）", warn=True)
    except Exception:  # noqa: BLE001
        rec("H4", "集成 Outbox", False, out.strip()[:100] or "无响应", warn=True)


def check_pages() -> None:
    pages = ("dashboard", "screen", "console")
    bad = []
    for p in pages:
        rc, out = sh(f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 "
                     f"-u '{AUTH}' -H 'Host: {INGRESS_HOST}' "
                     f"http://10.167.2.175:32231/pdm-api/{p}")
        if out.strip() != "200":
            bad.append(f"{p}={out.strip()}")
    rec("H5", "三个页面可访问", not bad,
        "全部 200" if not bad else "异常: " + ", ".join(bad))


def check_egress_policy() -> None:
    """历史事故：Egress 策略漏同命名空间 → Pod 间调用超时（docs/33 §2）。"""
    rc, out = sh(f"kubectl -n {NS} get netpol allow-egress-backends -o jsonpath="
                 f"'{{.spec.egress}}' 2>/dev/null")
    ok = "podSelector" in out
    rec("H6", "Egress 放行同命名空间", ok,
        "已放行" if ok else "缺失 podSelector —— 会导致 Pod 间调用超时")


def check_ingress_ports() -> None:
    """历史事故：只放行 8000，8081 被拦 → upstream timed out。"""
    rc, out = sh(f"kubectl -n {NS} get netpol allow-from-ingress-controller -o jsonpath="
                 f"'{{.spec.ingress}}' 2>/dev/null")
    ok = "8000" in out and "8081" in out
    rec("H7", "Ingress 放行 8000/8081", ok,
        "两个端口均已放行" if ok else f"端口不全: {out[:80]}")


def check_cpu() -> None:
    """取各节点 CPU requests 占比。describe 输出形如 `cpu  15380m (96%)  135100m (844%)`。"""
    rc, out = sh("kubectl describe node 2>/dev/null | grep -E '^[[:space:]]+cpu[[:space:]]' | head -4")
    pcts: list[int] = []
    for l in out.splitlines():
        m = re.findall(r"\((\d+)%\)", l)
        if m:
            pcts.append(int(m[0]))
    if not pcts:
        rec("H8", "CPU 配额余量", True, "未取到数据（跳过）", warn=True)
        return
    worst = max(pcts)
    rec("H8", "CPU 配额余量", worst < 90,
        f"最高节点 requests {worst}%（各节点 {pcts}）", warn=True)


def check_cronjobs() -> None:
    """检查最近一次 Job 是否成功。用 jsonpath 取结构化数据，避免文本 grep 误匹配。"""
    rc, out = sh(f"kubectl -n {NS} get jobs -o jsonpath="
                 f"'{{range .items[*]}}{{.metadata.name}}|{{.status.succeeded}}|{{.status.failed}}{{\"\\n\"}}{{end}}' 2>/dev/null")
    rows = [l for l in out.strip().splitlines() if l.strip() and "|" in l]
    if not rows:
        rec("H9", "CronJob 状态", True, "暂无 Job 记录", warn=True)
        return
    failed = []
    for l in rows:
        name, succ, fail = (l.split("|") + ["", "", ""])[:3]
        try:
            if int(fail or 0) > 0 and int(succ or 0) == 0:
                failed.append(name)
        except ValueError:
            pass
    rc2, cj = sh(f"kubectl -n {NS} get cronjob --no-headers 2>/dev/null | wc -l")
    rec("H9", "CronJob 状态", not failed,
        f"{cj.strip()} 个任务，{len(rows)} 条 Job 记录"
        + (f"，失败: {failed[:3]}" if failed else "，最近执行正常"),
        warn=True)


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只输出失败项")
    a = ap.parse_args()

    for fn in (check_pods, check_healthz, check_events, check_outbox, check_pages,
               check_egress_policy, check_ingress_ports, check_cpu, check_cronjobs):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            rec("H?", fn.__name__, False, f"检查异常: {exc}")

    hard_fail = [r for r in RESULTS if not r["ok"] and not r["warn"]]
    warns = [r for r in RESULTS if not r["ok"] and r["warn"]]

    if a.json:
        print(json.dumps({"results": RESULTS,
                          "failed": len(hard_fail), "warnings": len(warns)},
                         ensure_ascii=False, indent=2))
    else:
        print("谛听 PdM 巡检")
        print("=" * 62)
        for r in RESULTS:
            if r["ok"]:
                mark = "✅"
            elif r["warn"]:
                mark = "⚠️ "
            else:
                mark = "❌"
            if a.quiet and r["ok"]:
                continue
            print(f"{mark} {r['code']} {r['name']:<22} {r['detail']}")
        print("=" * 62)
        print(f"通过 {len(RESULTS)-len(hard_fail)-len(warns)}/{len(RESULTS)}"
              f"　失败 {len(hard_fail)}　告警 {len(warns)}")

    sys.exit(1 if hard_fail else (2 if warns else 0))


if __name__ == "__main__":
    main()
