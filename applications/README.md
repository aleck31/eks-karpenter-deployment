# EKS 应用部署管理

## 📋 目录说明

本目录管理 EKS 集群中部署的各类应用，每个应用独立成子目录，含配置清单与部署指南。

## 📁 目录结构

```
/applications/
├── README.md                    # 本说明文档
├── qwen3-speech/                # Qwen3 ASR 语音识别（含 Qwen3-TTS 可选组件）
├── voxcpm2-tts/                 # VoxCPM2 TTS 语音合成（OpenAI 兼容 + Voice 管理）
├── bitwarden/                   # Bitwarden 密码管理
├── convertx/                    # ConvertX 文件转换
├── auto-draw-io/                # Auto-Draw-IO 图表生成（Bedrock）
└── firecrawl/                    # Firecrawl 网页抓取 API（多组件栈）
```

## 📦 配置与环境值分离

多数应用采用 kustomize base + overlay，把「结构」与「环境取值」分开：

```
<应用>/
├── base/                        # 入库：通用清单，不含任何环境相关取值
└── overlays/
    ├── example/                 # 入库：示例取值，供复制
    └── <env-name>/              # 不入库：真实取值
```

部署：

```bash
cp -r <应用>/overlays/example <应用>/overlays/<env-name>
vi <应用>/overlays/<env-name>/kustomization.yaml    # 填入实际取值

kubectl config current-context                       # 先确认目标集群
kubectl apply -k <应用>/overlays/<env-name>
```

已采用该结构的应用：`qwen3-speech`、`voxcpm2-tts`、`bitwarden`、`convertx`、`auto-draw-io`。

`firecrawl` 无环境相关取值（namespace 由 `deploy.sh` 参数化），保持扁平结构。

## 📝 应用部署规范

### 目录命名
- 使用应用名称的小写形式，多单词用连字符分隔（如 `my-app`）

### 必需文件
- `[app-name]-deployment-guide.md` 或 `README.md` — 部署指南
- `base/kustomization.yaml` + 清单文件 — 通用结构
- `overlays/example/kustomization.yaml` — 示例取值

### 配置要求

- **环境相关取值不入库**：namespace、账号 ID、S3 桶名、EFS 文件系统 ID、访问域名
  一律由 overlay 注入。base 中使用占位符（`123456789012`、`yourdomain.com`、`fs-xxxx`）。
- **ECR 镜像地址**用 kustomize 的 `images:` 转换器替换，不要在 base 里写死账号 ID。
- **凭据不纳入 kustomize**：token、API key、密码改为 `kubectl create secret` 前置步骤，
  或使用 `*secret.yaml`（已被 `.gitignore` 排除）；可提供 `*secret.yaml.example` 作为模板。
- **多个可选服务**共处一个模块时，在 `base/` 下各自独立成目录，由 overlay 的 `resources`
  选择部署哪些（参考 `qwen3-speech/`）。
- 指南中的运维命令使用 `-n <namespace>` 而非真实 namespace。
- 包含完整部署步骤、故障排除、资源需求。

## 🔧 应用添加流程

1. 创建应用目录，清单放入 `base/`
2. 从其他模块复制 `overlays/example/`，标注需要哪些环境取值
3. 编写部署指南
4. `kubectl kustomize <应用>/overlays/example` 验证渲染
5. 建真实 overlay 并部署，用 `kubectl diff -k` 确认与预期一致
6. 更新本 README

## 📚 相关文档

- [EKS 集群配置](../eks/)
- [Karpenter 配置](../karpenter/)
- [GPU 支持](../gpu/)
- [集群管理工具](../tools/)
