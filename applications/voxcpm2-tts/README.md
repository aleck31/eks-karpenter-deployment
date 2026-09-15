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

1. **Controllable Cloning**（`voice` = 已注册 voice ID）— 使用参考音频克隆音色，音色一致
2. **Voice Design**（`voice_description` = 文字描述）— 按描述生成声音，音色每次不同

**两种模式由不同字段区分,不会互相回落。** `voice` 只接受已注册 ID，未注册直接返回 **404**；要用描述生成必须显式传 `voice_description`。

这样设计是因为让 `voice` 同时接受 ID 和描述会导致打错或过期的 ID 被当作描述使用，返回 200 加一个任意音色，调用方无从察觉。

注意 Cloning 保证的是**音色一致**，不是逐字节可复现 —— VoxCPM2 是 diffusion 模型，同一参考音频同一文本两次调用的输出字节不同，但音色相同。

已注册 voice 的参考音频若丢失，返回 **503** 而非退化到 Voice Design。同一个 voice ID 返回不同音色属于违背契约，因此宁可失败。

### 预置 Voice

13 个预置 voice 均已配置真实参考音频（非文字描述），走 Cloning 模式，音色稳定。

它们在 API 中标记为 `type: builtin`，**不可删除、不可替换参考音频**（返回 403）。这些录音是从 42 个候选样本中逐个试听筛选出的，模型输出不可复现，覆盖后无法还原。修改 `name` / `description` 允许。

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

### 制作预置参考音频

预置声纹本身用 Voice Design 生成，再注册为参考音频。要点如下。

**描述里必须显式约束录音环境。** VoxCPM 会把描述中的环境特征一并合成 —— 官方文档说 prompt 的 "background sounds and ambiance will be replicated"，这对生成同样成立。不写约束，模型可能自行加入环境音，听起来就是底噪。

```
Clean studio recording, no background noise, no room reverb
Dry close-mic studio recording, silent background
```

**一次生成挑不出好的，官方建议生成 1~3 次。** VoxCPM README 的 Risks 一节写明 Voice Design 与 Controllable Cloning 的结果 run 与 run 之间会变化。实践中每个 voice 生成 5 个候选、试听挑选比较稳妥。

**CFG 值影响明显，按文本长度调。** 短句提高（2.0~2.5）增强清晰度，长文降低（1.2~1.5）提升稳定性。同一描述不同 CFG 的产出差异可能大于不同描述之间的差异。

**音色相关的措辞会带来副作用。** 例如描述里强调 `Rich baritone`（浑厚男中音）会显著抬高低频能量，听感接近低频轰鸣。改为 `Mid-range voice, clear and articulate, not deep or boomy` 可缓解。

**语速不要指望用描述控制。** `unhurried` / `speaks slowly` / `deliberately slow pacing` 之类措辞对语速的影响不稳定，实测加了约束反而比不加更快。要放慢就在文本里加标点制造停顿。

**筛选只能靠试听。** 频谱指标（低频占比、高频占比、DC offset）与感知底噪不相关 —— 低频能量大部分属于音色而非噪声，据此排序会得出与听感相反的结论。指标只适合发现异常离群值（例如某条 DC offset 比同批高一到两个数量级）。

生成后按与服务端一致的方式归一化，再写入参考音频目录：

```bash
ffmpeg -i raw.wav -af loudnorm=I=-16:TP=-1.5:LRA=11 -ar 16000 -ac 1 ref.wav
```

预置 voice 受 403 保护，替换需直接写入共享卷，并同步 `registry.json` 里该条目的 `audio` 字段（否则 detail 会继续返回旧时长）。

### Voice 管理 API

```bash
# 列出所有 voice（摘要：voice_id / name / description / gender / type / builtin）
GET /v1/audio/voices

# 注册新 voice（上传参考音频，自动归一化为 16kHz mono -16 LUFS）
POST /v1/audio/voices
  Form: voice_id, name, description, gender, audio(file)
  → 403 若 voice_id 是预置 voice
  → 400 若 gender 不在 female / male / neutral / unknown 之内

# 查询单个 voice（含音频元数据与服务端质量判定，见下）
GET /v1/audio/voices/{voice_id}

# 更新 voice
PUT /v1/audio/voices/{voice_id}
  Form: name, description, gender, audio(file)
  → 403 若对预置 voice 传 audio；仅改 name/description 允许

# 删除 voice
DELETE /v1/audio/voices/{voice_id}
  → 403 若为预置 voice

# 试听参考音频（audio/wav）
GET /v1/audio/voices/{voice_id}/preview
  → 404 若参考音频缺失
```

### 性别字段

