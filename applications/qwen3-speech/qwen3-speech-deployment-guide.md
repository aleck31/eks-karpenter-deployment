# Qwen3 Speech (ASR + TTS) 部署指南

## 概述

在 EKS 集群中部署 Qwen3 ASR 1.7B 和 TTS 1.7B 模型，提供 OpenAI 兼容的语音识别和语音合成 API。
两个模型通过 NVIDIA GPU Time-Slicing 共享单张 T4 GPU，各占 45% 显存。

## 部署文件结构

```
applications/qwen3-speech/
├── qwen3-asr-deployment.yaml        # ASR Deployment + Service
├── qwen3-tts-deployment.yaml        # TTS Deployment + Service
├── qwen3-speech-ingress.yaml        # 共享 ALB Ingress (group: qwen3-speech)
├── qwen3-speech-efs-pvc.yaml        # EFS StorageClass + PVC
└── qwen3-speech-deployment-guide.md  # 本文档
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

详见 `gpu/gpu-deployment-guide.md`。

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
# 参考 gpu/gpu-deployment-guide.md 中的 Device Plugin 部署说明
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

### 3. 创建 EFS 存储

```bash
kubectl apply -f qwen3-speech-efs-pvc.yaml

# 验证
kubectl get pvc -n hosthree qwen3-models-pvc
# 预期: Bound
```

### 4. 部署 ASR

```bash
kubectl apply -f qwen3-asr-deployment.yaml
```

首次部署耗时较长：
- Karpenter 拉起 g4dn.xlarge Spot 实例 (~1-3 分钟)
- 拉取 qwenllm/qwen3-asr:latest 镜像 (~7 分钟，14.4GB)
- initContainer 下载模型到 EFS (~3-5 分钟)
- 主容器加载模型 + CUDA graph 编译 (~3-5 分钟)

### 5. 部署 TTS

等 ASR Pod 进入 Running 后再部署 TTS，避免 GPU 资源竞争导致调度到不同节点：

```bash
# 确认 ASR 已 Running
kubectl get pods -n hosthree -l app=qwen3-asr

kubectl apply -f qwen3-tts-deployment.yaml
```

### 6. 部署 Ingress

```bash
kubectl apply -f qwen3-speech-ingress.yaml

# 验证 ALB 地址
kubectl get ingress -n hosthree
```

### 7. 验证部署

```bash
# 查看 Pod 状态（两个都应 1/1 Running，同一节点）
kubectl get pods -n hosthree -o wide

# 检查 GPU Time-Slicing
kubectl get node -l node-type=gpu -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'
# 预期输出: 2

# 测试 ASR health
kubectl port-forward -n hosthree svc/qwen3-asr-service 8000:80 &
curl -s http://localhost:8000/health

# 测试 TTS health
kubectl port-forward -n hosthree svc/qwen3-tts-service 8880:80 &
curl -s http://localhost:8880/health | python3 -m json.tool
# 预期: "status": "healthy", "ready": true
```

## API 使用

```bash
ALB=<ALB_DNS_NAME>  # kubectl get ingress -n hosthree 查看实际地址

# ASR - 语音识别
curl http://$ALB:8000/v1/audio/transcriptions \
  -F "file=@audio.wav" \
  -F "model=/models/Qwen3-ASR-1.7B"

# TTS - 语音合成 (model 用 tts-1，不是模型目录名)
# 基本用法 (自动语言检测)
curl http://$ALB:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"你好，这是语音合成测试。","voice":"Vivian"}' \
  -o output.wav

# 指定语言 (强制英文输出)
curl http://$ALB:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1-en","input":"Hello, this is a TTS test.","voice":"Ryan"}' \
  -o output.wav
```

### TTS 参数说明

**model 参数** (控制输出语言，底层都是同一个模型):
- `tts-1` / `qwen3-tts` — 自动语言检测
- `tts-1-{lang}` — 强制输出语言: zh/en/ja/ko/de/fr/es/ru/pt/it
- `tts-1-hd` / `tts-1-hd-{lang}` — 同上，仅兼容 OpenAI API 命名，无质量区别

**voice 参数** (9 个内置音色):

| Voice | 描述 | 母语 |
|-------|------|------|
| Vivian | 明亮、略带锐利的年轻女声 | 中文 |
| Serena | 温暖、柔和的年轻女声 | 中文 |
| Sohee | 温暖韩国女声，情感丰富 | 韩文 |
| Ono_Anna | 活泼日本女声，轻盈灵动 | 日文 |
| Uncle_Fu | 成熟男声，低沉醇厚 | 中文 |
| Dylan | 年轻北京男声，清晰自然 | 中文 (北京话) |
| Eric | 活泼成都男声，略带沙哑 | 中文 (四川话) |
| Ryan | 有力男声，节奏感强 | 英文 |
| Aiden | 阳光美式男声，中频清晰 | 英文 |

每个音色可说所有 10 种语言，不限于母语。推荐使用母语获得最佳效果。

**其他参数**:
- `response_format`: mp3 (默认), opus, aac, flac, wav, pcm
- `speed`: 0.25 ~ 4.0 (默认 1.0)

**TTS 生成能力**:
- 最长语音: ~11 分钟 (max_new_tokens=8192, 12Hz)
- T4 实测 RTF: ~2.16 (生成 1 秒语音需 2.16 秒推理)
- 短文本 (~30字): ~20 秒推理
- 长文本 (~1000字): ~10 分钟推理

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
- Karpenter consolidation: WhenEmptyOrUnderutilized, 5 分钟后回收/right-sizing
- Spot 中断处理已启用 (SQS + EventBridge)，提前 10-20 分钟迁移

## 已知限制

- Time-Slicing 不提供显存隔离，两个模型可能互相影响
- T4 不支持 Flash Attention 2 (compute capability 7.5 < 8.0)，ASR 使用 FlashInfer + SDPA 替代，TTS 使用 torch SDPA
- ASR `--max-model-len 4096`: T4 显存有限 (gpu_memory_utilization=0.45)，KV cache 约 1.32 GiB，支持 ~3 个并发请求
- TTS 单线程推理 (Python GIL)，长文本推理期间 readiness probe 会失败，ALB 暂时摘流量，推理完成后自动恢复

## 故障排除

```bash
# 查看 Pod 日志
kubectl logs -n hosthree deployment/qwen3-asr -c asr
kubectl logs -n hosthree deployment/qwen3-tts

# 查看 initContainer 日志 (模型下载)
kubectl logs -n hosthree <pod-name> -c model-downloader

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
