"""边缘上行同步：把边缘 SQLite 的健康摘要推送到集团中心。

设计（docs/44 §3 三条铁律之 2：上行是"受控摘要"，不是"原始流"）：
- 只上传聚合指标：设备数 / 覆盖率 / 四级分布 / 平均 HI / 未关闭告警数
- 不上传单台设备数据、不上传原始波形（数据主权 L4）
- 幂等：同一站点重复上报只是覆盖 read_edge_summary 一行

执行时机：边缘 CronJob 每 10 分钟（与集团判据拉取同一个 Job）。
断网时静默失败，本地自治不受影响；恢复后下一轮自动补报。

用法（边缘 Pod 内）：
    python edge_sync.py \
        --center http://pdm-center:8000 \
        --tenant site-b \
        --name 天津零部件基地
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# 让脚本在容器内与仓库根两种场景都能 import app.*
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services", "python-diagnosis"))


def build_summary(tenant: str) -> dict:
    """从边缘本地库聚合摘要（复用 repository，SQLite 后端）。"""
    from app import repository as repo  # noqa: PLC0415

    ov = repo.overview_stats(tenant)
    summary = repo.alert_summary(tenant)
    avg_hi = None
    with repo.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT AVG(hi) AS a FROM read_asset_health WHERE tenant_id=%s", (tenant,))
        row = cur.fetchone()
        if row and row.get("a") is not None:
            avg_hi = round(float(row["a"]), 1)

    return {
        "total_assets": ov.get("total_assets", 0),
        "assessed": ov.get("assessed", 0),
        "coverage_pct": ov.get("coverage_pct", 0.0),
        "by_level": ov.get("by_level", {}),
        "avg_hi": avg_hi,
        "open_alerts": int(summary.get("open") or 0),
        "edge_version": os.getenv("EDGE_VERSION", "unknown"),
    }


def post_json(url: str, payload: dict, tenant: str, timeout: int = 15) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Tenant-Id": tenant},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # noqa: BLE001 - 网络错误按可重试处理
        return 0, str(e)[:200]


def main() -> int:
    ap = argparse.ArgumentParser(description="边缘健康摘要上行")
    ap.add_argument("--center", default=os.getenv("PDM_CENTER_URL", "http://python-diagnosis:8000"),
                    help="集团中心 API 地址")
    ap.add_argument("--tenant", required=True, help="边缘站点租户 ID，如 site-b")
    ap.add_argument("--name", default="", help="站点名称（注册用）")
    ap.add_argument("--region", default=os.getenv("EDGE_REGION", "CN"))
    args = ap.parse_args()

    summary = build_summary(args.tenant)
    print(f"[edge-sync] 摘要: {json.dumps(summary, ensure_ascii=False)}", flush=True)

    # 1) 注册/心跳（幂等）
    reg = {"name": args.name or args.tenant, "region": args.region,
           "device_count": summary["total_assets"],
           "edge_version": summary["edge_version"]}
    code, body = post_json(f"{args.center}/v1/sites/register", reg, args.tenant)
    print(f"[edge-sync] 注册: HTTP {code} {body}", flush=True)
    if code == 0:
        # 网络不通：本地自治继续，返回 0 让 CronJob 不告警（下轮补报）
        print("[edge-sync] 集团不可达，边缘自治继续（摘要未上报）", flush=True)
        return 0

    # 2) 上行摘要
    code, body = post_json(f"{args.center}/v1/sites/{args.tenant}/summary",
                           summary, args.tenant)
    print(f"[edge-sync] 摘要上行: HTTP {code} {body}", flush=True)
    return 0 if 200 <= code < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
