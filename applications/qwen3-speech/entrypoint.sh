#!/bin/bash
set -e

# Start vLLM ASR backend on :8000
vllm serve "$@" &

# Start OpenAI adapter on :8001
uvicorn asr-adapter:app --host 0.0.0.0 --port 8001 --app-dir /app &

wait -n
exit $?
