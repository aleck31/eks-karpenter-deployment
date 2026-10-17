# Breeze TTS 2 语音合成 (OpenAI 兼容接口)

单容器内跑两个进程：vLLM-Omni 推理服务 (`:8000`，仅监听 127.0.0.1) 与 OpenAI 兼容
adapter (`:8880`，对外)。模型权重与声纹（`/shared/voices`，索引为 `registry.json`）
放在同一个 RWX PVC 上。

**推理引擎为 vLLM-Omni，不是 Breeze 自带的 `breeze_infer` 参考实现。** 权重仍是
BreezeBlue 的 Breeze-TTS-2，vLLM-Omni 通过自带的 `breeze_tts_2` 适配器对其做连续
批处理调度，因此并发不再是 1。两者的实测对比与换用理由见
[docs/breeze-tts-serving-layer-eval.md](../../docs/breeze-tts-serving-layer-eval.md)。

接口、字段、错误码与响应头见 [api-reference.md](api-reference.md)。兼容基线为 OpenAI
Audio API 的 GA 规范（2026-10 对齐，`openai` Python SDK 3.31.0 实测）。

## 文件结构

```
applications/breeze2-tts/
├── Dockerfile                       # vLLM-Omni 基础镜像 + adapter 依赖
├── entrypoint.sh                    # vLLM-Omni :8000 + adapter :8880
├── base/
│   ├── openai-adapter.py            # 以 ConfigMap 挂载，改它无需重建镜像
│   ├── breeze2-tts-deployment.yaml  # Kubernetes Deployment + Service
│   ├── breeze2-tts-ingress.yaml     # 声明 ALB :8880
│   ├── vllm-omni-deploy-config.yaml # 后端的 --deploy-config，以 ConfigMap 挂载
│   └── kustomization.yaml
└── overlays/
    ├── example/                     # 入库：示例取值，供复制
    └── <env-name>/                  # 不入库：真实取值
```

## 前置条件

| 依赖 | 说明 |
|------|------|
| GPU NodePool | 见 `gpu/deployment-guide.md`，需 Time-Slicing |
| GPU 显存 | 服务固定占用约 9 GiB，与其它负载同卡时需留足余量；机型约束按环境在 overlay 中设置 |
| 模型 PVC | RWX（如 EFS），在 overlay 中指定名称；声纹目录须属于 uid 10001 |
| ALB :8880 空闲 | 同一 ALB group 内一个 listen-port 只能由一个 Ingress 声明 |

## 资源分配

| 项 | 值 | 依据 |
|---|---|---|
| 显存 | 约 9.0 GiB | 实测，含三项加速参数的 CUDA Graph |
| `nvidia.com/gpu` | 1 | Time-Slicing 下的虚拟槽位，数量由节点配置决定 |
| 内存 | 请求 3Gi / 上限 8Gi | |
| CPU | 请求 500m / 上限 2 | |
| 模型权重 | 7.2 GB | initContainer 下载到模型 PVC |

**显存由 deploy 配置的三项决定**，不随显卡容量自动变化：
`kv_cache_memory_bytes`（显式字节数，非比例）、`decode_cudagraph_batch_sizes`
与 `cudagraph_capture_sizes`（预捕获的图数量）。`gpu_memory_utilization` 虽是
总显存的比例，但在 KV 已显式限额时不是约束项 —— 实测 32 GB 卡上该值为 0.65
（即 21 GB 预算）而实占 9.6 GiB。

可降低显存的唯一途径是调小 `max_num_seqs` 与随之等比的 KV 限额，代价是并发。
各档位的实测对比见 `docs/breeze-tts-serving-layer-eval.md`。

**同卡余量须按各负载的峰值计算。** 2026-10-17 实测（32623 MiB 卡）：

| 负载 | 当前占用 | 峰值占用 | 分配方式 |
|------|---------|---------|---------|
| 本服务 | 9176 MiB | 9176 MiB | 固定，权重 + CUDA Graph |
| `r2t2-asr` | 12400 MiB | 12400 MiB | 固定，`--gpu-memory-utilization` 预分配 |
| `klimt-ml`（immich） | 3136 MiB | 11412 MiB | 动态，`MACHINE_LEARNING_MODEL_TTL=3600` 控制闲置卸载与重载 |

