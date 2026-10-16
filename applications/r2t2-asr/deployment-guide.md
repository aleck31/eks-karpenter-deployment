# R2T2 ASR 语音识别 (OpenAI 兼容接口)

基于网易有道 [Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2)
（Qwen3-ASR 架构、为流式重新训练）的语音识别服务，提供 OpenAI Audio API 的
`/v1/audio/transcriptions` 与 Realtime API（GA）转写会话。实时与批处理共用
同一个模型与同一条流式推理路径，单进程、约 9.6 GB 显存。

接口、事件与错误码见 [api-reference.md](api-reference.md)。

## 文件结构

```
applications/r2t2-asr/
├── Dockerfile                    # 官方 qwenllm/qwen3-asr 镜像 + R2T2 + FireRed VAD
├── base/
│   ├── asr-server.py             # 服务层，以 ConfigMap 挂载，改它无需重建镜像
│   ├── r2t2-asr-deployment.yaml  # Deployment + Service；initContainer 下载权重
│   ├── r2t2-asr-ingress.yaml     # ALB :8000
│   └── kustomization.yaml
├── overlays/
│   ├── example/                  # 入库：示例取值，供复制
│   └── <env-name>/               # 不入库：真实取值
└── tests/test_asr_server.py      # 服务层单元测试（假模型，不需要 GPU）
```

## 架构

```
客户端 ─ ALB :8000 ─ asr-server.py (uvicorn)
                      ├ POST /v1/audio/transcriptions ─┐
                      └ WS   /v1/realtime ─────────────┤ Session（每路一个）
                                                       │  └ R2T2 streaming_transcribe（官方实现，原样调用）
                                                       └ Batcher → vllm.LLM.generate（多路合并为一次批调用）
```

- **推理算法用官方实现**：`R2T2ASRModel.streaming_transcribe` / `finish_streaming_transcribe`
  不改写；逐块调度（自适应 max_new_tokens、幻觉检测）照官方 `ws_server.py`。
- **Batcher**：官方服务一个进程只服务一路流。这里各路流在线程中调用同步的
  `generate()`，由一个协调线程合并成一次 vLLM 批调用。8 路实测输出与单路
  在 81-85/86 个文件上逐字一致，吞吐为单路的 2.5 倍。
- **R2T2 离线模式不使用**：在 50-80 秒英文音频上整句漏识别。批处理也走流式路径。

## 关键参数（均有环境变量，默认值为实测选定）

| 参数 | 默认 | 依据 |
|------|------|------|
| `ASR_REALTIME_STEP_MS` | 480 | 对比官方 160 ms：准确率相当、首字时间相同、回改更少、GPU 计算约 1/3 |
| `ASR_FILE_STEP_MS` | 2000 | 480-2000 ms 扫描准确率持平，86 个文件耗时 183 s → 84 s |
| `ASR_FILE_PAUSE_CUT_SECONDS` / `_HARD_SECONDS` | 10 / 30 | 批处理：段满 10 s 后遇停顿即切，30 s 强切 |
| `ASR_STREAM_PAUSE_CUT_SECONDS` / `_HARD_SECONDS` | 15 / 30 | 实时、未开 server_vad |
| `ASR_VAD_PAUSE_CUT_SECONDS` / `_HARD_SECONDS` | 0 / 30 | 实时、开 server_vad：每次 VAD 判停都切 |
| `ASR_VAD_SILENCE_MS` | 500 | server_vad 未指定 `silence_duration_ms` 时的静音时长（OpenAI 默认值）。原先沿用 VAD 的 200 ms，逗号处就断轮，轮次碎片化 |
| `R2T2_KV_CACHE_GIB` | 3 | 约 28k token；按字节设置，占用不随显卡型号变化 |
| `R2T2_GPU_MEMORY_UTILIZATION` | 0.3 | 设了 KV 字节数后只作启动门槛 |
| `ASR_MAX_SESSIONS` | 16 | 超出时 WebSocket 以 1013 关闭 |

