#!/bin/bash
set -e

MODEL_PATH="${BREEZE_MODEL_PATH:-/shared/Breeze-TTS-2}"

# 加速参数是部署契约的一部分。改动会使所有已注册声纹存下的 seed 产出不同音色，
# 实测数据与取舍依据见部署指南「部署契约」。
BREEZE_FAST_FLAGS="${BREEZE_FAST_FLAGS:---fast-depth-decoder --fast-backbone-decode --fast-codec}"

# Breeze 自带推理服务。官方默认 7860，此处统一为 8000，与其它 tts 服务一致。
python -m breeze_infer.api "$MODEL_PATH" \
  --host 127.0.0.1 --port 8000 \
  ${BREEZE_FAST_FLAGS} &

# OpenAI 兼容 adapter（对外端口）
uvicorn openai-adapter:app --host 0.0.0.0 --port 8880 &

wait -n
exit $?
