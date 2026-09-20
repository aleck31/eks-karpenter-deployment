# Breeze TTS 2 语音合成 (OpenAI 兼容接口)

单容器内跑两个进程：Breeze 自带的 `breeze_infer` 推理服务 (`:8000`，仅监听 127.0.0.1) 与 OpenAI 兼容 adapter (`:8880`，对外)。声纹存放在 `/shared/voices`，
与 `voxcpm2-tts` 共用同一 PVC 与同一份 `registry.json`。

接口、字段、错误码与响应头见 [api-reference.md](api-reference.md)。

## 文件结构

```
applications/breeze2-tts/
├── Dockerfile.base                  # breeze-tts 源码 + 依赖，构建慢
├── Dockerfile                       # adapter 层，秒级
├── patches/                         # 上游补丁，构建期应用；见其 README
├── entrypoint.sh                    # 后端 :8000 + adapter :8880
├── base/
│   ├── openai-adapter.py            # 以 ConfigMap 挂载，改它无需重建镜像
│   ├── breeze2-tts-deployment.yaml  # Deployment + Service
│   ├── breeze2-tts-ingress.yaml     # 声明 ALB :8880
│   └── kustomization.yaml
└── overlays/
    ├── example/                     # 入库：示例取值，供复制
    └── <env-name>/                  # 不入库：真实取值
```

## 前置条件

| 依赖 | 说明 |
|------|------|
| GPU NodePool | 见 `gpu/deployment-guide.md`，需 Time-Slicing |
| 24 GB 显存机型 | Deployment 硬约束 g6/g5 系列；g4dn 的 16 GB 不够 |
| `qwen3-models-pvc` | 复用 qwen3-speech 的 EFS PVC，**须先部署** `applications/qwen3-speech/base/shared` |
| ALB :8880 空闲 | 同一 listen-port 只能由一个 Ingress 声明，见「接管 :8880」 |

## 资源分配

| 项 | 值 | 依据 |
|---|---|---|
| 显存 | 8.9 GiB | 实测，含三项加速参数的 CUDA Graph |
| `nvidia.com/gpu` | 1 | Time-Slicing 下每节点 2 个槽位，故一节点最多 2 个 GPU Pod |
| 内存 | 请求 3Gi / 上限 8Gi | |
| CPU | 请求 500m / 上限 2 | |
| 模型权重 | 7.2 GB on EFS | initContainer 下载，与其它服务共享 PVC |

## 构建

```bash
# 1. 基础镜像（含 breeze-tts 源码与依赖，构建较慢）
docker build -f Dockerfile.base -t <repo>/breeze2-tts:base-<breeze-ref> .

# 2. adapter 层（秒级）
docker build --build-arg BASE_IMAGE=<repo>/breeze2-tts:base-<breeze-ref> \
  -t <repo>/breeze2-tts:latest .
```

`Dockerfile.base` 会把 breeze-tts 的 commit 写入 `/opt/breeze-tts.commit`，
便于核对镜像对应的上游版本，并在此阶段应用 `patches/` 下的补丁。补丁在上游源码
不再匹配时**使构建失败**而非静默跳过，所以升级 `BREEZE_REF` 时会立刻暴露冲突。

## 部署

```bash
cp -r overlays/example overlays/<env-name>
vi overlays/<env-name>/kustomization.yaml     # namespace、ECR 地址、前缀列表 ID

kubectl config current-context                # 先确认目标集群
kubectl apply -k overlays/<env-name>
```

首次启动需下载 7.2 GB 权重（initContainer）加约 40 秒 CUDA Graph 捕获，
`startupProbe` 的 `failureThreshold: 60` 已覆盖。

### 只改 adapter

`base/openai-adapter.py` 由 `configMapGenerator` 生成 ConfigMap 并挂载到容器，
覆盖镜像内的同名文件。所以调整接口不必重建 14.7 GB 镜像：

```bash
vi base/openai-adapter.py
kubectl apply -k overlays/<env-name>
```

ConfigMap 名字带内容哈希，文件一改 Deployment 就会滚动更新，无需手动重启。
`strategy: Recreate` 会先终止旧 Pod 再建新的，约 1-2 分钟（模型重新加载）。

以下改动仍需重建镜像并推送：

