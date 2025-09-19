# EKS 监控部署指南

本文档介绍如何在 EKS 集群中部署自建 Prometheus + Node Exporter，并集成 AWS 托管 Grafana (AMG) 的混合监控方案。

## 项目概述

**混合监控架构**：
- **自建 Prometheus** - 在 EKS 集群内收集和存储指标
- **Node Exporter** - DaemonSet 模式收集节点级指标  
- **AWS Managed Grafana** - 托管可视化和告警服务
- **数据持久化** - 使用 EBS 存储 Prometheus 数据

## 架构优势

### ✅ 成本效益
- **Prometheus 自建** - 只需支付 EKS 计算资源费用
- **AMG 托管** - 按用户数付费，无需维护 Grafana 实例
- **存储优化** - 使用 EBS GP3 高性价比存储

### ✅ 运维平衡
- **数据控制** - Prometheus 配置完全自主
- **可视化托管** - Grafana 由 AWS 维护和更新
- **渐进迁移** - 后续可选择迁移到完全托管

### ✅ 功能完整
- **全栈监控** - 集群、节点、Pod、应用指标
- **自定义告警** - 灵活的告警规则配置
- **多数据源** - 可集成 CloudWatch、其他 Prometheus 等

## 系统要求

### 版本信息
- **Prometheus**: v3.5.0 (最新稳定版)
- **Node Exporter**: v1.8.2 (最新版)
- **Kubernetes API**: EndpointSlice (v1.33+ 推荐)

### 集群要求
- **EKS 版本** - 1.28+ (支持控制平面指标)
- **节点类型** - EC2 节点 (自动排除Fargate节点)
- **存储** - EBS CSI Driver 已安装
- **网络** - 节点间通信正常

### 资源需求

#### 测试环境
- **Prometheus** - 200m CPU, 512Mi 内存, 50Gi 存储
- **Node Exporter** - 100m CPU, 128Mi 内存 (每EC2节点)

#### 生产环境 (推荐)
- **Prometheus** - 1-2 CPU, 4-8Gi 内存, 100-500Gi 存储
- **Node Exporter** - 100m CPU, 128Mi 内存 (每EC2节点)
- **数据保留** - 30-90天 (根据合规要求)
- **备份策略** - 定期快照和异地备份

## 快速开始

### 1. 部署监控组件
```bash
# 部署 Prometheus 和 Node Exporter
cd /home/ubuntu/labzone/eks-env/tools/monitoring
kubectl apply -k .
```

### 2. 验证部署状态
```bash
# 检查 Prometheus 状态
kubectl get pods -n monitoring

# 检查 Node Exporter 状态  
kubectl get daemonset -n monitoring

# 访问 Prometheus UI (可选)
kubectl port-forward -n monitoring svc/prometheus 9090:9090
```

### 3. 创建 AMG 工作区

