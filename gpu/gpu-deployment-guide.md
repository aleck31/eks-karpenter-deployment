# EKS GPU 支持部署指南

## 概述

本指南帮助在现有 EKS + Karpenter 集群基础上添加 GPU 支持，用于机器学习工作负载。

## 🎯 GPU 架构设计

### 技术栈
- **AMI**: Deep Learning OSS Nvidia Driver AMI (AL2023)
- **GPU 实例**: G4dn, G5, G6, G6e, P3, P4 等系列
- **容器运行时**: containerd + nvidia-container-runtime
- **设备插件**: NVIDIA Device Plugin
- **调度策略**: Taint/Toleration + NodeSelector

### 架构原则
1. **专用节点池** - GPU 节点独立管理
2. **污点隔离** - 防止非 GPU 工作负载调度
3. **成本优化** - 支持 Spot 实例
4. **自动扩缩容** - 基于工作负载需求

## 🚀 部署步骤

### 1. 部署 GPU NodePool

GPU NodePool 推荐使用 EKS 优化的 NVIDIA AMI，预集成了 GPU 支持所需的核心组件。

**AL2023 EKS-optimized NVIDIA AMI** 包含以下预配置组件：

| 组件 | 版本 | 功能 | 状态 |
|------|------|------|------|
| **NVIDIA 驱动** | 470+ | GPU 硬件驱动程序 | ✅ 预装 |
| **CUDA 运行时** | 11.4+ | GPU 计算库和工具 | ✅ 预装 |
| **Container Runtime** | containerd + nvidia-runtime | 容器 GPU 访问支持 | ✅ 预配置 |
| **kubelet GPU 支持** | - | GPU 资源识别和管理 | ✅ 预配置 |
| **NVIDIA Device Plugin** | - | Kubernetes GPU 资源调度 | ❌ 需手动部署 |

#### 部署 NodePool

```bash
# 应用 GPU NodePool 配置
kubectl apply -f gpu/nodepool-gpu.yaml

# 验证 NodePool 创建
kubectl get nodepool nodepool-gpu
kubectl get ec2nodeclass nodeclass-gpu
```

**NodePool 配置要点**：
- **AMI 选择**: `amazon-eks-node-al2023-x86_64-nvidia-*` 
- **实例存储**: `instanceStorePolicy: RAID0` (本地 NVMe 优化)
- **用户数据**: 包含 kubelet 启动修复和存储挂载配置

### 2. 部署 NVIDIA Device Plugin

NVIDIA Device Plugin 负责 GPU 资源的发现、分配和管理，是生产环境的必需组件。
**Device Plugin 功能**:
- **GPU 资源发现**: 自动识别节点上的 GPU 设备
- **资源广告**: 向 Kubernetes API 报告 `nvidia.com/gpu` 资源
- **设备分配**: 为 Pod 分配专用 GPU 设备
- **资源隔离**: 防止多个 Pod 争抢同一 GPU

**为什么需要 Device Plugin？**

虽然 EKS GPU AMI 预装了 GPU 驱动和运行时，但 Kubernetes 层面的 GPU 资源管理需要额外的 Device Plugin：

| 功能 | EKS GPU AMI | NVIDIA Device Plugin |
|------|-------------|---------------------|
| GPU 硬件访问 | ✅ 支持 | - |
| 容器 GPU 运行 | ✅ 支持 | - |
| GPU 资源调度 | ❌ 不支持 | ✅ 提供 |
| 资源隔离管理 | ❌ 不支持 | ✅ 提供 |

#### 部署步骤
```bash
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.16.2/deployments/static/nvidia-device-plugin.yml
```

#### 验证 Device Plugin 部署状态
```bash
kubectl get daemonset -n kube-system nvidia-device-plugin-daemonset

# output:
NAME                             DESIRED   CURRENT   READY   UP-TO-DATE   AVAILABLE   NODE SELECTOR   AGE
nvidia-device-plugin-daemonset   1         1         1       1            1           <none>          2m
```

#### 检查 Device Plugin Pod 运行状态
```bash
kubectl get pods -n kube-system -l name=nvidia-device-plugin-ds

# output:
NAME                                   READY   STATUS    RESTARTS   AGE
nvidia-device-plugin-daemonset-xxxxx   1/1     Running   0          2m
```

### 3. 验证 GPU 节点

```bash
# 等待 GPU 节点启动 (可能需要几分钟)
kubectl get nodes -l node-type=gpu

# 检查节点 GPU 资源
kubectl describe node <gpu-node-name> | grep nvidia.com/gpu
```

## 🧪 测试 GPU 功能

### 1. 基础 GPU 检测 (快速验证)

```bash
# 部署基础 GPU 测试 - nvidia-smi 检测
kubectl apply -f tests/test-gpu-simple.yaml

# 查看测试结果
kubectl logs gpu-simple-test
```

