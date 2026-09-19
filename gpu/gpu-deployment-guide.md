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
disruption:
  consolidationPolicy: WhenEmptyOrUnderutilized
  consolidateAfter: 15m
  # 必须显式声明。默认 budgets 为 10%，节点数不足 10 时 floor(n × 10%) = 0，
  # 回收被完全禁止，空节点会无限期留存。
  budgets:
    - nodes: "1"
```

NodePool 名称与仓库文件不一致时（如 inference-env 的 NodePool 名为 `gpu`，
早于本仓库 `nodepool-*` 命名约定），直接 `apply -f` 会新建一套而非更新，
应只同步该段：

```bash
kubectl patch nodepool <name> --type merge \
  -p '{"spec":{"disruption":{"consolidateAfter":"15m","budgets":[{"nodes":"1"}]}}}'
```

改动 `disruption` 不影响 `karpenter.sh/nodepool-hash`，不会触发节点漂移重建。

### 时间切片不影响 Karpenter 开通决策

Karpenter 从 EC2 机型元数据取物理 GPU 数，不知道 device plugin 会按 `replicas` 切分。
N 个请求 `nvidia.com/gpu: 1` 的 Pod 会让它开 N 台机器，之后 kube-scheduler 按切片数
把它们装进更少的节点，多出来的节点空转直到被回收。

实测（Karpenter 1.9.0，`replicas: 2`）：2 个 GPU Pod → `nodeclaims=2`，
两个 Pod 实际落在同一台，另一台从创建起即为空。

NodeOverlay 的 `spec.capacity` 叠加 `nvidia.com/gpu` 无法改变这一点，只影响调度模拟。
上游 issue kubernetes-sigs/karpenter#2140 提出过该需求，已关闭未实现。
AWS 文档的建议是配合静态容量（NodePool `spec.replicas`）使用时间切片。

### 机型与 AZ 的最终选择权在 EC2

Karpenter 按价格排序候选机型后交给 `CreateFleet`，由 EC2 用
`price-capacity-optimized` 决定最终机型与 AZ —— 它同时权衡价格和容量池深度，
不会单纯选最便宜。实测最便宜的 g6.xlarge 各 AZ 的 Spot 放置评分均为最低档，
因此常被跳过。

因此 NodeOverlay 的 `priceAdjustment` 只能改变 Karpenter 传给 EC2 的排序，
影响有限且方向难预判；想确定性控制机型只能收窄机型列表，代价是缩小容量池。

### 按显存需求区分机型

Karpenter 按 CPU、内存、GPU **数量**选型，不感知各负载需要多少**显存**，
因此显存只用几 GB 的负载可能被放到 A10G 机型上，单价是 T4 的三倍以上。
显存需求应由工作负载自己声明，放在各应用的清单里，不集中在本模块。

大模型推理需排除 T4（g4dn 显存 16GB，KV cache 不足会导致崩溃重启）：

```yaml
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: node.kubernetes.io/instance-type
              operator: In
              values: [g6.xlarge, g6.2xlarge, g5.xlarge, g5.2xlarge]
```

轻量推理（向量化、人脸检测等，显存 ≲8GB）应优选 T4。用 `preferred` 而非
`required`，Spot 容量不足时可回退，不会阻塞调度：

```yaml
affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 60
        preference:
          matchExpressions:
            - key: node.kubernetes.io/instance-type
              operator: In
              values: [g4dn.xlarge, g4dn.2xlarge]
```

### 实例类型优先级
1. **轻量推理** (显存 ≲8GB): g4dn.xlarge (Spot)
2. **大模型推理** (显存 ≥20GB): g6.xlarge / g6.2xlarge (Spot)
3. **不可中断的推理**: 同上但用 On-Demand NodePool

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
