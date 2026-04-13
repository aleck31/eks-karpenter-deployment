# VoxCPM2 TTS - OpenAI 兼容 TTS 服务

基于 VoxCPM2 + Nano-vLLM 推理引擎，通过 OpenAI 兼容 adapter 对外提供 `/v1/audio/speech` 接口。

## 架构

```
下游应用 → ALB :8880 → adapter (OpenAI API) → localhost:8000 → VoxCPM2 (Nano-vLLM)
                        /v1/audio/speech              /generate
```

单容器内运行两个进程：
- Nano-vLLM backend (:8000) — GPU 推理引擎
- OpenAI adapter (:8880) — 接口转换 + 音频格式转码

## Voice ID 映射

adapter 将 OpenAI voice name 映射为 VoxCPM2 Voice Design 描述，模型根据描述生成对应风格的声音。

| Voice | 特点 | 适合场景 |
|-------|------|---------|
| `alloy` | 年轻女声，清晰平衡，自然自信 | 通用旁白、一般对话 |
| `ash` | 年轻男声，自信直接，略带沙哑 | 科技播客、产品介绍 |
| `ballad` | 温暖男声，富有表现力，旋律感强 | 故事讲述、情感内容 |
| `coral` | 友好女声，明亮对话风格，自然亲和 | 日常对话、客服 |
| `echo` | 年轻男声，平滑温暖，轻松随和 | 深夜电台、冥想引导 |
| `fable` | 英式绅士，深沉权威，节奏从容 | 有声书、经典文学 |
| `onyx` | 成熟男声，低沉共鸣，沉稳有力 | 纪录片旁白、正式场合 |
| `nova` | 年轻女声，活泼明亮，充满热情 | 儿童互动、教育内容 |
| `sage` | 沉稳女声，冷静安抚，温和权威 | 咨询指导、教学 |
| `shimmer` | 柔和女声，温柔治愈，如姐姐讲故事 | 睡前故事、安抚场景 |
| `verse` | 清晰男声，吐字精准，表现力强 | 专业配音、新闻播报 |
| `marin` | 自然女声，亲切接地气，真实温暖 | 生活分享、社交内容 |
| `cedar` | 稳重男声，成熟可靠，不疾不徐 | 商务沟通、导师指导 |
| `kids` | 活泼女声，童趣十足，如幼教老师 | 幼儿互动、儿童教育 |

> `kids` 为自定义扩展 voice ID，非 OpenAI 官方预设。

## 支持的输出格式

- `mp3` (默认)
- `opus` (48kHz/64kbps，适合语音消息)
- `wav`
- `flac`
- `aac`

## 部署

### 构建镜像

```bash
# 1. 先构建 Nano-vLLM 基础镜像 (从 nanovllm-voxcpm 仓库)
docker build -f deployment/Dockerfile -t nano-vllm-voxcpm-deployment:latest .

# 2. 构建含 adapter 的最终镜像
cd applications/voxcpm2-tts
docker build -t voxcpm2-tts:latest .
```

### 部署到 EKS

```bash
kubectl apply -f voxcpm2-tts-deployment.yaml
```

## API 调用示例

```bash
curl -X POST http://<service>/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tts-1",
    "input": "你好，欢迎使用 VoxCPM2 语音合成服务！",
    "voice": "nova",
    "response_format": "mp3"
  }' \
  --output speech.mp3
```

## 硬件要求

- GPU: NVIDIA L4 / T4 / A10G (至少 22GB 显存，与 ASR 共享)
- 模型显存: ~8GB，gpu_memory_utilization 设为 0.45 (~10GB)
- 与 Qwen3-ASR 共享同一 GPU (time-slicing)，各 0.45，留余量给 CUDA context
- 推理延迟: 短文本 ~1.5s, 中等文本 ~6s (L4, 非流式)
