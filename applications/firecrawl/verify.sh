#!/usr/bin/env bash
#
# Firecrawl 端到端验证
#
# readiness 端点只是心跳，不校验 Redis / PG / RabbitMQ / Playwright / 出站网络，
# 因此必须真实跑一次 scrape 才能判断可用。
#
# 用法:
#   ./verify.sh                    # port-forward 验证
#   ./verify.sh http://<alb-dns>   # 打已有端点
#   ./verify.sh --in-cluster       # 临时 Pod 从集群内验证

set -uo pipefail

NS="firecrawl"
PF_PORT="${PF_PORT:-13002}"
PASS=0; FAIL=0

ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[0;31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
info() { printf '\033[0;36m▸\033[0m %s\n' "$*"; }

info "组件状态"
for d in nuq-postgres redis rabbitmq playwright-service api worker nuq-worker nuq-prefetch-worker; do
  if ! kubectl -n "$NS" get deploy "$d" >/dev/null 2>&1; then
    bad "$d 不存在"; continue
  fi
  want="$(kubectl -n "$NS" get deploy "$d" -o jsonpath='{.spec.replicas}')"
  got="$(kubectl -n "$NS" get deploy "$d" -o jsonpath='{.status.readyReplicas}')"
  [[ "${got:-0}" == "$want" ]] && ok "$d ${got}/${want}" || bad "$d ${got:-0}/${want}"
done

CM_COUNT="$(kubectl -n "$NS" get cm firecrawl-config -o jsonpath='{.data.NUQ_WORKER_COUNT}' 2>/dev/null)"
REAL="$(kubectl -n "$NS" get deploy nuq-worker -o jsonpath='{.spec.replicas}' 2>/dev/null)"
[[ "$CM_COUNT" == "$REAL" ]] \
  && ok "NUQ_WORKER_COUNT 与 replicas 一致 ($CM_COUNT)" \
  || bad "NUQ_WORKER_COUNT ($CM_COUNT) != replicas ($REAL)"

# 重启计数是 Playwright OOM 的典型信号
RESTARTS="$(kubectl -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null | awk '$2>0')"
[[ -z "$RESTARTS" ]] && ok "无容器重启" || { bad "存在重启:"; echo "$RESTARTS" | sed 's/^/      /'; }

MODE="portforward"; BASE=""
if [[ "${1:-}" == "--in-cluster" ]]; then
  MODE="incluster"
elif [[ -n "${1:-}" ]]; then
  MODE="direct"; BASE="${1%/}"
fi

PF_PID=""
cleanup() { [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null; }
trap cleanup EXIT

case "$MODE" in
  portforward)
    info "port-forward svc/api :$PF_PORT"
    kubectl -n "$NS" port-forward svc/api "$PF_PORT:3002" >/dev/null 2>&1 &
    PF_PID=$!
    BASE="http://127.0.0.1:$PF_PORT"
    for _ in $(seq 1 20); do
      curl -fsS --max-time 2 "$BASE/v0/health/liveness" >/dev/null 2>&1 && break
      sleep 1
    done
    ;;
  direct)    info "直连 $BASE" ;;
  incluster) info "集群内验证" ;;
esac

run_curl() {
  if [[ "$MODE" == "incluster" ]]; then
    kubectl -n "$NS" run "fc-verify-$RANDOM" --rm -i --restart=Never \
      --image=curlimages/curl:8.11.1 --quiet -- "$@" 2>/dev/null
  else
    curl "$@" 2>/dev/null
  fi
}
url() { [[ "$MODE" == "incluster" ]] && echo "http://api:3002$1" || echo "$BASE$1"; }

info "readiness"
R="$(run_curl -fsS --max-time 10 "$(url /v0/health/readiness)")"
[[ "$R" == *'"ok"'* ]] && ok "readiness: $R" || bad "readiness 异常: ${R:-无响应}"

info "scrape markdown（无需 LLM provider）"
# curl 超时须大于请求体 timeout，否则拿不到 API 自己的超时响应
S="$(run_curl -sS --max-time 90 -X POST "$(url /v2/scrape)" \
      -H 'Content-Type: application/json' \
      -d '{"url":"https://example.com","formats":["markdown"],"timeout":60000}')"
if [[ "$S" == *'"success":true'* && "$S" == *'"markdown"'* ]]; then
  ok "scrape 成功"
  echo "$S" | head -c 300 | sed 's/^/      /'; echo
else
  bad "scrape 失败: ${S:0:600}"
  echo "      kubectl -n $NS logs deploy/api --tail=100"
  echo "      kubectl -n $NS logs deploy/nuq-worker --tail=100"
  echo "      kubectl -n $NS logs deploy/playwright-service --tail=100"
fi

info "map（覆盖 NuQ + RabbitMQ 链路）"
M="$(run_curl -sS --max-time 90 -X POST "$(url /v2/map)" \
      -H 'Content-Type: application/json' -d '{"url":"https://example.com"}')"
[[ "$M" == *'"success":true'* ]] && ok "map 成功" || bad "map 失败: ${M:0:300}"

if kubectl -n "$NS" get deploy extract-worker >/dev/null 2>&1; then
  info "/v2/extract 端点（需 extract-worker + LLM provider）"
  E="$(run_curl -sS --max-time 150 -X POST "$(url /v2/extract)" \
        -H 'Content-Type: application/json' \
        -d '{"urls":["https://example.com"],"prompt":"page title"}')"
  [[ "$E" == *'"success":true'* ]] && ok "extract 提交成功" \
    || bad "extract 失败（查 OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_NAME）: ${E:0:300}"
else
  info "extract-worker 未部署，跳过 /v2/extract"
fi

# scrape 的 json format 走 transformerStack（nuq-worker），不依赖 extract-worker
LLM_KEY="$(kubectl -n "$NS" get secret firecrawl-secret -o jsonpath='{.data.OPENAI_API_KEY}' 2>/dev/null)"
if [[ -n "$LLM_KEY" ]]; then
  info "scrape json format（需 LLM provider，不需 extract-worker）"
  J="$(run_curl -sS --max-time 120 -X POST "$(url /v2/scrape)" \
        -H 'Content-Type: application/json' \
        -d '{"url":"https://example.com","formats":[{"type":"json","prompt":"page title"}]}')"
  [[ "$J" == *'"success":true'* ]] && ok "json 抽取成功" || bad "json 抽取失败: ${J:0:300}"
else
  info "未配 LLM provider，跳过 json 抽取（纯 markdown 抓取不受影响）"
fi

info "search（无 SearXNG 时走 DuckDuckGo 兜底）"
Q="$(run_curl -sS --max-time 90 -X POST "$(url /v2/search)" \
      -H 'Content-Type: application/json' -d '{"query":"firecrawl","limit":3}')"
[[ "$Q" == *'"success":true'* ]] && ok "search 成功" || bad "search 失败: ${Q:0:300}"

echo
printf '通过 %d / 失败 %d\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
