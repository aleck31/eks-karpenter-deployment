# Breeze TTS 2 语音合成 (OpenAI 兼容接口)

单容器内跑两个进程：Breeze 自带的 `breeze_infer` 推理服务 (`:8000`，仅监听
127.0.0.1) 与 OpenAI 兼容 adapter (`:8880`，对外)。声纹存放在 `/shared/voices`，
与 `voxcpm2-tts` 共用同一 PVC 与同一份 `registry.json`。

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

`GET /v1/audio/voices` 的每项带 `ready` 字段，反映该声纹是否具备 `ref_text`
从而可用于合成 —— 缺失时不必逐个查详情才发现。

替换参考音频必须在同一请求内同时给出 `ref_text`：旧文字稿描述的是旧录音，
错配会降低克隆质量。

预置声纹受保护：删除、同 id 覆盖注册、替换参考音频均返回 403，元数据可改。

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

**单并发。** 后端一次只服务一个请求，并发请求会收到 HTTP 409。adapter 用一把锁
把并发转为排队，超出 `BREEZE_QUEUE_TIMEOUT`（默认 120s）返回 503。排队意味着
第二个请求的首字延迟等于前一个的总耗时，所以本模块适合单用户或低频调用场景。
副本数扩容无法解决：多副本无法共享 GPU 上的单份模型。

**长文本会退化。** 上下文 2048 位置 ÷ 12.5 Hz 帧率 ≈ 164 秒音频上限，而质量在
触限之前就开始下降（重复片段、语速漂移）。这是自回归 TTS 的共性，不是本模型特有。
adapter 目前把 `input` 原样透传，未做分句。长文本朗读应由调用方按句切分，
或后续在 adapter 内实现分句拼接。

**许可。** 推理代码为 Apache 2.0，**模型权重与自托管产出音频**适用
BreezeBlue Research and Non-Commercial License，仅限研究与非商业用途，
商业使用需 BreezeBlue 书面授权。

**语言。** 开放权重为中英双语。官网宣称的 50 语言指其托管服务，不适用于本部署。

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
