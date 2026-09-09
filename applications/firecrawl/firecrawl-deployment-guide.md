# Firecrawl 部署指南 (EKS)

## 项目概述

Firecrawl 把网页转成适合 LLM 消费的结构化内容。自托管开源版提供 scrape / crawl / map / search 核心接口，供集群内的 Agent、MCP server、RAG 管道调用。

本目录基于官方 `ghcr.io/firecrawl/*` 镜像，参照上游 Helm chart 的组件拓扑重写，未直接使用上游示例，原因见「与上游的差异」。

## 能力边界

按「是否需要 LLM provider」分三档。判断依据来自源码，不是文档推测。

### 档 1：默认栈直接可用，无需任何额外配置

| 能力 | 端点 / format |
|------|---------------|
| 抓取单页 | `/v2/scrape` |
| 批量抓取 | `/v2/batch/scrape` |
| 站点爬取 | `/v2/crawl` |
| 链接发现 | `/v2/map` |
| 网页搜索 | `/v2/search` |
| 文档解析 | `/v2/parse` |
| 纯文本 format | `markdown`、`html`、`rawHtml`、`links`、`images` |
| 规则化结构抽取 | `attributes`（CSS 选择器 + 属性名） |

`search` 走 DuckDuckGo 兜底即可用。`search/index.ts` 的引擎优先级是 `FIRE_ENGINE_BETA_URL` → `SEARXNG_ENDPOINT` → DuckDuckGo，前两者留空自动落到 DDG。接 SearXNG 属质量与配额升级，非必需。

### 档 2：需要 LLM provider，但不需要 extract-worker

| format | 说明 |
|--------|------|
| `json` | 按 schema / prompt 抽结构化字段 |
| `summary` | 内容摘要 |
| `deterministicJson` | 执行阶段是 sandbox 里的缓存 JS 提取器，但生成该提取器仍走 LLM |
| `changeTracking` (json mode) | 需同时指定 `markdown` |

这些在 `transformers/index.ts` 的 `transformerStack` 里，属抓取管道的一环，由 **nuq-worker** 执行。配好 provider 即生效，无需额外组件。

### 档 3：需要 LLM provider + extract-worker

| 端点 | 说明 |
|------|------|
| `/v2/extract` | 独立的抽取作业队列 |

extract-worker 只消费 `extract-queue`（`consumeExtractJobs`），与档 2 的 scrape 管道是两条独立路径。

### 拿不到的能力

| 能力 | 原因 |
|------|------|
| 截图（`screenshot`）、page actions | 依赖 Fire-engine，Cloud 组件 |
| 反爬 / IP 封锁绕过 | 同上；只能自接出站代理 `PROXY_SERVER` |
| Agent / Browser / Interact | Cloud only |

**结论：只要"抓网页 → markdown → 喂给自己的 Agent"，默认栈就够，不需要 LLM provider，也不需要 extract-worker。**

### LLM provider 说明

抽取路径只认 openai 这一个 provider。`lib/generic-ai.ts` 的 `providerList` 虽列了 anthropic / groq / google / vertex 等 9 个，但 `llmExtract.ts` 全部 13 处 `getModel()` 调用的 provider 实参都硬编码为 `"openai"`，且 `config.ts` 没有 provider 选择开关。因此配置方式唯一：

```
firecrawl-secret : OPENAI_API_KEY
firecrawl-config : OPENAI_BASE_URL + MODEL_NAME
```

端点可以是任何 OpenAI 兼容服务。留空 `OPENAI_BASE_URL` 则直连 `api.openai.com`；留空 `MODEL_NAME` 则回落到代码默认的 `gpt-4o-mini` / `gpt-4.1-mini`。

#### 对模型的硬性要求

| 要求 | 代码依据 |
|------|----------|
| OpenAI 兼容 `/v1` 端点 | provider 为 `createOpenAI({ apiKey, baseURL })` |
| 支持 strict JSON schema 结构化输出 | `strictJsonSchema: true`（llmExtract.ts 4 处）；`normalizeSchema` 强制 `additionalProperties: false` + `required` 列全字段，即 OpenAI strict 模式约束 |
| 上下文 ≥120k 无额外收益 | `trimToTokenLimit(markdown, 120000, "gpt-4o-mini", …)` —— 长度上限与分词器均硬编码，`MODEL_NAME` 不影响；`getModelLimits` 同样只以 `"gpt-4o-mini"` 调用 |

`calculateCost` 对内置价格表之外的模型返回 0，即 usage 里的成本显示为 0。自托管无计费，不影响功能。

#### 接 AWS Bedrock

