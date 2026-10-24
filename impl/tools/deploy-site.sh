#!/usr/bin/env bash
# =============================================================================
# 边缘站点一键部署（docs/44 §8 接入 SOP）
#
# 做什么：
#   1. 校验边缘节点可达 + K3s 就绪
#   2. 生成边缘 Secret（看板口令 + 业务鉴权密钥，随机值，不落盘明文）
#   3. 上传看板 ConfigMap（与中心同源，租户参数由 URL query 决定）
#   4. 渲染 edge-site.yaml（sed 替换 __SITE_ID__ 等占位符）并应用
#   5. 触发首次注册（curl 集团 /v1/sites/register）
#   6. 健康验证（/healthz + overview）
#
# 用法：
#   bash tools/deploy-site.sh --site site-b --name 天津零部件基地 \
#        --node 192.168.20.10 --center http://pdm-api.10.167.2.175.nip.io:32231/pdm-api
#
# 回滚：
#   bash tools/deploy-site.sh --site site-b --rollback
# =============================================================================
set -uo pipefail

SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=no -o LogLevel=ERROR -o ConnectTimeout=15"
HERE="$(cd "$(dirname "$0")" && pwd)"
EDGE_YAML="$HERE/../deploy/k8s/edge/edge-site.yaml"
DASH_DIR="$HERE/../deploy/k8s"
IMAGE_DEFAULT="10.167.2.175:30500/pdm/python-diagnosis:0.9.15"

SITE="" NAME="" NODE="" CENTER="http://pdm-api.10.167.2.175.nip.io:32231/pdm-api" IMAGE="$IMAGE_DEFAULT" ROLLBACK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site) SITE="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --node) NODE="$2"; shift 2;;
    --center) CENTER="$2"; shift 2;;
    --image) IMAGE="$2"; shift 2;;
    --rollback) ROLLBACK=1; shift;;
    *) echo "未知参数: $1" >&2; exit 1;;
  esac
done

[ -z "$SITE" ] && { echo "必须指定 --site（如 site-b）" >&2; exit 1; }
IMAGE_TAG="${IMAGE##*:}"

if [ "$ROLLBACK" = "1" ]; then
  echo "→ 回滚站点 $SITE（删除 pdm-edge 命名空间）"
  timeout 60 ssh $SSH_OPTS "root@$NODE" "k3s kubectl delete ns pdm-edge --wait=false" 2>&1 | tail -1
  echo "✅ 回滚指令已下发（PVC 数据保留，如需清数据手动删 PVC）"
  exit 0
fi

[ -z "$NODE" ] && { echo "部署必须指定 --node（边缘节点 IP）" >&2; exit 1; }
[ -f "$EDGE_YAML" ] || { echo "边缘清单缺失: $EDGE_YAML" >&2; exit 1; }

# ---------- 1. 前置校验 ----------
echo "=== 1/6 校验边缘节点 $NODE ==="
timeout 30 ssh $SSH_OPTS "root@$NODE" "command -v k3s >/dev/null && k3s kubectl get nodes" \
  || { echo "✗ 节点不可达或 K3s 未安装（先 curl -sfL https://get.k3s.io | sh -）" >&2; exit 1; }
echo "  ✓ K3s 就绪"

# ---------- 2. Secret ----------
echo "=== 2/6 生成边缘 Secret（随机口令，不落盘）==="
timeout 60 ssh $SSH_OPTS "root@$NODE" "
k3s kubectl -n pdm-edge create secret generic pdm-edge-auth \
  --from-literal=username=\"edge_${SITE}\" \
  --from-literal=password=\"\$(head -c 18 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 20)\" \
  --from-literal=auth-secret=\"\$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 48)\" \
  --dry-run=client -o yaml | k3s kubectl apply -f -" >/dev/null 2>&1 \
  && echo "  ✓ pdm-edge-auth 已生成" \
  || { echo "✗ Secret 创建失败" >&2; exit 1; }

