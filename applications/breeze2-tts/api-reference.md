# Breeze TTS 2 接口参考

部署与运维见 [deployment-guide.md](deployment-guide.md)。

**兼容基线**：OpenAI Audio API 的 GA 规范（按 2026-10 的官方 API Reference 对齐）——
[Create speech](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)
与 [Create voice](https://developers.openai.com/api/reference/resources/audio/subresources/voices/methods/create)，
错误体为 OpenAI 格式。已用 `openai` Python SDK **3.31.0** 实测 `audio.speech.create`
（含 `with_streaming_response`、`stream_format` audio/sse）与 `audio.voices.create`。
可直接 `OpenAI(base_url="http://<host>:8880/v1", api_key="<任意>")`。
与 OpenAI 的差异见「与 OpenAI 的差异」；其余端点（声纹查询/更新/删除/试听、临时克隆、
忙闲状态）为本服务扩展。

> 不兼容变更：早期版本的字段已移除——`stream` → `stream_format`，
> `voice_description` → `instructions`，注册字段 `audio` → `audio_sample`、`voice_id` 改为可选，
> 错误体不再含 `detail`，未知声纹由 404 改为 400。

Breeze 特有的两个声纹属性：

| 字段 | 位置 | 说明 |
|---|---|---|
| `ref_text` | 声纹属性（注册/更新时提供） | Breeze 从「参考音频 + 其准确文字稿」成对克隆。注册时未给则调用 `ASR_TRANSCRIBE_URL` 自动转写；转写不可用时必填 |
| `seed` | 声纹属性，也可在单次请求覆盖 | 采样种子，取值 **0–4294967295**。固定后端加速参数的前提下可复现，用于选定并锁住满意的音色 |

注册时若不指定 `seed`，写入后端默认值 **42**（`breeze_infer.api` 的
`seed: int = Form(42)`）。显式写入而非留空，是因为留空并不等于"不固定种子" ——
输出仍然是 seeded 的，只是那个值藏在后端函数签名里而不在 registry 里；
上游一旦改动该默认值，所有声纹的音色都会变而无从追溯。

`seed` 上界来自后端 `set_all_seeds()` 同时播种的四个 RNG 中最窄的一个：numpy 的
legacy MT19937 只接受无符号 32 位，先于 torch 抛异常（torch 本身文档化范围是
`[-2**63, 2**64-1]` 且负数会被映射，但这段余量在此不可达）。超范围由 adapter
返回 400 并说明边界，不会落到后端变成 500。

## 合成

```bash
# 使用已注册声纹
curl -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"breeze-tts-2","input":"今天天气不错。","voice":"norman08","response_format":"mp3"}' \
  --output out.mp3

# voice 也可写成对象（OpenAI 自定义声纹形式）；instructions 在保留音色的前提下调整语气
curl -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"breeze-tts-2","input":"今天天气不错。","voice":{"id":"norman08"},"instructions":"语速放慢，语气严肃。"}' \
  --output directed.mp3

# 覆盖 seed 试听（不改动声纹）
curl -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"breeze-tts-2","input":"今天天气不错。","voice":"norman08","seed":7}' --output s7.mp3

# Voice Design（扩展）：省略 voice、只给 instructions，按描述生成
curl -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"breeze-tts-2","input":"欢迎收听。","instructions":"一位温柔自信的年轻女性，声音清晰。","cfg_value":4}' \
  --output design.mp3
```

`speed` 仅接受 `1.0`，其它值返回 400（`param: speed`）：Breeze 未暴露语速控制，
重采样会改变音高。调节语速节奏请用 `instructions`。

## 流式

```bash
curl -N -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"breeze-tts-2","input":"...","voice":"norman08","stream_format":"audio","response_format":"wav"}' \
  --output stream.wav

# SSE：text/event-stream，事件 speech.audio.delta {audio: base64} … speech.audio.done {usage}
curl -N -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"breeze-tts-2","input":"...","voice":"norman08","stream_format":"sse","response_format":"pcm"}'
```

不给 `stream_format` 时整段合成后一次返回。`audio` / `sse` 两种都支持全部格式
（`sse` 的 delta 是所选格式的字节片段，base64 编码）。`speech.audio.done.usage`
中 `input_tokens` 为输入字符数、`output_tokens` 为 0——本模型没有 token 计量。`pcm` / `wav` 直接透传后端 PCM；mp3/opus/aac/flac 经
ffmpeg 管道边收边编码（这几种都是分帧或可流式格式），不会先缓冲完整波形。

`wav` 流式输出的 RIFF 头长度字段为 `0xFFFFFFFF`（长度未知），读到 EOF 为止的
播放器可正常处理。

两种模式都带时长响应头：

| 头 | 模式 | 性质 |
|----|------|------|
| `X-Audio-Duration` | 非流式 | 精确，波形已全部生成 |
| `X-Audio-Duration-Estimate` | 流式 | **估算**，按 3.7 字/秒推算；推流开始时真实时长尚不可知 |
| `X-Audio-Sample-Rate` | 两者 | 24000 |
| `X-Breeze-Segments` | 两者 | 分句数 |

## 长文本

`input` 不设长度上限，adapter 按句切分后逐段合成（见部署指南「已知限制」）。
三种模式实测（32 GB 显存卡，声纹 `alloy`，中文，每档均用 ASR 回转校验内容）：

| 字数 | 音频 | 分段 | RTF | 首字节 | 内容校验 |
|-----:|-----:|----:|----:|-------:|---------|
| 62 | 15.4s | 1 | 0.43–0.55 | 0.50s | 完整 |
| 186 | 39.6s | 2 | 0.52–0.57 | 0.51s | 完整 |
| 372 | 81.1s | 3 | 0.53–0.59 | 0.51s | 完整 |
| 746 | 174.0s | 5 | 0.47–0.52 | 0.55s | 完整 |
| 1250 | 287.9s | 9 | 0.46–0.54 | 0.53s | 完整 |
| 2504 | 567.8s | 17 | 0.49–0.50 | 0.69s | 完整 |

三点可以依赖：

- **RTF 与首字节都不随长度退化。** 分段使每段始终远离后端约 164 秒的上下文上限，
  所以 40 倍长度差下 RTF 仍稳定在 0.43–0.59，首字节只等第一段。
- **三种模式产出同一份音频。** 同一输入下 batch / `audio` / `sse` 的字节数完全
  相同（如 1250 字均为 13820204 字节），`sse` 的 base64 往返无损。
- **无重复、无丢失。** 用带唯一编号标记的文本合成后 ASR 回转，标记逐一命中，
  转写字数与原文去标点后字数之比为 1.000。重复是自回归 TTS 退化的典型表现，
  且数标记查不出来，需另外比对长度。

后端单并发，所以上表的墙钟时间同时是**其它请求的排队时长**：一句话约 7 秒，
1250 字约 137 秒。按此估算容量，必要时由调用方拆分请求。

## 并发：忙时立即拒绝，不排队

后端一次只服务一个请求。**第二个请求不会排队，立即返回 429**，
`Retry-After` 给出进行中请求的预估剩余秒数：

```
HTTP 429
Retry-After: 27
X-Queue-Depth: 1
{"error": {"code": "backend_busy", "status": 429, "message": "..."}}
```

实测 0.35 秒返回。早先的行为是握着连接等满 `BREEZE_QUEUE_TIMEOUT`（120 秒）
再报 503，客户端全程不知道要等多久，与服务挂死无法区分。

发请求前可先自检，避免上传完长文本才被拒：

```bash
curl http://<host>:8880/v1/audio/speech/status
# {"busy":true,"accepting":false,"projected_wait_seconds":26.1,
#  "retry_after_seconds":27,"concurrent_requests":1}
```

预估值由字数 ÷ 3.7 字/秒 × RTF 0.88 推算，只用于决定是否重试，不是承诺。

需要排队语义时（例如宁愿阻塞的批处理客户端）设 `BREEZE_QUEUE_REJECT_SECONDS`
为正数：预估等待在该值以内则排队，超出仍立即拒绝。

**客户端断开即释放后端。** 播放器上的停止按钮、被取消的请求、掉线，都会让
adapter 立刻停止读取后端并释放占用，下一次请求随即可被接受 —— 不必等这次合成
跑完。实测停止后 1 秒重试即返回 200。

这一点依赖构建期打在后端上的补丁：原生 `breeze_infer.api` 在响应被中途放弃时
不释放推理锁，此后所有请求收到 409 直到重启
（[issue #20](https://github.com/breezeblue-ai/breeze-tts/issues/20)，
补丁见 `patches/`）。补丁未生效时 adapter 只能读完当前分段才能释放，
表现为停止后仍有最多一个分段时长的 429。

## 临时克隆（不注册声纹）

参考音频随请求传入，用完即弃，适合一次性试听：

```bash
curl -X POST http://<host>:8880/v1/audio/clone \
  -H "Content-Type: application/json" \
  -d '{"input":"要合成的文本。",
       "reference_audio":"<base64>",
       "reference_format":"wav",
       "prompt_text":"参考音频的准确文字稿。",
       "response_format":"mp3"}' \
  --output out.mp3
```

`prompt_text` 必需 —— Breeze 不接受只有参考音频而无文字稿的请求。
`cfg_value` 与 `seed` 可选。该端点为扩展，不支持流式。

## 字段参考

`POST /v1/audio/speech` 请求体：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `model` | string | 必需 | 按 OpenAI 规范必填，值不参与路由（本服务只有一个模型，id 见 `/v1/models`） |
| `input` | string | 必需 | 待合成文本。超长自动分句，见「已知限制」 |
| `voice` | string \| `{"id"}` | 必需* | 已注册声纹 id。*省略且给出 `instructions` 时为 Voice Design（扩展） |
| `instructions` | string | — | 有 `voice` 时调整语气节奏（Voice Direction）；无 `voice` 时描述要生成的音色，**音色每次调用都不同** |
| `response_format` | string | `mp3` | `mp3` `opus` `aac` `flac` `wav` `pcm`（24 kHz s16le 单声道） |
| `stream_format` | string | — | `audio` 原始音频流，`sse` 事件流；省略则非流式 |
| `speed` | float | `1.0` | 仅支持 `1.0` |
| `seed` | int | 声纹存储值 | 扩展。`0` … `4294967295`，覆盖本次请求，不改动声纹 |
| `cfg_value` | float | — | 扩展。指令遵循强度，转为后端 `--cfg-scale`；上游建议配合 `instructions` 用 4 |

`POST /v1/audio/voices`（OpenAI Create voice，multipart）字段；`PUT` 为扩展，同名字段均可选：

| 字段 | 必需 | 说明 |
|------|------|------|
| `name` | ✅ | 显示名，可含中文 |
| `audio_sample` | ✅ | 参考音频，任意 ffmpeg 可解格式；服务端归一化为 -16 LUFS / 16kHz / 单声道 |
| `consent` | | OpenAI 规范字段，接受但不校验 |
| `ref_text` | | 扩展。参考音频的**准确**文字稿；省略则自动转写，转写不可用时返回 400 `ref_text_required` |
| `voice_id` | | 扩展。`[A-Za-z0-9_][A-Za-z0-9._-]{0,63}`；省略则生成 `voice_<16 位十六进制>` |
| `description` | | 扩展。自由文本备注 |
| `gender` | | 扩展。`male` `female` `unknown`（默认），由调用方声明 |
| `seed` | | 扩展。默认 42（后端默认值，显式写入而非留空） |

返回 Voice 对象（见下表），与 `GET /v1/audio/voices/{id}` 相同。

声纹记录字段（`GET /v1/audio/voices` 每项与 `GET /v1/audio/voices/{id}` 完全相同）：

| 字段 | 说明 |
|------|------|
| `id` `object`(`audio.voice`) `name` `created_at` `type`(`audio_sample`) | OpenAI Voice 对象字段；`created_at` 为 Unix 秒 |
| `description` `gender` | 注册时提供 |
| `builtin` | 预置声纹为 true，受保护 |
| `ref_text` `seed` | 合成所需 |
| `ready` | `ref_text` 与参考音频同时就位才为 true |
| `reference_audio` | `present` 或 `missing` |
| `updated_at` | Unix 秒。历史条目的 `created_at` 由录音 mtime 回填 |
| `duration_seconds` `sample_rate` `channels` `codec` `size_bytes` | 参考音频规格，缺失时为 `null` |
| `sample_sha256` | 参考音频内容哈希，同时用作 `/preview` 的 `ETag` |
| `lufs` `true_peak_db` `snr_db` | 服务端实测响度；`snr_db` 可能为 `null`（见下） |
| `warnings` | 结构化告警数组 |

## 声纹管理

```bash
# 注册（OpenAI 形式；ref_text 省略则自动转写）
curl -X POST http://<host>:8880/v1/audio/voices \
  -F name=Alice -F consent=cons_xxx -F audio_sample=@reference.wav
# 扩展字段：指定 id、给出文字稿
curl -X POST http://<host>:8880/v1/audio/voices \
  -F name=Alice -F voice_id=alice -F gender=female \
  -F ref_text="这是参考音频的准确文字稿。" -F audio_sample=@reference.wav

# 试听不同 seed 后，把选定的存入声纹
curl -X PUT http://<host>:8880/v1/audio/voices/alice -F seed=7

# 为已有声纹补 ref_text
curl -X PUT http://<host>:8880/v1/audio/voices/norman08 \
  -F ref_text="今天天气不错，我打算下去玩。妈妈，现在几点了？"
```

`GET /v1/audio/voices` 返回 `{"object":"list","data":[Voice…]}`，`GET /v1/audio/voices/{id}` 返回
其中一项。**两者字段完全相同**，后者没有额外信息，它的用途是按 id 取单条
（约 570 字节，整份列表约 16 KB）以及对不存在的 id 返回 404。

字段里与缓存有关的三个：

| 字段 | 用途 |
|------|------|
| `updated_at` | 参考音频、`ref_text`、`seed` 任一变动都会刷新。客户端把它并入合成缓存键即可自动失效 |
| `sample_sha256` | 参考音频的内容哈希。按体积做键时，等长替换不会变化，按哈希则必然变化 |
| `ready` | `ref_text` 与参考音频同时就位才为 true |

`GET /v1/audio/voices/{id}/preview` 返回 `ETag`（值为 `sample_sha256`）与
`Last-Modified`，带 `If-None-Match` 命中时回 304 空响应。

`warnings` 为结构化对象，`severity` 为 `error` 表示该声纹当前无法合成：

```json
{"code": "reference_short", "field": "duration_seconds",
 "value": 4.2, "threshold": 5.0, "severity": "warning",
 "message": "duration 4.2s is usable; 5-10s clones more reliably"}
```

注册响应里带服务端实测的 `lufs` / `true_peak_db` / `snr_db`，与告警阈值同源，
客户端的「重录提示」不必自行推断标准。`snr_db` 是峰值与噪声底之差的粗估，
`astats` 在部分输入上不输出噪声底，此时该字段为 `null` 而非 0。

`PUT` 替换参考音频（`audio_sample`）时，未给 `ref_text` 会重新转写：旧文字稿描述的是
旧录音，错配会降低克隆质量。`DELETE` 返回 `{"id","object":"audio.voice","deleted":true}`。

`POST` 到已存在的 id 返回 409,不会覆盖已存的录音；改动请用 `PUT`。
预置声纹另有保护：删除、同 id 覆盖注册、替换参考音频均返回 403，元数据可改。

## 错误响应

OpenAI 格式。`type` 由状态码决定：429 为 `rate_limit_error`，5xx 为 `server_error`，
其余为 `invalid_request_error`；`param` 指出出错字段（无则 `null`）。请求体校验失败
返回 400（不是 422）、`code` 为 `invalid_value`。

```json
{"error": {"message": "...", "type": "invalid_request_error",
           "param": "voice", "code": "voice_not_found"}}
```

| code | 状态 | 含义 |
|------|------|------|
| `voice_not_found` | 400 | 合成时 `voice` 未注册 |
| `voice_not_found` | 404 | 声纹管理端点（扩展）的 id 不存在 |
| `invalid_value` | 400 | 字段缺失或取值非法，见 `param` |
| `unsupported_value` | 400 | `speed` ≠ 1.0 |
| `ref_text_required` | 400 | 注册/替换音频时无 `ref_text` 且自动转写不可用 |
| `voice_exists` | 409 | 注册时 id 已被占用 |
| `reference_audio_missing` | 424 | 声纹已注册但参考音频不在存储上 |
| `ref_text_missing` | 424 | 缺 `ref_text`，无法克隆 |
| `backend_busy` | **429** | 后端正忙，请求未排队。按 `Retry-After` 重试 |
| `backend_busy` | 503 | 仅在启用排队时出现：等待超出 `BREEZE_QUEUE_TIMEOUT` |
| `backend_lock_stuck` | 503 | 后端报告有推理在跑，而本服务并无。见下 |
| `backend_returned_no_audio` | 502 | 后端接受了请求但没产出任何音频 |
| `backend_error` | 502 | 其它后端故障，不泄露内部地址 |
| `backend_not_ready` | 503 | 后端尚未就绪（启动中） |

`424` 与 `503` 的区分是刻意的：前者是存储状态问题，后者才是服务不可用，
而 ALB 在无 pod 时也返回 503。客户端据此可以分开处理，不必解析文案。

未显式命名 `code` 的错误（如参数校验失败、预置声纹保护）按状态码取兜底值：
`400 invalid_request`、`403 forbidden`、`404 not_found`、`409 conflict`、
`424 dependency_missing`、`502 backend_error`、`503 backend_unavailable`。
所以 `error.code` 始终存在，客户端不必处理其缺失。

**流式路径的错误同样带正确状态码。** `StreamingResponse` 一旦开始就已发出 200，
此后无法再改状态，所以第一个分段的后端请求在构造响应**之前**完成并确认拿到真实
音频字节。早先的实现把校验留在流内，结果是 200 加一个空载荷 —— mp3 下表现为
45 字节的 ID3 头、零个音频帧，播放器读不出时长。

`backend_lock_stuck` 是上游缺陷的信号：客户端断开时 `breeze_infer.api` 不释放
推理锁，此后所有请求都收到 409 且只能重启恢复
（[issue #20](https://github.com/breezeblue-ai/breeze-tts/issues/20)）。
本项目在构建期打了补丁（见 `patches/`），正常不会出现此码；若出现，说明补丁
未生效或上游源码已变动。

## 响应头

| 头 | 出现在 | 含义 |
|----|--------|------|
| `X-Audio-Duration` | 非流式 200 | 精确时长，秒 |
| `X-Audio-Duration-Estimate` | 流式 200 | **估算**时长；推流开始时真实值未知 |
| `X-Audio-Sample-Rate` | 合成 200 | 24000 |
| `X-Breeze-Segments` | 合成 200 | 分句数 |
| `X-Queue-Wait` | 流式 200 | 本请求取得后端的等待秒数，通常为 0 |
| `Retry-After` | 429 / 503 | 建议重试间隔，秒 |
| `X-Retry-After-Seconds` | 429 | 同上，数值形式，便于不解析 HTTP 日期语义的客户端 |
| `X-Queue-Depth` | 429 | 含本请求在内的排队深度 |
| `ETag` / `Last-Modified` | `/preview` 200 | `ETag` 值为 `sample_sha256` |

## 能力发现

`GET /v1/models` 返回模型 id、支持的输出格式、`seed` 取值范围、参考音频规格与
阈值，避免客户端把这些写死在配置里。

## 与 OpenAI 的差异

- `voice` 只接受已注册声纹 id（含预置的 `alloy` 等），不提供 OpenAI 内置音色本身。
- 省略 `voice`、仅给 `instructions` 时按描述生成音色（Voice Design），OpenAI 无此用法。
- `speed` 仅支持 1.0。
- SSE 的 `usage` 无真实 token：`input_tokens` 为输入字符数，`output_tokens` 为 0。
- Create voice 的 `consent` 不校验；Breeze 克隆需要文字稿，故多了 `ref_text`（可自动转写）。
- 后端一次只处理一个请求，忙时立即 429（见「并发」）。