| 改动 | 原因 |
|------|------|
| 新增 Python 依赖 | 依赖装在镜像层 |
| 修改 `entrypoint.sh` | 由 `COPY` 进镜像 |
| 升级 breeze-tts 源码 | 属于 `Dockerfile.base` |

验证运行中的版本：

```bash
kubectl exec deploy/breeze2-tts -- md5sum /opt/breeze-tts/openai-adapter.py
md5sum base/openai-adapter.py
```

### 接管 :8880

该端口原由 `voxcpm2-tts` 占用。同一 ALB group 内一个 listen-port 只能由一个
Ingress 声明，**两者不能同时存在**，否则 aws-load-balancer-controller 会因监听器
冲突拒绝调和。切换顺序：

```bash
# 1. 先只部署工作负载，不含 Ingress，确认 Pod Ready
kubectl apply -k overlays/<env-name> --prune=false
kubectl delete ingress breeze2-tts-ingress -n <ns>   # 若已随 apply 创建
kubectl rollout status deploy/breeze2-tts -n <ns>

# 2. 集群内直连验证，确认声纹可用
kubectl run curl-probe --rm -it --image=curlimages/curl --restart=Never -n <ns> -- \
  curl -s http://breeze2-tts-service/v1/audio/voices

# 3. 摘掉旧 Ingress，挂上新 Ingress
kubectl delete ingress voxcpm2-tts-ingress -n <ns>
kubectl apply -k overlays/<env-name>

# 4. 回滚：反向执行第 3 步
```

切换前需确认所有要用的声纹都已具备 `ref_text`，否则合成会返回 422。
可用集群内的 `qwen3-asr` 批量转写参考音频来回填。

## 验证部署

```bash
kubectl get pods -n <namespace> -o wide          # 应 1/1 Running
kubectl exec deploy/breeze2-tts -- curl -s localhost:8000/docs -o /dev/null -w '%{http_code}\n'
curl -s "http://<alb>:8880/v1/audio/voices" | head -c 200
```

`/ready` 与 `/health` 的区别：`/health` 只表示 adapter 进程活着，`/ready` 会确认
后端 :8000 可达。探针已分别用于 liveness 与 readiness/startup。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `BREEZE_MODEL_PATH` | `/shared/Breeze-TTS-2` | 模型权重目录（EFS 挂载） |
| `BREEZE_FAST_FLAGS` | 见「部署契约」 | 后端加速参数。**改动会使所有声纹存下的 seed 产出不同音色** |
| `BREEZE_BACKEND_URL` | `http://localhost:8000` | 后端地址。后端只监听 127.0.0.1，不对外暴露 |
| `VOICES_DIR` | `/shared/voices` | 声纹目录，与 `registry.json` 同级 |
| `BREEZE_MODEL_ID` | `breeze-tts-2` | `/v1/models` 返回的 id |
| `BREEZE_QUEUE_REJECT_SECONDS` | `0` | `0` 表示忙时立即 429 不排队；正数表示预估等待在该值以内则排队 |
| `BREEZE_QUEUE_TIMEOUT` | `120` | 仅在启用排队时生效，限制排队者的最长等待 |

## 部署契约

后端加速参数**固定为**：

```
--fast-depth-decoder --fast-backbone-decode --fast-codec
```

实测（L4 24GB，中文 60 字，RTF = 生成耗时 / 音频时长）：

| 配置 | 显存 | RTF | 启动 |
|---|---|---|---|
| 无参数 | 8.3 GiB | 2.33 | ~15s |
| `--fast-depth-decoder` | 9.0 GiB | 1.07 | 35s |
| **上述三项组合** | **8.9 GiB** | **0.88** | **40s** |
| `--fast-all` | 16.4 GiB | 0.88 | 104s |

`--fast-text-encoder` 与 `--fast-backbone-prefill` 两个阶段在一次生成里各只执行
一次，却按「batch size × 文本长度分桶」预捕获数十份静态 CUDA Graph 常驻显存，
单开分别多占 6.7 / 10.7 GiB 且不提速，因此不用 `--fast-all`。

**不要改动这组参数。** 同一 seed 只在同一参数组合下可复现（实测 eager 与上述
组合下 seed=2 输出长度分别为 268844 / 314924 字节），改动会使所有已注册声纹
存下的 seed 产出不同音色。

