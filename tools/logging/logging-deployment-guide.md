# 日志聚合部署指南 (Alloy + Loki + AMG)

**前置条件**：EFS CSI Driver、EKS Pod Identity Agent、ALB Controller 已就绪；已有 Amazon Managed Grafana 工作区

## 概述

采集集群内容器与节点日志，存入 Loki（chunk 落 S3），通过 Amazon Managed Grafana 查询。

```
容器 stdout/stderr ─┐
节点 journal      ─┴→ Alloy (DaemonSet) → Loki (Deployment) → S3 (chunk + index)
                                             ↑
                            AMG ─── internal ALB Ingress
```

**为什么不用 CloudWatch Logs**：$0.50/GB ingest 为主导成本且随量线性增长，
Logs Insights 另按扫描量 $0.005/GB 计费；Loki + S3 存储约 $0.023/GB 且查询不额外收费。

**为什么不部署 Grafana Pod**：Grafana 由 AMG 托管，仪表盘状态存于 AWS 侧，集群内无需部署。

## 环境信息

| 项目 | 值 |
|------|-----|
| 集群 | eks-karpenter-env |
| 区域 | ap-southeast-1 |
| Account | 123456789012 |
| 节点架构 | arm64 (Graviton) — 所有镜像须支持 arm64 |
| 容量类型 | 全部 Spot |
| S3 桶 | Loki 专用，实际值见 overlays/<env>/kustomization.yaml |
| 保留期 | 30 天 |
| Loki 版本 | 3.6.15 |
| Alloy 版本 | v1.18.1 |
| EFS StorageClass | efs-sc (动态供给) |
| AMG 工作区 | eks-env-monitoring |
| AWS Profile | me |

## 关键设计决策

### 1. Loki 使用专用 S3 桶，不与其他数据共用

Loki 在设计上假定独占一个桶，**不提供稳定的对象前缀配置**：

- `storage_config.object_prefix` 能为所有对象加前缀，但在 3.6 / 3.7 文档中均标记为 **Experimental**
- `schema_config.configs[].index.path_prefix` 是稳定选项，但仅作用于 index，不含 chunk
- Thanos objstore 客户端的 `storage_prefix` 规定只能包含数字、字母和短横线，无法表达多级路径

因此采用专用桶，Loki 写入桶根目录（`index/` 与租户目录），不依赖实验特性。
附带好处：生命周期规则与 IAM 策略均可作用于整桶，无需前缀条件，配置更简单且不易误删其他数据。

### 2. chunk 存 S3，WAL 存 EFS，不用 EBS

全部节点跑 Spot，Pod 被回收后可能重建在其他 AZ。EBS 卷有 AZ 亲和性，
换 AZ 后无法挂载，Pod 会卡在 `Pending`（现有 prometheus-pvc 使用 gp3 存在同样隐患）。

- **chunk / index → S3**：无 AZ 概念，Pod 重建无损
- **WAL → EFS (efs-sc)**：跨 AZ 可挂载。WAL 仅用于崩溃恢复未 flush 的数据，量小，
  EFS 的 NFS 延迟在本集群量级（约 1GB/天）下无影响

> 备选：WAL 用 `emptyDir`。本集群已启用 Karpenter Spot 中断处理，节点回收前会 cordon + drain，
> Loki 有 2 分钟做 graceful flush。但 EFS 更保险，故采用 EFS。

### 3. 保留期由 Loki compactor 管理，不用 S3 生命周期

Loki 的 index 与 chunk 存在引用关系。S3 生命周期按对象时间删除会导致
index 仍引用已删除的 chunk，查询报错。compactor 理解该结构，会先更新 index 再删数据。

S3 生命周期仅用于清理未完成的分片上传（卫生项）。

### 4. SingleBinary 模式

本集群规模小（56 Pod，约 1GB/天）。SingleBinary 将所有 Loki 组件跑在一个进程内，
运维面最小。日志量增长到 TB 级别再考虑拆分为 read/write/backend。

## 1. 设置环境变量

```bash
export CLUSTER_NAME=eks-karpenter-env
export AWS_DEFAULT_REGION=ap-southeast-1
export AWS_ACCOUNT_ID=123456789012
export BUCKET=<你的 Loki 专用桶名>
export PROFILE=me
```

## 2. 创建 S3 桶

```bash
aws s3api create-bucket \
  --bucket ${BUCKET} \
  --region ${AWS_DEFAULT_REGION} \
  --create-bucket-configuration LocationConstraint=${AWS_DEFAULT_REGION} \
  --profile ${PROFILE}

# 加密：SSE-S3（不用 SSE-KMS，Loki 请求量大，KMS 按请求计费会显著放大成本）
aws s3api put-bucket-encryption --bucket ${BUCKET} --profile ${PROFILE} \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# 阻止公开访问
aws s3api put-public-access-block --bucket ${BUCKET} --profile ${PROFILE} \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

# 生命周期：清理未完成的分片上传（对象过期由 Loki compactor 管理，不在此设置）
aws s3api put-bucket-lifecycle-configuration --bucket ${BUCKET} --profile ${PROFILE} \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "AbortIncompleteMultipartUploads",
      "Status": "Enabled",
      "Filter": {},
      "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 7 }
    }]
  }'

# 标签
aws s3api put-bucket-tagging --bucket ${BUCKET} --profile ${PROFILE} \
  --tagging "TagSet=[{Key=cluster,Value=${CLUSTER_NAME}},{Key=purpose,Value=loki-logs}]"
```

