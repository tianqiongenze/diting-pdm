#!/usr/bin/env bash
# =============================================================================
# 数据库迁移（docs/43 Phase H1）
#
# 背景：006/007/008/009/010 都是手工 SQL，没有版本化迁移机制——
# 曾因"ALTER 静默失败"排查走了大弯路（docs/43 §十）。本脚本把迁移固化：
#   - 按文件名顺序执行 db/tidb/*.sql
#   - 记录已执行的迁移到 schema_migration 表（幂等）
#   - 支持 --dry-run 只检查不执行
#
# 用法：
#   PDM_DB_HOST=... PDM_DB_USER=... PDM_DB_PASSWORD=... bash tools/migrate.sh
#   bash tools/migrate.sh --dry-run
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SQL_DIR="$HERE/../db/tidb"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

PY="${PYTHON_BIN:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

exec "$PY" - "$SQL_DIR" "$DRY_RUN" <<'PYEOF'
import glob
import hashlib
import os
import sys

sql_dir, dry_run = sys.argv[1], sys.argv[2] == "1"

try:
    import pymysql
except ImportError:
    print("✗ 需要 pymysql：pip install pymysql", file=sys.stderr)
    raise SystemExit(1)

cfg = dict(
    host=os.getenv("PDM_TIDB_HOST") or os.getenv("PDM_DB_HOST", "tidb.infra.svc.cluster.local"),
    port=int(os.getenv("PDM_TIDB_PORT") or os.getenv("PDM_DB_PORT", "4000")),
    user=os.getenv("PDM_TIDB_USER") or os.getenv("PDM_DB_USER", "pdm_diag"),
    password=os.getenv("PDM_TIDB_PASSWORD") or os.getenv("PDM_DB_PASSWORD", ""),
    database="pdm", charset="utf8mb4", autocommit=True, connect_timeout=10,
)

files = sorted(glob.glob(os.path.join(sql_dir, "*.sql")))
if not files:
    print(f"✗ 未找到迁移文件：{sql_dir}", file=sys.stderr)
    raise SystemExit(1)

try:
    conn = pymysql.connect(**cfg)
except Exception as exc:
    print(f"✗ 数据库连接失败：{exc}", file=sys.stderr)
    raise SystemExit(1)

cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS schema_migration (
  filename   VARCHAR(128) NOT NULL,
  checksum   CHAR(64)     NOT NULL,
  applied_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (filename)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
cur.execute("SELECT filename, checksum FROM schema_migration")
applied = {r[0]: r[1] for r in cur.fetchall()}

ok = skipped = failed = 0
for path in files:
    name = os.path.basename(path)
    with open(path, "rb") as fh:
        raw = fh.read()
    checksum = hashlib.sha256(raw).hexdigest()
    if applied.get(name) == checksum:
        print(f"  跳过 {name}（已执行，校验一致）")
        skipped += 1
        continue
    if name in applied and applied[name] != checksum:
        print(f"  ⚠ {name} 内容已变更（校验不一致），仍将执行")
    # 去注释行，按分号切分
    text = raw.decode("utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
    stmts = [s.strip() for s in body.split(";") if s.strip()]
    if dry_run:
        print(f"  [dry-run] {name}: {len(stmts)} 条语句")
        continue
    try:
        for st in stmts:
            # 跳过纯 DDL 之外的 USE/CREATE DATABASE（由连接指定库）
            low = st.lower()
            if low.startswith("use ") or low.startswith("create database"):
                continue
            cur.execute(st)
        cur.execute(
            "INSERT INTO schema_migration(filename,checksum) VALUES (%s,%s) "
            "ON DUPLICATE KEY UPDATE checksum=VALUES(checksum), applied_at=CURRENT_TIMESTAMP(3)",
            (name, checksum))
        print(f"  ✓ {name}（{len(stmts)} 条语句）")
        ok += 1
    except Exception as exc:
        print(f"  ✗ {name} 失败：{str(exc)[:200]}", file=sys.stderr)
        failed += 1

conn.close()
print(f"\n结果：成功 {ok} / 跳过 {skipped} / 失败 {failed}")
raise SystemExit(1 if failed else 0)
PYEOF
