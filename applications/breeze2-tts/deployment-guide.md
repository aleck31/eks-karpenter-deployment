# Breeze TTS 2 语音合成 (OpenAI 兼容接口)

单容器内跑两个进程：Breeze 自带的 `breeze_infer` 推理服务 (`:8000`，仅监听 127.0.0.1) 与 OpenAI 兼容 adapter (`:8880`，对外)。模型权重与声纹（`/shared/voices`，
索引为 `registry.json`）放在同一个 RWX PVC 上。

接口、字段、错误码与响应头见 [api-reference.md](api-reference.md)。兼容基线为 OpenAI
Audio API 的 GA 规范（2026-10 对齐，`openai` Python SDK 3.31.0 实测）。

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
| GPU 显存 | 服务固定占用约 9 GiB，与其它负载同卡时需留足余量；机型约束按环境在 overlay 中设置 |
| 模型 PVC | RWX（如 EFS），在 overlay 中指定名称；声纹目录须属于 uid 10001 |
| ALB :8880 空闲 | 同一 ALB group 内一个 listen-port 只能由一个 Ingress 声明 |

## 资源分配

| 项 | 值 | 依据 |
|---|---|---|
| 显存 | 约 9.0 GiB | 实测，含三项加速参数的 CUDA Graph |
| `nvidia.com/gpu` | 1 | Time-Slicing 下的虚拟槽位，数量由节点配置决定 |
| 内存 | 请求 3Gi / 上限 8Gi | |
| CPU | 请求 500m / 上限 2 | |
| 模型权重 | 7.2 GB | initContainer 下载到模型 PVC |

**显存占用是固定的，不随显卡容量变化。** Breeze 后端没有任何显存预算参数
（只有 `--host` / `--port` / `--fast-*`），占用由模型权重加 CUDA Graph 的实际
需要决定。实测 24 GB 卡上 8.9 GiB、32 GB 卡上 9.0 GiB，相差 0.7%。

与 vLLM 类服务同卡时情况相反：`--gpu-memory-utilization` 是**总显存的比例**，换到
更大的卡上会自动占更多。所以显存紧张时可回收的是那一方的预算，不是这里 ——
要降低本服务的显存只能减少 `--fast-*` 参数，而那会同时恶化 RTF 并使所有已注册
声纹的 seed 失效（见「部署契约」）。

**同卡余量要按各方峰值算，不是当前值。** 2026-10-17 在 32623 MiB 卡上实测：

| 负载 | 当前 | 峰值 | 性质 |
|------|------|------|------|
| 本服务 | 9176 MiB | 9176 | 固定，权重 + CUDA Graph |
| `r2t2-asr` | 12400 MiB | 12400 | 固定，`--gpu-memory-utilization` 预分配 |
| `klimt-ml`（immich） | 3136 MiB | 11412 | **波动**，`MACHINE_LEARNING_MODEL_TTL=3600` 闲置卸载、有请求重载 |

三方峰值之和 32988 MiB > 整卡 32623，**超 365 MiB**。当前不 OOM 只因 klimt 处于
已卸载状态。更早一次满载实测峰值 31743 MiB、余 387 MiB 不 OOM，那时 `r2t2-asr`
是 11.2 GiB，此后增长的 1.2 GB 正好吃掉了那点余量。

可回收的是 `r2t2-asr` 的预算（其 KV cache 5.93 GiB 对现有负载富余），或给
`klimt-ml` 设显存上限。本服务这一侧无从下手，理由见上一段。

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

### ALB 端口

同一 ALB group 内一个 listen-port 只能由一个 Ingress 声明，否则
aws-load-balancer-controller 会因监听器冲突拒绝调和。与其它服务共享 ALB 时，
先确认 :8880 未被占用；要从别的服务接管该端口，先删除对方的 Ingress 再部署本模块。

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
| `ASR_TRANSCRIBE_URL` | 空 | 注册声纹未给 `ref_text` 时调用的 OpenAI 兼容转写接口（`POST .../v1/audio/transcriptions`），在 overlay 中注入。未设或不可达时 `ref_text` 必填 |
| `BREEZE_QUEUE_REJECT_SECONDS` | `0` | `0` 表示忙时立即 429 不排队；正数表示预估等待在该值以内则排队 |
| `BREEZE_QUEUE_TIMEOUT` | `120` | 仅在启用排队时生效，限制排队者的最长等待 |

## 部署契约

后端加速参数**固定为**：

```
--fast-depth-decoder --fast-backbone-decode --fast-codec
```

实测（24 GB 显存卡，中文 60 字，RTF = 生成耗时 / 音频时长）：

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

**单并发，且这是推理引擎层的约束，不是模型或显存。** 后端一次只服务一个请求，
并发请求收到 HTTP 409，adapter 转成明确的 429 + `Retry-After`（见接口参考的
「并发」一节），**不排队**。