Bedrock 原生提供 OpenAI 兼容端点，**不需要 LiteLLM 一类的网关**：

```yaml
# firecrawl-config
OPENAI_BASE_URL: "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"
MODEL_NAME: "us.openai.gpt-5.6-luna"        # 或 global.openai.gpt-5.6-luna
# firecrawl-secret
OPENAI_API_KEY: "<Bedrock API key>"
```

两点注意：

- **Pod Identity 用不上。** 该端点走 Bedrock API key 的 bearer 认证，不是 SigV4；仓库内也没有任何 bedrock 代码或 `@ai-sdk/amazon-bedrock` 依赖，不存在 AWS SDK 调用点可供 Pod Identity 附着。与 auto-draw-io 现用的 `AWS_BEARER_TOKEN_BEDROCK` 是同一机制。
- **region 需跨区。** GPT-5.6 系列的模型卡未列出 ap-southeast-1，且 `bedrock-runtime` 端点不支持 In-Region 推理，必须用跨区推理 ID（`us.` 或 `global.` 前缀）。

#### 已验证可用的模型

GPT-5.6 家族（2026-07-09 发布，三档 Sol > Terra > Luna）：1M 上下文、128k 最大输出、结构化输出支持（Bedrock 模型卡 Capabilities 表已列）。Luna 为最低成本档，Global CRIS 定价 $0.20 / $1.20 每 1M token（输入 / 输出）。

由于输入被硬编码截到 120k token，百万上下文在此用不到，选型看成本与结构化输出质量即可。

**已知风险：** Firecrawl 默认走 Responses API 而非 Chat Completions（`generic-ai.ts` 中仅 o3-mini 被强制 `.chat()`，注释说明其在 Responses API 下返回空文本）。OpenAI 社区有报告指 `gpt-5.6-luna` 在 Responses API + Structured Outputs 下会于字符串值末尾混入乱码 token，而同请求走 Chat Completions 干净；因 `strict: true` 保证 schema 合法，脏数据不会报错而是直接流入结果。属单一社区报告，未独立复现。做法是先用 Luna 跑 `verify.sh` 的 json 抽取实测，若出现乱码把 `MODEL_NAME` 换成 `us.openai.gpt-5.6-terra`，无需改代码。

## 部署架构

```
集群内消费者 (Agent / MCP)
        │  http://api.firecrawl.svc.cluster.local:3002
        ▼
   ┌─── api ────────────────────────────────┐
   │                                        │
   ├─> playwright-service   (页面渲染)       │
   ├─> redis                (限流 / 缓存)    │
   ├─> rabbitmq             (NuQ 通知)      │
   └─> nuq-postgres         (队列存储, gp3) │
        ▲                                   │
        └── worker / nuq-worker ×2 / nuq-prefetch-worker
```

全部调度到 ARM64 (Graviton) 节点。镜像三者均为多架构，已确认含 `linux/arm64`。

## 文件清单

| 文件 | 内容 |
|------|------|
| `firecrawl-configmap.yaml` | 应用配置 + Playwright 配置 |
| `firecrawl-secret.yaml.example` | 凭据模板，入库 |
| `firecrawl-secret.yaml` | 可选能力凭据，由模板复制而来（`.gitignore` 排除，不入库） |
| `firecrawl-ebs-pvc.yaml` | nuq-postgres 数据卷 (gp3 10Gi) |
| `firecrawl-infra.yaml` | redis / rabbitmq / nuq-postgres + Service |
| `firecrawl-deployment.yaml` | playwright / api / worker / nuq-worker / prefetch + Service |
| `firecrawl-extract-worker.yaml` | 可选，LLM 抽取 |
| `firecrawl-ingress.yaml` | 可选，internal ALB |
| `deploy.sh` | 部署（处理顺序依赖、密码生成、不变量校验） |
| `verify.sh` | 端到端验证 |

## 部署步骤

```bash
# 1. 校验清单（不创建任何对象）
./deploy.sh --dry-run

# 2. 从模板创建 secret，按需填值（纯 markdown 抓取可跳过，留空即可）
cp firecrawl-secret.yaml.example firecrawl-secret.yaml
vi firecrawl-secret.yaml

# 3. 部署
./deploy.sh

# 4. 端到端验证
./verify.sh
```

可选组件：

```bash
# LLM 抽取（json/summary format）只需配 provider，无需额外组件：
#   编辑 firecrawl-configmap.yaml 的 OPENAI_BASE_URL + MODEL_NAME
#   编辑 firecrawl-secret.yaml 的 OPENAI_API_KEY
#   然后 ./deploy.sh 重新应用即可

./deploy.sh --with-extract    # 仅当需要 /v2/extract 端点
./deploy.sh --with-ingress    # internal ALB
```

