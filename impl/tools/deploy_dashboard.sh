#!/usr/bin/env bash
# 看板部署与一致性校验（多页面）
#
# 背景：看板走 ConfigMap 挂载（/app/static/*.html），不经镜像构建。
# 因此「改了仓库文件」与「线上生效」之间没有任何自动化保障，
# 曾出现代码已含趋势图/诊断分解、线上仍是旧版的问题。
#
# 设计教训：早期版本只比对 dashboard.html 一个页面，导致改 console.html
# 后 deploy 仍输出"无需重新部署"——把唯一被校验的页面当成全部页面。
# 现在逐页比对「本地文件 SHA」与「线上 HTTP 响应体 SHA」，缺一即失败。
#
# 用法：
#   bash tools/deploy_dashboard.sh deploy    # 部署（仓库文件 → ConfigMap）
#   bash tools/deploy_dashboard.sh verify    # 只校验（不改动）
#
# 退出码：0 全部一致/部署成功；1 有不一致或部署失败

set -uo pipefail

SERVER="${PDM_SERVER:-root@10.167.2.175}"
NS="${PDM_NS:-pdm-prod}"
CM="${PDM_DASHBOARD_CM:-pdm-dashboard}"
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=no -o LogLevel=ERROR -o ConnectTimeout=15"

BASE_URL="http://10.167.2.175:32231/pdm-api"
INGRESS_HOST="pdm-api.10.167.2.175.nip.io"
BASIC="pdmadmin:3r5P-ygrg-WAcK-W0yB-Vz1q"

HERE="$(cd "$(dirname "$0")" && pwd)"
K8S_DIR="$HERE/../deploy/k8s"

# 页面：本地文件:ConfigMap key:URL 路径
PAGES=(
  "dashboard-index.html:dashboard.html:dashboard"
  "screen.html:screen.html:screen"
  "console.html:console.html:console"
)

ACTION="${1:-verify}"

if [ "$ACTION" != "deploy" ] && [ "$ACTION" != "verify" ]; then
  echo "未知动作: $ACTION（应为 deploy 或 verify）" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 逐页取「线上 HTTP 响应体」的 sha256（用户真正看到的内容）
# ---------------------------------------------------------------------------
served_sha() {
  local page="$1"
  timeout 60 ssh $SSH_OPTS "$SERVER" \
    "curl -s --max-time 12 -u '$BASIC' -H 'Host: $INGRESS_HOST' '$BASE_URL/$page' | sha256sum | awk '{print \$1}'" \
    2>/dev/null
}

# 逐页取 Pod 内挂载文件的 sha256（排查"HTTP 与挂载不同步"）
pod_sha() {
  local key="$1" pod="$2"
  timeout 40 ssh $SSH_OPTS "$SERVER" \
    "kubectl -n $NS exec $pod -- sha256sum /app/static/$key 2>/dev/null | awk '{print \$1}'" \
    2>/dev/null
}

# ---------------------------------------------------------------------------
# 预检：本地文件 → 本地 SHA 与线上 SHA 对照
# ---------------------------------------------------------------------------
echo "=== 一致性预检 ==="
MISMATCH=0
declare -a LOCAL_SHAS=() RAW_SHAS=() KEYS=() PAGES_PATH=() SRCS=()
for pair in "${PAGES[@]}"; do
  src="${pair%%:*}"; rest="${pair#*:}"; key="${rest%%:*}"; page="${rest##*:}"
  if [ ! -f "$K8S_DIR/$src" ]; then
    echo "  ✗ $src 本地缺失"; MISMATCH=$((MISMATCH+1)); continue
  fi
  # 本地文件在 Windows 下是 CRLF，而服务端 open() 文本模式会把 CRLF 归一为 LF，
  # 因此「HTTP 响应体」与「磁盘文件」哈希必然不同。比对必须归一化：
  #   served  vs 本地去 CR 后的 SHA
  #   pod 文件 vs 本地原始 SHA（ConfigMap 原样保存字节）
  raw_sha=$(sha256sum "$K8S_DIR/$src" | awk '{print $1}')
  ls_sha=$(tr -d '\r' < "$K8S_DIR/$src" | sha256sum | awk '{print $1}')
  on_sha=$(served_sha "$page")
  if [ -z "$on_sha" ]; then on_sha="(unreachable)"; fi
  if [ "$ls_sha" = "$on_sha" ]; then
    echo "  ✓ $page  ${ls_sha:0:12}…  一致"
  else
    echo "  ✗ $page  本地 ${ls_sha:0:12}…  线上 ${on_sha:0:12}…  不一致"
    MISMATCH=$((MISMATCH+1))
  fi
  LOCAL_SHAS+=("$ls_sha"); RAW_SHAS+=("$raw_sha"); KEYS+=("$key")
  PAGES_PATH+=("$page"); SRCS+=("$src")
