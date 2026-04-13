#!/bin/bash
set -e

# Start Nano-vLLM backend on :8000
uv run --no-sync fastapi run deployment/app/main.py --host 0.0.0.0 --port 8000 &

# Start OpenAI adapter on :8880
uv run --no-sync uvicorn openai-adapter:app --host 0.0.0.0 --port 8880 &

wait -n
exit $?
