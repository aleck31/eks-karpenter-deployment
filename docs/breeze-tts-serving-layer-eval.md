# Breeze TTS 2 服务层评估：vLLM-Omni vs 上游参考实现

2026-10-17 实测。目标是在不换模型、不换声纹、不改音色的前提下提高并发。

## 背景

`applications/breeze2-tts` 的单并发是**推理引擎层**的约束：
`FastBreezeStreamingRuntime.iter_audio_chunks` 把单请求的解码状态挂在运行时实例上，
两个并发请求会互相覆写，所以那把 `threading.Lock` 是必需的（根因分析见该模块的
`deployment-guide.md`）。

vLLM-Omni 自带 `vllm_omni/entrypoints/openai/tts_adapters/breeze_tts_2.py`
与两份 deploy 配置，**用同一份权重**做批处理调度，支持 `ref_audio` + `ref_text`
克隆、`seed`、流式 codec 与 CFG —— 即我们用到的全部能力。

## 环境

| 项 | 值 |
|---|---|
| 卡 | RTX PRO 4500 Blackwell Server Edition，32623 MiB，**评估时独占** |
| 镜像 | `vllm/vllm-omni:nightly-x86_64` |
| 版本 | vllm_omni 0.31.0rc2.dev246+g6bc3eabd2 / vllm 0.31.0 / torch 2.13.0+cu130 |
| 权重 | `/shared/Breeze-TTS-2`（与生产同一份，EFS） |
| 参考音频 | 声纹 `norman08` 的 `ref.wav` + `ref_text`，全程不变 |

## 结果

负载形态为 adapter 分段后的并发请求（84 字/段），不是单个长文本 —— 后者会撞上
模型 2048 位置的上下文上限，不是生产会发生的情况。倍数为「音频秒 / 墙钟秒」。

| 配置 | `max_num_seqs` | 显存 | 首字节 | RTF | 并发 2 | 并发 4 | 并发 8 | 并发 20 |
|------|---------------|------|--------|-----|--------|--------|--------|---------|
| 上游参考实现（生产） | 1 | 9176 MiB | 0.158s | 0.414 | **429** | **429** | **429** | **429** |
| `breeze_tts_2.yaml` | 2 | 9828 MiB | 0.066s | 0.317 | 5.58x | 5.71x | 5.64x | — |
| 上述改 `max_num_seqs=4` | 4 | 11548 MiB | 0.068s | 0.317 | 5.62x | 9.11x | 10.31x | — |
| 上述改 `max_num_seqs=8` | 8 | 14356 MiB | 0.068s | 0.317 | 5.59x | 9.07x | 15.73x | — |
| 上述改 `max_num_seqs=20` | 20 | 18596 MiB | 0.068s | 0.317 | — | 8.97x | 16.24x | 29.33x |
| `breeze_tts_2_throughput.yaml` | 32 | 28552 MiB | 0.439s | 0.308 | — | — | — | 50.64x |

**全部档位零 429** —— 超出 `max_num_seqs` 的请求在内部排队，不再拒绝。

## 三个结论

**首字节与并发可以兼得。** 吞吐配置的 0.439s 来自 `initial_codec_chunk_frames: 16`，
与 `max_num_seqs` 无关。保留延迟配置的 codec 分块策略（`initial=1`、ramp `[1,2,4,5]`）
后，`max_num_seqs` 提到 20 首字节仍是 0.068s。

**官方吞吐配置不可用。** 28552 MiB 放不进共卡，首字节还退化 6.5 倍，换来的 50x
对我们的负载形态没有意义。

**单请求延迟才是 `max_num_seqs` 的真实收益。** 并发 4 时 `max_num_seqs=2` 每请求
平均 12.57s、`=4` 为 9.24s —— 差的 3.3 秒是第 3、4 个请求的排队时间。并发 ≤2 时
两者无差别，多配的显存纯浪费。

## seed 行为

同 seed 同输入五轮字节完全一致，vLLM-Omni 内部可复现。但采样器不是 `breeze_infer`
的，**同 seed 与上游实现产出不同的一次 take**（同一段文本 18.16s vs 15.68s）。
参考音频决定说话人，seed 只决定哪一次 take，听感确认说话人不变、同 seed 差异不大。

## 选型

取 `max_num_seqs=2`（官方 `breeze_tts_2.yaml`，无自定义 YAML 需维护）：显存比现状
只多 652 MiB，首字节快 2.4 倍、RTF 快 23%、并发不再 429。

没取 `=4`（+1.7 GB 换并发 4 时每请求快 3.3 秒）的原因是同卡余量 —— 该卡三方峰值
之和已超整卡 365 MiB，详见 `applications/breeze2-tts/deployment-guide.md`
的「资源分配」。余量恢复后改 YAML 即可提升，不必重建镜像。

## 迁移范围（未执行）

| 组件 | 处置 |
|------|------|
| 分句（`pysbd`，160 字） | 保留 —— 2048 是权重属性，服务层不做文本切分 |
| 声纹注册表 | 保留 —— vLLM-Omni 的 `/v1/audio/voices` 只返回 `{"voices":["default"]}` |
| GA 接口层（`stream_format`、错误码、字段校验） | 保留 |
| adapter 的后端调用 | 改写：multipart → `ref_audio` data URL + `ref_text` |
| `_BackendSlot` 单槽 + 429 + `/v1/audio/speech/status` | 删除 |
| `patches/`（上游推理锁泄漏补丁） | 删除 —— 该缺陷在参考实现中，不在 vLLM-Omni |
| `Dockerfile.base`（从 breeze 源码构建） | 删除，改用 `vllm/vllm-omni` 镜像 |
| 分段合成 | 串行 → 并发（2 路，约 2 倍） |

迁移后需重跑长文本分段验证与全部声纹的听感确认。

## 踩到的两个部署坑

评估用的临时 NodePool 配置错了两次，记下来免得重犯：

- **不能加自定义 taint。** `kube-system/nvidia-device-plugin` 只容忍 `nvidia.com/gpu`
  与 `CriticalAddonsOnly`，多一个 taint 它就不上该节点，GPU 永远不注册为可分配资源
  （现象：`nvidia.com/gpu: <none>`，pod 一直 Pending 报 `Insufficient nvidia.com/gpu`）。
- **`nvidia.com/gpu` 只能申请 1。** 时间切片下申请 3 个槽位会让容器运行时收到同一个
  GPU UUID 重复三次的 `NVIDIA_VISIBLE_DEVICES`，于是不创建 `/dev/nvidia0`，容器里
  反而看不到 GPU（`nvidia-smi` 报 `No devices were found`）。
- NodePool 改了 taint 后**已存在的节点不会回溯更新**，需删掉 NodeClaim 重建，
  或手动 `kubectl taint node <node> <key>-`。
