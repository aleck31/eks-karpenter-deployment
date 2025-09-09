# AWS EKS 集群创建指南

## 前置要求

### 环境信息
- AWS Profile: `lab`
- Region: `us-east-1` (按需选择区域)
- 集群名称: `eks-karpenter-env`

### 必需工具版本
- AWS CLI v2.x
- eksctl >= 0.150.0
- kubectl >= 1.28
- helm >= 3.8
- 使用 Karpenter v1.6.2 (稳定版本)
  - 官方 OCI 仓库：`oci://public.ecr.aws/karpenter/karpenter`
  - API 版本：`karpenter.sh/v1` 和 `karpenter.k8s.aws/v1`

## 1: 环境准备

### 1.1 安装必要工具

```bash
# 安装 eksctl
curl --silent --location "https://github.com/weaveworks/eksctl/releases/latest/download/eksctl_$(uname -s)_amd64.tar.gz" | tar xz -C /tmp
sudo mv /tmp/eksctl /usr/local/bin

# 安装 kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl
sudo mv kubectl /usr/local/bin/

# 安装 helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# 验证安装
eksctl version
kubectl version --client
helm version
```

### 1.2 验证 AWS 配置

```bash
# 验证 AWS 配置
aws sts get-caller-identity --profile lab
aws configure list --profile lab

# 设置默认 profile (可选)
export AWS_PROFILE=lab
export AWS_DEFAULT_REGION=us-east-1
```

## 2: EKS 集群创建

### 2.1 创建 eksctl 配置文件

参考配置文件：`cluster-config.yaml`

**重要说明**：
- 建议创建之前通过 `eksctl create cluster --dry-run` 进行验证
- eksctl 不支持自动添加 `karpenter.sh/discovery` 标签
- 需要在集群创建后手动添加这些标签（见验证步骤）

### 2.2 使用 eksctl 部署集群

```bash
# 创建集群 (大约需要 15-20 分钟)
eksctl create cluster -f cluster-config.yaml --profile lab

# 验证集群创建
kubectl get nodes
kubectl get pods -A

# 为 Karpenter 添加必要的资源标签
export CLUSTER_NAME=eks-karpenter-env
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --profile lab)

# 为私有子网添加 Karpenter 发现标签
for subnet in $(aws ec2 describe-subnets --filters "Name=tag:aws:cloudformation:logical-id,Values=SubnetPrivate*" "Name=tag:aws:cloudformation:stack-name,Values=eksctl-${CLUSTER_NAME}-cluster" --profile lab --query 'Subnets[].SubnetId' --output text); do
  echo "添加标签到子网: $subnet"
  aws ec2 create-tags --resources $subnet --tags Key=karpenter.sh/discovery,Value=${CLUSTER_NAME} --profile lab
done

# 为集群安全组添加 Karpenter 发现标签
CLUSTER_SG=$(aws eks describe-cluster --name ${CLUSTER_NAME} --profile lab --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)
echo "添加标签到安全组: $CLUSTER_SG"
aws ec2 create-tags --resources $CLUSTER_SG --tags Key=karpenter.sh/discovery,Value=${CLUSTER_NAME} --profile lab

# 更新 aws-auth ConfigMap 添加 Karpenter 节点角色
FARGATE_ROLE=$(aws iam list-roles --profile lab --query 'Roles[?contains(RoleName, `FargatePodExecutionRole`)].Arn' --output text)
kubectl patch configmap aws-auth -n kube-system --patch "
data:
  mapRoles: |
    - groups:
      - system:bootstrappers
      - system:nodes
      - system:node-proxier
      rolearn: ${FARGATE_ROLE}
      username: system:node:{{SessionName}}
    - groups:
      - system:bootstrappers
      - system:nodes
      rolearn: arn:aws:iam::${AWS_ACCOUNT_ID}:role/KarpenterNodeInstanceRole-${CLUSTER_NAME}
      username: system:node:{{EC2PrivateDNSName}}
"
```

## 3: 集群配置

### 3.1 Fargate Profile 配置说明

**注意**：Fargate Profiles 已在 cluster-config.yaml 中预配置，eksctl 会自动创建以下 Profiles：

- **default** - 用于 default 和 kube-system namespace（需要 `fargate: enabled` 标签）
- **karpenter** - 用于 karpenter namespace（需要 `fargate: enabled` 标签）  
- **portainer** - 用于 portainer namespace（需要 `fargate: enabled` 标签）

