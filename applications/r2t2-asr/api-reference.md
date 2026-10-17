# R2T2 ASR 接口参考

遵循 OpenAI Audio API 的 [`/v1/audio/transcriptions`](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions)
与 Realtime API（GA）的[转写会话](https://developers.openai.com/api/docs/guides/realtime-transcription)。
可直接使用官方 SDK：`OpenAI(base_url="http://<host>:8000/v1", api_key="<任意>")`。

**兼容基线**：OpenAI 官方 API Reference 的 GA 规范（按 2026-10 对齐），已用 `openai`
Python SDK **3.31.0** 实测 HTTP 与 Realtime 两种接口。Realtime 只实现 GA 协议
（`session.update` + `session.type: "transcription"`）；Beta 协议
（`OpenAI-Beta: realtime=v1` 头、`transcription_session.update` 事件）不支持，
后者返回 `invalid_event` 错误。

与 OpenAI 的差异见文末。

## 选哪种用法

FLEURS 普通话 / 英文实测字错误率（`openai` Python SDK 3.31.0，测法与明细见
[deployment-guide.md](deployment-guide.md#实测fleurs-普通话--英文)）：

| 用法 | 中文短句 | 英文短句 | 中文长段 | 英文长段 |
|------|------|------|------|------|
| HTTP 批处理 | 5.56% | 7.44% | 6.26% | 5.80% |
| 实时，server_vad 默认参数 | 5.50% | 8.61% | 6.26% | 8.07% |
| 实时，客户端 commit | 5.62% | 8.82% | 6.63% | 11.39% |

- 录音已完整在手：用 HTTP，英文明显更准。
- 实时：用 server_vad 默认参数（`silence_duration_ms` 500）。客户端 commit 的长段
  明显更差；只在需要自己控制轮次边界时用。
- 实时首字约 1.4-2.4 秒，轮次结束后约 0.3-0.4 秒给出 `completed`；delta 只追加、不回改。
- 调大 `silence_duration_ms` 会让轮次更长、`completed` 更晚；在中文测试句上 500 与
  1300 ms 识别结果相同，更大的值未在 FLEURS 上测过。

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
| `timestamp_granularities[]` | `segment`（默认）、`word`，可同时给；仅 `verbose_json`。`word` 时返回 `words` |

响应：

```json
{"text": "……", "usage": {"type": "duration", "seconds": 83}}
```

`verbose_json` 另含 `task`、`language`、`duration` 与 `segments`（`id`、`start`、`end`、`text`）；
`timestamp_granularities[]=word` 时另含 `words`（`word`、`start`、`end`，单位秒）。长度无上限。

`verbose_json` / `srt` / `vtt` 的 `segments` 是**字幕粒度**：按识别文本的句末标点切句，
超过 7 秒的句子再在逗号或词间停顿处切开；起止时间取该段首词与末词的强制对齐时间。
`words` 的单位是中文单字、英文单词，不含标点；时间分辨率 80 ms。识别仍按停顿切成
10-30 秒的段进行（切短会降低准确率），对齐在识别之后另做一遍，每分钟音频约增加 0.15 秒。
部署未加载对齐模型时，`segments` 退回识别段，请求 `word` 返回 400 `unsupported_value`。

```json
{"task": "transcribe", "language": "chinese", "duration": 72.18, "text": "……",
 "words": [{"word": "州", "start": 1.36, "end": 1.6}, …],
 "segments": [{"id": 0, "start": 1.36, "end": 5.44, "text": "州长办公室表示，伤者中有十九人是警察。"}, …],
 "usage": {"type": "duration", "seconds": 73}}
```

## POST /v1/audio/alignments（扩展）

已知文本的强制对齐：给出音频与其中说的文本，返回每句、每词的时间。**不做识别**，
返回的文字就是传入的文本，不会出现识别错误。用于给合成语音或已有文稿的录音配字幕、
跟读高亮。OpenAI 没有对应接口。

| 字段 | 说明 |
|------|------|
| `file` | 必填。任意 ffmpeg 可解码的音频 |
| `text` | 必填。音频中说的文本，可含标点（用于切句） |
| `language` | `zh`、`en`；不填按文本是否含汉字判断 |
| `response_format` | `json`（默认）、`srt`、`vtt` |
| `timestamp_granularities[]` | `segment`（默认）、`word`，可同时给 |

```json
{"duration": 14.6, "language": "zh", "text": "……",
 "segments": [{"id": 0, "start": 0.0, "end": 3.12, "text": "有一种说法认为，早起的人效率更高。"}, …],
 "words": [{"word": "有", "start": 0.0, "end": 0.24}, …]}
```

- `segments` 的切法与转写相同（句末标点，超 7 秒再按逗号或停顿切）。
- 同步返回：14.6 秒合成语音约 1.3 秒，72 秒录音约 0.8 秒，150 秒约 4 秒。
- 超过 120 秒的音频分窗对齐：先识别、用识别结果确定文本在各窗的位置，再在每窗内
  对齐给定文本。整段直接对齐在 217 秒处已漂移约 9 秒。
- 文本须与所说内容一致。不一致处（如文本写 `nineteen`、实际说 `19`，或文本有
  音频里没有的话）时间会偏，偏差局限在不一致的附近。
- 未加载对齐模型时返回 503 `aligner_unavailable`。

错误体为 `{"error": {"message", "type", "param", "code"}}`。后端不可用时返回 503 与 `Retry-After`。

## WS /v1/realtime（转写会话）

连接后收到 `session.created`。会话默认值即 GA 默认：`audio/pcm` 24 kHz、`server_vad`。

```json
{"type": "session.update", "session": {
  "type": "transcription",
  "audio": {"input": {
    "format": {"type": "audio/pcm", "rate": 24000},
    "transcription": {"model": "gpt-4o-transcribe", "language": "zh", "prompt": ""},
    "turn_detection": {"type": "server_vad", "silence_duration_ms": 500}
  }}
}}
```

| 字段 | 说明 |
|------|------|
| `format` | 仅 `audio/pcm`，`rate` 为 24000 或 16000 |
| `transcription.language` / `languages` | `zh`、`en`；二者只能给一个，多种语言时自动识别 |
| `transcription.prompt` | 上下文提示 |
| `turn_detection` | `{"type": "server_vad", ...}` 或 `null`（客户端 commit） |
| `turn_detection.silence_duration_ms` | 静音多久结束一轮，默认 **500**（同 OpenAI）。调大则轮次更长、`completed` 更晚，delta 不受影响 |
| `turn_detection.prefix_padding_ms` | 接受并回显，默认 300。轮次音频首尾相接，前一轮结束点之后的音频全部归下一轮，不会丢段首 |
| `turn_detection.threshold` | FireRed VAD 的语音概率阈值，默认 0.4 |

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
| `input_audio_buffer.speech_started` / `speech_stopped` | 带 `audio_start_ms` / `audio_end_ms`、`item_id`。时间为客户端音频中的位置（commit 时内部补的静音不计入）；`audio_end_ms` 是语音结束处，事件本身在其后 `silence_duration_ms` 才发出 |
| `input_audio_buffer.committed` | 轮次提交（客户端 commit 或 VAD），带 `item_id`、`previous_item_id` |
| `conversation.item.input_audio_transcription.delta` | `item_id`、`content_index`、`delta` |
| `conversation.item.input_audio_transcription.completed` | `item_id`、`content_index`、`transcript`、`languages: [{"code": "zh"}]` |
| `error` | `error: {type, code, message, param, event_id}`；`backend_unavailable` 后以 1013 关闭 |

所有服务端事件带 `event_id`。用 `item_id` 把 delta、committed、completed 对应到同一轮次。

## 与 OpenAI 的差异

- **delta 只追加、不改写**，在说话过程中即开始发送（早于该轮的 committed），`completed.transcript` 等于该轮 delta 依次拼接。
- **`turn_detection: null` 时**，客户端长时间不 commit，服务端会在轮次满 15 秒后的停顿处、或 30 秒时自行提交（发 `committed` 后接 `completed`），以限制单轮计算量。
- 不支持：`audio/pcmu`、`audio/pcma`、`semantic_vad`、`noise_reduction`、`include`（logprobs）、`keywords`、`delay`、说话人分离。字段若出现则忽略，格式与 turn_detection 类型不支持时报错。
- 仅支持中文与英文。

## 其他

| 接口 | 说明 |
|------|------|
| `GET /v1/models` | 返回 `confucius4-r2t2` |
| `GET /v1/realtime` | 本协议的机器可读说明 |
| `GET /health` | 进程存活；推理引擎故障时 503 |
| `GET /ready` | 模型加载与预热完成前 503 |
