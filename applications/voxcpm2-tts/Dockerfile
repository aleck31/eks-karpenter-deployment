# 基于 Nano-vLLM-VoxCPM 官方镜像，集成 OpenAI adapter
FROM nano-vllm-voxcpm-deployment:latest

# 安装 adapter 依赖 (ffmpeg 已在基础镜像中)
RUN uv pip install --no-cache-dir pydub httpx

# 复制 adapter 和启动脚本
COPY --chown=appuser:appuser openai-adapter.py /app/openai-adapter.py
COPY --chown=appuser:appuser entrypoint.sh /app/entrypoint.sh

USER root
RUN chmod +x /app/entrypoint.sh
USER appuser

WORKDIR /app
EXPOSE 8000 8880

ENTRYPOINT ["/app/entrypoint.sh"]