`gender` 取值 `female` / `male` / `neutral` / `unknown`，list 与 detail 均返回，可用于前端分组或筛选。

**由调用方在注册时声明，服务端不做自动推断。** 不传默认 `unknown`；取值不在枚举内返回 400。已注册的 voice 可通过 PUT 修正，预置 voice 也允许改（它属于元数据，不是音频内容）。

不从音频推断的原因：基频判别会误判女低音、男高音、童声与非二元发声，标错比不标更糟。13 个预置 voice 的性别已人工标注，用户注册的自定义 voice 若未声明则保持 `unknown`。

不要用 `description` 里的 "female" / "male" 字样判断性别 —— 它是自由文本、可被 PUT 改写，且 `female` 含有 `male` 子串，朴素匹配会误判。

**list 与 detail 的区别**：list 返回摘要，detail 额外返回 `created_at`（预置为 `null`）、音频元数据、以及 `reference_audio` 与 `warnings`。

detail 响应示例：

```json
{
  "voice_id": "cedar",
  "name": "Cedar",
  "description": "Steady, mature male mentor",
  "gender": "male",
  "created_at": null,
  "type": "builtin",
  "builtin": true,
  "reference_audio": "present",
  "duration_seconds": 5.3,
  "sample_rate": 16000,
  "channels": 1,
  "codec": "pcm_s16le",
  "size_bytes": 169806,
  "warnings": []
}
```

**`warnings` 由服务端判定，不要在客户端硬编码阈值**。当前会给出的判定：

| 情况 | 含义 |
|------|------|
| 时长 < 3s | 低于 VoxCPM 声明的克隆最小时长，音色漂移可预期 |
| 3s ≤ 时长 < 5s | 可用，但 5-10s 克隆更稳定 |
| 时长 > 15s | 超出有效区间，只增加请求体积与处理时间 |
| 采样率 ≠ 16000Hz | 该录音未经 API 归一化 |
| 声道 ≠ 1 | 同上 |
| `reference_audio: missing` | 录音已丢失，此 voice 无法用于合成 |

`reference_audio` 为 `missing` 时，音频元数据字段全部返回 `null`（不返回注册时的历史值），且该 voice 调用 `/v1/audio/speech` 会得到 503。

`POST` 注册成功时也会在响应里返回 `warnings`，便于在还持有原始素材时立即重录：

```json
{"voice_id": "my-voice", "status": "created",
 "warnings": ["duration 30.0s exceeds the 3-10s recommended range; ..."]}
```

参考音频要求：3-10 秒，干净无噪音，自然说话即可。任意格式上传，服务端统一归一化为 16kHz mono -16 LUFS。

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

### 按描述生成（Voice Design）

```bash
curl -X POST http://<ALB>:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "input": "要合成的文本",
    "voice_description": "Male voice. A calm narrator with a low steady tone"
  }' --output out.mp3
```

不传 `voice`，改传 `voice_description`。音色每次不同，需要稳定音色请注册声纹后用 `voice`。

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

### 声音克隆完整对接流程

下游接入自助声纹克隆的推荐顺序：

```bash
ALB=http://<ALB>:8880

# 1. 注册声纹，检查响应里的 warnings
curl -sX POST $ALB/v1/audio/voices \
  -F "voice_id=user-1024" -F "name=User 1024" \
  -F "audio=@recording.m4a"
# {"voice_id":"user-1024","status":"created","warnings":[]}
#   warnings 非空说明录音偏离推荐条件，建议提示用户重录

# 2. 试听确认音色
curl -s $ALB/v1/audio/voices/user-1024/preview -o preview.wav

# 3. 合成
curl -sX POST $ALB/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"要合成的文本","voice":"user-1024","response_format":"mp3"}' \
  -o out.mp3

# 4. 不再需要时删除
curl -sX DELETE $ALB/v1/audio/voices/user-1024
```

一次性克隆（不注册、不落盘）用 `/v1/audio/clone`，见上方「声音克隆」章节。适合临时试听或不需要复用的场景。

### 错误码

| 状态码 | 场景 | 客户端应对 |
|--------|------|-----------|
| 403 | 对预置 voice 执行 DELETE、或 PUT/POST 替换其参考音频 | 换一个 `voice_id`；UI 应对 `builtin: true` 的条目禁用删除与替换 |
| 404 | `voice` 未注册；或 `/preview` 的参考音频缺失 | 用 `GET /v1/audio/voices` 核对 ID；若本意是按描述生成，改用 `voice_description` |
| 503 | 已注册 voice 的参考音频缺失，拒绝退化到 Voice Design | 调 detail 确认 `reference_audio` 状态，重新注册 |
| 400 | 不支持的 `response_format` | 见「支持的输出格式」 |

