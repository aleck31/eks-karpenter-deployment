# EKS 应用部署管理

## 📋 目录说明

本目录用于管理 EKS 集群中部署的各种应用程序，每个应用都有独立的子目录包含其配置文件和部署指南。

## 📁 目录结构

```
/applications/
├── README.md                    # 本说明文档
├── bitwarden/                   # Bitwarden 密码管理服务
│   ├── bitwarden-deployment-guide.md
│   ├── bitwarden-configmap.yaml
│   ├── bitwarden-deployment.yaml
│   └── bitwarden-efs-pvc.yaml
├── firecrawl/                   # Firecrawl 网页抓取 API (多组件栈)
│   ├── firecrawl-deployment-guide.md
│   ├── firecrawl-configmap.yaml
│   ├── firecrawl-secret.yaml.example   # 凭据模板，复制后填值
│   ├── firecrawl-infra.yaml            # Redis + PostgreSQL + RabbitMQ
│   ├── firecrawl-deployment.yaml       # api + worker + playwright
│   ├── firecrawl-extract-worker.yaml
│   ├── firecrawl-ingress.yaml
│   ├── firecrawl-ebs-pvc.yaml
│   ├── deploy.sh
│   └── verify.sh
└── [future-apps]/               # 未来的应用部署
```

## 📝 应用部署规范

### 目录命名
- 使用应用名称的小写形式
- 多单词用连字符分隔 (如: `my-app`)

### 必需文件
- `[app-name]-deployment-guide.md` - 部署指南
- `[app-name]-deployment.yaml` - 主要部署配置
- `[app-name]-configmap.yaml` - 配置映射 (如需要)
- `[app-name]-pvc.yaml` - 持久卷声明 (如需要)

### 配置要求
- 所有真实信息使用占位符
- 包含完整的部署步骤
- 提供故障排除指南
- 记录资源需求和限制

## 🔧 应用添加流程

1. **创建应用目录**
2. **准备配置文件** (替换占位符)
3. **编写部署指南**
4. **测试部署流程**
5. **更新本 README**

## 📚 相关文档

- [EKS 集群配置](../eks/)
- [Karpenter 配置](../karpenter/)
- [GPU 支持](../gpu/)
