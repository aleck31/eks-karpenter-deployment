# Karpenter 多EBS卷最佳实践与GitHub Issue #2122解读

## 问题背景

### GitHub Issue #2122 详情
- **问题链接**: https://github.com/awslabs/amazon-eks-ami/issues/2122
- **影响版本**: EKS v1.31+ 使用 AL2023 AMI
- **核心问题**: pause容器缓存机制变更导致节点启动失败

### 问题原因分析

#### 1. AL2023 AMI变更
```bash
# EKS v1.30及之前 - containerd配置
sandbox_image = "602401143452.dkr.ecr.us-east-1.amazonaws.com/eks/pause:3.5"

# EKS v1.31+ - containerd配置  
sandbox_image = "localhost/kubernetes/pause"
```

#### 2. 冲突场景
```bash
# EKS最佳实践 - 多EBS卷配置脚本
systemctl stop containerd
mkfs -t ext4 /dev/nvme1n1
rm -rf /var/lib/containerd/*  # ⚠️ 删除了AMI中预缓存的pause容器
mount /dev/nvme1n1 /var/lib/containerd/
systemctl start containerd    # ❌ 找不到localhost/kubernetes/pause镜像
```

**注意**: GitHub Issue #2122中的脚本是**社区实践**，不是AWS官方提供的标准脚本。

#### 3. 错误表现
```bash
# 节点启动失败日志
containerd[4125]: failed to get sandbox image "localhost/kubernetes/pause": 
failed to pull image "localhost/kubernetes/pause": 
dial tcp 127.0.0.1:443: connect: connection refused
```

## 集群影响评估

### 当前状态检查
```bash
# 集群版本
kubectl version
# Server Version: v1.33.3-eks-b707fbb (✅ 受影响版本)

# 节点AMI类型
kubectl get nodes -o wide
# OS-IMAGE: Amazon Linux 2023.8.20250818 (✅ AL2023 AMI)

# containerd配置验证
kubectl debug node/ip-10-1-111-127.ap-southeast-1.compute.internal -it --image=busybox -- \
  chroot /host cat /etc/containerd/config.toml | grep sandbox_image
# sandbox_image = "localhost/kubernetes/pause" (✅ 确认受影响)
```

### 风险评估结果
- **🟢 当前安全**: 使用Karpenter自动节点管理，无手动操作风险
- **⚠️ 潜在风险**: 如果将来需要手动清理containerd或使用多EBS卷配置

## Workaround解决方案

### 社区脚本问题修复

保存预缓存的`localhost/kubernetes/pause`而不是直接删除
```bash
# ✅ 修复后的Workaround脚本
systemctl stop containerd
mkfs -t ext4 /dev/nvme1n1
mv /var/lib/containerd/* /tmp/containerd/     # 保存AMI缓存
mount /dev/nvme1n1 /var/lib/containerd/
mv /tmp/containerd/* /var/lib/containerd/     # 恢复pause容器缓存
systemctl start containerd
```

## Karpenter解决方案

### 为什么Karpenter不受影响

#### 1. 时序优势
| 场景 | 执行顺序 | 结果 |
|------|----------|------|
| **社区脚本** | 启动containerd → 缓存pause → 停止containerd → 删除缓存 → 挂载EBS → 重启containerd | ❌ 失败 |
| **Karpenter** | 挂载EBS → 启动containerd → pause直接缓存到EBS | ✅ 成功 |

#### 2. Karpenter声明式配置（推荐）
```yaml
# Karpenter EC2NodeClass - 创建时就配置存储
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: nodeclass-multi-ebs
spec:
  blockDeviceMappings:
  - deviceName: /dev/xvda          # 系统卷
    ebs:
      volumeSize: 50Gi
      volumeType: gp3
  - deviceName: /dev/xvdb          # containerd专用卷
    ebs:
      volumeSize: 200Gi
      volumeType: gp3
      iops: 10000
      throughput: 500
  userData: |
    #!/bin/bash
    # 在containerd启动前挂载EBS卷
    mkfs -t ext4 /dev/xvdb
    mkdir -p /var/lib/containerd
    mount /dev/xvdb /var/lib/containerd
    echo '/dev/xvdb /var/lib/containerd ext4 defaults 0 2' >> /etc/fstab
    
    # 初始化EKS节点
    /usr/bin/nodeadm init --cluster-name eks-karpenter-env
```

## AWS最佳实践：多EBS卷配置

### 核心收益

#### 1. 性能隔离
```bash
# 根卷 (/dev/xvda) - 系统操作
- OS日志、系统进程、应用日志

# containerd卷 (/dev/xvdb) - 容器操作  
- 镜像拉取/存储、容器层写入、临时文件系统
```

#### 2. 存储配额管理
- **根卷满** → 系统崩溃，节点不可用
- **containerd卷满** → 只影响容器，系统仍可管理
- **独立监控** → 分别设置告警阈值

#### 3. 性能优化配置
```yaml
blockDeviceMappings:
- deviceName: /dev/xvda      # 根卷 - 标准性能
  ebs:
    volumeType: gp3
    volumeSize: 50Gi
    iops: 3000
- deviceName: /dev/xvdb      # containerd - 高性能
  ebs:
    volumeType: gp3
    volumeSize: 200Gi
    iops: 10000              # 高IOPS用于镜像拉取
    throughput: 500          # 高吞吐量用于容器启动
```

#### 4. 故障隔离与恢复
- **EBS卷故障** → 只影响容器，系统可恢复
- **快速恢复** → 重新挂载新卷，重启containerd
- **数据保护** → 对containerd卷单独做快照

### 监控和运维

#### 存储监控
```bash
# 分别监控使用率
df -h /                    # 系统盘使用率
df -h /var/lib/containerd  # 容器存储使用率

# CloudWatch指标
- RootVolumeUtilization    # 根卷使用率告警 > 80%
- ContainerdVolumeUtilization # 容器卷使用率告警 > 90%
```

#### 自动化清理
```bash
# containerd卷空间不足时自动清理
docker system prune -af
docker volume prune -f
```

## 实施建议

### 1. 当前集群
- **✅ 继续使用现有配置** - Karpenter自动管理，无风险
- **✅ 监控此issue进展** - 关注官方解决方案

### 2. 新集群规划
- **推荐使用多EBS卷配置** - 符合AWS最佳实践
- **使用Karpenter声明式配置** - 避免手动操作风险
- **设置适当的存储监控** - 预防存储空间问题

### 3. 风险场景避免
```bash
# ❌ 避免手动操作
systemctl stop containerd
rm -rf /var/lib/containerd/*  # 危险操作

# ✅ 使用Karpenter自动化
kubectl apply -f nodeclass-multi-ebs.yaml
```

## 参考资料

- **GitHub Issue**: https://github.com/awslabs/amazon-eks-ami/issues/2122
- **EKS最佳实践**: https://docs.aws.amazon.com/eks/latest/best-practices/scale-data-plane.html
- **Karpenter文档**: https://karpenter.sh/docs/concepts/nodeclasses/
- **相关PR**: https://github.com/awslabs/amazon-eks-ami/pull/2000

---

**总结**: 通过Karpenter的声明式配置，我们可以安全地实施AWS多EBS卷最佳实践，同时天然规避GitHub Issue #2122的pause容器缓存问题。关键在于配置时序：先挂载存储，再启动containerd。
