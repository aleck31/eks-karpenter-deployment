# VoxCPM2 TTS - OpenAI 兼容 TTS 服务

基于 VoxCPM2 + Nano-vLLM 推理引擎，通过 OpenAI 兼容 adapter 对外提供 `/v1/audio/speech` 等语音合成接口。

## 架构

```
下游应用 → ALB :8880 → adapter (OpenAI API) → localhost:8000 → VoxCPM2 (Nano-vLLM)
                        /v1/audio/speech              /generate
                        /v1/audio/clone
                        /v1/audio/voices
```

单容器内运行两个进程：
- Nano-vLLM backend (:8000) — GPU 推理引擎
- OpenAI adapter (:8880) — 接口转换 + Voice 管理 + 音频格式转码

## Voice 系统

### 两种模式

1. **Controllable Cloning**（已注册 voice）— 使用参考音频克隆音色，声音一致性高
2. **Voice Design**（未注册 voice）— 使用文字描述生成声音，每次可能略有差异

已注册的 voice 优先走 Cloning 模式。当 voice ID 不在注册表中时，fallback 到 Voice Design。

### 预置 Voice

| Voice | 特点 | 适合场景 |
|-------|------|---------|
| `alloy` | 年轻女声，清晰平衡，自然自信 | 通用旁白、一般对话 |
| `ash` | 年轻男声，自信直接，略带沙哑 | 科技播客、产品介绍 |
| `ballad` | 温暖男声，富有表现力，旋律感强 | 故事讲述、情感内容 |
| `cedar` | 稳重男声，成熟可靠，不疾不徐 | 商务沟通、导师指导 |
| `coral` | 友好女声，明亮对话风格，自然亲和 | 日常对话、客服 |
| `echo` | 年轻男声，平滑温暖，轻松随和 | 深夜电台、冥想引导 |
| `fable` | 英式绅士，深沉权威，节奏从容 | 有声书、经典文学 |
| `marin` | 自然女声，亲切接地气，真实温暖 | 生活分享、社交内容 |
| `nova` | 年轻女声，活泼明亮，充满热情 | 儿童互动、教育内容 |
| `onyx` | 成熟男声，低沉共鸣，沉稳有力 | 纪录片旁白、正式场合 |
| `sage` | 沉稳女声，冷静安抚，温和权威 | 咨询指导、教学 |
| `shimmer` | 柔和女声，温柔治愈，如姐姐讲故事 | 睡前故事、安抚场景 |
| `verse` | 清晰男声，吐字精准，表现力强 | 专业配音、新闻播报 |

### Voice 管理 API

```bash
# 列出所有 voice
GET /v1/audio/voices

# 注册新 voice（上传参考音频，自动归一化为 16kHz mono -16 LUFS）
POST /v1/audio/voices
  Form: voice_id, name, description, audio(file)

# 查询单个 voice
GET /v1/audio/voices/{voice_id}

# 更新 voice（替换音频或修改信息）
PUT /v1/audio/voices/{voice_id}

# 删除 voice
DELETE /v1/audio/voices/{voice_id}

# 试听参考音频
GET /v1/audio/voices/{voice_id}/preview
```

参考音频要求：3-10 秒，干净无噪音，自然说话即可。上传时自动归一化。

## 支持的输出格式

- `mp3` (默认)
- `opus` (48kHz/64kbps，适合语音消息)
- `wav`
- `flac`
- `aac`

## API 调用示例

### 文本转语音

```bash
curl -X POST http://<ALB>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "input": "你好，欢迎使用语音合成服务！",
    "voice": "nova",
    "response_format": "mp3"
  }' --output speech.mp3
```

### 流式输出

```bash
curl -X POST http://<ALB>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "input": "流式模式下客户端能更快开始播放",
    "voice": "nova",
    "stream": true
  }' --output speech.mp3
```

注意: 流式模式仅输出 MP3，不支持格式转码。

### CFG 控制

`cfg_value` 参数控制模型遵循参考音色/描述的程度（默认 1.5）：

```bash
curl -X POST http://<ALB>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "input": "短句用高 CFG 更清晰",
    "voice": "alloy",
    "cfg_value": 2.0
  }' --output speech.mp3
```

- 短句 → 提高 CFG（更清晰）
- 长文本 → 降低 CFG（更稳定自然）
- 声音奇怪 → 降低 CFG

### 声音克隆（传入参考音频）

无需注册，直接传 base64 音频：

```bash
curl -X POST http://<ALB>:8880/v1/audio/clone \
  -H "Content-Type: application/json" \
  -d '{
    "input": "用克隆的声音说这段话",
    "reference_audio": "<base64 encoded wav>",
    "reference_format": "wav",
    "response_format": "mp3"
  }' --output cloned.mp3
```

### 注册自定义 Voice

```bash
curl -X POST http://<ALB>:8880/v1/audio/voices \
  -F "voice_id=my_voice" \
  -F "name=My Custom Voice" \
  -F "description=温柔的女声" \
  -F "audio=@reference.wav"
```

注册后即可在 `/v1/audio/speech` 中使用 `"voice": "my_voice"`。

## 存储

- 模型: EFS `/shared/VoxCPM2/`
- Voice 参考音频: EFS `/shared/voices/{voice_id}/ref.wav`
- Voice 注册表: EFS `/shared/voices/registry.json`

## 部署

### 构建镜像

```bash
# 构建 adapter 层（~3秒）
DOCKER_BUILDKIT=1 docker buildx build -t voxcpm2-tts:latest --load .
docker tag voxcpm2-tts:latest <account>.dkr.ecr.us-west-2.amazonaws.com/voxcpm2-tts:latest
docker push <account>.dkr.ecr.us-west-2.amazonaws.com/voxcpm2-tts:latest
```

### 部署到 EKS

```bash
# 先删旧 Pod 再部署新的，避免 GPU 争抢
kubectl scale deployment voxcpm2-tts -n hosthree --replicas=0
# 待旧 Pod 终止后
kubectl apply -f voxcpm2-tts-deployment.yaml
kubectl scale deployment voxcpm2-tts -n hosthree --replicas=1
```

## 硬件配置

- GPU: NVIDIA L4 (g6.xlarge Spot)
- 模型显存: ~9.6GB (gpu_memory_utilization: 0.45, max_model_len: 8192)
- 推理延迟: 短文本 ~1.5s, 中等文本 ~6s (非流式); 流式 TTFB ~0.37s
