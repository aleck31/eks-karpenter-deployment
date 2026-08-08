# Karpenter Spot 中断处理配置指南

**前置条件**：必须先完成 Karpenter 部署（参考 `karpenter/karpenter-deployment-guide.md`）

## 概述

启用 Karpenter Native Interruption Handling，通过 SQS 队列接收 EC2 Spot 中断信号，提前采取行动（cordon → 拉新节点 → drain），减少服务中断时间。

**信号类型与提前量：**

| 信号类型 | 提前量 | 说明 |
|---------|--------|------|
| Rebalance Recommendation | 10-20 分钟 | Spot 容量风险预警 |
| Spot Interruption Warning | 2 分钟 | 确认即将中断 |
| Instance State Change | 实时 | stopping/terminated |
| Scheduled Change | 数天 | AWS 维护事件 |

**架构：**

```
EC2 事件 → EventBridge Rules → SQS Queue → Karpenter 轮询
  → cordon 旧节点 → 启动新节点 → drain Pod → Pod 迁移到新节点
```

## 环境信息

| 项目 | 值 |
|------|-----|
| 集群 | eks-karpenter-env |
| 区域 | ap-southeast-1 |
| Account | 123456789012 |
| Karpenter 版本 | v1.9.0 (Helm) |
| Karpenter IAM Role | eksctl 生成的 Pod Identity Role（名称动态，见下方查询命令） |
| 认证方式 | Pod Identity |
| AWS Profile | me |

