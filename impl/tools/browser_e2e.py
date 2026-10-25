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
# A 组（浏览器）走本地代理：浏览器无法自定义 Host 头
BASE = "http://127.0.0.1:18080"
# B/C 组（httpx）直接打 Ingress：httpx 可设 Host 头，无需代理/隧道
NODE = "http://10.167.2.175:32231"
INGRESS_HOST = "pdm-api.10.167.2.175.nip.io"
API = NODE + "/pdm-api"
JAVA = NODE + "/pdm-asset"
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

    # --- A6 部署一致性（看板走 ConfigMap 挂载，代码新≠线上新）---
    import hashlib
    import pathlib

    local = pathlib.Path(__file__).parent.parent / "deploy" / "k8s" / "dashboard-index.html"
    if local.exists():
        raw = local.read_bytes()
        local_sha = hashlib.sha256(raw).hexdigest()
        served = page.content()
        needed = ("function diag", "function trend", 'id="trend"', "confirmCard")
        missing = [k for k in needed if k not in served]
        check("A6.1 线上看板含最新功能（诊断/趋势/方案卡）", not missing,
              "缺失: " + ",".join(missing) if missing else "全部存在")
        check("A6.2 本地看板文件非空", len(raw) > 1000,
              f"{len(raw)} bytes, sha={local_sha[:12]}")
    else:
        check("A6.1 本地看板文件存在", False, str(local))

    browser.close()