# ---------- 3. 看板 ConfigMap ----------
echo "=== 3/6 上传看板 ConfigMap ==="
for f in console.html screen.html dashboard-index.html; do
  timeout 60 scp $SSH_OPTS "$DASH_DIR/$f" "root@$NODE:/tmp/edge-$f" >/dev/null 2>&1 \
    || { echo "✗ 上传失败: $f" >&2; exit 1; }
done
timeout 60 ssh $SSH_OPTS "root@$NODE" "
k3s kubectl -n pdm-edge create configmap pdm-edge-dashboard \
  --from-file=console.html=/tmp/edge-console.html \
  --from-file=screen.html=/tmp/edge-screen.html \
  --from-file=dashboard.html=/tmp/edge-dashboard-index.html \
  --dry-run=client -o yaml | k3s kubectl apply -f -" >/dev/null 2>&1 \
  && echo "  ✓ 3 个页面已上传" || { echo "✗ ConfigMap 失败" >&2; exit 1; }

# ---------- 4. 渲染并应用工作负载 ----------
echo "=== 4/6 部署工作负载（站点 $SITE）==="
TMP_YAML=$(mktemp)
sed -e "s/__SITE_ID__/$SITE/g" \
    -e "s/__SITE_NAME__/$NAME/g" \
    -e "s|__IMAGE__|$IMAGE|g" \
    -e "s/__IMAGE_TAG__/$IMAGE_TAG/g" \
    -e "s|__CENTER_URL__|$CENTER|g" \
    "$EDGE_YAML" > "$TMP_YAML"
timeout 60 scp $SSH_OPTS "$TMP_YAML" "root@$NODE:/tmp/edge-site-rendered.yaml" >/dev/null 2>&1
rm -f "$TMP_YAML"
timeout 120 ssh $SSH_OPTS "root@$NODE" "k3s kubectl apply -f /tmp/edge-site-rendered.yaml" \
  | tail -3 || { echo "✗ 工作负载应用失败" >&2; exit 1; }

# ---------- 5. 等待就绪 ----------
echo "=== 5/6 等待 Pod 就绪（最多 120s）==="
READY=0
for i in $(seq 1 12); do
  sleep 10
  GOT=$(timeout 30 ssh $SSH_OPTS "root@$NODE" \
    "k3s kubectl -n pdm-edge get pods --no-headers 2>/dev/null | grep python-diagnosis | grep -c '1/1'" 2>/dev/null)
  if [ "${GOT:-0}" -ge 1 ]; then READY=1; echo "  ✓ Pod 就绪（${i}0s）"; break; fi
done
[ "$READY" = "0" ] && { echo "⚠️ Pod 未在 120s 内就绪，请查看: k3s kubectl -n pdm-edge describe pod -l app=python-diagnosis"; exit 1; }

# ---------- 6. 注册 + 健康验证 ----------
echo "=== 6/6 站点注册与健康验证 ==="
timeout 30 ssh $SSH_OPTS "root@$NODE" "
curl -fsS --max-time 10 -X POST $CENTER/v1/sites/register \
  -H 'Content-Type: application/json' -H 'X-Tenant-Id: $SITE' \
  -d '{\"name\":\"$NAME\",\"device_count\":0,\"edge_version\":\"$IMAGE_TAG\"}'" \
  && echo "  ✓ 已注册到集团" || echo "  ⚠️ 集团注册失败（断网时正常，恢复后 CronJob 自动补报）"

timeout 30 ssh $SSH_OPTS "root@$NODE" \
  "curl -fsS --max-time 10 http://localhost:32233/healthz" \
  && echo "  ✓ 边缘 healthz OK" || echo "  ⚠️ healthz 未通（NodePort 32233）"

echo ""
echo "✅ 站点 $SITE 部署完成"
echo "   看板:   http://$NODE:32233/console?tenant=$SITE  （Basic Auth: edge_${SITE}/<Secret 中随机口令>）"
echo "   摘要上行: 每 10 分钟自动上报（CronJob edge-sync）"
echo "   下一步: 导入设备台账 → python tools/seed_edge.py --site $SITE（待开发）"
