# Breeze TTS 2 服务镜像：vLLM-Omni 后端 + OpenAI 兼容 adapter。
#
# vLLM-Omni 自带 breeze_tts_2 适配器与 deploy 配置，对同一份权重做连续批处理调度，
# 故不再从 breeze-tts 源码构建基础镜像，也不再需要推理锁补丁（该缺陷位于上游参考
# 实现，见 breezeblue-ai/breeze-tts#20）。选型与实测见
# docs/breeze-tts-serving-layer-eval.md。
#
# 生产镜像推到 ECR，不在运行时依赖 Docker Hub：
#   docker build -t <account>.dkr.ecr.<region>.amazonaws.com/breeze2-tts:<tag> .
ARG BASE_IMAGE=vllm/vllm-omni:nightly-x86_64
FROM ${BASE_IMAGE}

# adapter 依赖。pydub 需要 ffmpeg 做 mp3/opus/aac 转码，基础镜像未含。
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pydub httpx python-multipart pysbd \
 && python3 -c "import pydub, httpx, pysbd, multipart, vllm_omni; print('deps ok')"

# 集群运行 ConfigMap 挂载的副本，这份仅供本地使用。
COPY base/openai-adapter.py /opt/breeze-tts/openai-adapter.py
COPY entrypoint.sh /opt/breeze-tts/entrypoint.sh
RUN chmod +x /opt/breeze-tts/entrypoint.sh

# 模型目录是本地路径；不设离线模式时会被当作仓库名去 Hugging Face 查询。
ENV HF_HUB_OFFLINE=1

WORKDIR /opt/breeze-tts
EXPOSE 8000 8880

ENTRYPOINT ["/opt/breeze-tts/entrypoint.sh"]
