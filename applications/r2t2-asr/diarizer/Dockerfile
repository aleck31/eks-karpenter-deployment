# r2t2-asr 说话人分离 sidecar：Nemotron 3 Diarization，CPU 推理。
#
# 与 ASR 镜像分开：NeMo 需要 Python >= 3.12，ASR 镜像锁定在 3.10 / transformers 4.57。
# NeMo 固定到 main 上的提交：PyPI 上的 3.0.0 不支持该模型的 rope 注意力。
#   DOCKER_BUILDKIT=1 docker build -f diarizer/Dockerfile -t <account>.dkr.ecr.<region>.amazonaws.com/r2t2-asr-diarizer:<tag> .
ARG NEMO_COMMIT=67ecdc083a8d3cd09172f35e2bb5c2a049b2f506

FROM python:3.12-slim AS build
ARG NEMO_COMMIT
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# CPU 版 torch 先装，后面的约束保证它不被换成 CUDA 版。
RUN pip install --no-cache-dir torch==2.9.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cpu \
 && printf 'torch==2.9.1\ntorchaudio==2.9.1\n' > /tmp/pin.txt \
 && pip install --no-cache-dir -c /tmp/pin.txt Cython packaging \
 && pip install --no-cache-dir -c /tmp/pin.txt \
        "nemo-toolkit[asr] @ git+https://github.com/NVIDIA-NeMo/Speech@${NEMO_COMMIT}" \
        "pyarrow<20" fastapi==0.128.0 "uvicorn==0.40.0" soundfile==0.13.1 \
 && python -c "from nemo.collections.asr.models import SortformerEncLabelModel; import torch; assert not torch.cuda.is_available(); print('deps ok', torch.__version__)"

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH HF_HUB_OFFLINE=1 PYTHONUNBUFFERED=1
# Kubernetes 中运行的是 ConfigMap 挂载的那份，这份供本地使用。
COPY base/diarizer-server.py /app/diarizer-server.py
WORKDIR /app
EXPOSE 8010
ENTRYPOINT ["uvicorn", "diarizer-server:app", "--host", "127.0.0.1", "--port", "8010", "--app-dir", "/app"]