**预期输出**：
```
+-----------------------------------------------------------------------------+
| NVIDIA-SMI 470.182.03   Driver Version: 470.182.03   CUDA Version: 11.4   |
|-------------------------------+----------------------+----------------------+
| GPU  Name        Persistence-M| Bus-Id        Disp.A | Volatile Uncorr. ECC |
| Fan  Temp  Perf  Pwr:Usage/Cap|         Memory-Usage | GPU-Util  Compute M. |
|                               |                      |               MIG M. |
|===============================+======================+======================|
|   0  Tesla T4            Off  | 00000000:00:1E.0 Off |                    0 |
| N/A   34C    P8     9W /  70W |      0MiB / 15109MiB |      0%      Default |
|                               |                      |                  N/A |
+-------------------------------+----------------------+----------------------+
```

### 2. PyTorch GPU 功能测试 (完整验证)

```bash
# 部署 PyTorch GPU 测试 - 完整 ML 框架验证
kubectl apply -f tests/test-gpu-pytorch.yaml

# 查看测试结果 (容器会持续运行)
kubectl logs gpu-pytorch-test

# 或直接在容器内执行测试
kubectl exec gpu-pytorch-test -- python3 -c "
import torch
print(f'CUDA available: {torch.cuda.is_available()}')
print(f'GPU name: {torch.cuda.get_device_name(0)}')
"
```

### 3. GPU + 本地存储测试 (存储集成)

```bash
# 部署 GPU + NVMe 存储测试
kubectl apply -f tests/test-gpu-nvme.yaml

# 查看存储测试结果
kubectl logs gpu-storage-test
```

## 📋 GPU 实例类型选择

### G4dn 系列 (NVIDIA T4)
- **适用场景**: 推理、轻量训练
- **性价比**: 高
- **推荐用途**: 模型推理、开发测试

| 实例类型 | GPU | vCPU | 内存 | 网络性能 |
|---------|-----|------|------|----------|
| g4dn.xlarge | 1x T4 | 4 | 16 GB | 最高 25 Gbps |
| g4dn.2xlarge | 1x T4 | 8 | 32 GB | 最高 25 Gbps |
| g4dn.4xlarge | 1x T4 | 16 | 64 GB | 最高 25 Gbps |

### G5 系列 (NVIDIA A10G)
- **适用场景**: 训练、推理、图形工作负载
- **性能**: 比 T4 高 2.5x
- **推荐用途**: 中等规模训练、高性能推理

| 实例类型 | GPU | vCPU | 内存 | 网络性能 |
|---------|-----|------|------|----------|
| g5.xlarge | 1x A10G | 4 | 16 GB | 最高 10 Gbps |
| g5.2xlarge | 1x A10G | 8 | 32 GB | 最高 10 Gbps |
| g5.4xlarge | 1x A10G | 16 | 64 GB | 最高 25 Gbps |

### P3 系列 (NVIDIA V100)
- **适用场景**: 大规模训练、HPC
- **性能**: 最高
- **推荐用途**: 深度学习训练、科学计算

| 实例类型 | GPU | vCPU | 内存 | 网络性能 |
|---------|-----|------|------|----------|
| p3.2xlarge | 1x V100 | 8 | 61 GB | 最高 10 Gbps |
| p3.8xlarge | 4x V100 | 32 | 244 GB | 10 Gbps |

## 💰 成本优化策略

### Spot 实例使用
```yaml
# 在 NodePool 中启用 Spot
requirements:
  - key: karpenter.sh/capacity-type
    operator: In
    values: ["spot"]  # 仅使用 Spot 实例
```

### 自动缩容配置
```yaml
# 快速缩容以节省成本
disruption:
  consolidationPolicy: WhenEmpty
  consolidateAfter: 30s  # 30秒后缩容空闲节点
```

### 实例类型优先级
1. **开发测试**: g4dn.xlarge (Spot)
2. **生产推理**: g5.xlarge (On-Demand)
3. **大规模训练**: p3.2xlarge (Spot + On-Demand 混合)

## 🔍 监控和故障排除

### 检查 GPU 资源
```bash
# 查看集群 GPU 资源总量
kubectl describe nodes | grep -A 5 "Allocatable:" | grep nvidia.com/gpu

# 查看 GPU 使用情况
kubectl top nodes --selector=node-type=gpu
```

### 常见问题

#### 1. GPU 节点无法启动
**检查**:
```bash
# 查看节点事件
kubectl describe node <gpu-node-name>

# 检查 Karpenter 日志
kubectl logs -n karpenter deployment/karpenter
```

#### 2. Device Plugin 无法运行
**检查**:
```bash
# 查看 Device Plugin 日志
kubectl logs -n kube-system -l name=nvidia-device-plugin-ds

# 验证 NVIDIA 驱动
kubectl exec -it <gpu-pod> -- nvidia-smi
```

#### 3. Pod 无法调度到 GPU 节点
**检查**:
```bash
# 确认 Toleration 和 NodeSelector
kubectl describe pod <gpu-pod>

# 检查节点污点
kubectl describe node <gpu-node> | grep Taints
```

## 📚 参考资料

- [EKS GPU 工作负载](https://docs.aws.amazon.com/eks/latest/userguide/gpu-ami.html)
- [Karpenter GPU 支持](https://karpenter.sh/docs/concepts/nodepools/)
- [NVIDIA Device Plugin](https://github.com/NVIDIA/k8s-device-plugin)
- [AWS Deep Learning AMI](https://docs.aws.amazon.com/dlami/latest/devguide/what-is-dlami.html)