`deploy.sh` 幂等，重复执行安全。

### 为什么需要脚本而非直接 kubectl apply

三件 YAML 表达不了的事：

1. **启动顺序** —— nuq-postgres 必须完成 initdb + pg_cron 初始化后 worker 才能连；rabbitmq 必须先起，否则 api 启动即崩。`kubectl apply -f .` 是并发的。
2. **生成型密码的幂等** —— PG 密码随机生成并写入 PVC，重跑若再生成一次就与卷内已初始化的密码不一致，全部 worker 认证失败。脚本检测 `firecrawl-db` 存在即跳过。
3. **跨文件不变量** —— `NUQ_WORKER_COUNT`（ConfigMap）必须等于 `nuq-worker` 的 `replicas`（Deployment）。两个文件里的两个数字，不校验会静默错位。

## 与上游的差异

上游两套 K8s 资源各有问题，故未直接采用：

| 上游资源 | 问题 |
|----------|------|
| `examples/kubernetes/cluster-install/` | 缺 RabbitMQ（但 api 通过 `NUQ_RABBITMQ_URL` 依赖它）；缺 nuq-prefetch-worker；`PLAYWRIGHT_MICROSERVICE_URL` 漏了 `/scrape` 后缀；PG 用 `emptyDir`；PG 密码硬编码 `password`；引用不存在的 `docker-registry-secret` |
| `examples/kubernetes/firecrawl-helm/` | 拓扑完整，但镜像指向第三方 `docker.io/winkkgmbh/*`；`resources.enabled: false` 默认不设 requests/limits |

本目录的调整：

- **镜像**：官方 ghcr。`firecrawl` 用版本标签 `2.11.209`；`playwright-service` 与 `nuq-postgres` 只有 `latest`，故用 digest 固定。
- **PG 持久化**：gp3 PVC 替代 `emptyDir`。`PGDATA` 指到子目录 `pgdata` —— gp3 是 ext4，卷根目录带 `lost+found`，initdb 会判定目录非空而跳过初始化，随后启动失败。
- **PG 更新策略**：`Recreate`。RWO 卷不能被新旧 Pod 同时挂载，默认滚动更新会死锁。
- **PG 密码**：deploy.sh 生成 40 字符随机值存 `firecrawl-db` Secret，不入库。限字母数字以免进连接串后需要 percent-encoding。
- **RabbitMQ**：补齐，并用 `4.1-alpine` 替代 `3-management`（不需要管理插件）。若遇 4.x 兼容问题回退：`kubectl -n firecrawl set image deploy/rabbitmq rabbitmq=rabbitmq:3-management`。
- **资源**：见下节。
- **nuq-worker**：5 → 2 副本，并加 `topologySpreadConstraints` 分散节点，降低 Spot 单节点回收的影响面。

## 资源规划

上游若启用 `resources`，requests 合计约 26G / 10 core（api 4G、worker 3G、nuq-worker 5×3G…），对当前 3 节点 `.large` 集群不可行。本目录按集群既有口径重算 —— CPU 可压缩故 request 压低、突发靠 limit；内存不可压缩故 limit 留足实余量。

| 组件 | 副本 | CPU req | Mem req | CPU lim | Mem lim | Node 堆 |
|------|------|---------|---------|---------|---------|---------|
| api | 1 | 300m | 1Gi | 2000m | 3Gi | 2048 |
| playwright-service | 1 | 300m | 1Gi | 2000m | 3Gi | — |
| worker | 1 | 200m | 768Mi | 1000m | 2Gi | 1280 |
| nuq-worker | 2 | 200m | 768Mi | 1000m | 2Gi | 1280 |
| nuq-prefetch-worker | 1 | 100m | 384Mi | 500m | 1Gi | 640 |
| nuq-postgres | 1 | 100m | 384Mi | 1000m | 1Gi | — |
| rabbitmq | 1 | 100m | 256Mi | 500m | 1Gi | — |
| redis | 1 | 50m | 128Mi | 500m | 512Mi | — |
| **合计 requests** | | **1550m** | **5.4Gi** | | | |

`--max-old-space-size` 必须低于容器 memory limit，否则 V8 会在触发 GC 前先被 OOMKill。改 limit 时要同步改堆上限。

约合 1 个额外 `m6g.large` Spot 节点（~$20/月）。收缩办法：`nuq-worker` 降到 1 副本（同步改 `NUQ_WORKER_COUNT`）、`MAX_CONCURRENT_PAGES` 从 6 降到 3 后 Playwright limit 可压到 2Gi。

