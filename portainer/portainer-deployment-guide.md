# Portainer 部署指南

## 前置要求

- EKS 集群已创建
- AWS LoadBalancer Controller 已安装
- EFS CSI Driver 已安装并配置 Pod Identity
- kubectl 已配置

## 方法一：官方 Agent（推荐用于已有 Portainer Server）

```bash
# 部署 Portainer Agent
kubectl apply -f https://downloads.portainer.io/ce2-33/portainer-agent-k8s-lb.yaml

# 验证部署
kubectl get pods -n portainer
kubectl get svc -n portainer
```

## 方法二：完整部署（Portainer CE + Agent + EFS 持久化存储）

### 1. 创建 Fargate Profile（可选）

```bash
# 获取 Fargate 执行角色
FARGATE_ROLE=$(aws eks describe-fargate-profile \
  --cluster-name eks-karpenter-env \
  --fargate-profile-name default \
  --region ap-southeast-1 \
  --profile me \
  --query 'fargateProfile.podExecutionRoleArn' \
  --output text)

# 创建 Portainer Fargate Profile
aws eks create-fargate-profile \
  --cluster-name eks-karpenter-env \
  --fargate-profile-name portainer \
  --pod-execution-role-arn $FARGATE_ROLE \
  --selectors namespace=portainer \
  --region ap-southeast-1 \
  --profile me
```

### 2. 配置 EFS 持久化存储

```bash
# 创建 Portainer 专用 StorageClass
kubectl apply -f portainer-efs-storageclass.yaml

# 创建 PVC
kubectl apply -f portainer-efs-pvc.yaml

# 验证存储
kubectl get storageclass efs-portainer
kubectl get pvc -n portainer
```

### 3. 部署 Portainer

```bash
# 部署完整 Portainer 套件（包含 EFS 存储配置）
kubectl apply -f portainer-deployment.yaml

# 验证部署
kubectl get pods -n portainer -o wide
kubectl get ingress -n portainer
```

### 4. 获取访问地址

```bash
# 获取 ALB 地址
kubectl get ingress -n portainer portainer-ingress \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```

## 存储配置说明

### EFS 存储优势
- **持久化** - Pod 重启后数据保持
- **共享** - 支持多 Pod 访问（如果需要）
- **Fargate 兼容** - 与 Fargate 完全兼容

### 存储路径
- **EFS 文件系统**: `fs-0123456789abcdef0`
- **存储路径**: `/portainer/pvc-<uuid>`
- **权限**: UID/GID 1000, 权限 700

## 配置 Portainer

1. **访问 Web UI**: `http://<ALB-ADDRESS>`
2. **创建管理员账户**
3. **添加 Kubernetes 环境**:
   - Environment type: **Kubernetes**
   - Connection method: **Agent**
   - Environment URL: `portainer-agent.portainer.svc.cluster.local:9001`

## 验证

```bash
# 检查所有组件
kubectl get all -n portainer

# 检查存储
kubectl get pvc,pv -n portainer

# 检查节点分布（应该在 Fargate 上）
kubectl get pods -n portainer -o wide

# 测试数据持久化
kubectl rollout restart deployment/portainer -n portainer
kubectl rollout restart deployment/portainer-agent -n portainer
```

## 清理

```bash
# 删除 Portainer（保留 PVC）
kubectl delete deployment,service,ingress -n portainer --all

# 完全清理（包括数据）
kubectl delete namespace portainer

# 删除 Fargate Profile（可选）
aws eks delete-fargate-profile \
  --cluster-name eks-karpenter-env \
  --fargate-profile-name portainer \
  --region ap-southeast-1 \
  --profile me
```

## 故障排除

### Portainer 安全超时问题
**现象**：Portainer 日志显示 "timed out for security purposes"
**原因**：Portainer 5分钟无访问自动锁定安全机制
**解决方案**：
```bash
# 重启 Portainer Pod
kubectl rollout restart deployment/portainer -n portainer

# 检查新 Pod 状态
kubectl get pods -n portainer
```

### ALB 访问问题
**现象 1**：HTTP 返回 307 重定向
**原因**：Portainer 内部重定向，但实际 HTTP 访问正常
**解决方案**：直接使用 HTTP 访问即可

**现象 2**：ALB 创建 HTTPS 监听器失败
**错误**：`A certificate must be specified for HTTPS listeners`
**原因**：配置了 HTTPS 但没有提供 SSL 证书
**解决方案**：
```bash
# 选项 1：移除 HTTPS 配置（推荐测试环境）
# 修改 Ingress annotations，只保留 HTTP:80

# 选项 2：添加 ACM 证书（生产环境）
# 添加 alb.ingress.kubernetes.io/certificate-arn 注解
```

### EFS 挂载问题
```bash
# 检查 EFS CSI Driver
kubectl get pods -n kube-system -l app=efs-csi-controller

# 检查 Pod Identity Association
aws eks list-pod-identity-associations --cluster-name eks-karpenter-env --profile me

# 检查 PVC 事件
kubectl describe pvc portainer-data-pvc -n portainer
```