done

if [ "$MISMATCH" = "0" ]; then
  echo "✅ 全部页面一致（${#KEYS[@]} 个）"
  [ "$ACTION" = "deploy" ] && echo "（无需重新部署）"
  exit 0
fi

if [ "$ACTION" = "verify" ]; then
  echo "❌ $MISMATCH 个页面不一致，执行 deploy 以同步"
  exit 1
fi

# ---------------------------------------------------------------------------
# 部署：上传 → 更新 ConfigMap → 等挂载 → 复核
# ---------------------------------------------------------------------------
echo "→ 上传并更新 ConfigMap（${#SRCS[@]} 个页面）…"
FROM_ARGS=""
for i in "${!SRCS[@]}"; do
  src="${SRCS[$i]}"; key="${KEYS[$i]}"
  timeout 60 scp $SSH_OPTS "$K8S_DIR/$src" "$SERVER:/root/$src" >/dev/null 2>&1 || {
    echo "  ✗ 上传失败: $src" >&2; exit 1; }
  FROM_ARGS="$FROM_ARGS --from-file=$key=/root/$src"
  echo "  ✓ $src → $key"
done

timeout 120 ssh $SSH_OPTS "$SERVER" \
  "kubectl -n $NS create configmap $CM $FROM_ARGS --dry-run=client -o yaml | kubectl apply -f -" \
  2>&1 | tail -1

# kubelet 同步 ConfigMap 到 Pod 内文件最长约 60–90s
echo "→ 等待挂载同步（最多 120s）…"
POD=""
for i in $(seq 1 12); do
  sleep 10
  POD=$(timeout 30 ssh $SSH_OPTS "$SERVER" \
    "kubectl -n $NS get pods --no-headers 2>/dev/null | grep python-diagnosis | grep '1/1' | awk '{print \$1}' | head -1" \
    2>/dev/null)
  [ -z "$POD" ] && continue
  SYNCED=1
  for j in "${!KEYS[@]}"; do
    got=$(pod_sha "${KEYS[$j]}" "$POD")
    [ "$got" != "${RAW_SHAS[$j]}" ] && SYNCED=0
  done
  if [ "$SYNCED" = "1" ]; then
    echo "  ✅ Pod 内 ${#KEYS[@]} 个页面均已同步（$POD，${i}0s）"
    break
  fi
done

# 最终复核：HTTP 响应体
echo "→ 最终复核（HTTP 响应体）…"
FAIL=0
for i in "${!PAGES_PATH[@]}"; do
  got=$(served_sha "${PAGES_PATH[$i]}")
  if [ "$got" = "${LOCAL_SHAS[$i]}" ]; then
    echo "  ✓ /${PAGES_PATH[$i]}  一致"
  else
    echo "  ✗ /${PAGES_PATH[$i]}  仍不一致（${got:0:12}… vs ${LOCAL_SHAS[$i]:0:12}…）"
    FAIL=$((FAIL+1))
  fi
done

if [ "$FAIL" = "0" ]; then
  echo "✅ 部署完成：${#PAGES_PATH[@]} 个页面全部一致"
  exit 0
fi
echo "⚠️  $FAIL 个页面未同步，稍后重跑 verify"
exit 1
