#!/usr/bin/env bash
# 并行填充 qianrenqianmian 课程平台测试数据
# 用法: ./seed_all.sh <scale> <shards>
set -u
cd "$(dirname "$0")/.."

SCALE="${1:-20}"
SHARDS="${2:-4}"
PY="./.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="./.venv/bin/python"

echo "=== 清空 14 张表 ==="
"$PY" - <<'PYEOF'
import pymysql
c = pymysql.connect(host="10.167.2.175", port=30210, user="root", password="", autocommit=True)
with c.cursor() as cur:
    cur.execute("SET @@tidb_mem_quota_query = 8589934592")
    for t in ("teaching_db.learning_path_steps", "teaching_db.assignments", "teaching_db.exams",
              "teaching_db.discussions", "teaching_db.live_sessions"):
        cur.execute(f"TRUNCATE TABLE {t}")
    cur.execute("DELETE FROM teaching_db.learning_paths")
    for t in ("course_db.resources", "course_db.knowledge_points", "course_db.chapters",
              "course_db.courses", "tenant_db.credit_transfer_records",
              "tenant_db.credit_transfer_rules", "iam_db.users", "iam_db.roles",
              "tenant_db.tenants"):
        cur.execute(f"TRUNCATE TABLE {t}")
print("已清空")
c.close()
PYEOF

echo "=== 启动 $SHARDS 个分片（scale=$SCALE）==="
PIDS=()
for i in $(seq 0 $((SHARDS - 1))); do
  "$PY" tools/seed_course_platform.py --scale "$SCALE" --shard "$i/$SHARDS" \
      > "/tmp/seed_s$i.log" 2>&1 &
  PIDS+=($!)
  echo "  shard $i pid=${PIDS[$i]}"
done

FAIL=0
for idx in "${!PIDS[@]}"; do
  if wait "${PIDS[$idx]}"; then
    echo "  shard $idx 完成"
  else
    echo "  shard $idx 失败，见 /tmp/seed_s$idx.log"
    FAIL=1
  fi
done

echo "=== 汇总 ==="
"$PY" - <<'PYEOF'
import pymysql
c = pymysql.connect(host="10.167.2.175", port=30210, user="root", password="",
                    cursorclass=pymysql.cursors.DictCursor)
tables = ["tenant_db.tenants", "iam_db.roles", "iam_db.users", "course_db.courses",
          "course_db.chapters", "course_db.knowledge_points", "course_db.resources",
          "teaching_db.learning_paths", "teaching_db.learning_path_steps",
          "teaching_db.assignments", "teaching_db.exams", "teaching_db.discussions",
          "teaching_db.live_sessions", "tenant_db.credit_transfer_rules",
          "tenant_db.credit_transfer_records"]
total = 0
with c.cursor() as cur:
    for t in tables:
        cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
        n = cur.fetchone()["n"]
        total += n
        print(f"  {t:<40} {n:>14,}")
print(f"  {'合计':<40} {total:>14,}")
c.close()
PYEOF

exit $FAIL
