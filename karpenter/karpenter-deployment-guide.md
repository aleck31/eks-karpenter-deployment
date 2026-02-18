# Karpenter 部署指南

**前置条件**：必须先完成 EKS 集群创建（参考 `eks/create-eks-cluster-guide.md`）

## 1. 验证前置条件

### 1.1 确认 EKS 集群状态

```bash
# 验证集群状态
kubectl get nodes
kubectl get pods -n kube-system

# 确认 Fargate Profile 已创建
aws eks describe-fargate-profile --cluster-name ${CLUSTER_NAME} --fargate-profile-name karpenter --profile ${AWS_PROFILE}

# 确认 OIDC Provider 已启用（IRSA 需要）
aws eks describe-cluster --name ${CLUSTER_NAME} --query "cluster.identity.oidc.issuer" --output text --profile ${AWS_PROFILE}
```

### 1.2 Fargate 上的 Karpenter 必须使用 IRSA

Karpenter 通过 Fargate Profile 调度（`podLabels.fargate=enabled`），而 **EKS Pod Identity 不支持 Fargate**：

- Pod Identity Agent 以 DaemonSet 方式运行，Fargate 不支持 DaemonSet
- 当 Pod Identity Association 存在时，EKS 会优先注入 Pod Identity 凭证（`169.254.170.23`），覆盖 IRSA
- 但 Fargate 上 Pod Identity Agent 不可达，导致 Karpenter 无法获取 AWS 凭证，健康检查超时反复重启
- 参考：[GitHub Issue #2274 - Enable EKS Pod Identities on EKS Fargate](https://github.com/aws/containers-roadmap/issues/2274)

**因此**：
1. Karpenter 的 ServiceAccount 必须使用 IRSA 注解（`eks.amazonaws.com/role-arn`）
2. 不能为 `karpenter:karpenter` 创建 Pod Identity Association（如 eksctl 自动创建了需删除）
3. 其他运行在 EC2 节点上的组件（VPC CNI、EBS CSI 等）可以正常使用 Pod Identity


### 1.3 设置环境变量

```bash
export CLUSTER_NAME=eks-karpenter-env
export AWS_DEFAULT_REGION=ap-southeast-1
export AWS_PROFILE=your-profile
export ROLE_SUFFIX=XXX

export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --profile ${AWS_PROFILE})
export CLUSTER_ENDPOINT=$(aws eks describe-cluster --name ${CLUSTER_NAME} --query "cluster.endpoint" --output text --profile ${AWS_PROFILE})

echo "集群: ${CLUSTER_NAME}"
echo "区域: ${AWS_DEFAULT_REGION}"
echo "账户: ${AWS_ACCOUNT_ID}"
```

## 2. 安装 Karpenter

### 2.1 创建 Karpenter 节点 IAM 角色

```bash
# 创建节点角色（使用预配置的信任策略）
aws iam create-role --role-name "KarpenterNodeInstanceRole-${ROLE_SUFFIX}" \
    --assume-role-policy-document file://karpenter-node-role-trust-policy.json \
    --profile ${AWS_PROFILE}

# 附加必要策略
for policy in AmazonEKSWorkerNodePolicy AmazonEKS_CNI_Policy AmazonEC2ContainerRegistryReadOnly AmazonSSMManagedInstanceCore; do
  aws iam attach-role-policy --role-name "KarpenterNodeInstanceRole-${ROLE_SUFFIX}" \
    --policy-arn "arn:aws:iam::aws:policy/${policy}" --profile ${AWS_PROFILE}
done

# 创建实例 Profile
aws iam create-instance-profile --instance-profile-name "KarpenterNodeInstanceProfile-${ROLE_SUFFIX}" --profile ${AWS_PROFILE}
aws iam add-role-to-instance-profile --instance-profile-name "KarpenterNodeInstanceProfile-${ROLE_SUFFIX}" \
    --role-name "KarpenterNodeInstanceRole-${ROLE_SUFFIX}" --profile ${AWS_PROFILE}
```

### 2.2 创建 Karpenter IRSA 角色

```bash
# 获取 OIDC Provider ID
OIDC_ID=$(aws eks describe-cluster --name ${CLUSTER_NAME} --region ${AWS_DEFAULT_REGION} --profile ${AWS_PROFILE} \
  --query "cluster.identity.oidc.issuer" --output text | sed 's|https://||')

# 从模板生成信任策略
sed -e "s|<AWS_ACCOUNT_ID>|${AWS_ACCOUNT_ID}|g" -e "s|<OIDC_ID>|${OIDC_ID}|g" \
  karpenter-irsa-trust-policy.json > /tmp/karpenter-trust-policy.json

# 创建 IRSA role
aws iam create-role --role-name "KarpenterIRSA-${ROLE_SUFFIX}" \
  --assume-role-policy-document file:///tmp/karpenter-trust-policy.json \
  --profile ${AWS_PROFILE}

# 附加 Karpenter 控制器策略（inline policy）
aws iam put-role-policy --role-name "KarpenterIRSA-${ROLE_SUFFIX}" \
  --policy-name KarpenterControllerPolicy \
  --policy-document file://karpenter-policy.json \
  --profile ${AWS_PROFILE}
```

> **注意**：如果 eksctl 在 `cluster-config.yaml` 中为 karpenter 创建了 Pod Identity Association，必须删除：
> ```bash
> aws eks list-pod-identity-associations --cluster-name ${CLUSTER_NAME} --region ${AWS_DEFAULT_REGION} --profile ${AWS_PROFILE} \
>   --query "associations[?namespace=='karpenter'].associationId" --output text | \
>   xargs -I{} aws eks delete-pod-identity-association --cluster-name ${CLUSTER_NAME} --association-id {} \
>     --region ${AWS_DEFAULT_REGION} --profile ${AWS_PROFILE}
> ```

### 2.3 使用 Helm 安装 Karpenter

```bash
# 安装 Karpenter v1.9.0（IRSA 模式，Fargate 调度）
helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
  --version "1.9.0" \
  --namespace "karpenter" \
  --create-namespace \
  --set "settings.clusterName=${CLUSTER_NAME}" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "settings.featureGates.spotToSpotConsolidation=true" \
  --set "serviceAccount.create=true" \
  --set "serviceAccount.name=karpenter" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=arn:aws:iam::${AWS_ACCOUNT_ID}:role/KarpenterIRSA-${ROLE_SUFFIX}" \
  --set "podLabels.fargate=enabled" \
  --set controller.resources.requests.cpu=1 \
  --set controller.resources.requests.memory=1Gi \
  --set controller.resources.limits.cpu=1 \
  --set controller.resources.limits.memory=1Gi

# 验证安装（Fargate 调度需 30-60s）
sleep 60 && kubectl get pods -n karpenter
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=20
```

## 3. 配置 NodePool

### 3.1 应用 NodePool 配置

```bash
# 应用预配置的 NodePool
kubectl apply -f nodepool-arm64.yaml
kubectl apply -f nodepool-amd64.yaml

# GPU NodePool（可选）
kubectl apply -f ../gpu/nodepool-gpu.yaml

# 验证配置
kubectl get nodepool
kubectl get ec2nodeclass
```

### 3.2 NodePool 配置说明

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

```bash
# 部署测试应用
kubectl apply -f ../tests/test-karpenter-simple.yaml

# 观察节点创建
kubectl get nodes -w

# 查看 Karpenter 日志
kubectl logs -f -n karpenter -l app.kubernetes.io/name=karpenter

# 清理测试资源
kubectl delete -f ../tests/test-karpenter-simple.yaml
```

## 5. 卸载 Karpenter

```bash
kubectl delete nodepool --all
helm uninstall karpenter -n karpenter
kubectl delete namespace karpenter
```

## 故障排除

### Karpenter Pod 无法启动（健康检查超时）

最常见原因是 IAM 凭证问题：

```bash
# 1. 确认 SA 有 IRSA 注解
kubectl get sa karpenter -n karpenter -o jsonpath='{.metadata.annotations}'

# 2. 确认没有 Pod Identity Association 干扰（应返回空）
aws eks list-pod-identity-associations --cluster-name ${CLUSTER_NAME} --region ${AWS_DEFAULT_REGION} --profile ${AWS_PROFILE} \
  --query "associations[?namespace=='karpenter']"

# 3. 确认 Pod 注入的是 IRSA 凭证（应看到 AWS_WEB_IDENTITY_TOKEN_FILE 和 AWS_ROLE_ARN）
#    而不是 Pod Identity 凭证（AWS_CONTAINER_CREDENTIALS_FULL_URI=169.254.170.23）
kubectl describe pod -n karpenter -l app.kubernetes.io/name=karpenter | grep -A2 "AWS_"
```

### 节点无法创建

```bash
# 检查 NodePool 状态
kubectl describe nodepool

# 检查 Karpenter 日志
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=100
```
