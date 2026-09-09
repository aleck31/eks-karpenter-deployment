#!/usr/bin/env bash
#
# Firecrawl 部署 (eks-karpenter-env / ap-southeast-1)
#
# 用法:
#   ./deploy.sh                  # 核心栈
#   ./deploy.sh --with-ingress   # 加 internal ALB Ingress
#   ./deploy.sh --with-extract   # 加 extract-worker (需先配 LLM provider)
#   ./deploy.sh --dry-run        # 只校验清单

set -euo pipefail

NS="firecrawl"
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WITH_INGRESS=false
WITH_EXTRACT=false
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --with-ingress) WITH_INGRESS=true ;;
    --with-extract) WITH_EXTRACT=true ;;
    --dry-run)      DRY_RUN=true ;;
    -h|--help)      sed -n '4,10p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[0;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || die "kubectl 未安装"

CTX="$(kubectl config current-context)"
log "context: $CTX"
case "$CTX" in
  *eks-karpenter-env*) ;;
  *) warn "context 不是 eks-karpenter-env"
     read -r -p "继续? [y/N] " a; [[ "$a" == "y" ]] || exit 1 ;;
esac

kubectl get storageclass gp3 >/dev/null 2>&1 || die "缺少 gp3 StorageClass"

if $DRY_RUN; then
  log "校验清单"
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply --dry-run=server -f - >/dev/null
  # 服务端 dry-run 要求 namespace 已存在，否则退到客户端严格校验
  if kubectl get namespace "$NS" >/dev/null 2>&1; then
    MODE_ARGS=(--dry-run=server)
    log "服务端 dry-run（含 admission）"
  else
    MODE_ARGS=(--dry-run=client --validate=strict)
    warn "namespace 不存在，仅客户端严格校验"
  fi
  rc=0
  for f in firecrawl-configmap.yaml firecrawl-secret.yaml firecrawl-ebs-pvc.yaml \
           firecrawl-infra.yaml firecrawl-deployment.yaml \
           firecrawl-extract-worker.yaml firecrawl-ingress.yaml; do
    [[ -f "$f" ]] || { printf '  %-36s SKIP\n' "$f"; continue; }
    printf '  %-36s' "$f"
    if out="$(kubectl apply "${MODE_ARGS[@]}" -f "$f" 2>&1)"; then
      echo "OK"
    else
      echo "FAIL"; echo "$out" | sed 's/^/      /'; rc=1
    fi
  done
  [[ $rc -eq 0 ]] && log "全部通过" || die "存在失败项"
  exit 0
fi

log "确保 namespace $NS 存在"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

# 密码写入 PVC 后不可变更，重跑时若重新生成会导致 worker 认证失败
if kubectl -n "$NS" get secret firecrawl-db >/dev/null 2>&1; then
  log "firecrawl-db 已存在，跳过生成"
else
  log "生成 firecrawl-db（PG 密码 + 连接串）"
  # 限字母数字，避免进 URL 后需要 percent-encoding
  PG_PASS="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)"
  PG_URL="postgresql://postgres:${PG_PASS}@nuq-postgres:5432/postgres"
  kubectl -n "$NS" create secret generic firecrawl-db \
    --from-literal="POSTGRES_PASSWORD=${PG_PASS}" \
    --from-literal="NUQ_DATABASE_URL=${PG_URL}" \
    --from-literal="NUQ_DATABASE_URL_LISTEN=${PG_URL}"
  unset PG_PASS PG_URL
fi

log "应用 ConfigMap / Secret / PVC"
kubectl apply -f firecrawl-configmap.yaml
# firecrawl-secret.yaml 命中仓库 .gitignore 的 *secret.yaml，新克隆时不存在
if [[ -f firecrawl-secret.yaml ]]; then
  kubectl apply -f firecrawl-secret.yaml
elif kubectl -n "$NS" get secret firecrawl-secret >/dev/null 2>&1; then
  log "本地无 firecrawl-secret.yaml，集群内已有该 Secret，保持不动"
else
  warn "firecrawl-secret.yaml 不存在，创建空 Secret（可选能力全关）"
  kubectl -n "$NS" create secret generic firecrawl-secret \
    --from-literal=OPENAI_API_KEY= \
    --from-literal=PROXY_PASSWORD= \
    --from-literal=REDIS_PASSWORD= \
    --from-literal=BULL_AUTH_KEY= \
    --from-literal=SELF_HOSTED_WEBHOOK_HMAC_SECRET= \
    --from-literal=LLAMAPARSE_API_KEY= \
    --from-literal=SLACK_WEBHOOK_URL= \
    --from-literal=FIRE_ENGINE_BETA_URL=