根因在 `FastBreezeStreamingRuntime.iter_audio_chunks`：单请求的解码状态挂在运行时
实例上（`self._backbone_graph` 连同其 KV cache、`self._codec_runtime`），两个并发
请求会互相覆写；解码循环还硬编码 `token[0]` / `depth_tokens[0]` 的单请求索引，而
batch 维度已被 CFG 的正负双分支占用（`branch_batch_size = 2 if single_cfg else 1`）。
所以那把 `threading.Lock` 是必需的，去掉会产出错乱音频而非更高吞吐。

不是模型的限制：输入准备层本就支持批（`prepare_inputs(..., requests: list[Request])`
内有 `for request in requests` 与 `_prepare_segment_batches`），`api.py` 只是传了
单元素列表。也不是 CUDA Graph 的限制：图按 batch size 存字典
（`self._backbone_graphs: dict[int, BackboneGraph]`），本就能同时持有多个尺寸。

显存只是**变通方案的**约束：多副本每个都要完整一份约 9 GiB，无法共享。

**换模型解决不了，换服务层可以。** 流式解码循环持有单请求状态是这一代自回归 TTS
的普遍做法。但 vLLM-Omni 自带 `breeze_tts_2` 适配器，**用同一份权重**做批处理调度，
克隆、seed、流式、CFG 全部支持，已实测可行（2026-10-17，RTX PRO 4500 32 GiB 独占）：

| 配置 | `max_num_seqs` | 显存 | 首字节 | RTF | 并发 2 | 并发 4 | 并发 8 |
|------|---------------|------|--------|-----|--------|--------|--------|
| 当前参考实现 | 1 | 9176 MiB | 0.158s | 0.414 | **429** | **429** | **429** |
| `breeze_tts_2.yaml` | 2 | 9828 MiB | 0.066s | 0.317 | 5.58x | 5.71x | 5.64x |
| 同上改 `max_num_seqs=4` | 4 | 11548 MiB | 0.068s | 0.317 | 5.62x | 9.11x | 10.31x |
| `breeze_tts_2_throughput.yaml` | 32 | 28552 MiB | 0.439s | 0.308 | — | — | 50.64x |

倍数为「音频秒 / 墙钟秒」。同 seed 同输入五轮字节一致，即 vLLM-Omni 内部可复现；
但采样器不是 `breeze_infer` 的，同 seed 与当前实现产出**不同的一次 take**（同文本
18.16s vs 15.68s），听感确认说话人不变。

两点与直觉相反：

- **首字节与并发可以兼得。** 吞吐配置那个 0.439s 来自 `initial_codec_chunk_frames: 16`，
  与 `max_num_seqs` 无关。保留延迟配置的 codec 分块策略（`initial=1`、ramp `[1,2,4,5]`）
  后，`max_num_seqs` 提到 20 首字节仍是 0.068s。
- **吞吐配置不可用。** 28552 MiB 放不进共卡，且首字节退化 6.5 倍。

选 `max_num_seqs=2`（官方 `breeze_tts_2.yaml`，无需自定义 YAML）：显存只比现状多
652 MiB，而首字节快 2.4 倍、RTF 快 23%、并发不再 429（内部排队）。2 路以上按需改
YAML 即可，不必重建镜像 —— 代价见「资源分配」一节的同卡余量。

**长文本由 adapter 分句，实测不随长度退化。** 上限来自模型自身 ——
`config.json` 的 `max_position_embeddings = 2048`，÷ 12.5 Hz 帧率 ≈ 164 秒音频，
而质量在触限之前就开始下降（重复片段、语速漂移），这是自回归 TTS 的共性。
adapter 用 `pysbd` 切句并贪心打包到 `MAX_SEGMENT_CHARS`（160），逐段合成后以
`SEGMENT_PAUSE_MS`（250ms）静音拼接，调用方不必自行切分。分句数见
`X-Breeze-Segments` 响应头。

这一层**换服务层也省不掉**：2048 是权重的属性，任何服务层都受同一约束，而服务层
本不该做文本切分。实测绕过 adapter 直接打 vLLM-Omni，830 字（≈2804 codec 帧）
静默截断，ASR 回转只命中 7/20 个标记、转写 328 字 vs 原文 740 字。

**不要为了细粒度时间戳调小 `MAX_SEGMENT_CHARS`。** 打包不只是规避上下文上限，
也是韵律决策：按句合成后固定 250ms 静音会替掉模型自己产生的停顿，各句还失去彼此的
韵律上下文。实测同一段 207 字文本，按句合成与打包的总时长只差 1%（29.65s / 29.93s），
客观指标分辨不出，但听感明显更差。因此段边界必然横跨数句，细粒度标记要靠事后对齐
从音频导出，归属与接口形状见 `r2t2-asr` 的 `POST /v1/audio/alignments`。

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

服务固定占用约 9 GiB 显存，可通过 Time-Slicing 与其它推理服务同卡部署而不必单开
节点，前提是同卡各服务的实测占用之和小于总显存。
