# Karpenter 部署指南

**前置条件**：必须先完成 EKS 集群创建（参考 `eks/create-eks-cluster-guide.md`）

## 1. 验证前置条件

### 1.1 确认 EKS 集群状态

```bash
# 验证集群状态
kubectl get nodes
kubectl get pods -n kube-system

# 确认 Karpenter 服务账户已创建
kubectl get serviceaccount karpenter -n karpenter 2>/dev/null || echo "需要先创建 EKS 集群"
```

### 1.2 设置环境变量

```bash
export CLUSTER_NAME=eks-karpenter-env
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --profile lab)
export CLUSTER_ENDPOINT=$(aws eks describe-cluster --name ${CLUSTER_NAME} --query "cluster.endpoint" --output text --profile lab)

echo "集群: ${CLUSTER_NAME}"
echo "区域: ${AWS_DEFAULT_REGION}"
echo "账户: ${AWS_ACCOUNT_ID}"
```

## 2. 安装 Karpenter

### 2.1 创建 Karpenter 节点 IAM 角色

```bash
# 创建节点角色（使用预配置的信任策略）
aws iam create-role --role-name "KarpenterNodeInstanceRole-${CLUSTER_NAME}" \
    --assume-role-policy-document file://karpenter-node-role-trust-policy.json \
    --profile lab

# 附加必要策略
aws iam attach-role-policy --role-name "KarpenterNodeInstanceRole-${CLUSTER_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy --profile lab

aws iam attach-role-policy --role-name "KarpenterNodeInstanceRole-${CLUSTER_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy --profile lab

aws iam attach-role-policy --role-name "KarpenterNodeInstanceRole-${CLUSTER_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly --profile lab

aws iam attach-role-policy --role-name "KarpenterNodeInstanceRole-${CLUSTER_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore --profile lab

# 创建实例 Profile
aws iam create-instance-profile --instance-profile-name "KarpenterNodeInstanceProfile-${CLUSTER_NAME}" --profile lab
aws iam add-role-to-instance-profile --instance-profile-name "KarpenterNodeInstanceProfile-${CLUSTER_NAME}" \
    --role-name "KarpenterNodeInstanceRole-${CLUSTER_NAME}" --profile lab
```

### 2.2 配置 Karpenter 控制器权限

```bash
# 创建 Karpenter 控制器策略（使用预配置文件）
aws iam create-policy \
  --policy-name "KarpenterControllerPolicy-${CLUSTER_NAME}" \
  --policy-document file://karpenter-policy.json \
  --profile lab

# 附加策略到 Karpenter 服务账户角色（已由 eksctl 创建）
aws iam attach-role-policy \
  --role-name "KarpenterServiceAccount-${CLUSTER_NAME}" \
  --policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/KarpenterControllerPolicy-${CLUSTER_NAME}" \
  --profile lab
```

### 2.3 使用 Helm 安装 Karpenter

```bash
# 登录到 ECR Public
aws ecr-public get-login-password --region us-east-1 --profile lab | helm registry login --username AWS --password-stdin public.ecr.aws

# 安装 Karpenter v1.6.3
helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
  --version "1.6.3" \
  --namespace "karpenter" \
  --create-namespace \
  --set "settings.clusterName=${CLUSTER_NAME}" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.create=true" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=arn:aws:iam::${AWS_ACCOUNT_ID}:role/KarpenterServiceAccount-${CLUSTER_NAME}" \
  --set "serviceAccount.name=karpenter" \
  --set "podLabels.fargate=enabled" \
  --wait

# 验证安装
kubectl get pods -n karpenter
kubectl logs -f -n karpenter -l app.kubernetes.io/name=karpenter
```

## 3. 配置 NodePool

### 3.1 应用 NodePool 配置

```bash
# 应用预配置的 NodePool
kubectl apply -f nodepool-arm64.yaml
kubectl apply -f nodepool-amd64.yaml

# 验证配置
kubectl get nodepool
kubectl get ec2nodeclass
```

### 3.2 NodePool 配置说明

项目包含两个预配置的 NodePool：

**ARM64 NodePool** (`nodepool-arm64.yaml`):
- 实例类型: m8g, c7g, r7g 系列
- 架构: ARM64 (Graviton)
- 优先级: 高 (weight: 100)
- 成本优化: 70% Spot + 30% On-Demand

**AMD64 NodePool** (`nodepool-amd64.yaml`):
- 实例类型: m7i, c7i, r7i 系列  
- 架构: AMD64 (Intel)
- 优先级: 中 (weight: 50)
- 兼容性: 传统 x86 应用

## 4. 测试验证

### 4.1 部署测试应用

```bash
# 部署测试应用
kubectl apply -f ../tests/test-karpenter-simple.yaml

# 观察节点创建
kubectl get nodes -w

# 查看 Karpenter 日志
kubectl logs -f -n karpenter -l app.kubernetes.io/name=karpenter
```

### 4.2 验证节点调度

```bash
# 查看节点分布
kubectl get nodes --show-labels | grep karpenter

# 查看 Pod 调度情况
kubectl get pods -o wide

# 清理测试资源
kubectl delete -f ../tests/test-karpenter-simple.yaml
```

### 5. 卸载 Karpenter

```bash
# 删除 NodePool
kubectl delete nodepool --all

# 卸载 Helm Chart
helm uninstall karpenter -n karpenter

# 删除 namespace
kubectl delete namespace karpenter
```

## 故障排除

### Karpenter Pod 无法启动
```bash
# 检查服务账户权限
kubectl describe serviceaccount karpenter -n karpenter

# 检查 Fargate Profile
aws eks describe-fargate-profile --cluster-name ${CLUSTER_NAME} --fargate-profile-name karpenter --profile lab
```

### 节点无法创建
```bash
# 检查 NodePool 状态
kubectl describe nodepool

# 检查 Karpenter 日志
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=100
```
