# Qwen3 Speech (ASR + TTS) 部署指南

接口、字段、错误码与响应头见 [api-reference.md](api-reference.md)。

## 概述

在 EKS 集群中部署 Qwen3 ASR 1.7B 和 TTS 1.7B 模型，提供 OpenAI 兼容的语音识别和语音合成 API。
两个模型通过 NVIDIA GPU Time-Slicing 共享单张 T4 GPU，各占 45% 显存。

## 部署文件结构

采用 kustomize base + overlay。三个服务（ASR、TTS CustomVoice、TTS Base）地位对等，
在 `base/` 下各自独立成目录；`base/shared/` 提供它们共用的模型存储。
环境相关取值（namespace、ECR 账号、EFS 文件系统 ID）不入库，由 `overlays/<env>/` 注入，
并在其 `resources` 中选择要部署哪些服务。

```
applications/qwen3-speech/
├── base/
│   ├── shared/                      # EFS StorageClass + PVC（各服务共用）
│   ├── asr/                         # ASR Deployment + Service + Ingress
│   │   └── asr-adapter.py           # 以 ConfigMap 挂载，改它无需重建镜像
│   ├── tts/                         # Qwen3-TTS CustomVoice Deployment + Service
│   └── tts-base/                    # Qwen3-TTS Base Deployment + Service（声音克隆）
├── overlays/
│   ├── example/                     # 入库：示例取值，供复制
│   │   ├── kustomization.yaml
│   │   └── efs-patch.yaml
│   └── <env-name>/                  # 不入库：真实取值
├── Dockerfile                       # ASR 镜像（vLLM + adapter）
├── entrypoint.sh
└── deployment-guide.md  # 本文档
```

## 架构

```
┌────────────────────────────────────────────────┐
│  g4dn.xlarge           - 1x T4 16GB            │
│  Time-Slicing: 1 物理 GPU    →   2 虚拟 GPU     │
│                                                │
│  Pod: qwen3-asr                  → 虚拟 GPU #1  │
│  Pod: qwen3-tts                  → 虚拟 GPU #2  │
└────────────────────────────────────────────────┘
         │
    EFS PVC (RWX, 共享模型存储)
    ├── /models/Qwen3-ASR-1.7B/          (3.87 GiB 显存)
    └── /models/Qwen3-TTS-CustomVoice/   (3.90 GiB 显存)
         │
    ALB Ingress (group: qwen3-speech)
    ├── :8000 → ASR (qwen-asr-serve, vLLM 0.14.0)
    └── :8880 → TTS (FastAPI, OpenAI 兼容)
```

## 组件说明

| 组件 | 镜像 | 端口 | 显存占用 | 说明 |
|------|------|------|----------|------|
| ASR | qwenllm/qwen3-asr:latest | 8000 | ~3.87 GiB | 官方 qwen-asr-serve (内置 vLLM 0.14.0) |
| TTS | ECR qwen3-tts:latest | 8880 | ~3.90 GiB | 社区 FastAPI 服务 (official backend) |

## 资源分配

| 容器 | CPU request | Memory request | GPU | 说明 |
|------|------------|----------------|-----|------|
| ASR (qwen-asr-serve) | 1 | 6Gi | 1 (虚拟) | gpu_memory_utilization=0.45, max_model_len=4096 |
| TTS (FastAPI) | 1 | 6Gi | 1 (虚拟) | TTS_BACKEND=official, TTS_DTYPE=bfloat16 |
| **总计** | **2 CPU** | **12Gi** | **2 (虚拟 / 1 物理)** | |

g4dn.xlarge 可分配: ~3.9 CPU / ~14.7Gi 内存 / 1 GPU (Time-Slicing 虚拟为 2)

## 前置条件

- GPU NodePool 已部署 (`gpu/nodepool-gpu.yaml`)
- NVIDIA Device Plugin 已配置 (`gpu/nvidia-device-plugin.yaml`)
- EFS CSI Driver 已安装
- ALB Ingress Controller 已安装

详见 `gpu/deployment-guide.md`。

## 部署步骤

### 1. 配置 GPU Time-Slicing

Time-Slicing 将 1 张物理 GPU 虚拟为多个 `nvidia.com/gpu` 资源，允许多个 Pod 共享同一张 GPU。

```bash
# 应用 Time-Slicing ConfigMap
kubectl apply -f ../../gpu/nvidia-time-slicing-config.yaml
```

然后更新 NVIDIA Device Plugin DaemonSet 挂载配置：

```bash
# 需要在 DaemonSet 中添加:
# 1. 环境变量 CONFIG_FILE=/config/config.yaml
# 2. Volume mount: nvidia-device-plugin-config ConfigMap → /config
# 参考 gpu/deployment-guide.md 中的 Device Plugin 部署说明
```

验证 Time-Slicing 生效（需要 GPU 节点运行后检查）：