> **注意**：若 Karpenter 运行在 Fargate 上，Fargate 不支持 Pod Identity
> （[roadmap #2274](https://github.com/aws/containers-roadmap/issues/2274)），
> 需改用 IRSA Role 并相应调整下方角色查询方式。

## 1. 设置环境变量

```bash
export CLUSTER_NAME=eks-karpenter-env
export AWS_DEFAULT_REGION=ap-southeast-1
export AWS_ACCOUNT_ID=123456789012
export QUEUE_NAME=karpenter-${CLUSTER_NAME}
export PROFILE=me

# 查询 Karpenter 的 Pod Identity 角色（名称由 eksctl 生成，非固定）
ASSOC_ID=$(aws eks list-pod-identity-associations \
  --cluster-name ${CLUSTER_NAME} --region ${AWS_DEFAULT_REGION} --profile ${PROFILE} \
  --query "associations[?serviceAccount=='karpenter'].associationId" --output text)

export KARPENTER_ROLE=$(aws eks describe-pod-identity-association \
  --cluster-name ${CLUSTER_NAME} --association-id ${ASSOC_ID} \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE} \
  --query 'association.roleArn' --output text | awk -F/ '{print $NF}')

echo "Karpenter Role: ${KARPENTER_ROLE}"
```

## 2. 创建 SQS 队列

```bash
aws sqs create-queue \
  --queue-name ${QUEUE_NAME} \
  --attributes '{
    "MessageRetentionPeriod": "300",
    "SqsManagedSseEnabled": "true"
  }' \
  --region ${AWS_DEFAULT_REGION} \
  --profile ${PROFILE}
```

设置队列策略（允许 EventBridge 发送消息）：

```bash
aws sqs set-queue-attributes \
  --queue-url "https://sqs.${AWS_DEFAULT_REGION}.amazonaws.com/${AWS_ACCOUNT_ID}/${QUEUE_NAME}" \
  --attributes '{
    "Policy": "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"EventBridgeToSQS\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":[\"events.amazonaws.com\",\"sqs.amazonaws.com\"]},\"Action\":\"sqs:SendMessage\",\"Resource\":\"arn:aws:sqs:'${AWS_DEFAULT_REGION}':'${AWS_ACCOUNT_ID}':'${QUEUE_NAME}'\"}]}"
  }' \
  --region ${AWS_DEFAULT_REGION} \
  --profile ${PROFILE}
```

验证：

```bash
aws sqs get-queue-attributes \
  --queue-url "https://sqs.${AWS_DEFAULT_REGION}.amazonaws.com/${AWS_ACCOUNT_ID}/${QUEUE_NAME}" \
  --attribute-names All \
  --region ${AWS_DEFAULT_REGION} \
  --profile ${PROFILE}
```

## 3. 创建 EventBridge 规则

4 条规则，将 EC2/Health 事件路由到 SQS：

```bash
QUEUE_ARN="arn:aws:sqs:${AWS_DEFAULT_REGION}:${AWS_ACCOUNT_ID}:${QUEUE_NAME}"

# 规则 1: Spot Interruption Warning (提前 2 分钟)
aws events put-rule \
  --name karpenter-spot-interruption \
  --event-pattern '{"source":["aws.ec2"],"detail-type":["EC2 Spot Instance Interruption Warning"]}' \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

aws events put-targets --rule karpenter-spot-interruption \
  --targets "[{\"Id\":\"karpenter-sqs\",\"Arn\":\"${QUEUE_ARN}\"}]" \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

# 规则 2: Rebalance Recommendation (提前 10-20 分钟)
aws events put-rule \
  --name karpenter-rebalance-recommendation \
  --event-pattern '{"source":["aws.ec2"],"detail-type":["EC2 Instance Rebalance Recommendation"]}' \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

aws events put-targets --rule karpenter-rebalance-recommendation \
  --targets "[{\"Id\":\"karpenter-sqs\",\"Arn\":\"${QUEUE_ARN}\"}]" \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

# 规则 3: Instance State Change
aws events put-rule \
  --name karpenter-instance-state-change \
  --event-pattern '{"source":["aws.ec2"],"detail-type":["EC2 Instance State-change Notification"]}' \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

aws events put-targets --rule karpenter-instance-state-change \
  --targets "[{\"Id\":\"karpenter-sqs\",\"Arn\":\"${QUEUE_ARN}\"}]" \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

# 规则 4: AWS Health (Scheduled Changes)
aws events put-rule \
  --name karpenter-scheduled-change \
  --event-pattern '{"source":["aws.health"],"detail-type":["AWS Health Event"]}' \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

aws events put-targets --rule karpenter-scheduled-change \
  --targets "[{\"Id\":\"karpenter-sqs\",\"Arn\":\"${QUEUE_ARN}\"}]" \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}
```

验证：

```bash
for rule in karpenter-spot-interruption karpenter-rebalance-recommendation karpenter-instance-state-change karpenter-scheduled-change; do
  echo "=== ${rule} ==="
  aws events describe-rule --name ${rule} --region ${AWS_DEFAULT_REGION} --profile ${PROFILE} --query '{State:State,EventPattern:EventPattern}' --output table
done
```

## 4. 添加 SQS 权限到 Karpenter IAM Role

SQS 权限已包含在仓库的 `karpenter/karpenter-policy.json` 中（`SQSInterruptionHandling` 语句，
资源通配 `arn:aws:sqs:*:*:karpenter-*` 以匹配 `karpenter-${CLUSTER_NAME}` 命名约定）。

**新建集群**：按 `karpenter-deployment-guide.md` 创建 KarpenterControllerPolicy 时已自动包含，无需额外操作。

**已有集群**：为托管策略创建新版本使其生效。

```bash
POLICY_ARN=$(aws iam list-attached-role-policies --role-name ${KARPENTER_ROLE} \
  --profile ${PROFILE} --query "AttachedPolicies[?PolicyName=='KarpenterControllerPolicy'].PolicyArn" \
  --output text)

aws iam create-policy-version \
  --policy-arn ${POLICY_ARN} \
  --policy-document file://karpenter/karpenter-policy.json \
  --set-as-default \
  --profile ${PROFILE}
```

> IAM 托管策略最多保留 5 个版本，超限时需先删除旧版本：
> `aws iam delete-policy-version --policy-arn ${POLICY_ARN} --version-id vN --profile ${PROFILE}`

验证：

```bash
V=$(aws iam get-policy --policy-arn ${POLICY_ARN} --profile ${PROFILE} \
  --query 'Policy.DefaultVersionId' --output text)

aws iam get-policy-version --policy-arn ${POLICY_ARN} --version-id ${V} --profile ${PROFILE} \
  --query 'PolicyVersion.Document.Statement[?Sid==`SQSInterruptionHandling`]' --output json
```

## 5. 更新 Karpenter Helm 配置

```bash
# 登录 ECR Public（token 可能过期）
aws ecr-public get-login-password --region us-east-1 --profile ${PROFILE} | helm registry login --username AWS --password-stdin public.ecr.aws

# 动态读取当前已部署版本，避免本次操作意外触发版本升级
# （版本升级应作为独立变更执行，不要和配置调整混在一起）
CHART_VER=$(helm list -n karpenter -o json | python3 -c \
  "import sys,json;print([r['chart'] for r in json.load(sys.stdin) if r['name']=='karpenter'][0].rsplit('-',1)[1])")
echo "当前 chart 版本: ${CHART_VER}"

helm upgrade karpenter oci://public.ecr.aws/karpenter/karpenter \
  --version "${CHART_VER}" \
  --namespace karpenter \
  --reset-then-reuse-values \
  --set "settings.interruptionQueue=${QUEUE_NAME}"
```

> **重要**：使用 `--reset-then-reuse-values`（helm ≥ 3.14），不要用 `--reuse-values`。
> `--reuse-values` 复用的是上一个 release 的**全部计算后 values**，其中包含旧 chart 的默认镜像 tag/digest。
> 这会导致 chart 版本号更新了、controller 镜像却仍停留在旧版本（`helm list` 显示新版本，
> 但 `kubectl get deployment karpenter -o jsonpath='{...image}'` 仍是旧 tag）。
> `--reset-then-reuse-values` 会重置为新 chart 默认值，再叠加用户显式设定的值，行为符合预期。
>
> 升级后务必核对实际镜像：
> ```bash
> kubectl get deployment karpenter -n karpenter \
>   -o jsonpath='{.spec.template.spec.containers[0].image}'
> ```

验证 Karpenter 重启并加载配置：

```bash
# 等待 Pod 重启
kubectl rollout status deployment/karpenter -n karpenter

# 确认中断队列配置生效
kubectl get deployment -n karpenter karpenter \
  -o jsonpath='{.spec.template.spec.containers[0].env}' | python3 -m json.tool | grep -A2 "INTERRUPTION"

# 检查 Karpenter 日志中的 SQS 相关信息
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=30 | grep -i "interrupt\|queue\|sqs"
```

## 6. 端到端验证

```bash
# 确认 Karpenter 能正常轮询 SQS（无报错即正常）
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=50 | grep -i "error.*sqs\|error.*queue"

# 查看当前 Spot 节点
kubectl get nodeclaim -o wide

# 发送测试消息到 SQS 验证连通性（可选）
aws sqs get-queue-attributes \
  --queue-url "https://sqs.${AWS_DEFAULT_REGION}.amazonaws.com/${AWS_ACCOUNT_ID}/${QUEUE_NAME}" \
  --attribute-names ApproximateNumberOfMessages \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}
```

## 清理（如需回滚）

```bash
# 1. 移除 Helm 中断队列配置
CHART_VER=$(helm list -n karpenter -o json | python3 -c \
  "import sys,json;print([r['chart'] for r in json.load(sys.stdin) if r['name']=='karpenter'][0].rsplit('-',1)[1])")
helm upgrade karpenter oci://public.ecr.aws/karpenter/karpenter \
  --version "${CHART_VER}" --namespace karpenter --reset-then-reuse-values \
  --set "settings.interruptionQueue="

# 2. 删除 EventBridge 规则
for rule in karpenter-spot-interruption karpenter-rebalance-recommendation karpenter-instance-state-change karpenter-scheduled-change; do
  aws events remove-targets --rule ${rule} --ids karpenter-sqs --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}
  aws events delete-rule --name ${rule} --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}
done

# 3. 删除 SQS 队列
aws sqs delete-queue \
  --queue-url "https://sqs.${AWS_DEFAULT_REGION}.amazonaws.com/${AWS_ACCOUNT_ID}/${QUEUE_NAME}" \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

# 4. 回退 IAM 策略版本（如需移除 SQS 权限）
#    先从 karpenter-policy.json 中删除 SQSInterruptionHandling 语句，再创建新版本
aws iam create-policy-version \
  --policy-arn ${POLICY_ARN} \
  --policy-document file://karpenter/karpenter-policy.json \
  --set-as-default \
  --profile ${PROFILE}
```

## 故障排除

### Karpenter 日志报 SQS 权限错误
```bash
# 确认托管策略当前版本含 SQS 权限
V=$(aws iam get-policy --policy-arn ${POLICY_ARN} --profile ${PROFILE} \
  --query 'Policy.DefaultVersionId' --output text)
aws iam get-policy-version --policy-arn ${POLICY_ARN} --version-id ${V} --profile ${PROFILE} \
  --query 'PolicyVersion.Document.Statement[?Sid==`SQSInterruptionHandling`]' --output json

# 确认 Pod Identity 关联生效（Karpenter 需重启才会拿到新凭证）
aws eks list-pod-identity-associations --cluster-name ${CLUSTER_NAME} \
  --region ${AWS_DEFAULT_REGION} --profile ${PROFILE} --output table

# 确认队列存在
aws sqs get-queue-url --queue-name ${QUEUE_NAME} --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}
```

### EventBridge 规则未触发
```bash
# 检查规则状态
aws events list-rules --name-prefix karpenter --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}

# 检查 Target 配置
aws events list-targets-by-rule --rule karpenter-spot-interruption --region ${AWS_DEFAULT_REGION} --profile ${PROFILE}
```
