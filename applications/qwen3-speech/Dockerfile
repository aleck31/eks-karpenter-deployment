# 基础镜像为 vllm/vllm-openai + soundfile/av，已推送至私有 ECR。
# 构建时传入实际仓库地址，避免账号 ID 硬编码：
#   docker build --build-arg BASE_IMAGE=<account>.dkr.ecr.<region>.amazonaws.com/qwen3-asr:latest .
ARG BASE_IMAGE=qwen3-asr:latest
FROM ${BASE_IMAGE}

# silero-vad declares its own torch, which downgrades the image from 2.11.0+cu130
# to 2.9.1+cu128 and breaks vLLM's compiled extension at import
# (undefined symbol c10_cuda_check_implementation). --no-deps keeps the engine's
# torch intact; onnxruntime is the only dependency silero-vad actually needs at
# runtime and is installed normally.
RUN pip install websockets python-multipart onnxruntime --no-cache-dir \
 && pip install --no-deps silero-vad --no-cache-dir

COPY asr-adapter.py /app/asr-adapter.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV ASR_BACKEND_URL=http://localhost:8000
ENV ASR_BACKEND_WS=ws://localhost:8000

EXPOSE 8000 8001

ENTRYPOINT ["/app/entrypoint.sh"]
