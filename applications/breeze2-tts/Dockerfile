# 基于 Breeze TTS 2 基础镜像（由 Dockerfile.base 构建），集成 OpenAI adapter。
# 基础镜像若已推送至私有 ECR，构建时传入实际地址：
#   docker build --build-arg BASE_IMAGE=<account>.dkr.ecr.<region>.amazonaws.com/breeze2-tts:base-<ref> .
ARG BASE_IMAGE=breeze2-tts:base
FROM ${BASE_IMAGE}

# adapter 依赖（ffmpeg 已在基础镜像中）
RUN pip install --no-cache-dir pydub httpx python-multipart pysbd

# 集群运行 ConfigMap 挂载的副本，这份仅供本地使用。
COPY --chown=appuser:appuser base/openai-adapter.py /opt/breeze-tts/openai-adapter.py
COPY --chown=appuser:appuser entrypoint.sh /opt/breeze-tts/entrypoint.sh

USER root
RUN chmod +x /opt/breeze-tts/entrypoint.sh
USER appuser

WORKDIR /opt/breeze-tts
EXPOSE 8000 8880

ENTRYPOINT ["/opt/breeze-tts/entrypoint.sh"]
