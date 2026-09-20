# Qwen3 语音套件接口参考

部署与运维见 [deployment-guide.md](deployment-guide.md)。

## ASR 非流式 (HTTP)

上传完整音频文件，返回转录文本：

```bash
curl -X POST http://<ALB>:8000/v1/audio/transcriptions \
  -F "file=@audio.wav"
```

响应：
```json
{"text": "Hello world.", "language": "english", "duration": 2.5}
```

**长度无上限。** 超过 150 秒的音频由 adapter 用 silero-vad 在语音间隙切分，
每段不超过 120 秒，并发 4 段转写后按输入顺序拼接。发生切分时多返回 `segments`：

```json
{"text": "...", "language": "chinese", "duration": 3600.0, "segments": 31}
```

实测一小时录音 31 段、95 秒返回。`duration` 由 ffprobe 测得，始终存在（可探测时）。

超时预算随时长伸缩：`max(300s, 时长 × 0.35)`。一小时音频为 1260 秒，
在 ALB 的 1800 秒 idle timeout 之内。

| 情况 | 状态 | 响应 |
|------|------|------|
| 超时 | 504 | `{"error": {"type": "timeout", ...}}` |
| 后端拒绝超长音频 | 400 | `{"error": {"type": "audio_too_long", "limit_seconds": N}}` |
| 后端其它错误 | 502 | `{"error": {"type": "backend_error"}}`，不泄露内部地址 |

`audio_too_long` 正常情况下不会出现（分段已消除长度限制），它是后端参数被
调小后的兜底。`limit_seconds` 按实测的 13.0 audio token/秒 换算得出。

`GET /health` 与 `GET /ready` 的区别：前者只表示 adapter 存活，
后者确认 vLLM 后端可达。**startupProbe 探的是 adapter 的 `:8001`**，
所以 Pod 显示 `1/1 Running` 时 vLLM 仍可能已死，排障时用 `/ready` 或直连 `:8000`。

## ASR 流式 (WebSocket Realtime)

实时语音识别，边录边转录。

**端点**: `ws://<ALB>:8000/v1/realtime`

**音频格式**: PCM16, 16kHz, mono, base64 编码，建议每块 4KB

### 协议说明

每个转录段由三步组成：

1. **`commit`**（不带 final）— 开始一个新的转录段，初始化 buffer
2. **`append`**（可多次）— 持续发送音频数据
3. **`commit {final: true}`** — 结束当前段，触发转录，**buffer 自动清空**

| 事件 | 方向 | 说明 |
|------|------|------|
| `session.created` | ← Server | 连接建立确认 |
| `session.update` | → Client | 验证模型（必须） |
| `input_audio_buffer.commit` | → Client | 开始新段 |
| `input_audio_buffer.append` | → Client | 发送音频块 |
| `input_audio_buffer.commit {final:true}` | → Client | 结束段，触发转录 |
| `transcription.delta` | ← Server | 流式 token 片段 |
| `transcription.done` | ← Server | 段最终结果（只发 1 次） |

### 关键行为（实测验证）

- **buffer 不累积**：每次 `commit {final: true}` 后 buffer 自动清空，下一段从零开始
- **必须有初始 commit**：append 之前必须先发一个不带 final 的 commit，否则音频不被处理
- **单连接可复用**：不需要每段重连，在同一连接中循环 commit→append→final 即可
- **每段只有 1 个 done**：收到 `transcription.done` 即表示本段结束
- **无 VAD**：服务端不会自动断句，分段完全由客户端决定
- **没有 `input_audio_buffer.clear`**：不需要手动清空，final 自动处理

### 稳定性依赖后端的 `--no-async-scheduling`

