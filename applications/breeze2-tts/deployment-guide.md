# Breeze TTS 2 语音合成 (OpenAI 兼容接口)

单容器内跑两个进程：Breeze 自带的 `breeze_infer` 推理服务 (`:8000`，仅监听 127.0.0.1) 与 OpenAI 兼容 adapter (`:8880`，对外)。声纹存放在 `/shared/voices`，
与 `voxcpm2-tts` 共用同一 PVC 与同一份 `registry.json`。

## 文件结构

```
applications/breeze2-tts/
├── Dockerfile.base                  # breeze-tts 源码 + 依赖，构建慢
├── Dockerfile                       # adapter 层，秒级
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
| 24 GB 显存机型 | Deployment 硬约束 g6/g5 系列；g4dn 的 16 GB 不够 |
| `qwen3-models-pvc` | 复用 qwen3-speech 的 EFS PVC，**须先部署** `applications/qwen3-speech/base/shared` |
| ALB :8880 空闲 | 同一 listen-port 只能由一个 Ingress 声明，见「接管 :8880」 |

## 资源分配

| 项 | 值 | 依据 |
|---|---|---|
| 显存 | 8.9 GiB | 实测，含三项加速参数的 CUDA Graph |
| `nvidia.com/gpu` | 1 | Time-Slicing 下每节点 2 个槽位，故一节点最多 2 个 GPU Pod |
| 内存 | 请求 3Gi / 上限 8Gi | |
| CPU | 请求 500m / 上限 2 | |
| 模型权重 | 7.2 GB on EFS | initContainer 下载，与其它服务共享 PVC |

## 构建

```bash
# 1. 基础镜像（含 breeze-tts 源码与依赖，构建较慢）
docker build -f Dockerfile.base -t <repo>/breeze2-tts:base-<breeze-ref> .

# 2. adapter 层（秒级）
docker build --build-arg BASE_IMAGE=<repo>/breeze2-tts:base-<breeze-ref> \
  -t <repo>/breeze2-tts:latest .
```

`Dockerfile.base` 会把 breeze-tts 的 commit 写入 `/opt/breeze-tts.commit`，
便于核对镜像对应的上游版本。

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

### 接管 :8880

该端口原由 `voxcpm2-tts` 占用。同一 ALB group 内一个 listen-port 只能由一个
Ingress 声明，**两者不能同时存在**，否则 aws-load-balancer-controller 会因监听器
冲突拒绝调和。切换顺序：

```bash
# 1. 先只部署工作负载，不含 Ingress，确认 Pod Ready
kubectl apply -k overlays/<env-name> --prune=false
kubectl delete ingress breeze2-tts-ingress -n <ns>   # 若已随 apply 创建
kubectl rollout status deploy/breeze2-tts -n <ns>

# 2. 集群内直连验证，确认声纹可用
kubectl run curl-probe --rm -it --image=curlimages/curl --restart=Never -n <ns> -- \
  curl -s http://breeze2-tts-service/v1/audio/voices

# 3. 摘掉旧 Ingress，挂上新 Ingress
kubectl delete ingress voxcpm2-tts-ingress -n <ns>
kubectl apply -k overlays/<env-name>

# 4. 回滚：反向执行第 3 步
```

切换前需确认所有要用的声纹都已具备 `ref_text`，否则合成会返回 422。
可用集群内的 `qwen3-asr` 批量转写参考音频来回填。

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
| `BREEZE_QUEUE_REJECT_SECONDS` | `0` | `0` 表示忙时立即 429 不排队；正数表示预估等待在该值以内则排队 |
| `BREEZE_QUEUE_TIMEOUT` | `120` | 仅在启用排队时生效，限制排队者的最长等待 |

## 接口

沿用 `voxcpm2-tts` 的请求契约，已有调用方无需改动。两个字段是新增的：

| 字段 | 位置 | 说明 |
|---|---|---|
| `ref_text` | 声纹属性（注册/更新时提供） | **必需**。Breeze 从「参考音频 + 其准确文字稿」成对克隆，只给音频会被后端拒绝 |
| `seed` | 声纹属性，也可在单次请求覆盖 | 采样种子，取值 **0–4294967295**。固定后端加速参数的前提下可复现，用于选定并锁住满意的音色 |

注册时若不指定 `seed`，写入后端默认值 **42**（`breeze_infer.api` 的
`seed: int = Form(42)`）。显式写入而非留空，是因为留空并不等于"不固定种子" ——
输出仍然是 seeded 的，只是那个值藏在后端函数签名里而不在 registry 里；
上游一旦改动该默认值，所有声纹的音色都会变而无从追溯。

`seed` 上界来自后端 `set_all_seeds()` 同时播种的四个 RNG 中最窄的一个：numpy 的
legacy MT19937 只接受无符号 32 位，先于 torch 抛异常（torch 本身文档化范围是
`[-2**63, 2**64-1]` 且负数会被映射，但这段余量在此不可达）。超范围由 adapter
返回 400 并说明边界，不会落到后端变成 500。

### 合成

```bash
# 使用已注册声纹
curl -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"今天天气不错。","voice":"norman08","response_format":"mp3"}' \
  --output out.mp3

