# 基于 Nano-vLLM-VoxCPM 基础镜像（由 Dockerfile.base 构建），集成 OpenAI adapter。
# 基础镜像若已推送至私有 ECR，构建时传入实际地址：
#   docker build --build-arg BASE_IMAGE=<account>.dkr.ecr.<region>.amazonaws.com/voxcpm2-tts:base-2.0.3 .
ARG BASE_IMAGE=nano-vllm-voxcpm-deployment:latest
FROM ${BASE_IMAGE}

# 安装 adapter 依赖 (ffmpeg 已在基础镜像中)
RUN uv pip install --no-cache-dir pydub httpx python-multipart

# 复制 adapter 和启动脚本
COPY --chown=appuser:appuser openai-adapter.py /app/openai-adapter.py
COPY --chown=appuser:appuser entrypoint.sh /app/entrypoint.sh

USER root
RUN chmod +x /app/entrypoint.sh
USER appuser

WORKDIR /app
EXPOSE 8000 8880

ENTRYPOINT ["/app/entrypoint.sh"]