```bash
# 节点应显示 nvidia.com/gpu: 2 (而非物理的 1)
kubectl get node -l node-type=gpu -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'
```

### 2. 创建 Namespace

根据需要创建 Namespace, 例如:

```bash
kubectl create namespace hosthree
```

### 3. 准备 overlay

```bash
# 复制示例，目录名用语义化环境名（如 inference-env）
cp -r overlays/example overlays/<env-name>

# 编辑取值：
#   kustomization.yaml  namespace、要部署哪些服务、ECR 镜像地址
#   efs-patch.yaml      EFS 文件系统 ID
vi overlays/<env-name>/kustomization.yaml
vi overlays/<env-name>/efs-patch.yaml
```

`resources` 中按需增删服务。例如仅部署 ASR：

```yaml
resources:
  - ../../base/shared
  - ../../base/asr
```

> `.gitignore` 默认忽略 `overlays/` 下全部目录、仅放行 `example/`，真实取值不会误提交。

### 4. 部署

```bash
kubectl config current-context          # 先确认目标集群
kubectl apply -k overlays/<env-name>
```

首次部署耗时较长：
- Karpenter 拉起 GPU Spot 实例 (~1-3 分钟)
- 拉取 ECR 镜像
- initContainer 下载模型到 EFS (~3-5 分钟)
- 主容器加载模型

同时部署多个服务时，它们通过 GPU Time-Slicing 共享同一张卡，
需确认显存总量足够（见「资源分配」一节）。

### 5. 只改 adapter

`base/asr/asr-adapter.py` 由 `configMapGenerator` 生成 ConfigMap 并挂载到容器，
覆盖镜像内的同名文件。所以调整接口不必重建 30 GB 镜像：

```bash
vi base/asr/asr-adapter.py
kubectl apply -k overlays/<env-name>
```

ConfigMap 名字带内容哈希，文件一改 Deployment 就会滚动更新，无需手动重启。
约 1-2 分钟（vLLM 重新加载模型）。

以下改动仍需重建镜像并推送：

| 改动 | 原因 |
|------|------|
| 新增 Python 依赖 | 依赖装在镜像层 |
| 修改 `entrypoint.sh` | 由 `COPY` 进镜像 |
| 更换 vLLM 版本 | 属于基础镜像 |

验证运行中的版本：

```bash
kubectl exec deploy/qwen3-asr -c asr -- md5sum /app/asr-adapter.py
md5sum base/asr/asr-adapter.py
```

改 vLLM 启动参数（`args`）属于 Deployment 本身，同样只需 `apply -k`，
但注意 `startupProbe` 探的是 adapter 的 `:8001`，vLLM 起不来时 Pod 仍会显示
`1/1 Running`。必须另外确认后端：

```bash
kubectl exec deploy/qwen3-asr -c asr -- \
  python3 -c "import httpx; print(httpx.get('http://localhost:8000/v1/models').status_code)"
```

### 6. 验证部署

```bash
# 查看 Pod 状态（两个都应 1/1 Running，同一节点）
kubectl get pods -n <namespace> -o wide

# 检查 GPU Time-Slicing
kubectl get node -l node-type=gpu -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'
# 预期输出: 2

# 测试 ASR health
kubectl port-forward -n <namespace> svc/qwen3-asr-service 8000:80 &
curl -s http://localhost:8000/health

# 测试 TTS health
kubectl port-forward -n <namespace> svc/qwen3-tts-service 8880:80 &
curl -s http://localhost:8880/health | python3 -m json.tool
# 预期: "status": "healthy", "ready": true
```

## 推理镜像信息

### ASR 镜像
- **镜像**: qwenllm/qwen3-asr:latest (Docker Hub)
- **来源**: https://github.com/QwenLM/Qwen3-ASR
- **大小**: ~14.4GB (包含 vLLM 0.14.0 + CUDA + 模型推理依赖)
- **启动命令**: `qwen-asr-serve /models/Qwen3-ASR-1.7B`

### TTS 镜像
- **镜像**: <AWS_ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/qwen3-tts:latest
- **来源**: https://github.com/groxaxo/Qwen3-TTS-Openai-Fastapi
- **大小**: ~6.2GB
- **Dockerfile target**: production (official backend)
- **本地 Patch**: `api/backends/official_qwen3_tts.py` — 将 `generate_custom_voice` / `generate_voice_clone` 等同步推理调用包装为 `asyncio.run_in_executor`，避免 GPU 推理阻塞事件循环导致 `/health` 无法响应

**重新构建镜像**:
```bash
cd /path/to/Qwen3-TTS-Openai-Fastapi
# 应用 patch 后构建
docker build --target production -t <AWS_ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/qwen3-tts:latest .
# 推送
aws ecr get-login-password --region <REGION> | docker login --username AWS --password-stdin <AWS_ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com
docker push <AWS_ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/qwen3-tts:latest
```