# 覆盖 seed 试听（不改动声纹）
curl -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"今天天气不错。","voice":"norman08","seed":7}' --output s7.mp3

# Voice Design：不用参考音频，按描述生成
curl -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"欢迎收听。","voice_description":"一位温柔自信的年轻女性，声音清晰。","cfg_value":4}' \
  --output design.mp3
```

`speed` 字段为兼容保留但不生效：Breeze 未暴露语速控制，重采样会改变它本应保持的
音高。要调节语气节奏请用 `voice_description`。

### 流式

```bash
curl -N -X POST http://<host>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"...","voice":"norman08","stream":true,"response_format":"wav"}' \
  --output stream.wav
```

`stream=true` 支持全部格式。`pcm` / `wav` 直接透传后端 PCM；mp3/opus/aac/flac 经
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

### 并发：忙时立即拒绝，不排队

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

### 临时克隆（不注册声纹）

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
`cfg_value` 与 `seed` 可选。该端点不支持 `stream`。

### 字段参考

`POST /v1/audio/speech` 请求体：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `input` | string | 必需 | 待合成文本。超长自动分句，见「已知限制」 |
| `voice` | string | `alloy` | 已注册声纹 id。与 `voice_description` 二选一 |
| `voice_description` | string | — | 按描述生成，不用参考音频。**音色每次调用都不同**，需要稳定音色请注册声纹 |
| `response_format` | string | `mp3` | `mp3` `opus` `aac` `flac` `wav` `pcm` |
| `stream` | bool | `false` | 全部格式均支持 |
| `seed` | int | 声纹存储值 | `0` … `4294967295`。覆盖本次请求，不改动声纹 |
| `cfg_value` | float | — | 指令遵循强度，转为后端 `--cfg-scale`。上游建议配合 `voice_description` 用 4 |
| `model` | string | `tts-1` | 为 OpenAI 兼容而接受，**不使用**。本服务只有一个模型 |
| `speed` | float | `1.0` | 为 OpenAI 兼容而接受，**不生效**。Breeze 无速率控制，重采样会改变音高；需要调整语速请用 `voice_description` |

`POST /v1/audio/voices` 表单字段（`PUT` 同名字段均可选，只传要改的）：

| 字段 | 必需 | 说明 |
|------|------|------|
| `voice_id` | ✅ | `[A-Za-z0-9_][A-Za-z0-9._-]{0,63}`。仅 ASCII，中文放 `name` |
| `audio` | ✅ | 参考音频，任意 ffmpeg 可解格式；服务端归一化为 -16 LUFS / 16kHz / 单声道 |
| `ref_text` | ✅ | 参考音频的**准确**文字稿。Breeze 不接受只有音频 |
| `name` | | 显示名，可含中文。默认等于 `voice_id` |
| `description` | | 自由文本备注 |
| `gender` | | `male` `female` `unknown`（默认）。由调用方声明，不从音频推断 |
| `seed` | | 默认 42（后端 `breeze_infer.api` 的默认值，显式写入而非留空） |

声纹记录字段（`GET /v1/audio/voices` 每项与 `GET /v1/audio/voices/{id}` 完全相同）：

| 字段 | 说明 |
|------|------|
| `voice_id` `name` `description` `gender` | 注册时提供 |
| `type` | `builtin` 或 `custom` |
| `builtin` | 布尔，等价于 `type == "builtin"`。预置声纹受保护 |
| `ref_text` `seed` | 合成所需 |
| `ready` | `ref_text` 与参考音频同时就位才为 true |
| `reference_audio` | `present` 或 `missing` |
| `created_at` `updated_at` | ISO 8601 UTC。历史条目的 `created_at` 由录音 mtime 回填 |
| `duration_seconds` `sample_rate` `channels` `codec` `size_bytes` | 参考音频规格，缺失时为 `null` |
| `sample_sha256` | 参考音频内容哈希，同时用作 `/preview` 的 `ETag` |
| `lufs` `true_peak_db` `snr_db` | 服务端实测响度；`snr_db` 可能为 `null`（见下） |
| `warnings` | 结构化告警数组 |

### 声纹管理

```bash
# 注册（ref_text 必需）
curl -X POST http://<host>:8880/v1/audio/voices \
  -F voice_id=alice -F name=Alice -F gender=female \
  -F ref_text="这是参考音频的准确文字稿。" \
  -F audio=@reference.wav