vLLM 的 AsyncScheduler 在流式分段的最后一个 prefill chunk 于生成中途追加时，
会让 `num_output_placeholders` 减到负数，断言失败并带走 EngineCore
（[vllm-project/vllm#35755](https://github.com/vllm-project/vllm/issues/35755)）。
表现是转录文本重复、随后 WebSocket 返回 `processing_error`，Pod 重启。

实测对照（19 秒音频）：

| 配置 | 断言失败 | 结果 |
|------|---------|------|
| async scheduling 开 | 4 次 | EngineCore 崩溃，四句只转出三句 |
| `--no-async-scheduling` | 0 次 | 文本完整 |

该缺陷在 vLLM main 分支仍未修复，修复 PR 未合并，**升级版本无效**。
Deployment 的 `--no-async-scheduling` 不可移除，代价是吞吐下降 3-8%
（并发 1/2/4/8 下实测 2.52→2.35、4.56→4.20、9.10→8.43、17.06→16.60 req/s）。

修复后实测 180 秒音频稳定、零崩溃，是上游报告崩溃阈值的 11 倍。

### 完整调用示例：连续 3 句话，得到 3 条独立结果

```
→ [建立 WebSocket 连接]
← {"type": "session.created", "id": "sess-xxx"}

→ {"type": "session.update", "model": "/models/Qwen3-ASR-1.7B"}

=== 句子 1 ===
→ {"type": "input_audio_buffer.commit"}
→ {"type": "input_audio_buffer.append", "audio": "<base64>"}
→ {"type": "input_audio_buffer.append", "audio": "<base64>"}
→ {"type": "input_audio_buffer.commit", "final": true}
← {"type": "conversation.item.input_audio_transcription.delta", "delta": "你"}
← {"type": "conversation.item.input_audio_transcription.delta", "delta": "好"}
← {"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好", "language": "chinese"}

=== 句子 2（buffer 已自动清空）===
→ {"type": "input_audio_buffer.commit"}
→ {"type": "input_audio_buffer.append", "audio": "<base64>"}
→ {"type": "input_audio_buffer.commit", "final": true}
← {"type": "conversation.item.input_audio_transcription.delta", "delta": "谢谢"}
← {"type": "conversation.item.input_audio_transcription.completed", "transcript": "谢谢", "language": "chinese"}

=== 句子 3 ===
→ {"type": "input_audio_buffer.commit"}
→ {"type": "input_audio_buffer.append", "audio": "<base64>"}
→ {"type": "input_audio_buffer.commit", "final": true}
← {"type": "conversation.item.input_audio_transcription.delta", "delta": "再见"}
← {"type": "conversation.item.input_audio_transcription.completed", "transcript": "再见", "language": "chinese"}
```

> 注：通过 adapter 时，事件名为 OpenAI 格式（`conversation.item.input_audio_transcription.*`）。
> 直连 vLLM 后端时为原始格式（`transcription.delta` / `transcription.done`）。

### 常见错误

| 错误用法 | 现象 | 原因 |
|----------|------|------|
| 没发初始 commit 直接 append | timeout 无响应 | buffer 未初始化 |
| commit 不带 final | timeout 无响应 | 不带 final 不触发转录 |
| final 后直接 append（不重新 commit） | timeout 或结果异常 | buffer 已清空但未重新初始化 |

### 单段长度限制

- max-model-len: 16384 tokens
- 约可处理 5-6 分钟连续音频/段
- 建议每段 30 秒 - 2 分钟，用客户端 VAD 检测静音切分

### Python 客户端示例

```python
import asyncio
import base64
import json
import websockets
import numpy as np

async def transcribe_segments(audio_segments: list[bytes], server_url: str):
    """
    audio_segments: list of PCM16 16kHz mono bytes
    """
    async with websockets.connect(server_url) as ws:
        await ws.recv()  # session.created
        await ws.send(json.dumps({
            "type": "session.update",
            "model": "/models/Qwen3-ASR-1.7B"
        }))

        results = []
        for seg in audio_segments:
            # 开始段
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

            # 分块发送 (4KB/块)
            for i in range(0, len(seg), 4096):
                chunk = base64.b64encode(seg[i:i+4096]).decode()
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": chunk
                }))

            # 结束段
            await ws.send(json.dumps({
                "type": "input_audio_buffer.commit",
                "final": True
            }))

            # 等待结果
            async for msg in ws:
                data = json.loads(msg)
                if data["type"] == "transcription.done":
                    results.append(data["text"])
                    break

        return results
```

## 快速调用示例

本环境 TTS 使用 Breeze TTS 2（见 `applications/breeze2-tts/`），以下 TTS 示例
适用于部署了 Qwen3-TTS 的环境。

```bash
ALB=<ALB_DNS_NAME>  # kubectl get ingress -n <namespace> 查看实际地址

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

## TTS 参数说明

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
