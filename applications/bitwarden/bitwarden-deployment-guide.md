# Bitwarden Lite 部署指南 (EKS)

## 📋 部署概述

本指南介绍如何在 EKS 集群中部署 Bitwarden Lite（原 Bitwarden Unified，于2025年12月正式发布时改名）密码管理服务。

### 部署特点
- **单容器部署** - 所有服务打包在一个镜像中
- **资源优化** - 最低 256MB RAM 即可运行
- **数据库** - 使用 SQLite 存储在 EFS 上
- **ARM64 支持** - 运行在 Karpenter ARM64 节点上
- **配置简单** - 环境变量配置，无需复杂 Helm

## 📁 部署文件结构

```
/bitwarden/
├── bitwarden-deployment-guide.md     # 本部署指南
├── bitwarden-configmap.yaml          # 环境变量配置
├── bitwarden-efs-pvc.yaml            # EFS 持久卷声明
└── bitwarden-deployment.yaml         # 应用部署 + Service + Ingress
```

## 🎯 前提条件

### 集群相关
- EKS 集群运行正常
- kubectl 已配置
- AWS Load Balancer Controller 已安装
- EFS CSI Driver 已安装
- EFS 存储类 (efs-sc) 可用

### 其它准备条件
- 域名 (例如: bitwarden.yourdomain.com)
- SMTP 服务器配置 (用于邮件通知)
- Bitwarden 安装 ID 和密钥

## 🔧 部署配置

### 配置方式说明
所有配置通过编辑 `bitwarden-configmap.yaml` 文件实现，例如：

### 1. 环境变量配置

| 变量 | 示例值 | 说明 |
|------|--------|----- |
| **BW_DOMAIN** | bitwarden.yourdomain.com | 访问域名 |
| **BW_DB_PROVIDER** | sqlite | 数据库类型 |
| **BW_INSTALLATION_ID** | your-installation-id-here | 安装 ID |
| **BW_INSTALLATION_KEY** | your-installation-key-here | 安装密钥 |

### 2. SMTP 配置

| 变量 | 示例值 | 说明 |
|------|--------|----- |
| **globalSettings__mail__replyToEmail** | no-reply@yourdomain.com | 回复邮箱 |
| **globalSettings__mail__smtp__host** | your-smtp-host | SMTP 服务器 |
| **globalSettings__mail__smtp__port** | 587 | SMTP 端口 |
| **globalSettings__mail__smtp__ssl** | true | SSL 启用 |
| **globalSettings__mail__smtp__username** | your-smtp-username | SMTP 用户名 |
| **globalSettings__mail__smtp__password** | your-smtp-password | SMTP 密码 |

### 3. 安全配置

| 变量 | 值 | 说明 |
|------|----|----- |
| **globalSettings__disableUserRegistration** | true | 禁用用户注册 |
| **adminSettings__admins** | admin@yourdomain.com  | 管理员邮箱 |

## 🚀 部署步骤

### 步骤 1: 创建 Namespace
```bash
kubectl create namespace bitwarden
```

### 步骤 2: 创建 EFS 持久卷
```bash
kubectl apply -f bitwarden-efs-pvc.yaml
```

### 步骤 3: 创建配置映射
```bash
kubectl apply -f bitwarden-configmap.yaml
```

### 步骤 4: 部署应用
```bash
kubectl apply -f bitwarden-deployment.yaml
```

### 步骤 5: 验证部署
```bash
kubectl get all -n bitwarden

# 检查 Pod 状态
kubectl get pods -n bitwarden -o wide
kubectl logs -n bitwarden deployment/bitwarden -f

# 检查服务状态
kubectl get svc -n bitwarden
kubectl get ingress -n bitwarden

# 检查存储状态
kubectl get pvc -n bitwarden
```

### 步骤 6: 启用公网访问

推荐通过 CloudFront 配置 VPC Origin 来安全访问 Internal ALB：

#### 6.1 为 Internal ALB 创建 VPC Origin
1. **CloudFront 控制台** → **VPC origins** → **Create VPC origin**
2. **Origin ARN**: 选择 Internal ALB 的 ARN
3. **等待部署完成** (最多15分钟)