## 访问方式

集群内（推荐）：

```
http://api.firecrawl.svc.cluster.local:3002
```

本地调试：

```bash
kubectl -n firecrawl port-forward svc/api 3002:3002
curl -X POST http://localhost:3002/v2/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"]}'
```

## 安全须知

**API 完全无鉴权**（`USE_DB_AUTHENTICATION=false`）。任何可达者都能让集群代抓任意 URL —— 对外表现为你的 NAT 出口 IP，也可借 scrape 探测 VPC 内网地址，构成 SSRF 通道。

因此：

- 默认不部署 Ingress，集群内用 ClusterIP 即可。
- `--with-ingress` 只开 **internal** ALB，并限制 `inbound-cidrs`。
- **不要**按 convertx / portainer 的模式挂 CloudFront 暴露公网 —— 那些应用自带登录，Firecrawl 没有。
- 确需外部访问，先做 ALB 层 OIDC/Cognito 认证，或前置自己的带鉴权网关。
- `USE_DB_AUTHENTICATION=true` 需要配套的数据库 schema 与应用配置，单改这一个变量不生效。

Playwright 侧已设 `ALLOW_LOCAL_WEBHOOKS=false`，阻止回调集群内网地址。

## 运维

```bash
# 状态
kubectl -n firecrawl get deploy,pod,pvc

# 日志
kubectl -n firecrawl logs deploy/api --tail=100
kubectl -n firecrawl logs deploy/nuq-worker --tail=100
kubectl -n firecrawl logs deploy/playwright-service --tail=100

# 资源实际占用（据此回调 requests）
kubectl -n firecrawl top pods

# 重启
kubectl -n firecrawl rollout restart deploy/api
```

### 升级

镜像已固定版本/digest，升级需显式改 `firecrawl-deployment.yaml`。改前先读目标版本的 `docker-compose.yaml` 与 `SELF_HOST.md`，确认组件拓扑和环境变量契约没变。

```bash
# 查可用版本
curl -s "https://ghcr.io/token?scope=repository:firecrawl/firecrawl:pull&service=ghcr.io" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])' \
  | xargs -I{} curl -s -H "Authorization: Bearer {}" \
      "https://ghcr.io/v2/firecrawl/firecrawl/tags/list?n=1000" \
  | python3 -c 'import json,sys;print([t for t in json.load(sys.stdin)["tags"] if t[0].isdigit() and "-" not in t][-10:])'

# 回滚
kubectl -n firecrawl rollout undo deploy/api
```

## 故障排除

**nuq-postgres CrashLoopBackOff，日志 `PGDATA is not a valid data directory`**
`PGDATA` 未指到子目录。gp3 是 ext4，卷根的 `lost+found` 让 initdb 判定目录非空而跳过初始化。确认 `PGDATA=/var/lib/postgresql/data/pgdata` 且 `volumeMounts.subPath=pgdata`。

**worker 日志 PG 认证失败**
`firecrawl-db` Secret 的密码与 PVC 内已初始化的不一致，通常因手工重建了 Secret。要么改回原密码，要么删 PVC 重新初始化（队列数据会丢）：
```bash
kubectl -n firecrawl delete deploy nuq-postgres
kubectl -n firecrawl delete pvc firecrawl-nuq-postgres-pvc
kubectl -n firecrawl delete secret firecrawl-db
./deploy.sh
```
注意 gp3 StorageClass 的 `reclaimPolicy` 是 `Retain`，删 PVC 后底层 EBS 卷仍在，需另行清理。

**api 启动即崩，日志提到 amqp / rabbitmq**
RabbitMQ 未就绪或不存在。`deploy.sh` 已做顺序等待；手工 apply 时容易漏。

**scrape 超时但 readiness 正常**
readiness 只是心跳。逐个查 playwright-service 日志、确认节点能出网、确认 `PLAYWRIGHT_MICROSERVICE_URL` 末尾带 `/scrape`。

**playwright-service 反复 OOMKilled**
降 `MAX_CONCURRENT_PAGES`，或抬 memory limit。`BLOCK_MEDIA=true` 已在生效，能省下大部分图片/字体流量。

**Ingress 上抓取请求 60s 被截断**
ALB 默认空闲超时 60s。清单已设 `idle_timeout.timeout_seconds=120`，确认注解生效。

## 参考

- [官方自托管指南](https://docs.firecrawl.dev/contributing/self-host)
- [开源版与 Cloud 对比](https://docs.firecrawl.dev/contributing/open-source-or-cloud)
- [上游 SELF_HOST.md](https://github.com/firecrawl/firecrawl/blob/main/SELF_HOST.md)