> **不要启用版本控制**。Loki 会持续删除对象，版本控制会保留旧版本与删除标记，
> 导致存储量虚高。新建桶默认未启用，无需操作。

验证：

```bash
aws s3api get-bucket-encryption --bucket ${BUCKET} --profile ${PROFILE}
aws s3api get-bucket-versioning --bucket ${BUCKET} --profile ${PROFILE}   # 应为空
aws s3api get-bucket-lifecycle-configuration --bucket ${BUCKET} --profile ${PROFILE}
```

## 3. 创建 IAM 角色与 Pod Identity 关联

```bash
# 信任策略：允许 Pod Identity 服务代入
cat > /tmp/loki-trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "pods.eks.amazonaws.com" },
      "Action": ["sts:AssumeRole", "sts:TagSession"]
    }
  ]
}
EOF

aws iam create-role \
  --role-name LokiS3Role-${CLUSTER_NAME} \
  --assume-role-policy-document file:///tmp/loki-trust-policy.json \
  --profile ${PROFILE}

# 权限策略：专用桶，无需前缀条件
cat > /tmp/loki-s3-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LokiObjectAccess",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/*"
    },
    {
      "Sid": "LokiBucketList",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::${BUCKET}"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name LokiS3Role-${CLUSTER_NAME} \
  --policy-name LokiS3Access \
  --policy-document file:///tmp/loki-s3-policy.json \
  --profile ${PROFILE}

# Pod Identity 关联（namespace 与 serviceAccount 须与清单一致）
aws eks create-pod-identity-association \
  --cluster-name ${CLUSTER_NAME} \
  --namespace logging \
  --service-account loki \
  --role-arn arn:aws:iam::${AWS_ACCOUNT_ID}:role/LokiS3Role-${CLUSTER_NAME} \
  --region ${AWS_DEFAULT_REGION} \
  --profile ${PROFILE}
```

验证：

```bash
aws eks list-pod-identity-associations --cluster-name ${CLUSTER_NAME} \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE} --output table
```

## 4. 部署 Loki 与 Alloy

采用 kustomize base + overlay：`base/` 只含通用结构，环境相关取值（集群名、区域、S3 桶名）
由 `overlays/<env>/` 通过 ConfigMap 注入，Loki 以 `-config.expand-env=true` 展开，
Alloy 以 `sys.env()` 读取。仓库中不含真实取值，无需部署前手工替换。

```bash
# 1. 复制示例 overlay，目录名建议用语义化环境名（如 general-env / inference-env）
cp -r tools/logging/overlays/example tools/logging/overlays/<env-name>

# 2. 编辑三个取值
#    CLUSTER_NAME    实际集群名，会成为 Loki 的 cluster 标签
#    AWS_REGION      桶所在区域
#    LOKI_S3_BUCKET  第 2 节创建的桶名
vi tools/logging/overlays/<env-name>/kustomization.yaml

# 3. 部署（先确认 kubectl context 指向对应集群）
kubectl config current-context
kubectl apply -k tools/logging/overlays/<env-name>
```

> `.gitignore` 默认忽略 `overlays/` 下的全部目录、仅放行 `example/`，
> 因此无论目录取什么名字，填入的真实取值都不会被误提交。

> **注意**：overlay 不决定部署到哪个集群，`kubectl` 的当前 context 才决定。
> 若 context 与 overlay 不匹配，命令仍会成功，但会在错误的集群里写入另一个环境的
> `cluster` 标签和 S3 桶。部署前务必核对 context。

目录结构：

```
tools/logging/
├── base/                        # 入库：通用清单，无环境相关取值
│   ├── kustomization.yaml
│   ├── logging-namespace.yaml
│   ├── loki-config.yaml         # 桶名/区域用 ${LOKI_S3_BUCKET} / ${AWS_REGION}
│   ├── loki-deployment.yaml     # Deployment + Service + SA + WAL PVC (efs-sc)
│   ├── loki-ingress.yaml        # internal ALB Ingress（供 AMG 访问）
│   ├── alloy-config.yaml        # 容器日志 + 节点 journal 采集
│   └── alloy-daemonset.yaml     # DaemonSet + RBAC
└── overlays/
    ├── example/                 # 入库：示例取值，供复制
    └── <env-name>/              # 不入库：真实取值（如 general-env）
```

> `base/` 不能单独部署——`${LOKI_S3_BUCKET}` 不会被展开，且缺少 `logging-env` ConfigMap。
> 这是有意设计，以排除"用占位符值部署成功"的可能。验证渲染可用 `overlays/example/`。


## 5. 验证部署

