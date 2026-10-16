# R2T2 ASR 镜像：官方 Qwen3-ASR 镜像 + R2T2 推理包 + FireRed 流式 VAD + 服务层。
#
# 基础镜像固定为 qwenllm/qwen3-asr（vLLM 0.14 / transformers 4.57）：R2T2 与
# qwen-asr 都锁定这组版本，在 sm_120（Blackwell）GPU 上实测可用。生产镜像
# 推到 ECR，不在运行时依赖 Docker Hub：
#   DOCKER_BUILDKIT=1 docker build -t <account>.dkr.ecr.<region>.amazonaws.com/r2t2-asr:<tag> .
ARG BASE_IMAGE=qwenllm/qwen3-asr@sha256:fb75b775f089e06e5a1aaebffd421e37505cc630d50c86d889d95ffa45a7e16a
FROM ${BASE_IMAGE}

# R2T2 固定到提交而非分支，权重的版本在 Deployment 的 initContainer 里固定。
ARG R2T2_COMMIT=26d55a54ce5670cff9947a167d8ed95d569fd4d9

# --no-deps：R2T2 声明的依赖（qwen-asr[vllm]、sanic 等）基础镜像已有或用不到，
# 让 pip 解析会改动镜像里与 GPU 匹配的 torch / vLLM。fireredvad 的运行依赖
# （kaldiio、kaldi-native-fbank、textgrid）逐个列出并固定版本。
RUN pip install --no-cache-dir --no-deps \
        "confucius4-r2t2 @ https://codeload.github.com/netease-youdao/Confucius4-R2T2/tar.gz/${R2T2_COMMIT}" \
        fireredvad==0.0.2 \
        kaldiio==2.18.1 \
        kaldi-native-fbank==1.22.3 \
        textgrid==1.6.1 \
        zhconv==1.4.3 \
 && python3 -c "import r2t2, fireredvad, kaldi_native_fbank, zhconv, fastapi, uvicorn, multipart, websockets; print('deps ok')"

# Kubernetes 中运行的是 ConfigMap 挂载的那份，这份供本地使用。
COPY base/asr-server.py /app/asr-server.py

# 模型目录是本地路径；不设离线模式时 vLLM 会把它当仓库名去 Hugging Face 查询。
ENV HF_HUB_OFFLINE=1

WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["uvicorn", "asr-server:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app"]