#### 6.2 创建 CloudFront 分发
1. **CloudFront 控制台** → **Distributions** → **Create distribution**
2. **Origin domain**: 选择刚创建的 VPC Origin
3. **Viewer protocol policy**: Redirect HTTP to HTTPS
4. **Allowed HTTP methods**: GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE
5. **Origin request policy**: CORS-S3Origin 或 AllViewer
6. **Alternate domain name (CNAME)**: 添加你的域名 (如: bitwarden.yourdomain.com)
7. **SSL certificate**: 选择对应域名的 ACM 证书

#### 6.3 配置域名解析
```
在 Route 53 或你的 DNS 提供商中创建 CNAME 记录，例如:
bitwarden.yourdomain.com → d1234567890.cloudfront.net
```

**最终实现访问路径**:
```
用户 → CloudFront (HTTPS) → Internal ALB (HTTP:80) → Service (80) → Pod (8080)
```

**完成后，通过 CloudFront 域名访问：https://bitwarden.yourdomain.com**

## 🔧 管理操作

### 查看应用日志
```bash
kubectl logs -n bitwarden deployment/bitwarden -f
```

### 重启服务
```bash
kubectl rollout restart deployment/bitwarden -n bitwarden
```

### 更新配置
```bash
# 修改 ConfigMap 后重启
kubectl apply -f bitwarden-configmap.yaml
kubectl rollout restart deployment/bitwarden -n bitwarden
```

### 扩缩容
```bash
kubectl scale deployment bitwarden --replicas=2 -n bitwarden
```

## 🔄 备份和恢复

### 数据备份
```bash
# 备份 SQLite 数据库
kubectl exec -n bitwarden deployment/bitwarden -- cp /etc/bitwarden/vault.db /tmp/
kubectl cp bitwarden/[pod-name]:/tmp/vault.db ./vault-backup-$(date +%Y%m%d).db
```

### 数据恢复
```bash
# 恢复 SQLite 数据库
kubectl cp ./vault-backup.db bitwarden/[pod-name]:/tmp/
kubectl exec -n bitwarden deployment/bitwarden -- cp /tmp/vault-backup.db /etc/bitwarden/vault.db
kubectl rollout restart deployment/bitwarden -n bitwarden
```

## 🐛 故障排除

### 常见问题

#### Pod 启动失败
```bash
kubectl describe pod -n bitwarden [pod-name]
kubectl logs -n bitwarden [pod-name]
```

#### 存储卷问题
```bash
kubectl describe pvc -n bitwarden bitwarden-data
kubectl get storageclass efs-sc
```

#### 网络连接问题
```bash
# 测试 Service 连接
kubectl run test-curl --image=curlimages/curl:latest --rm -it --restart=Never -- curl -I http://bitwarden-service.bitwarden.svc.cluster.local/alive

# 测试 Internal ALB 连接
kubectl run test-curl --image=curlimages/curl:latest --rm -it --restart=Never -- curl -I http://internal-k8s-bitwarden-bitwarde-038bec7911-328030193.ap-southeast-1.elb.amazonaws.com/alive
```

### 调试命令
```bash
# 进入容器调试
kubectl exec -it -n bitwarden deployment/bitwarden -- /bin/bash

# 查看配置文件
kubectl exec -n bitwarden deployment/bitwarden -- ls -la /etc/bitwarden/

# 查看环境变量
kubectl exec -n bitwarden deployment/bitwarden -- env | grep BW_
```

## 📝 更新升级

### 镜像更新
```bash
# 更新到最新版本
kubectl set image deployment/bitwarden -n bitwarden bitwarden=ghcr.io/bitwarden/self-host:beta

# 查看更新状态
kubectl rollout status deployment/bitwarden -n bitwarden
```

### 配置更新
```bash
# 更新配置后重启
kubectl apply -f bitwarden-configmap.yaml
kubectl rollout restart deployment/bitwarden -n bitwarden
```

## 📚 参考资料

- [Bitwarden 统一部署官方文档](https://bitwarden.com/help/install-and-deploy-unified-beta/)
- [Bitwarden GitHub 仓库](https://github.com/bitwarden/self-host/tree/main/docker-unified)
- [AWS Load Balancer Controller 文档](https://kubernetes-sigs.github.io/aws-load-balancer-controller/)
- [EFS CSI Driver 文档](https://github.com/kubernetes-sigs/aws-efs-csi-driver)