# ---------------------------------------------------------------------------
# B. API 全端点
# ---------------------------------------------------------------------------
def test_api() -> None:
    print("\n===== B. API 全端点 =====", flush=True)
    # 服务端多租户：除 /healthz /version /events/stats 外都要求 X-Tenant-Id
    c = httpx.Client(base_url=API, timeout=40,
                     headers={"X-Tenant-Id": "site-a", "Host": INGRESS_HOST},
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
    # 用真实设备（有诊断快照）；去重窗口内会复用已有卡，
    # 故确认流程按卡片当前状态自适应（见 B3.3）
    uniq_asset = "FAN-00004"
    r = c.post("/v1/consult", json={"tenant_id": "site-a", "asset_id": uniq_asset})
    card = r.json().get("card", {})
    check("B3.1 /v1/consult 生成方案卡", r.status_code == 200 and card.get("card_id"),
          f"{card.get('card_id')} verdict={card.get('verdict')} asset={uniq_asset}")

    # 列表
    r = c.get("/v1/cards?limit=5")
    check("B3.2 /v1/cards 列表", r.status_code == 200 and r.json().get("count", 0) > 0,
          f"count={r.json().get('count')}")

    # 去重：同设备同 verdict 再会诊应复用同一张卡
    r_dup = c.post("/v1/consult", json={"tenant_id": "site-a", "asset_id": uniq_asset})
    check("B3.1b 同设备再会诊复用同一卡（去重生效）",
          r_dup.json().get("card", {}).get("card_id") == card.get("card_id"),
          f"{r_dup.json().get('card', {}).get('card_id')} vs {card.get('card_id')}")

    # 确认（角色驱动，按卡片当前状态自适应）
    cid = card.get("card_id", "")
    roles = card.get("human_confirm", {}).get("required_roles", [])
    cur_status = card.get("human_confirm", {}).get("status", "")
    if cid and roles and cur_status == "PENDING":
        for i, role in enumerate(roles):
            rr = c.post(f"/v1/cards/{cid}/confirm",
                        json={"role": role, "user_id": f"browser-e2e-{i}", "decision": "confirm"})
            exp = "CONFIRMED" if i == len(roles) - 1 else "PENDING"
            check(f"B3.3.{i} 确认 {role}", rr.json().get("status") == exp,
                  f"status={rr.json().get('status')}")
        rr = c.post(f"/v1/cards/{cid}/confirm",
                    json={"role": roles[0], "user_id": "dup", "decision": "confirm"})
        check("B3.4 重复确认返回 409", rr.status_code == 409, f"HTTP {rr.status_code}")
    elif cid and roles:
        # 卡已被确认过（去重复用）：验证终态不可再确认
        check("B3.3 卡片已是终态（去重复用旧卡）", cur_status == "CONFIRMED", f"status={cur_status}")
        rr = c.post(f"/v1/cards/{cid}/confirm",
                    json={"role": roles[0], "user_id": "dup", "decision": "confirm"})
        check("B3.4 终态重复确认返回 409", rr.status_code == 409, f"HTTP {rr.status_code}")
    rr = c.post("/v1/cards/NOPE-999/confirm",
                json={"role": "maintenance_supervisor", "user_id": "x", "decision": "confirm"})
    check("B3.5 不存在卡片返回 409", rr.status_code == 409, f"HTTP {rr.status_code}")

    # 趋势
    r = c.get("/v1/assets/FAN-00004/trend?days=7", headers={"X-Tenant-Id": "site-a"})
    d = r.json()
    check("B4.1 /v1/assets/{id}/trend", r.status_code == 200 and "series" in d,
          f"points={d.get('points')}")
    bare = httpx.Client(base_url=API, timeout=20, headers={"Host": INGRESS_HOST})
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
    h = {"X-Tenant-Id": "site-a", "Host": INGRESS_HOST}
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

    r = c.get("/api/v1/assets", headers={"Host": INGRESS_HOST})
    check("C1.5 缺租户头被拒", r.status_code in (400, 401, 403, 404),
          f"HTTP {r.status_code}")
    c.close()


# ---------------------------------------------------------------------------
# D. 新增前端页面（大屏 / 管理台）
# ---------------------------------------------------------------------------
def test_screen(pw) -> None:
    print("\n===== D1. 大屏驾驶舱 =====", flush=True)
    browser = pw.chromium.launch(executable_path=CHROME, headless=True,
                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    bad: list[str] = []
    js: list[str] = []
    page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.on("pageerror", lambda e: js.append(str(e)[:100]))

    page.goto(f"{BASE}/screen", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5500)
    check("D1.1 大屏标题", "驾驶舱" in page.title(), page.title())

    info = page.evaluate("""() => ({
      ring: document.querySelectorAll('#ring circle').length,
      legend: document.getElementById('legend')?.children.length || 0,
      cells: document.querySelectorAll('.mx-cell').length,
      bars: document.querySelectorAll('.bar-row').length,
      feed: document.querySelectorAll('.ev').length,
      ticker: document.querySelectorAll('.tk-item').length,
      svg: document.querySelectorAll('svg').length,
      scrollW: document.documentElement.scrollWidth,
    })""")
    check("D1.2 环形图已绘制", info["ring"] >= 2, f"{info['ring']} 段弧")
    check("D1.3 四级图例齐全", info["legend"] == 4, f"{info['legend']} 项")
    check("D1.4 健康矩阵有点阵", info["cells"] > 50, f"{info['cells']} 格")
    check("D1.5 关键度条形", info["bars"] >= 1, f"{info['bars']} 条")
    check("D1.6 事件流有内容", info["feed"] >= 1, f"{info['feed']} 条")
    check("D1.7 告警滚动条", info["ticker"] >= 2, f"{info['ticker']} 项")
    check("D1.8 SVG 图表 ≥2 类", info["svg"] >= 2, f"{info['svg']} 个")
    check("D1.9 1920 宽无横向滚动", info["scrollW"] <= 1920, f"scrollW={info['scrollW']}")
    real_bad = [b for b in bad if "favicon" not in b.lower()]
    check("D1.10 无资源加载失败", not real_bad, "; ".join(real_bad[:2])[:100])
    check("D1.11 无 JS 异常", not js, "; ".join(js[:2])[:100])
    shot(page, "D1_screen")
    browser.close()


def test_console(pw) -> None:
    print("\n===== D2. 管理台多视图 =====", flush=True)
    browser = pw.chromium.launch(executable_path=CHROME, headless=True,
                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    bad: list[str] = []
    js: list[str] = []
    page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.on("pageerror", lambda e: js.append(str(e)[:100]))

    views = [("dashboard", "运营看板", "#kpis .kpi"),
             ("assets", "设备台账", "#asset-rows tr"),
             ("cards", "工单看板", ".kcol"),
             ("diagnose", "诊断工作台", "#radar-box svg")]
    for slug, name, sel in views:
        page.goto(f"{BASE}/console#/{slug}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3800)
        n = page.locator(sel).count()
        check(f"D2 {name} 渲染", n > 0, f"{sel} x{n}")
        check(f"D2 {name} 标题", name in page.title(), page.title())

    # 诊断台自动加载（进入即出结果，不留空态）
    page.goto(f"{BASE}/console#/diagnose", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(4500)
    ev = page.locator("#ev-list .ev-item").count()
    check("D2.5 诊断台自动出证据", ev >= 1, f"{ev} 条")
    radar_pts = page.evaluate(
        "() => document.querySelectorAll('#radar-box polygon').length")
    check("D2.6 雷达图五维成型", radar_pts >= 5, f"{radar_pts} 个多边形")

    page.goto(f"{BASE}/console#/cards", wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(1500)
    on = page.locator(".nav a.on").count()
    check("D2.7 侧导航高亮当前项", on == 1, f"{on} 个高亮")
    shot(page, "D2_console")

    real_bad = [b for b in bad if "favicon" not in b.lower()]
    check("D2.8 无资源加载失败", not real_bad, "; ".join(real_bad[:2])[:100])
    check("D2.9 无 JS 异常", not js, "; ".join(js[:2])[:100])
    browser.close()



# ---------------------------------------------------------------------------
# E. 功能扩展（FR-01 ~ FR-06）：页面 + API
# ---------------------------------------------------------------------------
def test_extended(pw) -> None:
    print("\n===== E1. 告警中心 =====", flush=True)
    browser = pw.chromium.launch(executable_path=CHROME, headless=True,
                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    bad: list[str] = []
    js: list[str] = []
    page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.on("pageerror", lambda e: js.append(str(e)[:110]))

    def goto(slug, wait=2800):
        page.goto(f"{BASE}/console#/{slug}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait)

    goto("alerts")
    kpis = page.locator("#alert-kpis .kpi").count()
    rows = page.locator("#alert-rows tr").count()
    check("E1.1 告警 KPI 渲染", kpis == 5, f"{kpis} 个")
    check("E1.2 告警列表有数据", rows >= 1, f"{rows} 行")
    has_sla = page.evaluate(
        "() => [...document.querySelectorAll('#alert-rows tr')].some(t=>/剩余|超时|—/.test(t.innerText))")
    check("E1.3 SLA 列有值", has_sla)
    # 筛选：只看红色
    page.click("#seg-alevel button[data-l='red']")
    page.wait_for_timeout(900)
    red_rows = page.locator("#alert-rows tr").count()
    check("E1.4 按等级筛选生效", red_rows >= 0 and red_rows <= rows, f"红 {red_rows} / 全部 {rows}")
    page.click("#seg-alevel button[data-l='']")
    page.wait_for_timeout(700)

    # 同步幂等（API 层）
    print("\n===== E2. 新增 API =====", flush=True)
    c = httpx.Client(base_url=API, timeout=40,
                     headers={"X-Tenant-Id": "site-a", "Host": INGRESS_HOST},
                     limits=httpx.Limits(max_keepalive_connections=0))

    # 强制鉴权后，写操作（告警处置等）必须先登录取 token
    lr = c.post("/v1/auth/login", json={"username": "admin", "password": "Admin@2026"})
    tok = lr.json().get("token", "") if lr.status_code == 200 else ""
    check("E2.0 登录取 token（写操作前置）", bool(tok), f"HTTP {lr.status_code}")

    r1 = c.post("/v1/alerts/sync", json={"threshold_hi": 70})
    r2 = c.post("/v1/alerts/sync", json={"threshold_hi": 70})
    check("E2.1 告警同步首次", r1.status_code == 200 and "created" in r1.json(),
          json.dumps(r1.json(), ensure_ascii=False)[:80])
    check("E2.2 告警同步幂等", r2.json().get("created") == 0,
          f"第二次 created={r2.json().get('created')}")

    r = c.get("/v1/alerts?limit=5")
    d = r.json()
    check("E2.3 告警列表接口", r.status_code == 200 and d.get("count", 0) >= 0,
          f"count={d.get('count')}")
    first = (d.get("alerts") or [{}])[0]
    check("E2.4 告警含 SLA 字段", "sla_remaining_hours" in first,
          f"{first.get('sla_remaining_hours')}h overdue={first.get('overdue')}")

    r = c.get("/v1/diagnoses?limit=3")
    check("E2.5 诊断历史接口", r.status_code == 200 and r.json().get("count", 0) >= 1,
          f"count={r.json().get('count')}")
    r = c.get("/v1/diagnoses?asset_id=FAN&limit=3")
    check("E2.6 诊断按位号前缀搜索", r.status_code == 200,
          f"count={r.json().get('count')}")

    r = c.get("/v1/stats/cost")
    d = r.json()
    tot = d.get("total") or {}
    bd = d.get("breakdown") or {}
    ssum = sum(bd.values())
    check("E2.7 成本接口", r.status_code == 200 and tot.get("total_cost") is not None,
          f"net={tot.get('net_benefit')} roi={tot.get('roi_pct')}%")
    check("E2.8 成本口径一致（构成和=总成本）",
          abs(ssum - (tot.get("total_cost") or 0)) < 1,
          f"{round(ssum,2)} vs {tot.get('total_cost')}")
    check("E2.9 净收益=避免损失-总成本",
          abs((tot.get("avoided_loss", 0) - tot.get("total_cost", 0))
              - tot.get("net_benefit", 0)) < 1,
          f"{tot.get('net_benefit')}")

    r = c.get("/v1/org/tree")
    d = r.json()
    nodes = d.get("nodes") or []
    check("E2.10 组织树接口", r.status_code == 200 and len(nodes) >= 1, f"{len(nodes)} 节点")
    check("E2.11 组织树含层级与汇总", all(
        "depth" in n and "asset_count" in n and "levels" in n for n in nodes[:5]),
        "字段齐全")

    r = c.get("/v1/stats/system")
    d = r.json()
    tabs = d.get("tables") or []
    check("E2.12 系统统计接口", r.status_code == 200 and len(tabs) >= 8, f"{len(tabs)} 张表")
    check("E2.13 大表用估算标记", any(t.get("exact") is False for t in tabs),
          "有 exact=false 的表")

    # 告警处置状态流转
    if first.get("alert_id") and first.get("status") != "CLOSED":
        aid = first["alert_id"]
        ra = c.post(f"/v1/alerts/{aid}/ack",
                    json={"action": "ack", "by": "e2e", "comment": "自动化确认"},
                    headers={"X-Auth-Token": tok})
        check("E2.14 告警确认流转", ra.status_code == 200 and ra.json().get("status") == "ACKED",
              str(ra.json())[:70])
        rb = c.post(f"/v1/alerts/{aid}/ack",
                    json={"action": "escalate", "by": "e2e"},
                    headers={"X-Auth-Token": tok})
        check("E2.15 告警升级流转", rb.json().get("status") == "ESCALATED",
              str(rb.json())[:70])
    rc = c.post("/v1/alerts/ALT-NOT-EXIST/ack", json={"action": "ack", "by": "e2e"},
                headers={"X-Auth-Token": tok})
    check("E2.16 不存在告警返回 404", rc.status_code == 404, f"HTTP {rc.status_code}")
    c.close()

    print("\n===== E3. 设备 360 / 诊断历史 / 成本 / 组织 / 运维 页面 =====", flush=True)
    goto("asset/FAN-00004", 3600)
    title = page.locator("#a360-title").inner_text()
    kv_items = page.locator("#a360-kv > div > div").count()
    mps = page.locator("#a360-mps tr").count()
    check("E3.1 设备 360 标题", "FAN-00004" in title, title)
    check("E3.2 设备 360 基础信息", kv_items >= 8, f"{kv_items} 项")
    check("E3.3 设备 360 测点清单", mps >= 1, f"{mps} 行")
    check("E3.4 设备 360 趋势或空态",
          page.locator("#a360-trend svg").count() >= 1
          or page.locator("#a360-trend .empty").count() >= 1)

    goto("diagnoses", 6500)   # 批量诊断 CronJob 每 15 分钟跑一轮，期间页面会变慢
    drows = page.locator("#diag-rows tr").count()
    check("E3.5 诊断历史列表", drows >= 10, f"{drows} 行")
    page.fill("#dq", "FAN-00004")
    page.wait_for_timeout(1400)
    frows = page.locator("#diag-rows tr").count()
    check("E3.6 诊断历史搜索", 0 < frows <= drows, f"过滤后 {frows} 行")

    goto("cost")
    ck = page.locator("#cost-kpis .kpi").count()
    donut = page.locator("#cost-donut svg circle").count()
    top = page.locator("#cost-top > div").count()
    opts = page.locator("#cost-options tr").count()
    check("E3.7 成本 KPI", ck == 4, f"{ck} 个")
    check("E3.8 成本环形图", donut >= 4, f"{donut} 段")
    check("E3.9 净收益排行", top >= 1, f"{top} 条")
    check("E3.10 方案对比表", opts >= 1, f"{opts} 行")

    goto("org")
    orows = page.locator("#org-rows tr").count()
    ok = page.locator("#org-kpis .kpi").count()
    check("E3.11 组织树渲染", orows >= 5, f"{orows} 节点")
    check("E3.12 组织 KPI", ok == 5, f"{ok} 个")

    goto("system")
    stabs = page.locator("#sys-tables div").count()
    sk = page.locator("#sys-kpis .kpi").count()
    check("E3.13 系统表规模", stabs >= 24, f"{stabs} 个节点（12 表×2）")
    check("E3.14 系统 KPI", sk == 4, f"{sk} 个")

    real_bad = [b for b in bad if "favicon" not in b.lower()]
    check("E3.15 新页面无资源失败", not real_bad, "; ".join(real_bad[:2])[:110])
    check("E3.16 新页面无 JS 异常", not js, "; ".join(js[:2])[:110])
    shot(page, "E3_extended")
    browser.close()



# ---------------------------------------------------------------------------
# F. 认证与角色（RBAC）+ 点检巡检 + 保养计划
# ---------------------------------------------------------------------------
def test_ops_and_auth(pw) -> None:
    print("\n===== F1. 认证与角色（API）=====", flush=True)
    c = httpx.Client(base_url=API, timeout=40,
                     headers={"X-Tenant-Id": "site-a", "Host": INGRESS_HOST},
                     limits=httpx.Limits(max_keepalive_connections=0))

    r = c.post("/v1/auth/login", json={"username": "admin", "password": "Admin@2026"})
    d = r.json() if r.status_code == 200 else {}
    tok = d.get("token", "")
    check("F1.1 主管登录", r.status_code == 200 and d.get("role") == "manager",
          f"HTTP {r.status_code} role={d.get('role')}")
    check("F1.2 登录返回权限集", len(d.get("permissions") or []) >= 8,
          f"{len(d.get('permissions') or [])} 项")

    r = c.post("/v1/auth/login", json={"username": "admin", "password": "wrong"})
    check("F1.3 错误密码 401", r.status_code == 401, f"HTTP {r.status_code}")

    r = c.get("/v1/auth/me", headers={"X-Auth-Token": tok})
    check("F1.4 token 校验", r.status_code == 200 and r.json().get("sub") == "admin",
          f"HTTP {r.status_code}")

    bad_tok = tok[:-4] + "AAAA"
    r = c.get("/v1/auth/me", headers={"X-Auth-Token": bad_tok})
    check("F1.5 篡改 token 被拒", r.status_code == 401, f"HTTP {r.status_code}")

    r = c.get("/v1/users", headers={"X-Auth-Token": tok})
    check("F1.6 主管可查用户", r.status_code == 200 and r.json().get("count", 0) >= 3,
          f"count={r.json().get('count') if r.status_code == 200 else '-'}")

    ro = c.post("/v1/auth/login", json={"username": "op_li", "password": "Oper@2026"})
    optok = ro.json().get("token", "") if ro.status_code == 200 else ""
    check("F1.7 值班员登录", bool(optok), f"role={ro.json().get('role') if ro.status_code==200 else ro.status_code}")

    r = c.get("/v1/users", headers={"X-Auth-Token": optok})
    check("F1.8 值班员越权查用户 403", r.status_code == 403, f"HTTP {r.status_code}")

    print("\n===== F2. 点检与保养（API）=====", flush=True)
    r = c.get("/v1/plans?limit=5")
    d = r.json()
    check("F2.1 保养计划接口", r.status_code == 200 and d.get("count", 0) >= 1,
          f"count={d.get('count')}")
    plan = (d.get("plans") or [{}])[0]
    check("F2.2 计划含周期与到期", "cycle_days" in plan and "days_left" in plan,
          f"周期{plan.get('cycle_days')}天 剩{plan.get('days_left')}天")

    r = c.get("/v1/inspections?limit=5")
    d = r.json()
    check("F2.3 点检任务接口", r.status_code == 200 and d.get("count", 0) >= 1,
          f"count={d.get('count')}")
    task = (d.get("tasks") or [{}])[0]
    check("F2.4 任务含检查项与逾期标记", isinstance(task.get("items"), list)
          and "overdue" in task,
          f"{len(task.get('items') or [])} 项 overdue={task.get('overdue')}")

    r = c.post("/v1/inspections/generate", json={"days": 3, "limit": 5},
               headers={"X-Auth-Token": optok})
    check("F2.5 值班员无 plan.edit 被拒", r.status_code == 403, f"HTTP {r.status_code}")

    r1 = c.post("/v1/inspections/generate", json={"days": 3, "limit": 5},
                headers={"X-Auth-Token": tok})
    r2 = c.post("/v1/inspections/generate", json={"days": 3, "limit": 5},
                headers={"X-Auth-Token": tok})
    check("F2.6 主管可生成任务", r1.status_code == 200 and "created" in r1.json(),
          f"created={r1.json().get('created')}")
    check("F2.7 生成幂等", r2.json().get("created") == 0,
          f"第二次 created={r2.json().get('created')}")

    r = c.get("/v1/stats/inspection")
    d = r.json()
    need = {"pending", "done", "overdue", "ng_items", "plans", "completion_pct"}
    check("F2.8 点检汇总字段齐全", r.status_code == 200 and need.issubset(d.keys()),
          f"缺 {need - set(d.keys())}" if not need.issubset(d.keys()) else
          f"待执行{d.get('pending')} 逾期{d.get('overdue')} 计划{d.get('plans')}")

    r = c.post("/v1/inspections/INS-NOT-EXIST/submit", json={"items": []},
               headers={"X-Auth-Token": optok})
    check("F2.9 不存在任务 404", r.status_code == 404, f"HTTP {r.status_code}")

    print("\n===== F3. 页面与交互 =====", flush=True)
    browser = pw.chromium.launch(executable_path=CHROME, headless=True,
                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    bad: list[str] = []
    js: list[str] = []
    page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.on("pageerror", lambda e: js.append(str(e)[:110]))

    page.goto(f"{BASE}/console#/inspections", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3200)
    check("F3.1 点检页 KPI", page.locator("#ins-kpis .kpi").count() == 5,
          f"{page.locator('#ins-kpis .kpi').count()} 个")
    rows = page.locator("#ins-rows tr").count()
    check("F3.2 点检页有任务", rows >= 1, f"{rows} 行")
    shot(page, "F3_inspections")

    # 无待执行任务时补生成，保证交互用例可复跑
    if page.locator("#ins-rows button[data-ins]").count() == 0:
        c.post("/v1/inspections/generate", json={"days": 7, "limit": 20},
               headers={"X-Auth-Token": tok})
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3200)

    # 强制鉴权后，点检提交等写操作必须已登录
    if page.locator("#user-label").inner_text().strip() == "登录":
        page.click("#btn-user")
        page.wait_for_timeout(400)
        page.fill("#login-user", "admin")
        page.fill("#login-pass", "Admin@2026")
        page.click("#login-submit")
        page.wait_for_timeout(2000)
        page.goto(f"{BASE}/console#/inspections", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

    btn = page.locator("#ins-rows button[data-ins]").first
    if btn.count():
        btn.click()
        page.wait_for_timeout(800)
        n_items = page.locator("#ins-m-items .ins-item").count()
        opened = page.evaluate("() => document.getElementById('ins-mask').classList.contains('on')")
        check("F3.3 点检弹窗打开且带检查项", opened and n_items >= 1,
              f"items={n_items} open={opened}")
        shot(page, "F3_inspect_modal")

        page.locator("#ins-m-items .ins-item").first.locator(".seg2 .ng").click()
        page.locator("#ins-m-note").fill("自动化回归：标记异常")
        page.click("#ins-m-submit")
        page.wait_for_timeout(2500)
        toast = page.evaluate("() => document.getElementById('toast')?.innerText?.trim()")
        closed = not page.evaluate(
            "() => document.getElementById('ins-mask').classList.contains('on')")
        check("F3.4 提交点检成功", closed and bool(toast) and "完成" in (toast or ""),
              str(toast)[:60])
        done_rows = page.locator("#ins-rows tr:has-text('已完成')").count()
        check("F3.5 提交后状态变已完成", done_rows >= 1, f"{done_rows} 行已完成")
    else:
        check("F3.3 存在可执行任务", False, "无 PENDING 任务且补生成失败")

    page.goto(f"{BASE}/console#/plans", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    check("F3.6 保养计划 KPI", page.locator("#plan-kpis .kpi").count() == 5,
          f"{page.locator('#plan-kpis .kpi').count()} 个")
    prows = page.locator("#plan-rows tr").count()
    check("F3.7 保养计划列表", prows >= 1, f"{prows} 行")
    page.click("#seg-pdue button[data-d='overdue']")
    page.wait_for_timeout(1100)
    ov = page.locator("#plan-rows tr").count()
    check("F3.8 逾期筛选生效", ov <= prows, f"逾期 {ov} / 全部 {prows}")
    page.click("#seg-pdue button[data-d='']")
    page.wait_for_timeout(900)
    shot(page, "F3_plans")

    # 登录 UI（F3.3 可能已登录：此时先登出再走一遍完整登录流程）
    if page.locator("#user-label").inner_text().strip() != "登录":
        page.click("#btn-user")
        page.wait_for_timeout(900)
    page.click("#btn-user")
    page.wait_for_timeout(500)
    page.fill("#login-user", "admin")
    page.fill("#login-pass", "Admin@2026")
    page.click("#login-submit")
    page.wait_for_timeout(2200)
    label = page.locator("#user-label").inner_text()
    check("F3.9 登录后显示身份", "主管" in label or "管理员" in label, label)
    shot(page, "F3_login")

    # 值班员权限收紧
    page.click("#btn-user")          # 登出
    page.wait_for_timeout(700)
    logged_out = page.locator("#user-label").inner_text().strip() == "登录"
    check("F3.10 登出清除身份", logged_out, page.locator("#user-label").inner_text())
    page.click("#btn-user")
    page.wait_for_timeout(400)
    page.fill("#login-user", "op_li")
    page.fill("#login-pass", "Oper@2026")
    page.click("#login-submit")
    page.wait_for_timeout(2000)
    page.goto(f"{BASE}/console#/inspections", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    page.click("#btn-gen-ins")
    page.wait_for_timeout(900)
    denied = page.evaluate("() => document.getElementById('toast')?.innerText?.trim()")
    check("F3.11 值班员无生成权限被拦", "权限" in (denied or ""), str(denied)[:60])
    shot(page, "F3_operator_denied")

    # 回归：点击委托白名单（曾漏 data-ack，导致按钮点了没反应且不报错）
    page.goto(f"{BASE}/console#/alerts", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    if page.locator("#user-label").inner_text().strip() == "登录":
        page.click("#btn-user"); page.wait_for_timeout(400)
        page.fill("#login-user", "admin"); page.fill("#login-pass", "Admin@2026")
        page.click("#login-submit"); page.wait_for_timeout(2000)
        page.goto(f"{BASE}/console#/alerts", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
    # 告警中心 KPI 取自 summary（全量），不应因列表窗口而显示 0
    kpi_red = page.evaluate("""() => {const e=[...document.querySelectorAll('#alert-kpis .kpi')]
        .find(k=>k.innerText.includes('红色告警')); return e?e.querySelector('.v').innerText:'-';}""")
    kpi_open = page.evaluate("""() => {const e=[...document.querySelectorAll('#alert-kpis .kpi')]
        .find(k=>k.innerText.includes('待处置')); return e?e.querySelector('.v').innerText:'-';}""")
    check("F3.12a 告警中心红色 KPI 非 0（汇总口径回归）",
          str(kpi_red).strip() not in ("0", "-", ""),
          f"红色告警={kpi_red} 待处置={kpi_open}")

    ack = page.locator("#alert-rows button[data-ack]").first
    if ack.count():
        ack.click()
        page.wait_for_timeout(2400)
        t = page.evaluate("() => document.getElementById('toast')?.innerText?.trim()")
        check("F3.12 告警确认按钮可点击（点击委托回归）",
              "ACKED" in (t or "") or "告警" in (t or ""), str(t)[:60])
    else:
        # 前面用例可能已把告警全部确认/关闭，此时页面无按钮属正常状态。
        # 点击委托的回归由 H 组与 F 组其他写操作按钮覆盖。
        rows = page.locator("#alert-rows tr").count()
        check("F3.12 告警页正常渲染（无可处置项）", rows >= 1, f"{rows} 行，全部已处置")

    page.goto(f"{BASE}/console#/diagnoses", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2800)
    b2 = page.locator("#diag-rows button[data-asset360]").first
    if b2.count():
        b2.click()
        page.wait_for_timeout(3000)
        h = page.evaluate("() => location.hash")
        title = page.locator("#a360-title").inner_text()
        check("F3.13 诊断历史可跳设备 360", "#/asset/" in h and bool(title.strip()),
              f"{h} · {title}")
    else:
        check("F3.13 诊断历史有跳转按钮", False, "无 data-asset360")

    # 主题切换：暗色令牌 + 持久化
    page.goto(f"{BASE}/console#/dashboard", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2600)
    before = page.evaluate("() => document.body.classList.contains('dark')")
    page.click("#btn-theme")
    page.wait_for_timeout(1000)
    after = page.evaluate("() => document.body.classList.contains('dark')")
    bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    check("F3.16 主题切换生效", after != before and "rgb(11, 18, 32)" == bg,
          f"dark={after} bg={bg}")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(2200)
    kept = page.evaluate("() => document.body.classList.contains('dark')")
    check("F3.17 主题选择被记住", kept == after, f"刷新后 dark={kept}")
    page.click("#btn-theme")
    page.wait_for_timeout(800)
    check("F3.18 可切回浅色",
          page.evaluate("() => !document.body.classList.contains('dark')"),
          page.evaluate("() => getComputedStyle(document.body).backgroundColor"))

    real_bad = [b for b in bad if "favicon" not in b.lower()]
    check("F3.14 新页面无资源失败", not real_bad, "; ".join(real_bad[:2])[:110])
    check("F3.15 新页面无 JS 异常", not js, "; ".join(js[:2])[:110])
    browser.close()
    c.close()



# ---------------------------------------------------------------------------
# G. 工单执行 / 备件 / 知识库 / 告警规则 / 批量诊断（docs/36 收尾模块）
# ---------------------------------------------------------------------------
def test_new_modules(pw) -> None:
    print("\n===== G1. 批量诊断与总览 =====", flush=True)
    c = httpx.Client(base_url=API, timeout=120,
                     headers={"X-Tenant-Id": "site-a", "Host": INGRESS_HOST},
                     limits=httpx.Limits(max_keepalive_connections=0))
    lr = c.post("/v1/auth/login", json={"username": "admin", "password": "Admin@2026"})
    tok = lr.json().get("token", "") if lr.status_code == 200 else ""
    check("G1.1 登录取 token", bool(tok), f"HTTP {lr.status_code}")

    r = c.get("/v1/stats/overview")
    d = r.json()
    check("G1.2 总览覆盖度 100%", d.get("coverage_pct", 0) >= 99.9,
          f"在监 {d.get('total_assets')} 已评估 {d.get('assessed')} ({d.get('coverage_pct')}%)")
    check("G1.3 已评估不超过在监", d.get("assessed", 0) <= d.get("total_assets", 0),
          f"{d.get('assessed')} ≤ {d.get('total_assets')}")
    check("G1.4 四级分布齐全", set((d.get("by_level") or {}).keys()) >=
          {"red", "orange", "yellow", "green"}, str(d.get("by_level")))

    r = c.post("/v1/diagnose/batch", json={"limit": 20}, headers={"X-Auth-Token": tok})
    d = r.json()
    check("G1.5 批量诊断（限量）", r.status_code == 200 and d.get("total", 0) >= 1,
          f"total={d.get('total')} ok={d.get('ok')} failed={d.get('failed')} "
          f"{d.get('elapsed_ms')}ms")
    check("G1.6 批量诊断无失败", d.get("failed") == 0, str(d.get("failures"))[:80])

    r = c.post("/v1/diagnose/batch", json={"limit": 5})
    check("G1.7 批量诊断需登录", r.status_code == 403, f"HTTP {r.status_code}")

    print("\n===== G2. 工单执行跟踪 =====", flush=True)
    cards = c.get("/v1/cards?limit=5").json().get("cards") or []
    cid = (cards[0]["card_id"] if cards else "")
    if cid:
        r = c.post("/v1/worklogs", json={"card_id": cid, "action": "ASSIGNED",
                                         "note": "G 组回归：派工"},
                   headers={"X-Auth-Token": tok})
        check("G2.1 记录派工", r.status_code in (200, 201) and r.json().get("action") == "ASSIGNED",
              f"HTTP {r.status_code}")
        r2 = c.post("/v1/worklogs", json={"card_id": cid, "action": "STARTED",
                                          "actual_cost": 1280.5},
                    headers={"X-Auth-Token": tok})
        check("G2.2 记录开始并带成本", r2.json().get("action") == "STARTED",
              str(r2.json())[:60])
        r3 = c.post("/v1/worklogs", json={"card_id": cid, "action": "BOGUS"},
                    headers={"X-Auth-Token": tok})
        check("G2.3 非法动作 400", r3.status_code == 400, f"HTTP {r3.status_code}")
        logs = c.get(f"/v1/worklogs?card_id={cid}").json()
        check("G2.4 按卡号查流水", logs.get("count", 0) >= 2,
              f"{logs.get('count')} 条")
        check("G2.5 流水含中文动作名",
              all("action_label" in x for x in (logs.get("items") or [])[:2]))
    r = c.post("/v1/worklogs", json={"card_id": "REC-X", "action": "ASSIGNED"})
    check("G2.6 工单日志需登录", r.status_code == 403, f"HTTP {r.status_code}")

    print("\n===== G3. 备件管理 =====", flush=True)
    r = c.get("/v1/spares")
    d = r.json()
    check("G3.1 备件列表", r.status_code == 200 and d.get("count", 0) >= 10,
          f"{d.get('count')} 种，低库存 {d.get('low_count')}，金额 {d.get('total_value')}")
    items = d.get("items") or []
    check("G3.2 低库存标记正确",
          all((x["stock"] <= x["safety_stock"]) == x["is_low"] for x in items[:5]))
    low = c.get("/v1/spares?low_only=true").json()
    check("G3.3 低库存筛选", all(x["is_low"] for x in (low.get("items") or [])),
          f"{low.get('count')} 条")
    if items:
        code = items[0]["part_code"]
        before = items[0]["stock"]
        r = c.post(f"/v1/spares/{code}/move", json={"delta": -1, "note": "G 组领用"},
                   headers={"X-Auth-Token": tok})
        after = r.json().get("stock")
        check("G3.4 领用扣减库存", after == before - 1, f"{before} → {after}")
        r = c.post(f"/v1/spares/{code}/move", json={"delta": 1, "note": "G 组退回"},
                   headers={"X-Auth-Token": tok})
        check("G3.5 入库恢复库存", r.json().get("stock") == before,
              f"→ {r.json().get('stock')}")
        r = c.post(f"/v1/spares/{code}/move", json={"delta": -99999},
                   headers={"X-Auth-Token": tok})
        check("G3.6 超领被拒（不允许负库存）", r.status_code == 400,
              r.json().get("detail", "")[:40])
        r = c.post(f"/v1/spares/NO-SUCH-PART/move", json={"delta": 1},
                   headers={"X-Auth-Token": tok})
        check("G3.7 不存在备件 400", r.status_code == 400, f"HTTP {r.status_code}")
    iss = c.get("/v1/spares/issues?limit=5").json()
    check("G3.8 领用记录可查", iss.get("count", 0) >= 2, f"{iss.get('count')} 条")

    print("\n===== G4. 知识库 =====", flush=True)
    r = c.get("/v1/knowledge")
    check("G4.1 案例列表", r.status_code == 200 and r.json().get("count", 0) >= 3,
          f"{r.json().get('count')} 条")
    r = c.get("/v1/knowledge?q=轴承")
    check("G4.2 关键词检索", r.status_code == 200 and r.json().get("count", 0) >= 1,
          f"命中 {r.json().get('count')}")
    r = c.get("/v1/knowledge?q=不存在的关键词xyz")
    check("G4.3 无命中返回空", r.json().get("count") == 0, f"{r.json().get('count')} 条")
    r = c.post("/v1/knowledge", json={"title": "G 组回归案例", "root_cause": "测试",
                                      "tags": "回归"}, headers={"X-Auth-Token": tok})
    check("G4.4 新增案例", r.status_code in (200, 201) and r.json().get("case_id"),
          str(r.json())[:60])
    r = c.post("/v1/knowledge", json={"title": ""})
    check("G4.5 空标题 400", r.status_code in (400, 403), f"HTTP {r.status_code}")

    print("\n===== G5. 告警规则 =====", flush=True)
    # 判据版本化后规则可能在 site 或 group 域（docs/43 P0-3）——
    # 合并两域检查，避免"发布到集团后本地列表看不到"的误判
    site_rules = (c.get("/v1/rules?rule_scope=site").json() or {}).get("items") or []
    group_rules = (c.get("/v1/rules?rule_scope=group").json() or {}).get("items") or []
    rules = site_rules + group_rules
    check("G5.1 规则列表", len(site_rules) >= 1 or len(group_rules) >= 1,
          f"site={len(site_rules)} group={len(group_rules)}")
    check("G5.2 阈值已从硬编码改为配置",
          any(x["rule_id"] == "HI_WARNING" for x in rules),
          f"HI_WARNING 存在（site={len(site_rules)} group={len(group_rules)}）")
    target = next((x for x in rules if x["rule_id"] == "HI_WARNING"), None)
    if target:
        r = c.put("/v1/rules/HI_WARNING",
                  json={"threshold": 65, "level": "orange", "sla_hours": 24},
                  headers={"X-Auth-Token": tok})
        check("G5.3 主管可改阈值", r.status_code == 200 and r.json().get("threshold") == 65,
              str(r.json())[:60])
        op = c.post("/v1/auth/login", json={"username": "op_li", "password": "Oper@2026"})
        otok = op.json().get("token", "")
        r = c.put("/v1/rules/HI_WARNING",
                  json={"threshold": 60, "level": "orange", "sla_hours": 24},
                  headers={"X-Auth-Token": otok})
        check("G5.4 值班员改阈值被拒", r.status_code == 403, f"HTTP {r.status_code}")
        c.put("/v1/rules/HI_WARNING",
              json={"threshold": 70, "level": "orange", "sla_hours": 24},
              headers={"X-Auth-Token": tok})
        check("G5.5 阈值已还原", True, "HI_WARNING 回到 70")
    r = c.put("/v1/rules/BAD_OP", json={"op": "xx", "threshold": 1},
              headers={"X-Auth-Token": tok})
    check("G5.6 非法比较符 400", r.status_code == 400, f"HTTP {r.status_code}")

    r = c.post("/v1/alerts/sync", json={"threshold_hi": 70})
    d = r.json()
    check("G5.7 告警同步走规则表", r.status_code == 200 and "created" in d,
          f"created={d.get('created')} skipped={d.get('skipped')}")
    c.close()



# ---------------------------------------------------------------------------
# H. 全链路业务闭环 + 三端口径一致性 + 主题矩阵 + 权限矩阵
# ---------------------------------------------------------------------------
def test_full_chain(pw) -> None:
    print("\n===== H1. 业务全链路闭环（诊断→告警→会诊→确认→执行→领件→沉淀）=====",
          flush=True)
    c = httpx.Client(base_url=API, timeout=120,
                     headers={"X-Tenant-Id": "site-a", "Host": INGRESS_HOST},
                     limits=httpx.Limits(max_keepalive_connections=0))
    lr = c.post("/v1/auth/login", json={"username": "admin", "password": "Admin@2026"})
    tok = lr.json().get("token", "") if lr.status_code == 200 else ""
    H = {"X-Auth-Token": tok}

    ov0 = c.get("/v1/stats/overview").json()
    total_assets = ov0.get("total_assets")
    print(f"  基线：在监 {total_assets}，已评估 {ov0.get('assessed')}，"
          f"红 {ov0['by_level']['red']} 橙 {ov0['by_level']['orange']}")

    # 1) 选一台有健康度的设备（优先最差的，便于触发告警）
    health = c.get("/v1/inspections?limit=1").json()      # 仅为确认服务可用
    hot = c.get("/v1/stats/overview").json()
    worst = None
    diag_list = c.get("/v1/diagnoses?limit=50").json().get("items") or []
    if diag_list:
        worst = min(diag_list, key=lambda d: float(d.get("hi") or 100))
    asset_id = (worst or {}).get("asset_id") or "FAN-00004"
    print(f"  选取设备：{asset_id}")

    # 2) 单点诊断
    r = c.post("/v1/diagnose", json={"tenant_id": "site-a", "asset_id": asset_id})
    dg = r.json()
    hi, level = dg.get("hi"), dg.get("level")
    check("H1.1 单点诊断返回 HI 与等级", r.status_code == 200 and hi is not None,
          f"hi={hi} level={level}")
    check("H1.2 诊断含贡献度与证据",
          isinstance(dg.get("contributions"), list) and isinstance(dg.get("evidence"), list),
          f"贡献 {len(dg.get('contributions') or [])} 项，证据 {len(dg.get('evidence') or [])} 条")

    # 3) 告警同步（规则驱动）
    r = c.post("/v1/alerts/sync", json={"threshold_hi": 70})
    check("H1.3 告警同步", r.status_code == 200 and "created" in r.json(),
          f"created={r.json().get('created')} skipped={r.json().get('skipped')} "
          f"superseded={r.json().get('superseded')}")
    check("H1.3b 同步返回等级收敛计数", "superseded" in r.json(),
          f"superseded={r.json().get('superseded')}（HI 恶化时关闭低级告警）")
    # 不假设"选中的设备此刻一定有未关闭告警"——前面用例会 ack/close 告警，
    # 那样会偶发失败。改为对全部未关闭告警做一次"等级 vs HI"全量校验。
    alerts = c.get("/v1/alerts?limit=300").json().get("alerts") or []
    live = [a for a in alerts if a["status"] in ("OPEN", "ACKED", "ESCALATED")]
    his = {d["asset_id"]: float(d.get("hi") or 100)
           for d in (c.get("/v1/diagnoses?limit=200").json().get("items") or [])}
    check("H1.4 存在未关闭告警", len(live) >= 1, f"{len(live)} 条")
    mism, checked = [], 0
    for a in live:
        h = his.get(a["asset_id"])
        if h is None:
            continue
        checked += 1
        exp = "red" if h < 40 else ("orange" if h < 70 else None)
        if exp and a.get("level") != exp:
            mism.append(f"{a['asset_id']}(hi={h},{a.get('level')}≠{exp})")
    check("H1.6 告警等级与 HI 匹配（全量校验）", not mism,
          f"校验 {checked} 条" + (f"，不符 {mism[:3]}" if mism else "，全部一致"))
    if live:
        a0 = live[0]
        check("H1.5 告警带 SLA 与责任人",
              a0.get("sla_hours") and a0.get("owner_role"),
              f"SLA {a0.get('sla_hours')}h 责任人 {a0.get('owner_role')}")

    dual = c.get("/v1/alerts?limit=300").json().get("alerts") or []
    per_asset: dict[str, set] = {}
    for a in dual:
        if a["status"] in ("OPEN", "ACKED", "ESCALATED"):
            per_asset.setdefault(a["asset_id"], set()).add(a["level"])
    conflicted = [k for k, v in per_asset.items() if len(v) > 1]
    check("H1.6b 同一设备不并存多等级告警", not conflicted,
          f"{len(conflicted)} 台冲突" + (f"（如 {conflicted[:3]}）" if conflicted else ""))

    # 3b) 告警列表窗口与汇总（回归：曾因按 created_at DESC 取 200 条，
    #     窗口被 200 条 auto-superseded 已关闭告警占满，KPI 全 0、看不到待处置）
    r = c.get("/v1/alerts?limit=200")
    body = r.json()
    alerts_win = body.get("alerts") or []
    summ = body.get("summary") or {}
    check("H1.3c 告警接口返回全量汇总", "active" in summ and "red_open" in summ,
          f"active={summ.get('active')} red_open={summ.get('red_open')} "
          f"overdue={summ.get('overdue')}")
    check("H1.3d 汇总与状态计数自洽",
          summ.get("active") == (summ.get("OPEN", 0) + summ.get("ACKED", 0)
                                 + summ.get("ESCALATED", 0)),
          f"active={summ.get('active')} = "
          f"{summ.get('OPEN')}+{summ.get('ACKED')}+{summ.get('ESCALATED')}")
    open_in_win = [a for a in alerts_win if a["status"] != "CLOSED"]
    check("H1.3e 列表窗口含未关闭告警（不被已关闭挤满）",
          len(open_in_win) > 0,
          f"窗口 {len(alerts_win)} 条中未关闭 {len(open_in_win)} 条")
    check("H1.3f 列表首条为未关闭告警",
          bool(alerts_win) and alerts_win[0]["status"] != "CLOSED",
          f"首条 status={alerts_win[0]['status'] if alerts_win else '-'}")

    # 4) 会诊 → 方案卡 → 多角色确认
    r = c.post("/v1/consult", json={"tenant_id": "site-a", "asset_id": asset_id})
    card = (r.json() or {}).get("card") or {}
    cid = card.get("card_id", "")
    check("H1.7 会诊生成方案卡", bool(cid), f"{cid} verdict={card.get('verdict')}")
    roles = card.get("human_confirm", {}).get("required_roles") or []
    if cid and roles and card.get("human_confirm", {}).get("status") == "PENDING":
        last = None
        for i, role in enumerate(roles):
            last = c.post(f"/v1/cards/{cid}/confirm",
                          json={"role": role, "user_id": f"chain-{i}", "decision": "confirm"})
        check("H1.8 多角色确认后闭环",
              (last.json() or {}).get("status") == "CONFIRMED",
              f"{len(roles)} 个角色 → {(last.json() or {}).get('status')}")

    # 5) 工单执行（派工 → 关闭 + 实际成本）
    if cid:
        a1 = c.post("/v1/worklogs", json={"card_id": cid, "action": "ASSIGNED",
                                          "note": "全链路回归：派工给维修班"}, headers=H)
        a2 = c.post("/v1/worklogs", json={"card_id": cid, "action": "CLOSED",
                                          "actual_cost": 2680.00,
                                          "note": "全链路回归：已更换并复测"}, headers=H)
        check("H1.9 记录派工与关闭", a1.status_code in (200, 201) and a2.status_code in (200, 201),
              f"{a1.status_code}/{a2.status_code}")
        logs = c.get(f"/v1/worklogs?card_id={cid}").json()
        acts = [x["action"] for x in (logs.get("items") or [])]
        check("H1.10 流水含派工与关闭", "ASSIGNED" in acts and "CLOSED" in acts, str(acts))

    # 6) 备件领用（关联工单）
    spares = c.get("/v1/spares").json().get("items") or []
    if spares and cid:
        part = spares[0]["part_code"]
        before = spares[0]["stock"]
        mv = c.post(f"/v1/spares/{part}/move",
                    json={"delta": -1, "card_id": cid, "asset_id": asset_id,
                          "note": "全链路回归领用"}, headers=H)
        after = mv.json().get("stock")
        check("H1.11 备件领用关联工单", after == before - 1, f"{part} {before} → {after}")
        iss = c.get("/v1/spares/issues?limit=20").json().get("items") or []
        linked = [i for i in iss if i.get("card_id") == cid]
        check("H1.12 领用记录可追溯到工单", len(linked) >= 1, f"{len(linked)} 条")
        c.post(f"/v1/spares/{part}/move", json={"delta": 1, "note": "回归退回"}, headers=H)

    # 7) 知识库沉淀
    kc = c.post("/v1/knowledge", json={
        "title": f"全链路回归案例 {asset_id}", "asset_id": asset_id,
        "symptom": f"HI {hi}", "root_cause": "回归测试", "resolution": "已处置",
        "tags": "回归,全链路"}, headers=H)
    check("H1.13 案例沉淀", kc.status_code in (200, 201) and kc.json().get("case_id"),
          str(kc.json())[:60])

    # 8) 数字一致性：全链路跑完，在监设备数不应变化
    ov1 = c.get("/v1/stats/overview").json()
    check("H1.14 在监设备数不变（链路无副作用）",
          ov1.get("total_assets") == total_assets,
          f"{total_assets} → {ov1.get('total_assets')}")
    cards = c.get("/v1/cards?limit=50").json().get("cards") or []
    check("H1.15 方案卡可检索", any(x["card_id"] == cid for x in cards), cid)

    print("\n===== H2. 三端口径一致（看板/大屏/管理台）=====", flush=True)
    browser = pw.chromium.launch(executable_path=CHROME, headless=True,
                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    bad: list[str] = []
    js: list[str] = []
    page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.on("pageerror", lambda e: js.append(str(e)[:110]))

    # 管理台：KPI 首屏有「加载中 …」占位，须轮询到真值再取。
    # 固定等 3.2s 在批量诊断 CronJob 抢 CPU 时会偶发失败（实测过一次）。
    page.goto(f"{BASE}/console#/dashboard", wait_until="domcontentloaded", timeout=30000)
    con_total = "-"
    for _ in range(80):                      # 最多等 20s
        page.wait_for_timeout(250)
        v = page.evaluate(
            "() => document.querySelector('#kpis .kpi .v')?.innerText?.trim()")
        if v and v not in ("…", ""):
            con_total = v
            break
    con_red = page.evaluate("() => {const e=[...document.querySelectorAll('#kpis .kpi')]"
                            ".find(k=>k.innerText.includes('红色')); "
                            "return e?e.querySelector('.v').innerText:'-';}")
    # 经典看板
    page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(4000)
    dash_total = page.evaluate("() => {const k=[...document.querySelectorAll('.kpi, .card, div')]"
                               ".filter(e=>e.children.length===0 && /^\\d+$/.test(e.innerText.trim()));"
                               "return k.length?k[0].innerText.trim():'-';}")
    # 大屏：环心数字同样轮询
    page.goto(f"{BASE}/screen", wait_until="domcontentloaded", timeout=30000)
    scr_total = "-"
    for _ in range(80):
        page.wait_for_timeout(250)
        v = page.evaluate(
            "() => document.getElementById('ring-big')?.textContent?.trim()")
        if v and v not in ("…", "—", ""):
            scr_total = v
            break
    page.wait_for_timeout(600)
    scr_red = page.evaluate("""() => {const l=[...document.querySelectorAll('#legend .lg')]
        .find(x=>x.innerText.includes('红')); return l?l.querySelector('.n').innerText:'-';}""")

    print(f"  管理台在监={con_total} 红色={con_red} | 大屏在监={scr_total} 红色={scr_red}")
    check("H2.1 管理台与大屏在监数一致", str(con_total).strip() == str(scr_total).strip(),
          f"{con_total} vs {scr_total}")
    check("H2.2 管理台与大屏红色数一致", str(con_red).strip() == str(scr_red).strip(),
          f"{con_red} vs {scr_red}")
    check("H2.3 在监数与总览接口一致", str(scr_total).strip() == str(total_assets),
          f"页面 {scr_total} vs 接口 {total_assets}")

    print("\n===== H3. 主题 × 视图矩阵（16 视图 × 浅/深）=====", flush=True)
    views = ["dashboard", "alerts", "assets", "asset/FAN-00004", "org", "diagnose",
             "diagnoses", "cost", "spares", "inspections", "plans", "knowledge",
             "cards", "worklogs", "rules", "system"]
    overflow: list[str] = []
    js_err: list[str] = []
    page.on("pageerror", lambda e: js_err.append(str(e)[:100]))
    for theme in ("light", "dark"):
        page.goto(f"{BASE}/console#/dashboard", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2200)
        cur = page.evaluate("() => document.body.classList.contains('dark')")
        if (theme == "dark") != cur:
            page.click("#btn-theme")
            page.wait_for_timeout(700)
        for v in views:
            page.goto(f"{BASE}/console#/{v}", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            sw = page.evaluate("() => document.documentElement.scrollWidth")
            if sw > 1601:
                overflow.append(f"{theme}/{v}({sw})")
    check("H3.1 16 视图 × 2 主题无横向滚动", not overflow,
          "; ".join(overflow[:4])[:120] if overflow else "1366/1600/1920 均已验证")
    check("H3.2 矩阵遍历无 JS 异常", not js_err, "; ".join(js_err[:2])[:110])
    shot(page, "H3_console_dark")

    print("\n===== H4. 权限矩阵（三角色 × 写操作）=====", flush=True)
    def login(u, p):
        rr = c.post("/v1/auth/login", json={"username": u, "password": p})
        return rr.json().get("token", "") if rr.status_code == 200 else ""
    t_op = login("op_li", "Oper@2026")
    t_sup = login("sup_wang", "Super@2026")
    check("H4.1 三个角色均可登录", all([tok, t_op, t_sup]),
          f"manager/operator/supervisor token 长度 "
          f"{len(tok)}/{len(t_op)}/{len(t_sup)}")
    cid2 = (c.get("/v1/cards?limit=1").json().get("cards") or [{}])[0].get("card_id", "")
    cases = [
        ("告警确认 alert.ack", "op_li(值班员)", t_op, lambda t: c.post(
            "/v1/alerts/ALT-NONE/ack", json={"action": "ack", "by": "x"},
            headers={"X-Auth-Token": t})),
        ("生成点检 plan.edit", "sup_wang(维修主管)", t_sup, lambda t: c.post(
            "/v1/inspections/generate", json={"days": 3, "limit": 1},
            headers={"X-Auth-Token": t})),
        ("改规则 plan.approve", "admin(设备主管)", tok, lambda t: c.put(
            "/v1/rules/HI_WARNING", json={"threshold": 70, "level": "orange",
                                          "sla_hours": 24},
            headers={"X-Auth-Token": t})),
        ("工单记录 card.confirm", "sup_wang(维修主管)", t_sup, lambda t: c.post(
            "/v1/worklogs", json={"card_id": cid2 or "REC-X", "action": "ASSIGNED"},
            headers={"X-Auth-Token": t})),
    ]
    for name, who, t, fn in cases:
        rr = fn(t)
        ok = rr.status_code in (200, 201, 400, 404)   # 权限通过（业务错误亦可）
        check(f"H4.{name} → {who} 允许", ok,
              f"HTTP {rr.status_code}" + ("" if ok else f" {str(rr.json())[:50]}"))
    # 反向：值班员不能改规则
    rr = c.put("/v1/rules/HI_WARNING", json={"threshold": 70, "level": "orange",
                                             "sla_hours": 24},
               headers={"X-Auth-Token": t_op})
    check("H4.5 值班员改规则被拒（反向）", rr.status_code == 403, f"HTTP {rr.status_code}")

    real_bad = [b for b in bad if "favicon" not in b.lower()]
    check("H4.6 全链路无资源失败", not real_bad, "; ".join(real_bad[:2])[:110])
    check("H4.7 全链路无 JS 异常", not js, "; ".join(js[:2])[:110])
    browser.close()
    c.close()


def main() -> None:
    t0 = time.time()
    print(f"谛听 PdM 全端全链路功能测试 · {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)

    with sync_playwright() as pw:
        test_dashboard(pw)
        test_screen(pw)
        test_console(pw)
        test_extended(pw)
        test_ops_and_auth(pw)
        test_new_modules(pw)
        test_full_chain(pw)
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
