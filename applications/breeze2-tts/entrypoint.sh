#!/bin/bash
set -e

MODEL_PATH="${BREEZE_MODEL_PATH:-/shared/Breeze-TTS-2}"

# 加速参数是部署契约的一部分，不要随意改动。
#
# 实测（L4 24GB，中文 60 字，取 RTF = 生成耗时 / 音频时长）：
#   无参数                                        8.3 GiB  RTF 2.33
#   --fast-depth-decoder                          9.0 GiB  RTF 1.07
#   + --fast-backbone-decode --fast-codec         8.9 GiB  RTF 0.88
#   --fast-all（再加 text-encoder/backbone-prefill）16.4 GiB  RTF 0.88
#
# text-encoder 与 backbone-prefill 两个阶段在一次生成里各只执行一次，按
# 「batch size × 长度分桶」预捕获数十份静态 CUDA Graph 常驻显存，单开分别多占
# 6.7 / 10.7 GiB 却不提速。故不启用 --fast-all。
#
# 另：同一 seed 只在同一参数组合下可复现。声纹里存下的 seed 依赖此组合不变，
# 改动会使所有已注册声纹的音色发生变化。
BREEZE_FAST_FLAGS="${BREEZE_FAST_FLAGS:---fast-depth-decoder --fast-backbone-decode --fast-codec}"

# Breeze 自带推理服务（内部后端）
# 官方默认端口为 7860，此处统一为 8000，与其它 tts 服务约定一致
python -m breeze_infer.api "$MODEL_PATH" \
  --host 127.0.0.1 --port 8000 \
  ${BREEZE_FAST_FLAGS} &

# OpenAI 兼容 adapter（对外端口）
uvicorn openai-adapter:app --host 0.0.0.0 --port 8880 &

wait -n
exit $?
