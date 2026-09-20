# APerf EKS 部署指南

APerf 是 AWS 开源的性能分析工具，本文档介绍如何在 EKS 集群中部署 APerf 进行节点性能监控。

## 项目概述

**APerf (AWS Performance)** 是一个命令行性能分析工具，它整合了多种性能监控工具的功能：
- 替代多个工具：`perf`, `sysstat`, `sysctl`, `ebpf` 等
- 数据可视化：生成交互式HTML报告
- 对比分析：支持多个运行结果的并排比较
- 专为AWS优化：特别适用于Graviton处理器性能分析

## 部署架构

- **Job 模式**：按需在指定节点运行性能分析任务
- **特权容器**：需要访问主机系统资源进行性能数据收集
- **数据持久化**：分析结果存储到 PVC 中，便于后续下载
- **主机访问**：挂载主机的 `/proc`, `/sys`, `/dev`, `/boot` 等目录
- **自动清理**：分析完成后Job自动结束，节省资源

## 系统要求

### 权限要求
- **特权容器**：`privileged: true`
- **主机网络**：`hostNetwork: true` 
- **主机PID**：`hostPID: true`
- **主机路径访问**：需要读取系统性能数据

### 节点要求
- **仅支持EC2节点**：不支持Fargate（需要特权容器）
- **按需标记**：只在标记了`performance-monitoring=enabled`的节点运行
- **架构自适应**：自动检测ARM64/x86_64架构并下载对应版本

### 性能优化
- **大核心数优化**：自动设置 `perf_event_mux_interval_ms=100ms`
- **资源限制**：合理的CPU/内存限制避免影响节点性能
- **存储要求**：建议使用EFS支持多节点数据共享

## 快速开始

### 1. 标记目标节点
```bash
# 标记需要分析的EC2节点
kubectl label nodes <node-name> performance-monitoring=enabled

# 查看已启用的节点
kubectl get nodes -l performance-monitoring=enabled
```

### 2. 部署APerf Job
```bash
# 部署性能分析Job
cd /home/ubuntu/labzone/eks-env/tools/aperf
kubectl apply -k .
```

### 3. 监控执行进度
```bash
# 查看Job状态
kubectl get jobs -n aperf-system

# 查看执行日志
kubectl logs -n aperf-system job/aperf-analysis -f
```

### 4. 下载分析报告
```bash
# 创建临时Pod访问数据
kubectl run temp-access --image=public.ecr.aws/amazonlinux/amazonlinux:2023 \
  -n aperf-system --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"access","image":"public.ecr.aws/amazonlinux/amazonlinux:2023","command":["sleep","300"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"aperf-data"}}]}}'

# 下载报告文件
kubectl cp aperf-system/temp-access:/data/aperf-analysis-*/report-*.tar.gz ./aperf-report.tar.gz

# 解压查看报告
tar -xzf aperf-report.tar.gz
# 用浏览器打开 report-*/index.html
```

### 5. 清理资源
```bash
# 使用 Kustomize 清理所有资源
kubectl delete -k .

# 禁用节点监控（可选）
kubectl label nodes <node-name> performance-monitoring-
```

### 节点管理
```bash
# 启用节点性能监控
kubectl label nodes ip-10-1-111-127.ap-southeast-1.compute.internal performance-monitoring=enabled

# 禁用节点性能监控
kubectl label nodes ip-10-1-111-127.ap-southeast-1.compute.internal performance-monitoring-
```

**说明**: 
- **按需部署**: 只在标记了`performance-monitoring=enabled`的EC2节点上运行
- **不支持Fargate**: APerf需要特权容器，只能在EC2节点运行

## 故障排除

### 常见问题
1. **Pod Pending** - 检查节点标签和资源可用性
2. **权限错误** - 确认特权容器配置正确
3. **工具缺失** - 使用Amazon Linux 2023镜像确保兼容性
4. **数据不完整** - 容器化限制，考虑直接在节点运行

### 日志查看
```bash
# 查看Job执行日志
kubectl logs -n aperf-system job/aperf-analysis

# 查看Pod详细信息
kubectl describe pod -n aperf-system <pod-name>
```

## 监控指标

APerf 收集以下性能数据：
- **CPU**: 利用率和性能计数器 (PMU)
- **内存**: 使用情况和虚拟内存统计
- **磁盘**: I/O 统计和磁盘利用率
- **网络**: 网络统计和连接信息
- **中断**: 中断数据和CPU分布
- **进程**: 进程性能分析和资源使用
- **系统**: 内核配置和sysctl参数

### 报告文件说明
```bash
# 典型的报告文件结构
report-xxx/
├── index.html          # 主报告页面（用浏览器打开）
├── data/               # 原始性能数据
├── charts/             # 图表数据
└── assets/             # 样式和脚本文件
```

## 故障排除

### 常见问题
1. **权限不足**
   ```bash
   # 检查特权容器配置
   kubectl describe pod -n aperf-system aperf-monitor-xxx
   ```

2. **性能开销过大**
   ```bash
   # 检查perf_event_mux_interval_ms设置
   kubectl logs -n aperf-system aperf-monitor-xxx
   ```

3. **存储访问问题**
   ```bash
   # 检查PVC状态
   kubectl get pvc -n aperf-system
   ```

## 注意事项

1. **特权容器**：APerf 需要特权模式访问系统资源
2. **存储空间**：性能数据可能占用较多存储空间，建议定期清理
3. **节点选择**：可通过 nodeSelector 限制部署到特定节点
4. **资源限制**：根据需要调整 CPU/内存限制
5. **安全考虑**：特权容器具有较高权限，仅在受信任环境中使用

## 参考资料

- [APerf GitHub仓库](https://github.com/aws/aperf)
- [AWS性能分析博客](https://aws.amazon.com/blogs/compute/using-amazon-aperf-to-go-from-50-below-to-36-above-performance-target/)
- [Graviton性能优化指南](https://github.com/aws/aws-graviton-getting-started/blob/main/perfrunbook/README.md)
