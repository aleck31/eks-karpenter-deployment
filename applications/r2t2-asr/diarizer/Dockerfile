# r2t2-asr 说话人分离 sidecar：Nemotron 3 Diarization，CPU 推理。
#
# 与 ASR 镜像分开：NeMo 需要 Python >= 3.12，ASR 镜像锁定在 3.10 / transformers 4.57。
# NeMo 固定到 main 上的提交：PyPI 上的 3.0.0 不支持该模型的 rope 注意力。
# CAM++（3D-Speaker，Apache-2.0）用于 8 个通道用满时的说话人核对：权重打进镜像（28 MB），
# 模型代码取 speakerlab 中 campplus 的两个文件。
#   DOCKER_BUILDKIT=1 docker build -f diarizer/Dockerfile -t <account>.dkr.ecr.<region>.amazonaws.com/r2t2-asr-diarizer:<tag> .
ARG NEMO_COMMIT=67ecdc083a8d3cd09172f35e2bb5c2a049b2f506
ARG SPEAKERLAB_COMMIT=065629c313eaf1a01c65c640c46d77e61e9607b4
ARG CAMPPLUS_URL=https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/resolve/v2.0.2/campplus_cn_common.bin
ARG CAMPPLUS_SHA256=3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8

FROM python:3.12-slim AS build
ARG NEMO_COMMIT
ARG SPEAKERLAB_COMMIT
ARG CAMPPLUS_URL
ARG CAMPPLUS_SHA256
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git curl \
 && rm -rf /var/lib/apt/lists/*
RUN d=/opt/campplus/speakerlab/models/campplus && mkdir -p $d \
 && touch /opt/campplus/speakerlab/__init__.py /opt/campplus/speakerlab/models/__init__.py $d/__init__.py \
 && for f in DTDNN.py layers.py; do \
      curl -fsSL -o $d/$f https://raw.githubusercontent.com/modelscope/3D-Speaker/${SPEAKERLAB_COMMIT}/speakerlab/models/campplus/$f; done \
 && curl -fsSL -o /opt/campplus/campplus_cn_common.bin "${CAMPPLUS_URL}" \
 && echo "${CAMPPLUS_SHA256}  /opt/campplus/campplus_cn_common.bin" | sha256sum -c -
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# CPU 版 torch 先装，后面的约束保证它不被换成 CUDA 版。
RUN pip install --no-cache-dir torch==2.9.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cpu \
 && printf 'torch==2.9.1\ntorchaudio==2.9.1\n' > /tmp/pin.txt \
 && pip install --no-cache-dir -c /tmp/pin.txt Cython packaging \
 && pip install --no-cache-dir -c /tmp/pin.txt \
        "nemo-toolkit[asr] @ git+https://github.com/NVIDIA-NeMo/Speech@${NEMO_COMMIT}" \
        "pyarrow<20" fastapi==0.128.0 "uvicorn==0.40.0" soundfile==0.13.1 \
 && PYTHONPATH=/opt/campplus python -c "from nemo.collections.asr.models import SortformerEncLabelModel; from speakerlab.models.campplus.DTDNN import CAMPPlus; import scipy, torchaudio.compliance.kaldi, torch; assert not torch.cuda.is_available(); print('deps ok', torch.__version__)"

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/campplus /opt/campplus
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/campplus HF_HUB_OFFLINE=1 PYTHONUNBUFFERED=1
# Kubernetes 中运行的是 ConfigMap 挂载的那份，这份供本地使用。
COPY base/diarizer-server.py /app/diarizer-server.py
WORKDIR /app
EXPOSE 8010
ENTRYPOINT ["uvicorn", "diarizer-server:app", "--host", "127.0.0.1", "--port", "8010", "--app-dir", "/app"]
