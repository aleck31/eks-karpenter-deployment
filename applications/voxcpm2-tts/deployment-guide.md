# VoxCPM2 TTS - OpenAI 兼容 TTS 服务

> 同一 ALB 的 :8880 一个 listen-port 只能由一个 Ingress 声明，本模块与
> `applications/breeze2-tts` 不能同时挂载 Ingress。两者共用 `/shared/voices`
> 下的同一份 `registry.json`；breeze2-tts 额外要求每个声纹带 `ref_text`
> （参考音频的准确文字稿），该字段本模块会忽略。

基于 VoxCPM2 + Nano-vLLM 推理引擎，通过 OpenAI 兼容 adapter 对外提供 `/v1/audio/speech` 等语音合成接口。

接口、字段、错误码与响应头见 [api-reference.md](api-reference.md)。

## 架构

```
下游应用 → ALB :8880 → adapter (OpenAI API) → localhost:8000 → VoxCPM2 (Nano-vLLM)
                        /v1/audio/speech              /generate
                        /v1/audio/clone
                        /v1/audio/voices
```

单容器内运行两个进程：
- Nano-vLLM backend (:8000) — GPU 推理引擎
- OpenAI adapter (:8880) — 接口转换 + Voice 管理 + 音频格式转码

## 存储

- 模型: EFS `/shared/VoxCPM2/`
- Voice 参考音频: EFS `/shared/voices/{voice_id}/ref.wav`
- Voice 注册表: EFS `/shared/voices/registry.json`
- 注册表 schema 版本: EFS `/shared/voices/.schema`

存储目录须归属容器运行用户（uid 10001）。目录属 root 时读操作正常但 POST/PUT/DELETE 会因
`PermissionError` 返回 500，表现为「能查不能写」：

```bash
chown -R 10001:10001 /shared/voices
find /shared/voices -type d -exec chmod 775 {} \;
find /shared/voices -type f -exec chmod 664 {} \;
```

`registry.json` 条目结构：

```json
{
  "cedar": {
    "file": "ref.wav",
    "name": "Cedar",
    "description": "Steady, mature male mentor",
    "builtin": true,
    "audio": {"duration_seconds": 5.3, "sample_rate": 16000,
              "channels": 1, "codec": "pcm_s16le", "size_bytes": 169806}
  }
}
```

- `builtin: true` 是预置 voice 的保护标记，缺失则 DELETE/PUT 不会被拦截。手工铺入预置 voice 时必须写入
- `audio` 在注册时探测并存下，detail 读取时不再跑 ffprobe
- 通过 API 注册的条目还带 `created_at`；预置 voice 无此字段

`.schema` 记录已执行的迁移版本。启动时的回填逻辑据此判断是否需要运行，**不依赖字段是否存在** ——
早期版本用「无 `created_at` 即预置」推断 `builtin`，该推断仅对预置那批数据成立；若每次启动都重跑，
任何因写入中断而缺 `created_at` 的用户声纹都会被永久标为预置并锁进 403。

> 模型存储复用 qwen3-speech 模块的 `qwen3-models-pvc`，
> 需先部署 `applications/qwen3-speech/base/shared/`（EFS StorageClass + PVC）。

## 部署

采用 kustomize base + overlay，环境相关取值（namespace、ECR 账号）不入库。

```
applications/voxcpm2-tts/
├── base/
│   ├── kustomization.yaml
│   ├── voxcpm2-tts-deployment.yaml   # Deployment + Service
│   └── voxcpm2-tts-ingress.yaml      # ALB Ingress (group: speech-services)
├── overlays/
│   ├── example/                      # 入库：示例取值，供复制
│   └── <env-name>/                   # 不入库：真实取值
├── Dockerfile                        # adapter 层
├── Dockerfile.base                   # Nano-vLLM 基础镜像
├── openai-adapter.py
└── entrypoint.sh
```

### 构建镜像

```bash
# 基础镜像（首次或依赖变更时）
docker build -f Dockerfile.base -t voxcpm2-tts:base-2.0.3 .

# adapter 层（~3秒）。BASE_IMAGE 指向基础镜像所在位置
DOCKER_BUILDKIT=1 docker buildx build --load \
  --build-arg BASE_IMAGE=<account>.dkr.ecr.<region>.amazonaws.com/voxcpm2-tts:base-2.0.3 \
  -t voxcpm2-tts:latest .

docker tag voxcpm2-tts:latest <account>.dkr.ecr.<region>.amazonaws.com/voxcpm2-tts:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/voxcpm2-tts:latest
```

### 准备 overlay

```bash
cp -r overlays/example overlays/<env-name>
# 编辑 namespace 与 ECR 镜像地址
vi overlays/<env-name>/kustomization.yaml
```

> `.gitignore` 默认忽略 `overlays/` 下全部目录、仅放行 `example/`，真实取值不会误提交。

### 部署到 EKS

```bash
kubectl config current-context          # 先确认目标集群

# 先清空旧 Pod 再部署，避免与新 Pod 争抢同一张 GPU 导致 OOM
kubectl scale deployment voxcpm2-tts -n <namespace> --replicas=0
# 待旧 Pod 完全终止后
kubectl apply -k overlays/<env-name>
```

## 硬件配置

- GPU: NVIDIA L4 (g6.xlarge Spot)
- 模型显存: ~9.6GB (gpu_memory_utilization: 0.45, max_model_len: 8192)
- 推理延迟: 短文本 ~1.5s, 中等文本 ~6s (非流式); 流式 TTFB ~0.37s
