# 配置 EKS Web Console 访问权限

## 问题描述

在 EKS Web Console 中访问集群时收到以下错误：

```
Your current IAM principal doesn't have access to Kubernetes objects on this cluster.
This might be due to the current principal not having an IAM access entry with permissions to access the cluster.
```

## 解决方案：使用 aws-auth ConfigMap

### 步骤 1: 查询当前 IAM Principal

在 AWS CloudShell 或本地终端中执行：

```bash
aws sts get-caller-identity
```

示例输出：
```json
{
    "UserId": "ABCDEFGNR425I3BB7DYXM:ws-demo",
    "Account": "123456789012",
    "Arn": "arn:aws:sts::123456789012:assumed-role/Admin/ws-demo"
}
```

### 步骤 2: 更新 aws-auth ConfigMap

根据 ARN 类型选择对应的配置方法：

#### 方法 1: Role 映射（为 IAM 角色添加集群访问权限, 推荐）

适用于 `assumed-role` 类型的 ARN，为整个角色授权：

```bash
kubectl patch configmap aws-auth -n kube-system --patch '
data:
  mapRoles: |
    # 保留现有的角色映射...
    - groups:
      - system:masters
      rolearn: arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME
      username: admin:{{SessionName}}
'
```

#### 方法 2: User 映射（为 IAM User 添加集群访问权限）

适用于特定用户会话：

```bash
kubectl patch configmap aws-auth -n kube-system --patch '
data:
  mapUsers: |
    - userarn: arn:aws:sts::ACCOUNT_ID:assumed-role/ROLE_NAME/USER_NAME
      username: admin-user
      groups:
        - system:masters
'
```

### 步骤 3: 验证配置

```bash
kubectl get configmap aws-auth -n kube-system -o yaml
```

### 步骤 4: 测试访问

1. 等待 2-3 分钟让配置生效
2. 刷新 EKS Web Console
3. 现在应该可以正常访问集群资源

## 实际案例

### 问题场景
- **用户身份**: `arn:aws:sts::123456789012:assumed-role/Admin/ws-demo`
- **集群名称**: `eks-karpenter-env`

### 解决配置
```bash
# 为 Admin 角色添加权限
kubectl patch configmap aws-auth -n kube-system --patch '
data:
  mapRoles: |
    - groups:
      - system:masters
      rolearn: arn:aws:iam::123456789012:role/Admin
      username: admin:{{SessionName}}
'
```

### 验证结果
✅ Role 映射生效，Web Console 可以正常访问集群资源

## 注意事项

1. **Role 映射 vs User 映射**：
   - Role 映射：为整个 IAM 角色授权，更灵活
   - User 映射：为特定用户会话授权，更精确

2. **权限级别**：
   - `system:masters`：集群管理员权限
   - 可根据需要调整为更细粒度的权限

3. **生效时间**：
   - 配置更新后需要 2-3 分钟生效
   - 建议刷新浏览器页面

## 相关命令

```bash
# 查看当前 aws-auth 配置
kubectl get configmap aws-auth -n kube-system -o yaml

# 查看当前 IAM 身份
aws sts get-caller-identity

# 测试 kubectl 访问
kubectl get nodes
```