三方峰值之和为 32988 MiB，超出总容量 365 MiB。当前未发生 OOM 的原因是
`klimt-ml` 处于模型已卸载状态。2026-10 早期一次满载测量峰值为 31743 MiB、
余量 387 MiB，当时 `r2t2-asr` 占用 11.2 GiB，其后增长的 1.2 GB 已消耗该余量。

可回收的显存位于 `r2t2-asr`（其 KV cache 为 5.93 GiB，高于现有负载需要），
或由 immich 侧为 `klimt-ml` 设置显存上限。本服务无可调项，理由同上。

## 构建

```bash
docker build -t <repo>/breeze2-tts:<tag> .
```

单阶段：基础镜像 `vllm/vllm-omni:nightly-x86_64`（约 9.1 GB 压缩、解压后约 18 GB）
加一层 adapter 依赖。构建机根分区需预留 20 GB 以上。

基础镜像为 nightly，没有稳定 tag。升级时须先核对 `vllm_omni/deploy/breeze_tts_2.yaml`
是否变更 —— 该文件决定并发与首字节，且随镜像一起更新。

## 部署

```bash
cp -r overlays/example overlays/<env-name>
vi overlays/<env-name>/kustomization.yaml     # namespace、ECR 地址、前缀列表 ID

kubectl config current-context                # 先确认目标集群
kubectl apply -k overlays/<env-name>
```

首次启动需下载 7.2 GB 权重（initContainer）。之后到 `1/1 Running` 的实测耗时：
**缓存未命中约 390 秒、命中 268 秒**。`startupProbe` 的 `failureThreshold: 60` ×
`periodSeconds: 10` = 600 秒已覆盖。

| 配置 | 命中缓存时启动 | `max_autotune` 测速 | 每次重启写入 EFS |
|------|---------------|--------------------|-----------------|
| 仅 `VLLM_CACHE_ROOT` + `TRITON_CACHE_DIR` | 351s | 24 次 | 215-400 MB |
| 再加 `VLLM_USE_AOT_COMPILE=0` | 378s | 24 次 | 10 MB |
| 再加 `TORCHINDUCTOR_CACHE_DIR`（当前） | **268s** | **0 次** | **0** |

剩余耗时主要是 CUDA Graph 捕获与后端 warmup 中的实际推理，缓存无法消除。

三类编译缓存都须落在模型 PVC 上，否则 Pod 重建即丢：

| 变量 | 内容 | 默认位置 |
|------|------|---------|
| `VLLM_CACHE_ROOT` | vLLM 编译产物、flashinfer autotune | `~/.cache/vllm` |
| `TRITON_CACHE_DIR` | warmup 期间 JIT 的 Triton kernel | `/root/.triton` |
| `TORCHINDUCTOR_CACHE_DIR` | Inductor 产物与 `max_autotune` 测速结果 | `/tmp/torchinductor_root` |

