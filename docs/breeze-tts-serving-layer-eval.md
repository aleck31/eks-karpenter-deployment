# Breeze TTS 2 服务层评估报告

| | |
|---|---|
| 评估日期 | 2026-10-17 |
| 评估对象 | vLLM-Omni 作为 Breeze TTS 2 的服务层，对照上游参考实现 `breeze_infer.api` |
| 结论 | 采用 vLLM-Omni，配置 `max_num_seqs=2`；迁移未执行 |

## 1. 目的与约束

提高 `applications/breeze2-tts` 的并发能力，约束条件为模型权重、声纹注册表与
输出音色均不变。

现有部署的单并发是**推理引擎层**限制，非模型或显存限制：
`FastBreezeStreamingRuntime.iter_audio_chunks` 将单请求的解码状态
（`self._backbone_graph` 及其 KV cache、`self._codec_runtime`）持有于运行时实例，
并发请求相互覆写，故 `api.py` 中的 `threading.Lock` 为必需项。完整根因分析见
`applications/breeze2-tts/deployment-guide.md`「已知限制」。

vLLM-Omni 提供 `vllm_omni/entrypoints/openai/tts_adapters/breeze_tts_2.py`
及两份 deploy 配置，对同一权重实施连续批处理调度，支持 `ref_audio` + `ref_text`
声纹克隆、`seed`、流式 codec 与 CFG，覆盖现有部署使用的全部推理能力。

## 2. 测试环境

| 项 | 值 |
|---|---|
| GPU | RTX PRO 4500 Blackwell Server Edition，32623 MiB，评估期间独占 |
| 实例 | g7.2xlarge（Spot），专用 Karpenter NodePool |
| 镜像 | `vllm/vllm-omni:nightly-x86_64` |
| 软件版本 | vllm_omni 0.31.0rc2.dev246+g6bc3eabd2 / vLLM 0.31.0 / torch 2.13.0+cu130 |
| 权重 | `/shared/Breeze-TTS-2`，与生产环境同一副本（EFS） |
| 对照基线 | 生产 `breeze2-tts`，同卡与 `r2t2-asr`、`klimt-ml` 共享（Time-Slicing） |

## 3. 测量方法

**负载形态。** 请求文本为 84 字单段，对应 adapter 分段后的实际请求粒度
（`MAX_SEGMENT_CHARS = 160`）。不使用单个长文本，因其会触及模型上下文上限
（见第 6 节），非生产负载形态。

**受控变量。** 全部测量使用声纹 `norman08` 的同一 `ref.wav` 与 `ref_text`，
`seed=42`，`response_format` 固定。参考音频不同会改变输出时长，污染 RTF。

**指标定义。**

| 指标 | 定义 | 采集方式 |
|------|------|---------|
| RTF | 生成墙钟耗时 ÷ 输出音频时长 | 预热 1 次后取 5 轮均值 |
| 首字节（TTFA） | 请求发出至收到首个音频字节 | `stream_format=audio`，`response_format=pcm`，3 轮均值 |
| 吞吐 | 单位墙钟时间内产出的音频秒数（音频秒/墙钟秒） | 同时发起 N 个请求，取全部完成的总音频时长 ÷ 总墙钟 |
| 显存 | `nvidia-smi` 报告的已用显存 | 基准运行结束时采样，含 CUDA Graph 与 KV cache |

**口径一致性。** 初次测量中延迟配置与中间配置分别使用了不同的测试脚本
（文本长度与时长计算方式不同），数据不可比。表 4 的全部数据已用同一脚本、
同一文本重测。

## 4. 结果

| 配置 | `max_num_seqs` | 显存 | TTFA | RTF | 并发 2 | 并发 4 | 并发 8 | 并发 20 |
|------|---------------|------|------|-----|--------|--------|--------|---------|
| 上游参考实现（生产） | 1 | 9176 MiB | 0.158s | 0.414 | 429 | 429 | 429 | 429 |
| `breeze_tts_2.yaml` | 2 | 9828 MiB | 0.066s | 0.317 | 5.58x | 5.71x | 5.64x | — |
| 同上，`max_num_seqs=4` | 4 | 11548 MiB | 0.068s | 0.317 | 5.62x | 9.11x | 10.31x | — |
| 同上，`max_num_seqs=8` | 8 | 14356 MiB | 0.068s | 0.317 | 5.59x | 9.07x | 15.73x | — |
| 同上，`max_num_seqs=20` | 20 | 18596 MiB | 0.068s | 0.317 | — | 8.97x | 16.24x | 29.33x |
| `breeze_tts_2_throughput.yaml` | 32 | 28552 MiB | 0.439s | 0.308 | — | — | — | 50.64x |

并发列为吞吐（音频秒/墙钟秒）。`max_num_seqs` 以外的参数继承对应基准配置；
中间配置的 `kv_cache_memory_bytes` 与 `decode_cudagraph_batch_sizes` 按
`max_num_seqs` 等比调整。

全部配置在各并发档位均无 429 响应：超出 `max_num_seqs` 的请求在服务端排队。