# 试听不同 seed 后，把选定的存入声纹
curl -X PUT http://<host>:8880/v1/audio/voices/alice -F seed=7

# 为已有声纹补 ref_text
curl -X PUT http://<host>:8880/v1/audio/voices/norman08 \
  -F ref_text="今天天气不错，我打算下去玩。妈妈，现在几点了？"
```

`GET /v1/audio/voices` 返回每个声纹的完整记录，`GET /v1/audio/voices/{id}` 返回
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

替换参考音频必须在同一请求内同时给出 `ref_text`：旧文字稿描述的是旧录音，
错配会降低克隆质量。

`POST` 到已存在的 id 返回 409,不会覆盖已存的录音；改动请用 `PUT`。
预置声纹另有保护：删除、同 id 覆盖注册、替换参考音频均返回 403，元数据可改。

### 错误响应

同时给出 `detail`（字符串，兼容旧调用）与 `error`（带稳定的 `code`）：

```json
{"detail": "...", "error": {"code": "reference_audio_missing",
                            "message": "...", "status": 424}}
```

| code | 状态 | 含义 |
|------|------|------|
| `voice_not_found` | 404 | id 不存在 |
| `voice_exists` | 409 | 注册时 id 已被占用 |
| `reference_audio_missing` | 424 | 声纹已注册但参考音频不在存储上 |
| `ref_text_missing` | 424 | 缺 `ref_text`，无法克隆 |
| `backend_busy` | 503 | 排队超出 `BREEZE_QUEUE_TIMEOUT` |
| `backend_not_ready` | 503 | 后端尚未就绪 |

`424` 与 `503` 的区分是刻意的：前者是存储状态问题，后者才是服务不可用，
而 ALB 在无 pod 时也返回 503。客户端据此可以分开处理，不必解析文案。

### 能力发现

`GET /v1/models` 返回模型 id、支持的输出格式、`seed` 取值范围、参考音频规格与
阈值，避免客户端把这些写死在配置里。

## 部署契约

后端加速参数**固定为**：

```
--fast-depth-decoder --fast-backbone-decode --fast-codec
```

实测（L4 24GB，中文 60 字，RTF = 生成耗时 / 音频时长）：

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

**单并发。** 后端一次只服务一个请求，并发请求会收到 HTTP 409。adapter 把它转成
明确的 429 + `Retry-After`（见「并发」一节），**不排队**。所以本模块适合单用户或
低频调用场景。副本数扩容无法解决：多副本无法共享 GPU 上的单份模型，且时间切片下
每节点只有 2 个 GPU 槽位。真并发需要独占节点 —— 单实例占 8.9 GiB，24 GB 卡可容纳
两个，但当前节点与 `qwen3-asr` 共享，仅余约 3.5 GiB。

**长文本由 adapter 分句。** 后端上下文 2048 位置 ÷ 12.5 Hz 帧率 ≈ 164 秒音频上限，
而质量在触限之前就开始下降（重复片段、语速漂移），这是自回归 TTS 的共性。
adapter 用 `pysbd` 切句并贪心打包到 `MAX_SEGMENT_CHARS`（160），逐段合成后以
`SEGMENT_PAUSE_MS`（250ms）静音拼接，调用方不必自行切分。分句数见
`X-Breeze-Segments` 响应头。

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
| 换了节点后声纹丢失 | 声纹在 EFS，不会丢；检查 PVC 是否挂载 | 确认 `VOICES_DIR=/shared/voices` |

重启时用 `scale --replicas=0` 等 Pod 消失后再 `--replicas=1`，或 `rollout restart`。
`kubectl delete pod --wait=false` 会绕过 `strategy: Recreate`，新旧 Pod 同时争抢
GPU 槽位，导致新 Pod 落到另一个节点。

## 成本

单个 g6.xlarge Spot 约 $0.22/小时。与 `qwen3-asr` 共享节点时通过 Time-Slicing
同用一张 L4（8.9 + 7.7 GiB，24 GB 内可容纳），不需要额外节点。