### 🎯 **Fargate Profile 最佳实践**

**精确标签控制**：
- 使用 `fargate: enabled` 标签精确控制哪些 Pod 运行在 Fargate
- 避免全量捕获（无标签选择器），防止意外的高成本

**架构规划**：
- **管理组件** → Fargate（Karpenter, Portainer, Load Balancer Controller）
- **系统组件** → EC2（CSI Controllers, CoreDNS）
- **应用负载** → EC2（默认）

**验证 Fargate Profiles**：
```bash
# 查看已创建的 Fargate Profiles
aws eks list-fargate-profiles --cluster-name eks-karpenter-env --region us-east-1 --profile lab

# 查看具体配置
aws eks describe-fargate-profile --cluster-name eks-karpenter-env --fargate-profile-name default --region us-east-1 --profile lab
```

### 3.2 新 Addon 安装最佳实践

**为新安装的 addon 添加 Fargate 标签的方法：**

1. **EKS Addon 配置参数**（推荐）：
```bash
aws eks create-addon \
  --cluster-name eks-karpenter-env \
  --addon-name aws-load-balancer-controller \
  --configuration-values '{"podLabels":{"fargate":"enabled"}}'
```

2. **安装后立即 patch**（通用）：
```bash
kubectl patch deployment <addon-deployment-name> -n <namespace> \
  -p '{"spec":{"template":{"metadata":{"labels":{"fargate":"enabled"}}}}}'
```

3. **Helm values**（如果使用 Helm）：
```yaml
podLabels:
  fargate: enabled
```

### 3.3 Pod Identity Associations 配置

**注意**：Pod Identity 已在 cluster-config.yaml 中预配置，无需手动迁移。

**Pod Identity 优势**（相比传统 IRSA）：
- ✅ **无需管理 OIDC Provider** - 自动管理
- ✅ **简化 IAM 信任策略** - 更简洁的权限配置
- ✅ **更好的跨账户支持** - 企业级权限管理
- ✅ **未来兼容性保证** - AWS 推荐的现代方式

**已预配置的组件**：
- `eks-pod-identity-agent` addon - 自动安装
- 所有服务账户使用 Pod Identity 权限模式
- OIDC Provider 自动启用

**验证 Pod Identity 配置**：
```bash
# 检查 Pod Identity Agent 状态
aws eks describe-addon \
  --cluster-name eks-karpenter-env \
  --addon-name eks-pod-identity-agent \
  --region us-east-1 \
  --profile lab \
  --query '{Status:status,Version:addonVersion}'

# 查看 Pod Identity Associations（集群创建后）
aws eks list-pod-identity-associations \
  --cluster-name eks-karpenter-env \
  --region us-east-1 \
  --profile lab
```

### 3.4 从 IRSA 迁移到 Pod Identity（现有集群）

**注意**：如果是现有集群需要从 IRSA 迁移到 Pod Identity，推荐使用 migrate-to-pod-identity 迁移工具，支持自动发现需要迁移的 addon 和服务账户，自动更新 IAM 角色信任策略。

**迁移步骤**：

1. **预览迁移计划**：
```bash
# 查看哪些组件可以迁移
eksctl utils migrate-to-pod-identity \
  --cluster eks-karpenter-env \
  --region ap-southeast-1 \
  --profile lab
```

2. **执行迁移**：
```bash
# 执行迁移并移除 OIDC 信任关系
eksctl utils migrate-to-pod-identity \
  --cluster eks-karpenter-env \
  --region ap-southeast-1 \
  --profile lab \
  --approve \
  --remove-oidc-provider-trust-relationship
```

3. **重启相关服务**：
```bash
# 重启迁移的组件使其使用新的 Pod Identity
kubectl rollout restart daemonset/aws-node -n kube-system
kubectl rollout restart deployment/ebs-csi-controller -n kube-system
kubectl rollout restart deployment/efs-csi-controller -n kube-system
```

**验证迁移结果**：
```bash
# 检查 Pod Identity 关联
aws eks list-pod-identity-associations \
  --cluster-name eks-karpenter-env \
  --region ap-southeast-1 \
  --profile lab

# 验证服务账户不再有 IRSA 注解
kubectl get serviceaccount -n kube-system aws-node -o yaml | grep -i role-arn
```

## 4: 存储配置

### ⚠️ **重要说明：CSI Drivers 已自动安装**