单请求延迟（并发 4，均值）：`max_num_seqs=2` 为 12.57s，`=4` 为 9.24s。

## 5. 分析

**TTFA 与并发无耦合关系。** `breeze_tts_2_throughput.yaml` 的 TTFA 为 0.439s，
较延迟配置高 6.6 倍，成因是 `initial_codec_chunk_frames: 16`，与 `max_num_seqs`
无关。保留延迟配置的 codec 分块策略（`initial_codec_chunk_frames: 1`、
`codec_chunk_ramp: [1,2,4,5]`）后，`max_num_seqs` 提升至 20 时 TTFA 仍为 0.068s。

**官方吞吐配置不适用于本部署。** 显存需求 28552 MiB 超出共卡可用额度，
且 TTFA 退化至 0.439s。

**`max_num_seqs` 的收益体现在排队延迟。** 吞吐提升的实质是减少排队：并发 4 时
`max_num_seqs=2` 与 `=4` 的单请求延迟差 3.33s，即第 3、4 个请求的等待时间。
并发不超过 `max_num_seqs` 时两配置无差异。

**seed 语义变更。** 同 seed 同输入连续 5 轮输出字节一致，vLLM-Omni 内部可复现。
但其采样器与 `breeze_infer` 不同，同 seed 在两种服务层下产出不同采样轨迹
（同一文本输出时长 18.16s 与 15.68s）。参考音频决定说话人特征，seed 决定采样轨迹，
故音色不受影响；已通过听辨确认。已注册声纹存储的 seed 在迁移后指向不同输出。

## 6. 模型上下文上限

`config.json` 的 `max_position_embeddings = 2048`，codec 帧率 12.5 Hz，
对应约 164 秒音频上限。该值为权重属性，两种服务层共同受限，且超限不产生错误响应
而是静默截断。

验证：绕过 adapter 直接向 vLLM-Omni 提交 830 字文本（约 2804 codec 帧），
输出音频 102.80s，短于同条件下 494 字的 120.00s；ASR 回转命中 7/20 个标记，
转写 328 字，原文 740 字（去标点）。

故 adapter 的分句与分段打包（`pysbd` + `MAX_SEGMENT_CHARS`）在迁移后须保留。

## 7. 结论与建议

采用 `breeze_tts_2.yaml`（`max_num_seqs=2`），无需维护自定义 deploy 配置。
相对现有部署：显存增加 652 MiB，TTFA 降低 58%，RTF 降低 23%，并发请求由拒绝
（429）改为排队。

未采用 `max_num_seqs=4`（增加 1720 MiB，并发 4 时单请求延迟降低 3.33s），
原因为同卡显存余量不足：该卡三方负载峰值之和已超出总容量 365 MiB，
详见 `applications/breeze2-tts/deployment-guide.md`「资源分配」。余量恢复后
可仅修改 deploy 配置提升，无需重建镜像。

## 8. 迁移范围（未执行）

| 组件 | 处置 | 依据 |
|------|------|------|
| 分句与分段打包（`pysbd`，160 字） | 保留 | 模型上下文上限，见第 6 节 |
| 声纹注册表 | 保留 | vLLM-Omni 的 `/v1/audio/voices` 仅返回 `{"voices":["default"]}`，无持久化注册能力 |
| GA 接口层（`stream_format`、错误码、字段校验） | 保留 | 调用方契约 |
| adapter 后端调用 | 改写 | 请求体由 multipart 改为 `ref_audio` data URL + `ref_text` |
| `_BackendSlot`、429 响应、`/v1/audio/speech/status` | 删除 | 服务端已实现排队 |
| `patches/release-inference-lock-on-disconnect.py` | 删除 | 所修复缺陷位于参考实现，vLLM-Omni 不涉及 |
| `Dockerfile.base` | 删除 | 改用 `vllm/vllm-omni` 镜像，不再从源码构建 |
| 分段合成 | 串行改并发 | `max_num_seqs=2` 下预期提升约 2 倍 |

迁移后须重新执行长文本分段验证（含 ASR 回转内容核对）与全部已注册声纹的听辨确认。

## 附录：评估环境配置约束

临时 NodePool 的以下三项配置错误均导致 Pod 无法获得 GPU，记录以备复用。

**不可附加自定义 taint。** `kube-system/nvidia-device-plugin` 仅容忍
`nvidia.com/gpu` 与 `CriticalAddonsOnly`。附加其他 taint 后该 DaemonSet 不调度至
节点，GPU 不注册为可分配资源。现象为节点 `allocatable.nvidia.com/gpu` 为空、
Pod 持续 Pending 并报 `Insufficient nvidia.com/gpu`。

**`nvidia.com/gpu` 请求量须为 1。** Time-Slicing 下请求 3 个槽位时，容器运行时
接收到的 `NVIDIA_VISIBLE_DEVICES` 为同一 GPU UUID 重复三次，不创建 `/dev/nvidia0`，
容器内 `nvidia-smi` 报 `No devices were found`。

**NodePool 的 taint 变更不回溯既有节点。** 需删除对应 NodeClaim 触发重建，
或对节点执行 `kubectl taint node <node> <key>-`。
