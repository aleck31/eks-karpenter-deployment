# R2T2 ASR 接口参考

遵循 OpenAI Audio API 的 [`/v1/audio/transcriptions`](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions)
与 Realtime API（GA）的[转写会话](https://developers.openai.com/api/docs/guides/realtime-transcription)。
可直接使用官方 SDK：`OpenAI(base_url="https://<host>/v1", api_key="<任意>")`，实时会话为 `wss://<host>/v1/realtime`（443，不带端口）。
只接受 HTTPS：`<host>` 须为证书覆盖的自定义域名，ALB 默认的 `*.elb.amazonaws.com` 名字证书不匹配。

## 认证

与 OpenAI 相同：`Authorization: Bearer <API key>`，SDK 的 `api_key` 即此值。WebSocket 同样用该请求头；
浏览器无法设置 WebSocket 请求头，可按 OpenAI 的做法改用子协议
`["realtime", "openai-insecure-api-key.<API key>"]`。`/health`、`/ready` 不需要 key。

缺失或错误时返回 401，带 `WWW-Authenticate: Bearer`，`error.code` 为 `missing_api_key` 或
`invalid_api_key`。WebSocket 在握手阶段即返回 401，不建立连接。

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
| `response_format` | `json`（默认）、`text`、`verbose_json`、`srt`、`vtt`、`diarized_json` |
| `stream` | `true` 时以 SSE 返回 `transcript.text.delta` / `transcript.text.done`（仅 `json`、`text`） |
| `temperature` | 接受并忽略（贪心解码） |
| `timestamp_granularities[]` | `segment`（默认）、`word`，可同时给；仅 `verbose_json`。`word` 时返回 `words` |
| `chunking_strategy` | 接受并忽略：上传总是在停顿处切段识别 |
| `max_speakers` | 扩展，仅 `diarized_json`。1-8，可能发言的最多人数；多出的说话人并入最相近的一位。给出时不做 8 人上限的声纹核对 |
| `known_speaker_names[]` / `known_speaker_references[]` | 尚不支持，给出时返回 400 |

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

### diarized_json：说话人分离

与 OpenAI `gpt-4o-transcribe-diarize` 的返回形状相同：

```json
{"task": "transcribe", "duration": 4507.1, "text": "……",
 "segments": [{"type": "transcript.text.segment", "id": "seg_0", "start": 1.36, "end": 5.44,
               "text": "州长办公室表示，伤者中有十九人是警察。", "speaker": "A"}, …],
 "usage": {"type": "duration", "seconds": 4508}}
```

- `speaker` 为 `A`、`B`、…，按首次发言的顺序编号；同一录音内同一人始终是同一字母。
- 每个识别出的词归给当时正在说话的人（多人重叠时取覆盖该词最多的一位，无人时取最近的一段），
  段在句末和说话人切换处断开，超过 7 秒再按逗号或停顿切，与 `verbose_json` 的字幕段一致。
- **每个文件最多分出 8 人**（模型的输出通道数）。8 个通道都用上时（未给 `max_speakers`），
  每个通道的单人语音用 CAM++ 声纹再核对一遍：不像该通道主声音的部分，以及长于 1 秒却不在任何
  通道里的语音，`speaker` 记为 `"unknown"`，响应顶层多一个 `warnings`（见下）。这能减少多出的人
  被记到别人名下，但不能消除，多出的人也不会各自得到标签。实测 AliMeeting 拼接的 12 人录音：
  多出 4 人的语音 44% 标为 `unknown`，其余仍记在某个字母上（多为短句、与他人重叠或紧贴他人发言）；
  8 位被分出的人的语音没有被标 `unknown`。8 人对照录音中也没有。会议的本地与远端两侧仍建议
  分别作为两个文件提交。
- 远端声音若被本地麦克风再次录入（未做回声消除的混合音轨），同一人会被拆成两个同时发言的
  说话人。
- 两人同时说话时只有一份文字稿，较轻的一方通常识别不出来，因而也无法归属。
- 耗时：说话人分离在 CPU 上与识别并行，总耗时约为二者中较长的一个；实测 64 分钟录音 100 秒，75 分钟 104 秒。
  8 个通道用满时声纹核对另加 20-35 秒（50-75 分钟录音）。
- 不支持 `stream`；部署未启用说话人分离时返回 400 `unsupported_value`。

**与 OpenAI 规范的差异**（只在 8 个通道用满时出现）：

```json
{"task": "transcribe", "duration": 4500.0, "text": "……",
 "segments": [{"type": "transcript.text.segment", "id": "seg_7", "start": 61.2, "end": 63.0,
               "text": "……", "speaker": "unknown"}, …],
 "usage": {"type": "duration", "seconds": 4500},
 "warnings": [{"code": "speaker_limit_reached",
               "message": "The diarizer used all 8 of its speaker slots. If more people spoke, speech that matched none of them is labelled \"unknown\"."}]}
```

- `speaker: "unknown"` 类型仍是字符串，SDK 照常解析；但 OpenAI 文档只说标签是 `A`、`B`、…
  或 `known_speaker_names[]` 中的名字。按字母推算人数或下标的客户端需把 `unknown` 单独处理；
  它代表所有未能归属的人，不是一个人。
- `warnings` 是扩展字段，OpenAI 的 `TranscriptionDiarized` 没有。openai-python 的模型允许额外字段
  （`resp.warnings` 或 `resp.model_extra["warnings"]`），openai-go 在 `resp.JSON.ExtraFields["warnings"]`；
  严格按 schema 拒绝未知字段的客户端会出错。

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
- 超过 90 秒的音频分窗对齐：先识别、用识别结果确定文本在各窗的位置，再在每窗内
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
| `turn_detection.silence_duration_ms` | 静音多久结束一轮，默认 **500**（同 OpenAI）。语音后收到这么长的静音即结束（VAD 自身约 70 ms 的检测延迟已扣除，故比设定值短约 70 ms 的停顿也可能断轮）。调大则轮次更长、`completed` 更晚，delta 不受影响 |
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
| `input_audio_buffer.speech_started` / `speech_stopped` | 带 `audio_start_ms` / `audio_end_ms`、`item_id`。时间为客户端音频中的位置（commit 时内部补的静音不计入），长会话不漂移；`audio_end_ms` 是语音结束处，事件本身在其后 `silence_duration_ms` 才发出。`speech_started` 带的是它开启的那一轮的 `item_id`，在上一轮 `completed` 之后发出 |
| `input_audio_buffer.committed` | 轮次提交（客户端 commit 或 VAD），带 `item_id`、`previous_item_id` |
| `conversation.item.input_audio_transcription.delta` | `item_id`、`content_index`、`delta` |
| `conversation.item.input_audio_transcription.completed` | `item_id`、`content_index`、`transcript`、`languages: [{"code": "zh"}]` |
| `error` | `error: {type, code, message, param, event_id}`；`backend_unavailable` 后以 1013 关闭 |

所有服务端事件带 `event_id`。用 `item_id` 把 delta、committed、completed 对应到同一轮次。

**长轮次的切分**：一轮连续说话超过 15 秒后，在第一个语音概率低于阈值的帧（词间或句间的短停顿）切开；到 20 秒仍无停顿，则在最后 1 秒内语音概率最低处切开（这 1 秒在切点确定前暂不解码，delta 会晚到至多约 1.5 秒）。切开处被切的一轮先 `speech_stopped`（`audio_end_ms` 为切点）再 `completed`（无识别文字时 `transcript` 为空），之后才是下一轮的 `speech_started`；话没停时它的 `audio_start_ms` 就等于切点，若切点处其实是一段停顿的开头，则取停顿后真正开口的位置。只在 `server_vad` 下切分；`turn_detection: null` 时由客户端 commit 决定。

VAD 每攒够 50 ms 音频运行一次；客户端停止发送 100 ms 后会把剩余音频也跑完，所以轮次在静音收齐后至多约 100 ms 内结束。

## 与 OpenAI 的差异

- **delta 只追加、不改写**，在说话过程中即开始发送（早于该轮的 committed），`completed.transcript` 等于该轮 delta 依次拼接。
- **`turn_detection: null` 时**，客户端长时间不 commit，服务端会在轮次满 15 秒后的停顿处、或 30 秒时自行提交（发 `committed` 后接 `completed`），以限制单轮计算量。
- 不支持：`audio/pcmu`、`audio/pcma`、`semantic_vad`、`noise_reduction`、`include`（logprobs）、`keywords`、`delay`。字段若出现则忽略，格式与 turn_detection 类型不支持时报错。
- `diarized_json`：最多 8 人；`known_speaker_names[]` / `known_speaker_references[]` 尚不支持（返回 400）；`chunking_strategy` 忽略；`prompt` 仍然生效（OpenAI 的 diarize 模型不支持）；`max_speakers` 为扩展。
- 仅支持中文与英文。

## 其他

| 接口 | 说明 |
|------|------|
| `GET /v1/models` | 返回 `confucius4-r2t2` |
| `GET /v1/realtime` | 本协议的机器可读说明 |
| `GET /health` | 进程存活；推理引擎故障时 503 |
| `GET /ready` | 模型加载与预热完成前 503 |