fi
kubectl apply -f firecrawl-ebs-pvc.yaml

CM_COUNT="$(kubectl -n "$NS" get cm firecrawl-config -o jsonpath='{.data.NUQ_WORKER_COUNT}')"
YAML_REPLICAS="$(python3 - <<'PY'
import yaml
for d in yaml.safe_load_all(open('firecrawl-deployment.yaml')):
    if d and d.get('kind') == 'Deployment' and d['metadata']['name'] == 'nuq-worker':
        print(d['spec']['replicas'])
PY
)"
[[ "$CM_COUNT" == "$YAML_REPLICAS" ]] \
  || die "NUQ_WORKER_COUNT($CM_COUNT) != nuq-worker replicas($YAML_REPLICAS)，先同步两处"
log "NUQ_WORKER_COUNT 一致 ($CM_COUNT)"

# 依赖先于应用起：PG 未完成 initdb + pg_cron 时 worker 连不上，RabbitMQ 缺失时 API 启动即崩
log "部署依赖组件"
kubectl apply -f firecrawl-infra.yaml

log "等待 nuq-postgres（首次含 initdb + pg_cron）"
kubectl -n "$NS" rollout status deploy/nuq-postgres --timeout=10m \
  || die "nuq-postgres 未就绪: kubectl -n $NS logs deploy/nuq-postgres"

log "等待 redis / rabbitmq"
kubectl -n "$NS" rollout status deploy/redis --timeout=5m || die "redis 未就绪"
kubectl -n "$NS" rollout status deploy/rabbitmq --timeout=8m \
  || { warn "若日志显示 4.x 兼容问题，回退上游版本:"
       warn "  kubectl -n $NS set image deploy/rabbitmq rabbitmq=rabbitmq:3-management"
       die "rabbitmq 未就绪"; }

log "部署应用组件"
kubectl apply -f firecrawl-deployment.yaml

kubectl -n "$NS" rollout status deploy/playwright-service --timeout=10m || die "playwright 未就绪"
for d in api worker nuq-worker nuq-prefetch-worker; do
  kubectl -n "$NS" rollout status "deploy/$d" --timeout=8m \
    || die "$d 未就绪: kubectl -n $NS logs deploy/$d --tail=100"
done

if $WITH_EXTRACT; then
  KEY="$(kubectl -n "$NS" get secret firecrawl-secret -o jsonpath='{.data.OPENAI_API_KEY}' 2>/dev/null || true)"
  BASE="$(kubectl -n "$NS" get cm firecrawl-config -o jsonpath='{.data.OPENAI_BASE_URL}')"
  MODEL="$(kubectl -n "$NS" get cm firecrawl-config -o jsonpath='{.data.MODEL_NAME}')"
  [[ -z "$KEY"   ]] && warn "OPENAI_API_KEY 为空，抽取会失败"
  [[ -z "$BASE"  ]] && warn "OPENAI_BASE_URL 为空，将直连 api.openai.com"
  [[ -z "$MODEL" ]] && warn "MODEL_NAME 为空，回落 gpt-4o-mini / gpt-4.1-mini"
  log "部署 extract-worker"
  kubectl apply -f firecrawl-extract-worker.yaml
  kubectl -n "$NS" rollout status deploy/extract-worker --timeout=8m || die "extract-worker 未就绪"
fi

if $WITH_INGRESS; then
  warn "API 无鉴权，仅部署 internal ALB，勿挂 CloudFront 暴露公网"
  kubectl apply -f firecrawl-ingress.yaml
  for _ in $(seq 1 40); do
    ADDR="$(kubectl -n "$NS" get ing firecrawl -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
    [[ -n "$ADDR" ]] && break
    sleep 15
  done
  [[ -n "${ADDR:-}" ]] && log "Ingress: http://$ADDR" || warn "ALB 地址未就绪: kubectl get ing -n $NS"
fi

echo
kubectl -n "$NS" get deploy,pvc
echo
log "完成。跑 ./verify.sh 做端到端验证"
log "集群内地址: http://api.${NS}.svc.cluster.local:3002"