## 模型下载说明

两个模型通过 initContainer 从 HuggingFace 下载到 EFS 共享存储：

| 模型 | HuggingFace ID | EFS 路径 | 大小 |
|------|---------------|----------|------|
| ASR | Qwen/Qwen3-ASR-1.7B | /models/Qwen3-ASR-1.7B | ~3.5GB (2 个 safetensors 分片) |
| TTS | Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice | /models/Qwen3-TTS-CustomVoice | ~3.5GB |

- initContainer 会检查 safetensors 文件完整性(所有分片 + config.json)，不完整则重新下载
- Spot 中断恢复时模型已在 EFS，跳过下载，恢复更快

## 成本说明

- 实例类型: g4dn.xlarge (1x T4, 4 vCPU, 16GB)，Spot ~$0.16/h
- 两个模型共享单张 GPU (Time-Slicing)，相比双节点节省 50%
- Karpenter consolidation: WhenEmptyOrUnderutilized, 30 分钟后回收/right-sizing (GPU 节点启停代价大，避免频繁整合)
- Spot 中断处理已启用 (SQS + EventBridge)，提前 10-20 分钟迁移

## 环境变量

ASR adapter（容器 `:8001`，对外端口）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ASR_BACKEND_URL` | `http://localhost:8000` | vLLM HTTP 地址 |
| `ASR_BACKEND_WS` | `ws://localhost:8000` | vLLM realtime WebSocket 地址 |
| `ASR_SEGMENT_MIN_SECONDS` | `150` | 低于此时长直接透传，不切分 |
| `ASR_SEGMENT_MAX_SECONDS` | `120` | 单段上限。按实测 13.0 token/秒 折合约 1560 token，留有余量 |
| `ASR_VAD_MIN_GAP_SECONDS` | `0.35` | silero-vad 判定为可切点的最小语音间隙 |
| `ASR_SEGMENT_CONCURRENCY` | `4` | 并发转写的段数。同时限制 ffmpeg 切分进程数，容器 CPU 上限为 2 |
| `ASR_TRANSCRIBE_TIMEOUT` | `300` | 超时下限 |
| `ASR_TIMEOUT_PER_AUDIO_SECOND` | `0.35` | 超时系数。实际预算 `max(下限, 时长 × 系数)` |

切点取语音间隙的**中点**而非边界：取起点会切掉前一个词的尾音，取终点会切掉后一个
词的起音。VAD 失败时回退到按时钟均分，仍有界但可能切断句子。

用 silero-vad 而非 ffmpeg `silencedetect`（后者是能量阈值，不是 VAD）。
粉噪对照实测：

| 噪声幅度 | silero-vad | silencedetect |
|---------|-----------|---------------|
| 0.00 | 37 个间隙 | 49 个 |
| 0.05 | 39 | 51 |
| **0.10** | **34** | **0 — 完全失效** |
| 0.20 | 32 | 0 |

幅度 0.10 时能量阈值找不到任何间隙，每次切分都退化为按时钟切，正是要避免的
"切断句子"。会议室的空调、投影仪、键盘声很容易超过该幅度。

## 已知限制

- Time-Slicing 不提供显存隔离，两个模型可能互相影响
- T4 不支持 Flash Attention 2 (compute capability 7.5 < 8.0)，ASR 使用 FlashInfer + SDPA 替代，TTS 使用 torch SDPA
- ASR `--max-model-len 4096`: T4 显存有限 (gpu_memory_utilization=0.45)，KV cache 约 1.32 GiB，支持 ~3 个并发请求
- TTS 单线程推理 (Python GIL)，建议通过 `run_in_executor` patch 解决事件循环阻塞问题，推理期间 `/health` 可正常响应。如使用未 patch 的上游镜像，长文本推理会导致 readiness probe 失败、ALB 返回 503

## 故障排除

```bash
# 查看 Pod 日志
kubectl logs -n <namespace> deployment/qwen3-asr -c asr
kubectl logs -n <namespace> deployment/qwen3-tts

# 查看 initContainer 日志 (模型下载)
kubectl logs -n <namespace> <pod-name> -c model-downloader

# ASR 启动失败常见原因:
# 1. "weights were not initialized" → 模型文件不完整，删除 EFS 目录重新下载
# 2. "KV cache is needed...larger than available" → 降低 --max-model-len 或提高 --gpu-memory-utilization
# 3. "FA2 is only supported on compute capability >= 8" → 正常 warning，T4 自动回退到其他 attention backend

# TTS 启动失败常见原因:
# 1. health 返回 "initializing" → 模型还在加载，等待 1-2 分钟
# 2. "status": "healthy" 但 400 错误 → 检查 model 参数，应用 tts-1 而非模型目录名

# 检查 GPU 资源
kubectl describe node -l node-type=gpu | grep -A5 "Allocated resources"
```