基础镜像必须含 C 编译器：`--fast-depth-decoder` 走 `torch.compile` (inductor)，
缺 gcc 会在启动时报 `InductorError: Failed to find C compiler` 并退出，不会降级运行。
`Dockerfile.base` 基于 `pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime`（自带 Breeze
要求的 torch 2.9.1）并显式安装 `build-essential`。不用 devel 变体：它含完整 CUDA
toolkit，镜像体积翻倍（实测 28.7GB），而 inductor 只需 gcc，不需要 nvcc。

## 已知限制

**单并发。** 后端一次只服务一个请求，并发请求会收到 HTTP 409。adapter 把它转成
明确的 429 + `Retry-After`（见「并发」一节），**不排队**。所以本模块适合单用户或
低频调用场景。副本数扩容无法解决：多副本无法共享 GPU 上的单份模型，且时间切片下
每节点只有 2 个 GPU 槽位。真并发需要独占节点 —— 单实例占 8.9 GiB，24 GB 卡可容纳
两个，但当前节点与 `qwen3-asr` 共享，仅余约 3.5 GiB。

**长文本由 adapter 分句。** 后端上下文 2048 位置 ÷ 12.5 Hz 帧率 ≈ 164 秒音频上限，
而质量在触限之前就开始下降（重复片段、语速漂移），这是自回归 TTS 的共性。
adapter 用 `pysbd` 切句并贪心打包到 `MAX_SEGMENT_CHARS`（160），逐段合成后以
`SEGMENT_PAUSE_MS`（250ms）静音拼接，调用方不必自行切分。分句数见
`X-Breeze-Segments` 响应头。

分句用 `pysbd` 而非自行实现：手写规则在 `3.14 和 e 是 2.718。`、`Dr. Smith went
to Washington.`、`Version 1.2.3 shipped on Jan. 5.` 这类输入上都会切错。
`pysbd` 对中英混排在 `language="zh"` 与 `"en"` 下输出一致，无需语言检测。

**许可。** 推理代码为 Apache 2.0，**模型权重与自托管产出音频**适用
BreezeBlue Research and Non-Commercial License，仅限研究与非商业用途，
商业使用需 BreezeBlue 书面授权。

**语言。** 开放权重为中英双语。官网宣称的 50 语言指其托管服务，不适用于本部署。

## 故障排除

| 现象 | 原因 | 处理 |
|------|------|------|
| 启动即退出，日志 `InductorError: Failed to find C compiler` | 基础镜像缺 gcc | 用 `Dockerfile.base`，它装了 `build-essential` |
| `ValueError: 't5_gemma_module' is already used` | `transformers` 被升级 | 不要在此镜像里装会牵动 transformers 的包 |
| 并发请求 503 | 后端单并发，排队超 `BREEZE_QUEUE_TIMEOUT` | 降低并发；扩副本无效（见「已知限制」） |
| 同一 seed 音色变了 | 加速参数被改动 | 恢复为部署契约里的三项组合 |
| Pod 一直 Pending | GPU 槽位被占满或无 24GB 机型 | `kubectl describe pod` 看调度事件 |
| 停止后持续 429，GPU 却空闲 | 后端推理锁泄漏，补丁未生效 | 容器内 `python3 patches/release-inference-lock-on-disconnect.py --check --path /opt/breeze-tts/breeze_infer/api.py`；未打上则重建基础镜像 |
| 构建报 `refusing to patch blindly` | 上游源码已变动，补丁不再匹配 | 对照 [issue #20](https://github.com/breezeblue-ai/breeze-tts/issues/20) 确认是否已修复；已修则删补丁，未修则更新补丁 |
| 换了节点后声纹丢失 | 声纹在 EFS，不会丢；检查 PVC 是否挂载 | 确认 `VOICES_DIR=/shared/voices` |

重启时用 `scale --replicas=0` 等 Pod 消失后再 `--replicas=1`，或 `rollout restart`。
`kubectl delete pod --wait=false` 会绕过 `strategy: Recreate`，新旧 Pod 同时争抢
GPU 槽位，导致新 Pod 落到另一个节点。

## 成本

单个 g6.xlarge Spot 约 $0.22/小时。与 `qwen3-asr` 共享节点时通过 Time-Slicing
同用一张 L4（8.9 + 7.7 GiB，24 GB 内可容纳），不需要额外节点。