**通过 cluster-config.yaml 自动安装的组件**：
1. **EBS CSI Driver** - 已通过 addon 自动安装，包含 IAM 权限
2. **EFS CSI Driver** - 已通过 addon 自动安装，包含 IAM 权限
3. **S3 CSI Driver** - 已通过 addon 自动安装，包含 IAM 权限

**CSI Controller 调度策略**：
- ✅ **CSI Controller** → 自动运行在 EC2 节点（需要特权容器）
- ✅ **CSI Node** → 可以运行在任何节点（包括 Fargate）
- ✅ **管理组件** → 运行在 Fargate（添加 `fargate: enabled` 标签）

### 4.1 创建 EFS 文件系统

```bash
# 获取 VPC ID
VPC_ID=$(aws eks describe-cluster --name eks-karpenter-env --query "cluster.resourcesVpcConfig.vpcId" --output text --profile lab)

# 获取 CIDR 块
CIDR_BLOCK=$(aws ec2 describe-vpcs --vpc-ids $VPC_ID --query "Vpcs[0].CidrBlock" --output text --profile lab)

# 创建安全组
SECURITY_GROUP_ID=$(aws ec2 create-security-group \
  --group-name EFS-SecurityGroup-eks-karpenter-env \
  --description "Security group for EFS mount targets" \
  --vpc-id $VPC_ID \
  --output text \
  --query 'GroupId' \
  --profile lab)

# 添加 NFS 入站规则
aws ec2 authorize-security-group-ingress \
  --group-id $SECURITY_GROUP_ID \
  --protocol tcp \
  --port 2049 \
  --cidr $CIDR_BLOCK \
  --profile lab

# 创建 EFS 文件系统
EFS_ID=$(aws efs create-file-system \
  --creation-token eks-karpenter-env-efs \
  --performance-mode generalPurpose \
  --throughput-mode provisioned \
  --provisioned-throughput-in-mibps 100 \
  --encrypted \
  --output text \
  --query 'FileSystemId' \
  --profile lab)

echo "EFS File System ID: $EFS_ID"

# 获取子网 ID
SUBNET_IDS=$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=availability-zone,Values=ap-southeast-1a,ap-southeast-1b,ap-southeast-1c" \
  --query 'Subnets[?MapPublicIpOnLaunch==`false`].SubnetId' \
  --output text \
  --profile lab)

# 为每个子网创建挂载目标
for subnet in $SUBNET_IDS; do
  aws efs create-mount-target \
    --file-system-id $EFS_ID \
    --subnet-id $subnet \
    --security-groups $SECURITY_GROUP_ID \
    --profile lab
done
```

### 4.2 创建存储类配置

**注意**：项目中已包含预配置的存储类文件，需要更新 EFS ID 后应用。

```bash
# 更新 general-storageclasses.yaml 中的 EFS ID（使用上一步创建的 EFS ID）
sed -i "s/fs-0123456789abcdef0/$EFS_ID/g" eks/general-storageclasses.yaml

# 应用存储类配置
kubectl apply -f eks/general-storageclasses.yaml

# 验证存储类
kubectl get storageclass
```

### 4.3 验证 S3 CSI Driver

**✅ 自动安装**：S3 CSI Driver 已在 cluster-config.yaml 中配置为自动安装的 EKS Addon。

**验证安装**：
```bash
# 检查 S3 CSI Driver Addon 状态
aws eks describe-addon \
  --cluster-name eks-karpenter-env \
  --addon-name aws-mountpoint-s3-csi-driver \
  --profile lab \
  --query 'addon.{Status:status,Version:addonVersion}'

# 验证 S3 CSI Driver Pod
kubectl get pods -n kube-system -l app=s3-csi-node

# 验证 CSI Driver 注册
kubectl get csidriver s3.csi.aws.com
```

### 4.4 创建测试 S3 存储桶

```bash
# 创建 S3 存储桶
BUCKET_NAME="eks-karpenter-env-storage-$(date +%s)"
aws s3 mb s3://$BUCKET_NAME --region us-east-1 --profile lab

echo "S3 Bucket: $BUCKET_NAME"
```

## 5: AWS LoadBalancer Controller 安装

**使用 LoadBalancer Controller 的优势**：
- ✅ **现代化** - 使用 ALB 替代即将弃用的 Classic Load Balancer
- 💰 **成本效率** - ALB 比 Classic Load Balancer 更经济
- 🚀 **功能丰富** - 支持路径路由、SSL 终止、WAF 集成等
- 📊 **更好监控** - 集成 CloudWatch 指标和日志