#### 方式一：AWS 控制台 (推荐)
1. 打开 [Amazon Managed Grafana 控制台](https://console.aws.amazon.com/grafana/)
2. 点击 "Create workspace"
3. 配置工作区：
   - **Workspace name**: `EKS-Monitoring`
   - **Description**: `EKS集群监控工作区`
   - **Authentication**: AWS IAM Identity Center (推荐)
   - **Permission type**: Service managed
4. 等待工作区创建完成 (约2-3分钟)

#### 方式二：AWS CLI
```bash
# 创建 AMG 工作区 (需要相应IAM权限)
aws grafana create-workspace \
  --workspace-name "EKS-Monitoring" \
  --workspace-description "EKS集群监控工作区" \
  --account-access-type CURRENT_ACCOUNT \
  --authentication-providers AWS_SSO \
  --permission-type SERVICE_MANAGED \
  --region ap-southeast-1
```

#### 权限要求
创建AMG工作区需要以下IAM权限：
- `grafana:CreateWorkspace`
- `grafana:DescribeWorkspace`
- `iam:CreateRole` (如果使用服务管理权限)
- `iam:AttachRolePolicy`

### 4. 配置 Prometheus 数据源

#### 获取 Prometheus 端点
```bash
# AMG通过VPC连接访问：使用内网ALB
kubectl get ingress prometheus-ingress -n monitoring

# 获取内网ALB地址
PROMETHEUS_URL=$(kubectl get ingress prometheus-ingress -n monitoring -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "Prometheus URL: http://$PROMETHEUS_URL:9090"
```

#### 在 AMG 中添加数据源
1. 登录 AMG 工作区 (需要Admin权限)
2. 导航到 **Administration** → **Data sources** (或 **Configuration** → **Data sources**)
3. 点击 **Add data source** → 选择 **Prometheus**
4. 配置连接：
   - **Name**: `EKS-Prometheus`
   - **URL**: `http://internal-k8s-monitori-promethe-xxx.us-east-1.elb.amazonaws.com:9090`
   - **Access**: Server (default)
   - **HTTP Method**: GET
5. 点击 **Save & test** 验证连接

**注意**: 确保AMG工作区已配置VPC连接，使用相同的安全组和私有子网。

### 5. 导入监控仪表板

#### 推荐仪表板
```bash
# EKS 集群概览
Dashboard ID: 15757 - Kubernetes / Views / Global

# 节点监控
Dashboard ID: 1860 - Node Exporter Full

# Pod 监控
Dashboard ID: 15758 - Kubernetes / Views / Pods

# 工作负载监控  
Dashboard ID: 15759 - Kubernetes / Views / Namespaces
```

#### 导入步骤
1. 在 AMG 中点击 **+** → **Import**
2. 输入 Dashboard ID 或上传 JSON 文件
3. 选择 Prometheus 数据源
## 验证部署

### 检查组件状态
```bash
# 检查所有监控组件
kubectl get all -n monitoring

# 验证 Prometheus 目标
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
curl http://localhost:9090/api/v1/targets
pkill -f "kubectl port-forward"

# 检查 Node Exporter 指标
kubectl get pods -n monitoring -l app=node-exporter
```

### 验证指标收集
```bash
# 检查节点指标
curl -s http://prometheus-endpoint:9090/api/v1/query?query=up{job="node-exporter"}

# 检查 API Server 指标  
curl -s http://prometheus-endpoint:9090/api/v1/query?query=up{job="kubernetes-apiservers"}

# 检查 Pod 指标
curl -s http://prometheus-endpoint:9090/api/v1/query?query=kube_pod_info
```

## 故障排除

### 常见问题

#### Node Exporter 无法启动
```bash
# 检查节点选择器
kubectl describe daemonset node-exporter -n monitoring

# 确认 EC2 节点标签
kubectl get nodes --show-labels | grep -v fargate
```

#### Prometheus 权限错误
```bash
# 检查 RBAC 权限
kubectl describe clusterrole prometheus

# 验证 ServiceAccount
kubectl get serviceaccount prometheus -n monitoring
```

#### AMG 连接失败
- 检查安全组规则 (端口 9090)
- 验证 VPC 网络连通性
- 确认 Prometheus 服务类型和端点

### 性能优化

#### Prometheus 配置调优
```yaml
# 生产环境建议配置
global:
  scrape_interval: 30s      # 降低抓取频率
  evaluation_interval: 30s  # 降低规则评估频率
  
storage:
  tsdb:
    retention.time: 30d     # 数据保留30天
    retention.size: 100GB   # 存储大小限制
```

#### 资源限制调整
```yaml
# 根据集群规模调整
resources:
  requests:
    cpu: 500m-2000m
    memory: 2Gi-8Gi
  limits:
    cpu: 2000m-4000m  
    memory: 4Gi-16Gi
```

## 最佳实践

### 安全配置
- 使用 HTTPS 连接 AMG
- 配置适当的 IAM 权限
- 启用 VPC 端点 (生产环境)
- 定期更新组件版本

### 监控策略
- 设置关键指标告警
- 配置数据备份策略
- 监控存储使用情况
- 定期检查组件健康状态

### 成本优化
- 使用 Spot 实例运行 Prometheus
- 调整数据保留策略
- 优化抓取间隔和目标
- 使用 AMG 按需付费模式

## 相关文档

- [EKS 集群部署指南](../../eks/create-eks-cluster-guide.md)
- [Karpenter 部署指南](../../karpenter/karpenter-deployment-guide.md)
- [AWS Managed Grafana 用户指南](https://docs.aws.amazon.com/grafana/)
- [Prometheus 官方文档](https://prometheus.io/docs/)

## 监控指标

### 集群级指标
- **API Server** - 请求延迟、错误率、吞吐量
- **etcd** - 延迟、存储使用、集群健康
- **调度器** - 调度延迟、队列深度
- **控制器** - 工作队列、同步延迟

### 节点级指标
- **CPU** - 使用率、负载、上下文切换
- **内存** - 使用率、缓存、交换
- **磁盘** - I/O、使用率、延迟
- **网络** - 流量、错误、连接数

### Pod 级指标
- **资源使用** - CPU、内存实际使用
- **状态监控** - 重启次数、就绪状态
- **网络** - Pod 间通信指标

## 配置说明

### Prometheus 配置
```yaml
# 数据保留期
retention: "15d"

# 存储配置
storage:
  volumeClaimTemplate:
    spec:
      storageClassName: gp3
      resources:
        requests:
          storage: 50Gi
```

### Node Exporter 配置
```yaml
# 收集的指标类型
args:
  - --path.procfs=/host/proc
  - --path.sysfs=/host/sys
  - --collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($$|/)
```

## 故障排除

### 常见问题
1. **Pod Pending** - 检查存储类和节点资源
2. **指标缺失** - 验证 Service Discovery 配置
3. **存储不足** - 调整保留期或扩容 PVC
4. **网络问题** - 检查 Security Group 和 NACLs

### 日志查看
```bash
# Prometheus 日志
kubectl logs -n monitoring deployment/prometheus

# Node Exporter 日志
kubectl logs -n monitoring daemonset/node-exporter

# 配置验证
kubectl get configmap -n monitoring prometheus-config -o yaml
```

## 最佳实践

### 性能优化
1. **采样频率** - 根据需求调整 scrape_interval
2. **数据保留** - 平衡存储成本和历史数据需求
3. **标签优化** - 避免高基数标签导致内存问题
4. **查询优化** - 使用 recording rules 预计算常用指标

### 安全配置
1. **网络隔离** - 使用 NetworkPolicy 限制访问
2. **RBAC** - 最小权限原则配置服务账户
3. **数据加密** - 启用 EBS 卷加密
4. **访问控制** - 通过 AMG 管理用户权限

### 告警策略
1. **分层告警** - 区分警告、严重、紧急级别
2. **告警抑制** - 避免告警风暴
3. **通知渠道** - 集成 SNS、Slack、PagerDuty
4. **SLO 监控** - 基于业务指标设置告警

## 成本优化

### 存储优化
- 使用 GP3 替代 GP2 存储类
- 合理设置数据保留期
- 定期清理无用的时间序列

### 计算优化
- 使用 Spot 实例运行监控组件
- 根据负载调整资源请求和限制
- 启用 HPA 自动扩缩容

## 扩展集成

### 其他数据源
- **CloudWatch** - AWS 服务指标
- **X-Ray** - 分布式追踪
- **ELK Stack** - 日志聚合分析

### 告警集成
- **SNS** - 邮件和短信通知
- **Slack** - 团队协作通知
- **PagerDuty** - 值班管理

## 参考资料

- [Prometheus 官方文档](https://prometheus.io/docs/)
- [Node Exporter 指标说明](https://github.com/prometheus/node_exporter)
- [AWS Managed Grafana 用户指南](https://docs.aws.amazon.com/grafana/)
- [EKS 监控最佳实践](https://aws.github.io/aws-eks-best-practices/reliability/docs/observability/)