```bash
# Pod 状态
kubectl get pods -n logging -o wide

# Loki 就绪
kubectl port-forward -n logging svc/loki 3100:3100 &
curl -s http://localhost:3100/ready

# 确认 S3 已写入（启动后数分钟才会 flush 首批 chunk）
aws s3 ls s3://${BUCKET}/ --recursive --profile ${PROFILE} | head

# 确认 Pod Identity 生效（不应出现 AccessDenied）
kubectl logs -n logging -l app=loki --tail=50 | grep -iE "s3|denied|error"

# 确认 Alloy 正在采集
kubectl logs -n logging -l app=alloy --tail=30 | grep -iE "error|target"
```

## 6. 接入 Amazon Managed Grafana

AMG 已配置 VPC 连通性（与集群同 VPC），可通过 internal ALB 访问 Loki，
方式与现有 `prometheus-ingress` 相同。

```bash
kubectl get ingress -n logging loki-ingress \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```

在 AMG 控制台添加数据源：

1. Configuration → Data sources → Add data source → Loki
2. URL 填 `http://<上述 ALB 地址>:3100`
3. Save & test

> AMG 的安全组需允许访问 Loki 的 ALB。若 test 失败，检查
> ALB 安全组入站规则是否放行 AMG 的安全组。

### 常用 LogQL

```logql
# 某 namespace 全部日志
{namespace="hostwo"}

# 某应用的错误
{namespace="klimt"} |= "error"

# 按 Pod 聚合错误率
sum by (pod) (rate({namespace="hostwo"} |= "error" [5m]))

# 节点系统日志
{job="journal", unit="kubelet.service"}
```

## 日志覆盖范围

Loki 采集的是节点上 `/var/log/pods/` 的容器 stdout/stderr，以及节点 journal。
以下日志**不在** Loki 中：

| 日志类型 | 所在位置 | 说明 |
|---|---|---|
| 容器内文件日志 | 容器可写层 | DaemonSet 无法读取，需应用改为输出 stdout |
| EKS 控制平面 | CloudWatch（30 天） | AWS 托管，在 AMG 中加 CloudWatch 数据源查看 |
| K8s 审计日志 | 未启用 | 量大，按需评估 |
| ALB 访问日志 | 未启用 | ALB 仅支持写 S3，为独立议题 |

## 成本预估

按容器日志约 30GB/月估算：

| 项目 | 月成本 |
|------|--------|
| S3 存储（30 天滚动，压缩后约 10-15GB） | ~$0.35 |
| S3 请求（PUT/GET） | ~$0.50 |
| EFS（WAL，约 1GB） | ~$0.30 |
| 计算（复用现有节点） | $0 |
| **合计** | **~$1.2** |

对比 CloudWatch Logs 方案：ingest $15 + 存储 $0.9 + 查询按扫描量，约 $16+/月。

## 故障排除

### Loki Pod 卡在 Pending

```bash
kubectl describe pod -n logging -l app=loki | grep -A10 Events
```

常见原因：EFS PVC 未能绑定。确认 EFS CSI Controller 在运行：

```bash
kubectl get deployment efs-csi-controller -n kube-system
```

### Loki 日志报 S3 AccessDenied

```bash
# 确认 Pod Identity 关联存在且 namespace/SA 匹配
aws eks list-pod-identity-associations --cluster-name ${CLUSTER_NAME} \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE} --output table

aws iam get-role-policy --role-name LokiS3Role-${CLUSTER_NAME} \
  --policy-name LokiS3Access --profile ${PROFILE}
```

Pod Identity 关联创建后，Loki Pod 需重启才能取得凭证：

```bash
kubectl rollout restart deployment/loki -n logging
```

### AMG 数据源 test 失败

```bash
kubectl get ingress -n logging loki-ingress
# 确认 ALB 已就绪，且 ALB 安全组放行 AMG 安全组
```

### Spot 中断后日志出现缺口

确认 WAL 在 EFS 上且 graceful shutdown 时间充足：

```bash
kubectl get pvc -n logging
kubectl get pod -n logging -l app=loki \
  -o jsonpath='{.items[0].spec.terminationGracePeriodSeconds}'
```

## 清理（如需回滚）

```bash
kubectl delete -k tools/logging/

ASSOC=$(aws eks list-pod-identity-associations --cluster-name ${CLUSTER_NAME} \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE} \
  --query "associations[?serviceAccount=='loki'].associationId" --output text)
aws eks delete-pod-identity-association --cluster-name ${CLUSTER_NAME} \
  --association-id ${ASSOC} --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

aws iam delete-role-policy --role-name LokiS3Role-${CLUSTER_NAME} \
  --policy-name LokiS3Access --profile ${PROFILE}
aws iam delete-role --role-name LokiS3Role-${CLUSTER_NAME} --profile ${PROFILE}

# S3 数据（谨慎：不可恢复）
# aws s3 rm s3://${BUCKET}/ --recursive --profile ${PROFILE}
# aws s3api delete-bucket --bucket ${BUCKET} --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}
```