### 5.1 安装 AWS LoadBalancer Controller

**注意**：服务账户已在 cluster-config.yaml 中自动创建，包含所需的 IAM 权限。

```bash
# 1. 添加 EKS Helm 仓库
helm repo add eks https://aws.github.io/eks-charts
helm repo update

# 2. 获取 VPC ID
VPC_ID=$(aws eks describe-cluster --name eks-karpenter-env --query "cluster.resourcesVpcConfig.vpcId" --output text --profile lab)

# 3. 安装 AWS LoadBalancer Controller
helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName=eks-karpenter-env \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set vpcId=$VPC_ID \
  --set region=us-east-1 \
  --set podLabels.fargate=enabled

# 4. 验证安装
kubectl get deployment -n kube-system aws-load-balancer-controller
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller
```

### 5.2 使用 ALB Ingress 替代 LoadBalancer Service

安装完成后，可以使用 ALB Ingress 替代传统的 LoadBalancer Service（默认创建 Classic Load Balancer）。

```yaml
# 示例：ALB Ingress 配置
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: example-ingress
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80}]'
spec:
  ingressClassName: alb
  rules:
  - http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: example-service
            port:
              number: 80
```

### 5.3 Gateway API CRD 更新（可选）

如果使用 Gateway API 功能，需要更新 CRD：

```bash
# 更新 Gateway API CRDs（仅在使用 Gateway API 时需要）
kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/main/config/crd/gateway/gateway-crds.yaml

# 验证 CRD 更新
kubectl get crd | grep gateway
```

## 6: 验证和测试

### 6.1 测试 ALB Ingress

```bash
# 应用 ALB Ingress 测试配置
kubectl apply -f ../tests/test-alb-ingress.yaml

# 等待 ALB 创建完成
kubectl get ingress test-alb-ingress -w

# 获取 ALB 地址
ALB_URL=$(kubectl get ingress test-alb-ingress -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "ALB URL: http://$ALB_URL"

# 测试访问
curl http://$ALB_URL
```

### 6.2 测试 EFS 存储

```bash
# 应用 EFS 存储测试配置
kubectl apply -f ../tests/test-storage-efs.yaml

# 验证 PVC 状态
kubectl get pvc efs-pvc

# 查看测试 Pod 日志
kubectl logs -f efs-test-pod

# 验证数据持久化
kubectl exec efs-test-pod -- cat /mnt/efs/test.txt
```

### 6.3 测试 S3 挂载

```bash
# 更新 S3 存储桶名称
sed -i "s/eks-karpenter-env-storage-1234567890/$BUCKET_NAME/g" ../tests/test-storage-s3.yaml

# 应用 S3 存储测试配置（使用 PV + PVC 方式）
kubectl apply -f ../tests/test-storage-s3.yaml

# 验证 PV 和 PVC 状态
kubectl get pv s3-pv
kubectl get pvc s3-claim

# 查看测试 Pod 状态和日志
kubectl get pod s3-test-pod
kubectl logs s3-test-pod --tail=5

# 验证 S3 数据同步
aws s3 ls s3://$BUCKET_NAME/ --profile lab
kubectl exec s3-test-pod -- cat /mnt/s3/test.txt
```

### 6.4 清理测试资源

```bash
# 清理测试资源
kubectl delete -f ../tests/test-storage-efs.yaml
kubectl delete -f ../tests/test-storage-s3.yaml
kubectl delete -f ../tests/test-alb-ingress.yaml
## 清理资源

### 清理测试资源
```bash
# 清理测试资源
kubectl delete -f ../tests/test-storage-efs.yaml
kubectl delete -f ../tests/test-storage-s3.yaml  
kubectl delete -f ../tests/test-alb-ingress.yaml
```

### 6.5 完全清理集群
```bash
# 删除集群（会自动清理大部分资源）
eksctl delete cluster --name eks-karpenter-env --profile lab

# 手动清理残留资源
# 删除 S3 存储桶
aws s3 rb s3://$BUCKET_NAME --force --profile lab

# 删除 EFS 文件系统（可选，如果要保留数据可跳过）
aws efs delete-file-system --file-system-id $EFS_ID --profile lab
```

## 注意事项

1. 确保 AWS 账户有足够的服务限额
2. 确保部署区域的实例类型可用性
3. EFS 和 S3 的成本需要考虑在内
4. 定期检查和更新组件版本
5. 监控集群资源使用情况和成本
