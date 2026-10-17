#!/bin/bash
set -e

MODEL_PATH="${BREEZE_MODEL_PATH:-/shared/Breeze-TTS-2}"

# 后端为 vLLM-Omni，deploy 配置决定并发与首字节。默认取官方延迟配置：
# max_num_seqs=2、initial_codec_chunk_frames=1。实测数据与选型依据见
# docs/breeze-tts-serving-layer-eval.md。改并发须同时调整 adapter 的
# BREEZE_BACKEND_CONCURRENCY。
OMNI_CONFIG="${BREEZE_OMNI_CONFIG:-$(python3 -c 'import vllm_omni,pathlib;print(pathlib.Path(vllm_omni.__file__).parent/"deploy"/"breeze_tts_2.yaml")')}"

# mode: 3 的 torch.compile 产物与 CUDA Graph 可缓存，缓存命中可省去大部分启动时间。
# 缓存键含模型、配置与 GPU 架构，三者任一变化即失效并重新编译。
export VLLM_CACHE_ROOT="${VLLM_CACHE_ROOT:-/shared/cache/vllm}"
mkdir -p "$VLLM_CACHE_ROOT"

# 模型 id 对应上游模型名 Breeze-TTS-2（注意与模块目录名 breeze2-tts 的词序不同）。
# adapter 的 MODEL_ID 读同一个变量，两处必须一致，否则后端不认这个 model 名。
MODEL_ID="${BREEZE_MODEL_ID:-breeze-tts-2}"

vllm-omni serve "$MODEL_PATH" \
  --deploy-config "$OMNI_CONFIG" \
  --omni \
  --host 127.0.0.1 --port 8000 \
  --served-model-name "$MODEL_ID" &

# OpenAI 兼容 adapter（对外端口）
uvicorn openai-adapter:app --host 0.0.0.0 --port 8880 &

wait -n
exit $?
