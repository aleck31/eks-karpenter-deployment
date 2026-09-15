# Portainer 部署指南

## 前置要求

- EKS 集群已创建
- AWS LoadBalancer Controller 已安装
- EFS CSI Driver 已安装并配置 Pod Identity
- kubectl 已配置

## 镜像标签用 `lts`，不要用 `latest`

base 清单固定为 `portainer/portainer-ce:lts` 与 `portainer/agent:lts`。

`imagePullPolicy` 为 `Always`，而节点均为 Spot，因此镜像版本实际由节点回收时机决定，
不由部署者决定。用 `latest` 时任何一次 Pod 重建都可能拉到新的大版本：曾因此拉到一个
开始校验 `--trusted-origins` 格式的版本，而清单当时传的是裸域名，导致 CrashLoopBackOff
持续三天（重启 894 次）。`latest` 已移动，事后也无法确定出事前是哪个版本。

`lts` 只在长期支持线内滚动，安全补丁照常跟进，但不会跳到 STS 或新的大版本。
Portainer 同时发布 `lts` 与 `sts`，而 `latest` 跟随较新的那条（含破坏性变更）。

升级到新的大版本时应显式改这里的标签并在应用前查阅 release note，而不是依赖标签漂移。

## 方法一：官方 Agent（推荐用于已有 Portainer Server）

```bash
# 部署 Portainer Agent
kubectl apply -f https://downloads.portainer.io/ce2-33/portainer-agent-k8s-lb.yaml

# 验证部署
kubectl get pods -n portainer
kubectl get svc -n portainer
```

## 方法二：完整部署（Portainer CE + Agent + EFS 持久化存储）

### 1. 准备 overlay

采用 kustomize base + overlay，环境相关取值（EFS 文件系统 ID、访问域名）不入库。

```bash
cp -r overlays/example overlays/<env-name>

# 编辑两处：
#   efs-patch.yaml     fileSystemId 改为你的 EFS 文件系统 ID
#   domain-patch.yaml  访问域名（Portainer 的 --trusted-origins）
vi overlays/<env-name>/efs-patch.yaml
vi overlays/<env-name>/domain-patch.yaml
```

> `.gitignore` 默认忽略 `overlays/` 下全部目录、仅放行 `example/`，真实取值不会误提交。

### 2. 创建 Namespace

```bash
kubectl create namespace portainer
```

### 3. 部署

一次性创建 StorageClass、PVC、Portainer CE、Agent、Service、Ingress：

```bash
kubectl config current-context          # 先确认目标集群
kubectl apply -k overlays/<env-name>

# 验证
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

# 检查节点分布
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
aws eks list-pod-identity-associations --cluster-name eks-karpenter-env --profile lab

# 检查 PVC 事件
kubectl describe pvc portainer-data-pvc -n portainer
```