**`VLLM_USE_AOT_COMPILE=0` 是对上游缺陷的绕行**
（[vllm-omni#9042](https://github.com/vllm-project/vllm-omni/issues/9042)）。
AOT 路径的缓存键每次启动都不同，产物写入
`torch_compile_cache/torch_aot_compile/<hash>/` 后从不被读取，每次重启新增
215-400 MB。AOT 开启时 vLLM 还会把 `TORCHINDUCTOR_CACHE_DIR` 无条件覆写为该
hash 目录，因此单独设置 `TORCHINDUCTOR_CACHE_DIR` 无效，两项须同时设置。上游修复
后两项一并移除，并清理 `torch_aot_compile/` 下的遗留目录。

缓存键含 torch / Triton 版本、deploy 配置与 GPU 架构，任一变化即重新编译一次。

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
| 升级 vLLM-Omni | 基础镜像 tag |

验证运行中的版本：

```bash
kubectl exec deploy/breeze2-tts -- md5sum /opt/breeze-tts/openai-adapter.py
md5sum base/openai-adapter.py
```

### ALB 端口

同一 ALB group 内一个 listen-port 只能由一个 Ingress 声明，否则
aws-load-balancer-controller 会因监听器冲突拒绝调和。与其它服务共享 ALB 时，
先确认 :8880 未被占用；要从别的服务接管该端口，先删除对方的 Ingress 再部署本模块。

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
| `BREEZE_OMNI_CONFIG` | 镜像内 `breeze_tts_2.yaml` | 后端 deploy 配置。决定并发与首字节，**改动会使所有声纹存下的 seed 产出不同采样轨迹** |
| `VLLM_CACHE_ROOT` | `/shared/cache/vllm` | torch.compile 与 CUDA Graph 缓存目录 |
| `TRITON_CACHE_DIR` | `/shared/cache/breeze-triton` | Triton kernel 缓存目录 |
| `TORCHINDUCTOR_CACHE_DIR` | `/shared/cache/breeze-inductor` | Inductor 与 autotune 缓存目录 |
| `VLLM_USE_AOT_COMPILE` | `0` | 绕开 vllm-omni#9042，见「部署」中的启动耗时 |
| `BREEZE_BACKEND_URL` | `http://localhost:8000` | 后端地址。后端只监听 127.0.0.1，不对外暴露 |
| `VOICES_DIR` | `/shared/voices` | 声纹目录，与 `registry.json` 同级 |
| `BREEZE_MODEL_ID` | `breeze-tts-2` | `/v1/models` 返回的 id |
| `ASR_TRANSCRIBE_URL` | 空 | 注册声纹未给 `ref_text` 时调用的 OpenAI 兼容转写接口（`POST .../v1/audio/transcriptions`），在 overlay 中注入。未设或不可达时 `ref_text` 必填 |
| `BREEZE_BACKEND_MODEL` | 同 `BREEZE_MODEL_ID` | 后端识别的模型名，须与 `entrypoint.sh` 的 `--served-model-name` 一致 |
| `BREEZE_BACKEND_CONCURRENCY` | `4` | 单请求分段并发上限，须与 deploy 配置的 `max_num_seqs` 一致 |

## 部署契约

后端 deploy 配置为 `base/vllm-omni-deploy-config.yaml`，以 ConfigMap 挂载到
`/opt/breeze-tts/deploy/` 并由 `BREEZE_OMNI_CONFIG` 指定，**覆盖镜像内的
`vllm_omni/deploy/breeze_tts_2.yaml`**。因此调并发不必重建镜像。

关键取值：

| 项 | 值 | 作用 |
|---|---|---|
| `max_num_seqs` | 4 | 并发；超出者在后端排队 |
| `kv_cache_memory_bytes` | 1 GiB | 与 `max_num_seqs` 等比 |
| `decode_cudagraph_batch_sizes` | `[1, 2, 4]` | 同上 |
| `initial_codec_chunk_frames` | 1 | 首字节，实测 0.066s |
| `codec_chunk_ramp` | `[1, 2, 4, 5]` | 同上 |
| `max_model_len` | 2048 | 模型上下文，见「已知限制」 |

**改动配置会改变采样轨迹。** 同一 seed 只在同一配置下可复现。

**改 `max_num_seqs` 必须同步改 `BREEZE_BACKEND_CONCURRENCY`**，两者分别是后端的
批容量与 adapter 的分段并发上限。只改前者不会报错，但长文本的分段仍按旧上限发送，
拿不到吞吐；只改后者会让多余的分段在后端排队，拉长每个调用方的延迟。

**不要为提并发去动 codec 分块参数。** 首字节与并发互不耦合：官方吞吐配置的
`initial_codec_chunk_frames: 16` 会把首字节从 0.066s 推到 0.439s，而
`max_num_seqs` 提到 20 时首字节仍是 0.068s。

各档位的显存与吞吐实测见 `docs/breeze-tts-serving-layer-eval.md`。



## 已知限制

**并发为 4，超出者在后端排队而非被拒。** 由 deploy 配置的 `max_num_seqs` 决定，
adapter 侧的 `BREEZE_BACKEND_CONCURRENCY` 须与之一致。线上实测（与 `r2t2-asr`、
`klimt-ml` 同卡）：

| 并发 | 成功 | 总墙钟 | 单请求均值 |
|---|---|---|---|
| 2 | 2/2 | 7.00s | 6.92s |
| 4 | 4/4 | 10.31s | 9.76s |
| 6 | 6/6 | 12.25s | 9.10s |

扩副本不划算：每副本需完整一份约 11 GiB 显存，无法共享权重。提高并发应改
deploy 配置的 `max_num_seqs` 与 KV 限额，各档位的显存与吞吐见
`docs/breeze-tts-serving-layer-eval.md`。

**长文本由 adapter 分句，实测不随长度退化。** 上限来自模型自身：`config.json` 的
`max_position_embeddings = 2048`，除以 12.5 Hz 帧率约为 164 秒音频，而质量在触限
之前即开始下降（重复片段、语速漂移），为自回归 TTS 的共性。adapter 用 `pysbd`
切句并贪心打包至 `MAX_SEGMENT_CHARS`（160），逐段合成后以 `SEGMENT_PAUSE_MS`
（250ms）静音拼接，调用方无需自行切分。分段数见 `X-Breeze-Segments` 响应头。

该上限为权重属性而非后端实现属性，更换服务层不能消除，且超限不产生错误响应而是
静默截断。

**不要为了细粒度时间戳调小 `MAX_SEGMENT_CHARS`。** 打包不只是规避上下文上限，
也是韵律决策：按句合成后固定 250ms 静音会替掉模型自己产生的停顿，各句还失去彼此的
韵律上下文。实测同一段 207 字文本，按句合成与打包的总时长只差 1%（29.65s / 29.93s），
客观指标分辨不出，但听感明显更差。因此段边界必然横跨数句，细粒度标记要靠事后对齐
从音频导出，归属与接口形状见 `r2t2-asr` 的 `POST /v1/audio/alignments`。

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
| `startupProbe` 超时被杀 | 缓存未命中，编译耗时超 600 秒 | 确认 `VLLM_CACHE_ROOT` 可写且在 PVC 上；首次部署属正常，重启应命中缓存 |
| `/shared/cache/vllm` 每次重启增长数百 MB | `VLLM_USE_AOT_COMPILE` 未设为 0（vllm-omni#9042） | 补上该变量，删除 `torch_compile_cache/torch_aot_compile/` 下的旧目录 |
| `/ready` 返回 503 但 Pod Running | 后端未就绪，adapter 已起 | 容器内 `curl localhost:8000/v1/models`；看 `vllm-omni` 的启动日志 |
| 后端 400 `model not recognised` | `BREEZE_BACKEND_MODEL` 与 `--served-model-name` 不一致 | 两者取同一值 |
| 同一 seed 音色变了 | deploy 配置被改动 | 恢复为部署契约中的配置 |
| Pod 一直 Pending | GPU 槽位被占满 | `kubectl describe pod` 看调度事件 |
| 显存不足启动失败 | 同卡其它负载峰值叠加 | 见「资源分配」的余量测算 |
| 换了节点后声纹丢失 | 声纹在 EFS，不会丢；检查 PVC 是否挂载 | 确认 `VOICES_DIR=/shared/voices` |

重启时用 `scale --replicas=0` 等 Pod 消失后再 `--replicas=1`，或 `rollout restart`。
`kubectl delete pod --wait=false` 会绕过 `strategy: Recreate`，新旧 Pod 同时争抢
GPU 槽位，导致新 Pod 落到另一个节点。

## 成本

服务固定占用约 9 GiB 显存，可通过 Time-Slicing 与其它推理服务同卡部署而不必单开
节点，前提是同卡各服务的实测占用之和小于总显存。
