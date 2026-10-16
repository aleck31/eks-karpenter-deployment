# R2T2 ASR 接口参考

遵循 OpenAI Audio API 的 [`/v1/audio/transcriptions`](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions)
与 Realtime API（GA）的[转写会话](https://developers.openai.com/api/docs/guides/realtime-transcription)。
可直接使用官方 SDK：`OpenAI(base_url="http://<host>:8000/v1", api_key="<任意>")`。
已用 `openai` Python SDK 3.31 验证两种接口。

与 OpenAI 的差异见文末。

## POST /v1/audio/transcriptions

multipart/form-data：

| 字段 | 说明 |
|------|------|
| `file` | 必填。任意 ffmpeg 可解码的音频 |
| `model` | 接受并忽略（只有一个模型） |
| `language` | ISO-639-1/3 代码：`zh`、`en`（及 `cmn`、`zh-cn` 等）；不填自动识别 |
| `prompt` | 上下文提示，传给模型 |
| `response_format` | `json`（默认）、`text`、`verbose_json`、`srt`、`vtt` |
| `stream` | `true` 时以 SSE 返回 `transcript.text.delta` / `transcript.text.done`（仅 `json`、`text`） |
| `temperature` | 接受并忽略（贪心解码） |

响应：

```json
{"text": "……", "usage": {"type": "duration", "seconds": 83}}
```

`verbose_json` 另含 `task`、`language`、`duration` 与 `segments`（`id`、`start`、`end`、`text`）。
音频在停顿处切段，`segments` 即这些段。长度无上限。

错误体为 `{"error": {"message", "type", "param", "code"}}`。后端不可用时返回 503 与 `Retry-After`。

## WS /v1/realtime（转写会话）

连接后收到 `session.created`。会话默认值即 GA 默认：`audio/pcm` 24 kHz、`server_vad`。

```json
{"type": "session.update", "session": {
  "type": "transcription",
  "audio": {"input": {
    "format": {"type": "audio/pcm", "rate": 24000},
    "transcription": {"model": "gpt-4o-transcribe", "language": "zh", "prompt": ""},
    "turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 200}
  }}
}}
```

| 字段 | 说明 |
|------|------|
| `format` | 仅 `audio/pcm`，`rate` 为 24000 或 16000 |
| `transcription.language` / `languages` | `zh`、`en`；二者只能给一个，多种语言时自动识别 |
| `transcription.prompt` | 上下文提示 |
| `turn_detection` | `{"type": "server_vad", ...}` 或 `null`（客户端 commit） |

### 客户端事件

| 事件 | 说明 |
|------|------|
| `session.update` | 见上 |
| `input_audio_buffer.append` | `{"audio": "<base64 PCM16>"}` |
| `input_audio_buffer.commit` | 结束当前轮次；buffer 为空时返回 `input_audio_buffer_commit_empty` 错误 |
| `input_audio_buffer.clear` | 丢弃当前轮次，回复 `input_audio_buffer.cleared` |

### 服务端事件

| 事件 | 说明 |
|------|------|
| `session.created` / `session.updated` | 当前会话配置 |
| `input_audio_buffer.speech_started` / `speech_stopped` | 带 `audio_start_ms` / `audio_end_ms`、`item_id` |
| `input_audio_buffer.committed` | 轮次提交（客户端 commit 或 VAD），带 `item_id`、`previous_item_id` |
| `conversation.item.input_audio_transcription.delta` | `item_id`、`content_index`、`delta` |
| `conversation.item.input_audio_transcription.completed` | `item_id`、`content_index`、`transcript`、`languages: [{"code": "zh"}]` |
| `error` | `error: {type, code, message, param, event_id}`；`backend_unavailable` 后以 1013 关闭 |

所有服务端事件带 `event_id`。用 `item_id` 把 delta、committed、completed 对应到同一轮次。

## 与 OpenAI 的差异

- **delta 只追加、不改写**，在说话过程中即开始发送（早于该轮的 committed），`completed.transcript` 等于该轮 delta 依次拼接。
- **`turn_detection: null` 时**，客户端长时间不 commit，服务端会在轮次满 15 秒后的停顿处、或 30 秒时自行提交（发 `committed` 后接 `completed`），以限制单轮计算量。
- 不支持：`audio/pcmu`、`audio/pcma`、`semantic_vad`、`noise_reduction`、`include`（logprobs）、`keywords`、`delay`、说话人分离、词级时间戳。字段若出现则忽略，格式与 turn_detection 类型不支持时报错。
- 仅支持中文与英文。

## 其他

| 接口 | 说明 |
|------|------|
| `GET /v1/models` | 返回 `confucius4-r2t2` |
| `GET /v1/realtime` | 本协议的机器可读说明 |
| `GET /health` | 进程存活；推理引擎故障时 503 |
| `GET /ready` | 模型加载与预热完成前 503 |