**为什么要切段**：R2T2 每块都把整段从头重新喂一遍，段越长计算越多，且长段在
FLEURS 上会整句漏识别（60 s 段三个文件里两次）。段太短则缺上下文，零碎错误增多。
上表是在 pause_cut 0-30 s 上扫描后选出的没有漏句的设置。

**VAD 只决定切点、不丢音频**：FireRed VAD 对音量敏感（-47 dBFS 的英文录音只认出
6% 的语音）。批处理的段首尾相连；VAD 的输入按 3 秒窗口做增益，模型听到的仍是原音。

**实时切轮在判停点拆块**：VAD 判停时所在的 480 ms 块里可能已有下一句的开头。
早先整块归给正在结束的轮次，下一句首字随之丢失（「这是…」成了「是…」）。现在块在判停点
拆开，前半归本轮、后半归下一轮；结束的轮次另补 0.5 s 静音再收尾，与客户端 commit 一致，
否则末字常不输出（「第一句」成了「第一」）。修正后 FLEURS 12 个文件 server_vad 错误率
11.4% → 6.5%。

## 前置条件

| 依赖 | 说明 |
|------|------|
| GPU NodePool | 见 `gpu/deployment-guide.md`；显存 ≥ 12 GB，与其他服务共卡时需 Time-Slicing |
| 模型存储 | 一个 RWX 的 PVC（如 EFS），在 overlay 中指定，存放权重与驱动 JIT 缓存 |
| AWS Load Balancer Controller | Ingress 在 ALB 上监听 8000 |

## 构建镜像

基础镜像约 14 GB（解压后约 30 GB），构建机至少留 50 GB 空闲。

```bash
DOCKER_BUILDKIT=1 docker build -t <account>.dkr.ecr.<region>.amazonaws.com/r2t2-asr:<tag> .
# 镜像内跑单元测试（依赖版本与生产一致）
docker run --rm -v $PWD:/src -w /src --entrypoint bash <image> \
    -c "pip install -q pytest==8.4.2 && python3 -m pytest tests -q"
docker push <account>.dkr.ecr.<region>.amazonaws.com/r2t2-asr:<tag>
```

## 部署

```bash
cp -r overlays/example overlays/<env-name>   # 填入账号、namespace、PVC、前缀列表
kubectl config current-context               # 确认目标集群
kubectl apply -k overlays/<env-name>
```

- 首次启动：initContainer 下载权重（R2T2 约 4 GB + FireRed VAD），版本固定在
  Deployment 中；之后启动约 1.5 分钟，其中预热约 3 秒。
- 显卡驱动的 JIT 缓存放在模型 PVC 的 `cache/r2t2-cuda` 子目录。驱动或 GPU 型号变化后首次
  启动会重新编译（约 50 秒），Pod 就绪前完成，不影响请求。
- 更新策略为 Recreate：先停旧 Pod 再起新 Pod，避免两份模型同时占用显存。

## 实测（FLEURS 普通话 / 英文）

| 用法 | 中文短句 | 英文短句 | 长段 |
|------|------|------|------|
| HTTP 批处理 | 5.7% | 7.2% | 见下 |
| WebSocket 实时 | 5.9% | 6.6% | 开 server_vad 最好 |

- 实时首字约 1.3-2.2 秒；音频结束后约 0.3 秒给出最终结果；文本只追加、不回改。
- 长段测试素材是把 8 句不同说话人的录音拼接而成，句间音量差最大超过 50 dB；
  漏句几乎都发生在小声句紧跟大声句的位置。真实的连续录音少有这种落差，
  长音频的结论需用真实会议录音复核。

## 许可

代码 Apache 2.0；模型权重为网易有道模型使用许可：月活超过 1 亿或年营收超过
10 亿元需另行申请授权，且不得用于改进其他 AI 模型（见权重仓库 `MODEL_LICENSE`）。
