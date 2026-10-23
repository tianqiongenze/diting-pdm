#!/usr/bin/env python3
"""谛听 PdM 全端全链路无头浏览器功能测试。

覆盖：
  A. 健康看板（真实用户操作路径）：渲染 → 诊断 → 趋势 → 会诊 → 方案卡确认
  B. API 全端点（经本地代理，与浏览器同路径）
  C. java-asset 资产服务 API

产物：tools/browser_test_report.json + screenshots/*.png

用法： python tools/browser_e2e.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime

import httpx
from playwright.sync_api import sync_playwright

CHROME = r"C:\Users\Lenovo\.agent-browser\browsers\chrome-156.0.8078.47\chrome.exe"
BASE = "http://127.0.0.1:18080"           # python-diagnosis（本地代理）
JAVA = "http://127.0.0.1:18081"           # java-asset（ssh 转发）
SHOTS = os.path.join(os.path.dirname(__file__), "..", "..", "browser_test_shots")

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "pass": bool(ok), "detail": detail})
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def shot(page, tag: str) -> str:
    os.makedirs(SHOTS, exist_ok=True)
    p = os.path.join(SHOTS, f"{tag}.png")
    page.screenshot(path=p, full_page=True)
    return p


# ---------------------------------------------------------------------------
# A. 看板（无头浏览器）
# ---------------------------------------------------------------------------
def test_dashboard(pw) -> None:
    print("\n===== A. 健康看板（无头浏览器 · Chromium）=====", flush=True)
    browser = pw.chromium.launch(executable_path=CHROME, headless=True,
                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 1440, "height": 1000})

    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    # 资源加载失败用响应码判定：console 里的 404 文本不含 URL，无法区分
    # favicon 这类无害请求与真实资源缺失
    bad_responses: list[str] = []
    page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url}")
            if r.status >= 400 else None)

    # --- A1 打开与渲染 ---
    page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    title = page.title()
    check("A1.1 页面标题正确", "谛听" in title, title)

    for sel, label in [("#k-assets", "KPI-设备数"), ("#k-cards", "KPI-方案卡"),
                       ("#k-red", "KPI-红色"), ("#rows", "热点设备表"),
                       ("#result", "诊断结果区"), ("#trend", "趋势区"),
                       ("#cards", "方案卡区"), ("#log", "操作日志")]:
        try:
            n = page.locator(sel).count()
            check(f"A1.2 {label} 元素存在", n > 0, f"{sel} x{n}")
        except Exception as exc:  # noqa: BLE001
            check(f"A1.2 {label} 元素存在", False, str(exc)[:60])

    # KPI 是否有实际数值（非占位）
    page.wait_for_timeout(1500)
    kpi = {}
    for k in ("k-assets", "k-cards", "k-red", "k-orange", "k-yellow", "k-transport"):
        try:
            kpi[k] = page.locator(f"#{k}").inner_text().strip()
        except Exception:  # noqa: BLE001
            kpi[k] = ""
    check("A1.3 KPI 已填充数值", all(v for v in kpi.values()), json.dumps(kpi, ensure_ascii=False))

    # 热点设备表是否有行
    try:
        rows = page.locator("#rows tr").count()
        check("A1.4 热点设备表有数据行", rows > 0, f"{rows} 行")
    except Exception as exc:  # noqa: BLE001
        check("A1.4 热点设备表有数据行", False, str(exc)[:60])
    shot(page, "A1_dashboard_initial")

    # --- A2 诊断链路 ---
    page.fill("#asset", "FAN-00004")
    page.wait_for_timeout(300)
    # 找到诊断按钮（三种 onclick 之一）
    btn = None
    for sel in ['button:has-text("诊断")', '[onclick^="diag("]', '[onclick^="diagnose("]']:
        if page.locator(sel).count():
            btn = page.locator(sel).first
            break
    if btn is None:
        check("A2.1 找到诊断按钮", False, "未匹配到任何诊断按钮")
    else:
        check("A2.1 找到诊断按钮", True)
        btn.click()
        page.wait_for_timeout(4000)
        result = page.locator("#result").inner_text()
        check("A2.2 诊断结果已渲染", len(result.strip()) > 20, result.strip()[:80].replace("\n", " "))
        low = result.lower()
        check("A2.3 结果含健康指数 HI", "hi" in low, result.strip()[:60].replace("\n", " "))
        check("A2.4 结果含等级", any(x in result for x in ("红色", "橙色", "黄色", "绿色",
                                                          "红", "橙", "黄", "绿")),
              result.strip()[:60].replace("\n", " "))
        shot(page, "A2_diagnose_result")

    # --- A3 趋势链路 ---
    tbtn = None
    for sel in ['button:has-text("趋势")', '[onclick*="trend("]']:
        if page.locator(sel).count():
            tbtn = page.locator(sel).first
            break
    if tbtn is None:
        check("A3.1 找到趋势按钮", False)
    else:
        check("A3.1 找到趋势按钮", True)
        tbtn.click()
        page.wait_for_timeout(4000)
        svg_n = page.locator("#trend svg").count()
        path_n = page.locator("#trend svg path").count()
        check("A3.2 趋势图 SVG 已渲染", svg_n > 0, f"svg={svg_n}")
        check("A3.3 折线路径存在", path_n >= 2, f"path={path_n}")
        note = ""
        try:
            note = page.locator("#trend-note").inner_text().strip()
        except Exception:  # noqa: BLE001
            pass
        check("A3.4 趋势说明已更新", bool(note), note[:70])
        shot(page, "A3_trend_chart")

    # --- A4 会诊链路（生成方案卡）---
    cbtn = None
    for sel in ['button:has-text("会诊")', '[onclick^="consult("]']:
        if page.locator(sel).count():
            cbtn = page.locator(sel).first
            break
    if cbtn is None:
        check("A4.1 找到会诊按钮", False)
    else:
        check("A4.1 找到会诊按钮", True)
        cbtn.click()
        page.wait_for_timeout(6000)
        cards = page.locator("#cards").inner_text()
        check("A4.2 方案卡已生成", len(cards.strip()) > 20, cards.strip()[:90].replace("\n", " "))
        shot(page, "A4_consult_cards")

    # --- A5 控制台与资源 ---
    js_errors = [e for e in console_errors
                 if "is not defined" in e or "SyntaxError" in e
                 or "TypeError" in e or "Uncaught" in e]
    check("A5.1 无 JS 运行时异常", not js_errors, "; ".join(js_errors[:3])[:120])
    real_bad = [b for b in bad_responses if "favicon" not in b.lower()]
    check("A5.2 无资源加载失败(4xx/5xx)", not real_bad, "; ".join(real_bad[:3])[:120])

    browser.close()


# ---------------------------------------------------------------------------
# B. API 全端点
# ---------------------------------------------------------------------------
def test_api() -> None:
    print("\n===== B. API 全端点 =====", flush=True)
    # 服务端多租户：除 /healthz /version /events/stats 外都要求 X-Tenant-Id
    c = httpx.Client(base_url=BASE, timeout=40, headers={"X-Tenant-Id": "site-a"},
                     limits=httpx.Limits(max_keepalive_connections=0))

    r = c.get("/healthz")
    check("B1.1 /healthz", r.status_code == 200 and r.json().get("status") == "ok",
          json.dumps(r.json(), ensure_ascii=False))

    r = c.get("/version")
    check("B1.2 /version", r.status_code == 200, r.text[:80])

    r = c.get("/events/stats")
    d = r.json()
    check("B1.3 /events/stats 传输层", r.status_code == 200 and d.get("transport") == "pulsar",
          json.dumps(d, ensure_ascii=False)[:90])

    r = c.get("/integration/stats")
    check("B1.4 /integration/stats", r.status_code == 200 and isinstance(r.json(), dict),
          json.dumps(r.json(), ensure_ascii=False)[:90])

    # 诊断
    r = c.post("/v1/diagnose", json={"tenant_id": "site-a", "asset_id": "FAN-00004"})
    d = r.json()
    check("B2.1 /v1/diagnose", r.status_code == 200 and "hi" in d,
          f"hi={d.get('hi')} level={d.get('level')}")
    # 实际字段为 contributions（各维度贡献度）+ evidence（证据链）
    check("B2.2 诊断含贡献度分解", isinstance(d.get("contributions"), list),
          f"contributions={len(d.get('contributions') or [])} 项")
    check("B2.3 诊断含证据链", isinstance(d.get("evidence"), list),
          f"evidence={len(d.get('evidence') or [])} 条")
    check("B2.4 诊断含 trace_id", bool(d.get("trace_id")), str(d.get("trace_id"))[:40])

    # 会诊（落库）
    r = c.post("/v1/consult", json={"tenant_id": "site-a", "asset_id": "FAN-00004"})
    card = r.json().get("card", {})
    check("B3.1 /v1/consult 生成方案卡", r.status_code == 200 and card.get("card_id"),
          f"{card.get('card_id')} verdict={card.get('verdict')}")

    # 列表
    r = c.get("/v1/cards?limit=5")
    check("B3.2 /v1/cards 列表", r.status_code == 200 and r.json().get("count", 0) > 0,
          f"count={r.json().get('count')}")

    # 确认（角色驱动）
    cid = card.get("card_id", "")
    roles = card.get("human_confirm", {}).get("required_roles", [])
    if cid and roles:
        for i, role in enumerate(roles):
            rr = c.post(f"/v1/cards/{cid}/confirm",
                        json={"role": role, "user_id": f"browser-e2e-{i}", "decision": "confirm"})
            exp = "CONFIRMED" if i == len(roles) - 1 else "PENDING"
            check(f"B3.3.{i} 确认 {role}", rr.json().get("status") == exp,
                  f"status={rr.json().get('status')}")
        rr = c.post(f"/v1/cards/{cid}/confirm",
                    json={"role": roles[0], "user_id": "dup", "decision": "confirm"})
        check("B3.4 重复确认返回 409", rr.status_code == 409, f"HTTP {rr.status_code}")
    rr = c.post("/v1/cards/NOPE-999/confirm",
                json={"role": "maintenance_supervisor", "user_id": "x", "decision": "confirm"})
    check("B3.5 不存在卡片返回 409", rr.status_code == 409, f"HTTP {rr.status_code}")

    # 趋势
    r = c.get("/v1/assets/FAN-00004/trend?days=7", headers={"X-Tenant-Id": "site-a"})
    d = r.json()
    check("B4.1 /v1/assets/{id}/trend", r.status_code == 200 and "series" in d,
          f"points={d.get('points')}")
    bare = httpx.Client(base_url=BASE, timeout=20)
    r2 = bare.get("/v1/assets/FAN-00004/trend")
    check("B4.2 趋势缺租户头 400", r2.status_code == 400, f"HTTP {r2.status_code}")
    bare.close()

    # MCP 端点
    r = c.get("/mcp")
    check("B5.1 /mcp 可访问", r.status_code in (200, 405, 406), f"HTTP {r.status_code}")

    c.close()


# ---------------------------------------------------------------------------
# C. java-asset
# ---------------------------------------------------------------------------
def test_java() -> None:
    print("\n===== C. java-asset 资产服务 =====", flush=True)
    h = {"X-Tenant-Id": "site-a"}
    c = httpx.Client(base_url=JAVA, timeout=30,
                     limits=httpx.Limits(max_keepalive_connections=0))

    r = c.get("/api/v1/assets?limit=3", headers=h)
    d = r.json()
    check("C1.1 资产列表", r.status_code == 200 and isinstance(d, list) and d,
          f"{len(d)} 条，首条 {d[0].get('asset_id')}")

    aid = d[0]["asset_id"]
    r = c.get(f"/api/v1/assets/{aid}", headers=h)
    body = r.json() if r.status_code == 200 else {}
    got = (body.get("asset") or {}).get("asset_id")
    mps = body.get("measure_points") or []
    check("C1.2 资产详情", r.status_code == 200 and got == aid, f"asset_id={got}")
    check("C1.2b 详情含测点列表", len(mps) > 0, f"{len(mps)} 个测点")

    r = c.get("/api/v1/assets/stats/criticality", headers=h)
    check("C1.3 关键度统计", r.status_code == 200, r.text[:100])

    r = c.get("/api/v1/assets/NOPE-1", headers=h)
    check("C1.4 不存在资产 404", r.status_code == 404, f"HTTP {r.status_code}")

    r = c.get("/api/v1/assets", headers={})
    check("C1.5 缺租户头被拒", r.status_code in (400, 401, 403, 404),
          f"HTTP {r.status_code}")
    c.close()


def main() -> None:
    t0 = time.time()
    print(f"谛听 PdM 全端全链路功能测试 · {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)

    with sync_playwright() as pw:
        test_dashboard(pw)
    test_api()
    test_java()

    passed = sum(1 for r in RESULTS if r["pass"])
    failed = [r for r in RESULTS if not r["pass"]]
    summary = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(time.time() - t0, 1),
        "total": len(RESULTS),
        "passed": passed,
        "failed": len(failed),
        "failures": failed,
        "results": RESULTS,
    }
    out = os.path.join(os.path.dirname(__file__), "browser_test_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(f"\n通过 {passed}/{len(RESULTS)}，失败 {len(failed)} 项，"
          f"耗时 {summary['duration_s']}s", flush=True)
    print(f"报告: {out}", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