### 命名约定

`voice_id` 是全局扁平命名空间，没有租户隔离 —— 任何调用方都能覆盖或删除他人注册的自定义声纹（预置 voice 除外，有 403 保护）。多用户场景建议自行加前缀，如 `tenant-{id}-{name}`。

## 存储

- 模型: EFS `/shared/VoxCPM2/`
- Voice 参考音频: EFS `/shared/voices/{voice_id}/ref.wav`
- Voice 注册表: EFS `/shared/voices/registry.json`
- 注册表 schema 版本: EFS `/shared/voices/.schema`

存储目录须归属容器运行用户（uid 10001）。目录属 root 时读操作正常但 POST/PUT/DELETE 会因
`PermissionError` 返回 500，表现为「能查不能写」：

```bash
chown -R 10001:10001 /shared/voices
find /shared/voices -type d -exec chmod 775 {} \;
find /shared/voices -type f -exec chmod 664 {} \;
```

`registry.json` 条目结构：

```json
{
  "cedar": {
    "file": "ref.wav",
    "name": "Cedar",
    "description": "Steady, mature male mentor",
    "builtin": true,
    "audio": {"duration_seconds": 5.3, "sample_rate": 16000,
              "channels": 1, "codec": "pcm_s16le", "size_bytes": 169806}
  }
}
```

- `builtin: true` 是预置 voice 的保护标记，缺失则 DELETE/PUT 不会被拦截。手工铺入预置 voice 时必须写入
- `audio` 在注册时探测并存下，detail 读取时不再跑 ffprobe
- 通过 API 注册的条目还带 `created_at`；预置 voice 无此字段

`.schema` 记录已执行的迁移版本。启动时的回填逻辑据此判断是否需要运行，**不依赖字段是否存在** ——
早期版本用「无 `created_at` 即预置」推断 `builtin`，该推断仅对预置那批数据成立；若每次启动都重跑，
任何因写入中断而缺 `created_at` 的用户声纹都会被永久标为预置并锁进 403。

> 模型存储复用 qwen3-speech 模块的 `qwen3-models-pvc`，
> 需先部署 `applications/qwen3-speech/base/shared/`（EFS StorageClass + PVC）。

## 部署

采用 kustomize base + overlay，环境相关取值（namespace、ECR 账号）不入库。

```
applications/voxcpm2-tts/
├── base/
│   ├── kustomization.yaml
│   ├── voxcpm2-tts-deployment.yaml   # Deployment + Service
│   └── voxcpm2-tts-ingress.yaml      # ALB Ingress (group: speech-services)
├── overlays/
│   ├── example/                      # 入库：示例取值，供复制
│   └── <env-name>/                   # 不入库：真实取值
├── Dockerfile                        # adapter 层
├── Dockerfile.base                   # Nano-vLLM 基础镜像
├── openai-adapter.py
└── entrypoint.sh
```

### 构建镜像

```bash
# 基础镜像（首次或依赖变更时）
docker build -f Dockerfile.base -t voxcpm2-tts:base-2.0.3 .

# adapter 层（~3秒）。BASE_IMAGE 指向基础镜像所在位置
DOCKER_BUILDKIT=1 docker buildx build --load \
  --build-arg BASE_IMAGE=<account>.dkr.ecr.<region>.amazonaws.com/voxcpm2-tts:base-2.0.3 \
  -t voxcpm2-tts:latest .

docker tag voxcpm2-tts:latest <account>.dkr.ecr.<region>.amazonaws.com/voxcpm2-tts:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/voxcpm2-tts:latest
```

### 准备 overlay

```bash
cp -r overlays/example overlays/<env-name>
# 编辑 namespace 与 ECR 镜像地址
vi overlays/<env-name>/kustomization.yaml
```

> `.gitignore` 默认忽略 `overlays/` 下全部目录、仅放行 `example/`，真实取值不会误提交。

### 部署到 EKS

```bash
kubectl config current-context          # 先确认目标集群

# 先清空旧 Pod 再部署，避免与新 Pod 争抢同一张 GPU 导致 OOM
kubectl scale deployment voxcpm2-tts -n <namespace> --replicas=0
# 待旧 Pod 完全终止后
kubectl apply -k overlays/<env-name>
```

## 硬件配置

- GPU: NVIDIA L4 (g6.xlarge Spot)
- 模型显存: ~9.6GB (gpu_memory_utilization: 0.45, max_model_len: 8192)
- 推理延迟: 短文本 ~1.5s, 中等文本 ~6s (非流式); 流式 TTFB ~0.37s
