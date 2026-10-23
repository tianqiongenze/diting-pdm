#!/usr/bin/env bash
# 看板部署与一致性校验
#
# 背景：看板走 ConfigMap 挂载（/app/static/dashboard.html），不经镜像构建。
# 因此「改了仓库文件」与「线上生效」之间没有任何自动化保障，
# 曾出现代码已含趋势图/诊断分解、线上仍是旧版的问题。
#
# 用法：
#   bash tools/deploy_dashboard.sh deploy    # 部署（仓库文件 → ConfigMap）
#   bash tools/deploy_dashboard.sh verify    # 只校验（不改动）
#   bash tools/deploy_dashboard.sh deploy --force   # 跳过一致性预检直接部署
#
# 退出码：0 一致/部署成功；1 不一致或部署失败

set -uo pipefail

SERVER="${PDM_SERVER:-root@10.167.2.175}"
NS="${PDM_NS:-pdm-prod}"
CM="${PDM_DASHBOARD_CM:-pdm-dashboard}"
KEY="${PDM_DASHBOARD_KEY:-dashboard.html}"
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=15"

HERE="$(cd "$(dirname "$0")" && pwd)"
K8S_DIR="$HERE/../deploy/k8s"

# 三个页面：本地文件 -> ConfigMap key
PAGES=(
  "dashboard-index.html:dashboard.html"
  "screen.html:screen.html"
  "console.html:console.html"
)

LOCAL_FILE="$K8S_DIR/dashboard-index.html"
# jsonpath 中 key 的点号需转义，否则 {.data.dashboard.html} 会解析失败返回空
ESCAPED_KEY=$(printf '%s' "$KEY" | sed 's/\./\\./g')

ACTION="${1:-verify}"
FORCE="${2:-}"

if [ ! -f "$LOCAL_FILE" ]; then
  echo "❌ 本地文件不存在: $LOCAL_FILE" >&2
  exit 1
fi

LOCAL_SHA=$(sha256sum "$LOCAL_FILE" | awk '{print $1}')
echo "本地文件: $LOCAL_FILE"
echo "本地 SHA256: ${LOCAL_SHA:0:16}…"

# ---------------------------------------------------------------------------
# 取线上 ConfigMap 内容的 sha256
# 注意：jsonpath 里 key 的点号必须转义（dashboard\.html），否则取到空值；
#      且内容含 CRLF，必须落盘后算哈希，不能靠管道拼接。
# ---------------------------------------------------------------------------
remote_sha() {
  timeout 60 ssh $SSH_OPTS "$SERVER" \
    "kubectl -n $NS get cm $CM -o jsonpath='{.data.$ESCAPED_KEY}' > /tmp/.dash_cm 2>/dev/null; sha256sum /tmp/.dash_cm 2>/dev/null | awk '{print \$1}'" \
    2>/dev/null
}

ONLINE_SHA=$(remote_sha)
if [ -z "$ONLINE_SHA" ]; then
  echo "⚠️  无法读取线上 ConfigMap（可能尚未创建）"
  ONLINE_SHA="(none)"
fi
echo "线上 SHA256: ${ONLINE_SHA:0:16}…"

if [ "$LOCAL_SHA" = "$ONLINE_SHA" ]; then
  echo "✅ 一致性检查通过：仓库文件与线上内容一致"
  [ "$ACTION" = "deploy" ] && echo "（无需重新部署）"
  exit 0
fi

echo "❌ 不一致：仓库文件与线上内容不同"

if [ "$ACTION" = "verify" ]; then
  echo "   执行 'bash tools/deploy_dashboard.sh deploy' 以同步"
  exit 1
fi

if [ "$ACTION" != "deploy" ]; then
  echo "未知动作: $ACTION（应为 deploy 或 verify）" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------
echo "→ 上传并更新 ConfigMap（3 个页面）…"
FROM_ARGS=""
for pair in "${PAGES[@]}"; do
  src="${pair%%:*}"; key="${pair##*:}"
  if [ ! -f "$K8S_DIR/$src" ]; then
    echo "⚠️  跳过缺失文件: $src"
    continue
  fi
  timeout 60 scp $SSH_OPTS "$K8S_DIR/$src" "$SERVER:/root/$src" >/dev/null 2>&1 || {
    echo "❌ 上传失败: $src" >&2; exit 1; }
  FROM_ARGS="$FROM_ARGS --from-file=$key=/root/$src"
  echo "   ✓ $src → $key"
done

timeout 120 ssh $SSH_OPTS "$SERVER" \
  "kubectl -n $NS create configmap $CM $FROM_ARGS --dry-run=client -o yaml | kubectl apply -f -" \
  2>&1 | tail -1

# ---------------------------------------------------------------------------
# 等待 kubelet 同步挂载（ConfigMap 更新到 Pod 内文件最长约 60–90s）
# ---------------------------------------------------------------------------
echo "→ 等待挂载同步（最多 120s）…"
POD=""
for i in $(seq 1 12); do
  sleep 10
  POD=$(timeout 30 ssh $SSH_OPTS "$SERVER" \
    "kubectl -n $NS get pods --no-headers 2>/dev/null | grep python-diagnosis | grep '1/1' | awk '{print \$1}' | head -1" \
    2>/dev/null)
  [ -z "$POD" ] && continue
  IN_POD=$(timeout 30 ssh $SSH_OPTS "$SERVER" \
    "kubectl -n $NS exec $POD -- sha256sum /app/static/$KEY 2>/dev/null | awk '{print \$1}'" \
    2>/dev/null)
  if [ "$IN_POD" = "$LOCAL_SHA" ]; then
    echo "✅ Pod 内文件已同步（$POD，${i}0s）"
    break
  fi
done

# ---------------------------------------------------------------------------
# 最终校验：比对 HTTP 响应体（真正用户看到的）
# ---------------------------------------------------------------------------
FINAL=$(timeout 60 ssh $SSH_OPTS "$SERVER" \
  "curl -s --max-time 10 -u pdmadmin:Diting2026! -H 'Host: pdm-api.10.167.2.175.nip.io' http://10.167.2.175:32231/pdm-api/dashboard | sha256sum | awk '{print \$1}'" \
  2>/dev/null)

if [ "$FINAL" = "$LOCAL_SHA" ]; then
  echo "✅ dashboard 已一致"
else
  echo "⚠️  dashboard 响应与仓库文件不一致（SHA ${FINAL:0:12}… vs ${LOCAL_SHA:0:12}…）"
  echo "   可能 Pod 尚未完成挂载同步，稍后重跑 verify"
fi

echo "→ 校验其余页面可访问"
OK=0; FAIL=0
for pair in "${PAGES[@]}"; do
  key="${pair##*:}"
  page="${key%.html}"
  code=$(timeout 40 ssh $SSH_OPTS "$SERVER" \
    "curl -s -o /dev/null -w '%{http_code}' --max-time 8 -u pdmadmin:Diting2026! \
     -H 'Host: pdm-api.10.167.2.175.nip.io' http://10.167.2.175:32231/pdm-api/$page" \
    2>/dev/null)
  if [ "$code" = "200" ]; then
    echo "   ✓ /$page 200"; OK=$((OK+1))
  else
    echo "   ✗ /$page $code"; FAIL=$((FAIL+1))
  fi
done

[ "$FAIL" = "0" ] && echo "✅ 全部页面就绪（$OK 个）" && exit 0
echo "⚠️  $FAIL 个页面异常"; exit 1
