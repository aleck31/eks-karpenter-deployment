# Auto Draw.io 部署指南 (EKS)

## 📋 部署概述

本指南在 EKS 集群中部署 Auto Draw.io AI 驱动的图表生成器，支持内网和公网访问。

### 应用特点
- **AI 驱动** - 集成 Amazon Bedrock Claude 模型
- **图表生成** - 支持 AWS 架构图、流程图等
- **Web 界面** - 基于 Next.js 的现代化界面
- **ARM64 优化** - 运行在 Karpenter ARM64 节点上
- **双重访问** - 支持内网 ALB 和公网 CloudFront 访问

### 部署架构
```
用户请求 → CloudFront (全球CDN)
            ↓
        VPC Origin (AWS内网连接)
            ↓
        Internal ALB (ap-southeast-1)
            ↓
        Auto Draw.io Pod (ARM64 Karpenter节点)
            ↓
        Amazon Bedrock Claude API (us-west-2)
```

### 关键组件说明：
• **CloudFront** - 全球CDN，SSL终端，DDoS保护  
• **VPC Origin** - 允许CloudFront访问内网ALB的关键组件  
• **Internal ALB** - 内网负载均衡器，不直接暴露公网  
• **ARM64 Pod** - 运行在Karpenter管理的Graviton节点上  
• **Bedrock API** - AI模型调用，位于us-west-2区域  

## 📁 部署文件结构

```
applications/auto-draw-io/
├── auto-draw-io-deployment-guide.md     # 本部署指南
├── auto-draw-io-configmap.yaml          # 非敏感环境变量配置
├── auto-draw-io-secret.yaml             # 敏感信息配置 (AWS 凭据)
└── auto-draw-io-deployment.yaml         # 应用部署 + Service + Ingress
```

## 🎯 前提条件

### 集群相关
- EKS 集群运行正常 (v1.33+)
- kubectl 已配置
- AWS Load Balancer Controller 已安装 (支持 IRSA)
- Karpenter ARM64 节点池可用

### AWS 服务准备
- **Amazon Bedrock** - 已启用 Claude 模型访问权限
- **IAM 用户** - 具有 Bedrock 调用权限的 Access Key
- **Route53** - 托管域名 (公网访问需要)
- **ACM 证书** - us-east-1 区域的通配符证书 (CloudFront 需要)

## 🔧 配置准备

### 1. AWS Bedrock 权限配置

确保 IAM 用户具有以下权限：
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream"
            ],
            "Resource": "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-*"
        }
    ]
}
```

### 2. 环境变量配置

编辑 `auto-draw-io-configmap.yaml`：
```yaml
data:
  AI_PROVIDER: "bedrock"
  AI_MODEL: "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
  AWS_REGION: "us-west-2"  # 根据 Bedrock 可用区域调整
  TEMPERATURE: "0"
```

### 3. 敏感信息配置

编辑 `auto-draw-io-secret.yaml`：
```yaml
stringData:
  # ⚠️ 替换为实际的 AWS 凭据
  AWS_ACCESS_KEY_ID: "YOUR_ACCESS_KEY_ID"
  AWS_SECRET_ACCESS_KEY: "YOUR_SECRET_ACCESS_KEY"
  
  # ⚠️ 替换为自定义访问码
  ACCESS_CODE_LIST: "your-custom-access-code"
```

**⚠️ 安全提醒**：
- 使用 `stringData` 而非 `data` 避免手动 base64 编码错误
- 不要将包含真实凭据的文件提交到版本控制
- 定期轮换 Access Key

## 🚀 部署步骤

### 步骤 1: 部署配置
```bash
# 创建 ConfigMap
kubectl apply -f auto-draw-io-configmap.yaml

# 创建 Secret
kubectl apply -f auto-draw-io-secret.yaml
```

### 步骤 2: 部署应用
```bash
# 部署应用 (Deployment + Service + Internal Ingress)
kubectl apply -f auto-draw-io-deployment.yaml
```

### 步骤 3: 验证内网部署
```bash
# 检查 Pod 状态
kubectl get pods -n hostwo -l app=auto-draw-io

# 检查 Internal ALB
kubectl get ingress auto-draw-io-ingress -n hostwo

# 内网测试访问
kubectl run test-pod --image=curlimages/curl:latest --rm -i --restart=Never -- \
  curl -s -o /dev/null -w "%{http_code}" http://INTERNAL-ALB-ADDRESS
```

## 🌐 公网访问配置

### 步骤 4: 创建 VPC Origin

```bash
# 获取 Internal ALB ARN
ALB_ARN=$(aws elbv2 describe-load-balancers \
  --query "LoadBalancers[?DNSName=='INTERNAL-ALB-DNS-NAME'].LoadBalancerArn" \
  --output text)

# 创建 VPC Origin
aws cloudfront create-vpc-origin \
  --vpc-origin-endpoint-config Name=auto-draw-io-alb,Arn=$ALB_ARN,HTTPPort=80,HTTPSPort=443,OriginProtocolPolicy=http-only \
  --tags Items='[{Key=Application,Value=auto-draw-io}]'
```

### 步骤 5: 创建 CloudFront 分发

1. **Origin 配置** - 使用 `VpcOriginConfig` 而非 `CustomOriginConfig`：
```json
{
  "DomainName": "internal-k8s-hostwo-autodraw-xxx.elb.amazonaws.com",
  "VpcOriginConfig": {
    "VpcOriginId": "vo_xxxxxxxxxxxxx",
    "OriginReadTimeout": 30,
    "OriginKeepaliveTimeout": 5
  }
}
```

2. **证书配置** - 使用 us-east-1 区域的证书：
```json
{
  "ViewerCertificate": {
    "ACMCertificateArn": "arn:aws:acm:us-east-1:ACCOUNT:certificate/CERT-ID",
    "SSLSupportMethod": "sni-only",
    "MinimumProtocolVersion": "TLSv1.2_2021"
  }
}
```

3. **缓存行为** - 支持所有 HTTP 方法：
```json
{
  "AllowedMethods": {
    "Quantity": 7,
    "Items": ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
  },
  "ForwardedValues": {
    "QueryString": true,
    "Cookies": {"Forward": "all"},
    "Headers": {"Quantity": 1, "Items": ["*"]}
  }
}
```

### 步骤 6: 配置 DNS 记录

```bash
# 创建 CNAME 记录指向 CloudFront
aws route53 change-resource-record-sets \
  --hosted-zone-id YOUR-HOSTED-ZONE-ID \
  --change-batch '{
    "Changes": [{
      "Action": "CREATE",
      "ResourceRecordSet": {
        "Name": "autodraw.yourdomain.com",
        "Type": "CNAME",
        "TTL": 300,
        "ResourceRecords": [{"Value": "dxxxxx.cloudfront.net"}]
      }
    }]
  }'
```

## 📝 访问方式

### 内网访问
- **ALB 地址**: `http://internal-k8s-hostwo-autodraw-xxx.elb.amazonaws.com`
- **集群内**: `http://auto-draw-io-service.hostwo.svc.cluster.local`

### 公网访问
- **自定义域名**: `https://autodraw.yourdomain.com`
- **CloudFront**: `https://dxxxxx.cloudfront.net`

## 🔧 管理操作

### 查看应用日志
```bash
kubectl logs -n hostwo deployment/auto-draw-io -f
```

### 重启服务
```bash
kubectl rollout restart deployment/auto-draw-io -n hostwo
```

### 更新配置
```bash
# 更新 ConfigMap 后重启
kubectl apply -f auto-draw-io-configmap.yaml
kubectl rollout restart deployment/auto-draw-io -n hostwo

# 更新 Secret 后重启
kubectl apply -f auto-draw-io-secret.yaml
kubectl rollout restart deployment/auto-draw-io -n hostwo
```

### 扩缩容
```bash
kubectl scale deployment auto-draw-io --replicas=2 -n hostwo
```

## 🐛 故障排除

### 常见问题

#### 1. Bedrock 权限错误
```
The request signature we calculated does not match the signature you provided
```

**解决方案**：
- 检查 AWS 凭据是否正确 (无多余空格)
- 确认 IAM 用户有 Bedrock 权限

#### 2. CloudFront 502 错误
```
CloudFront wasn't able to resolve the origin domain name
```

**解决方案**：
- 确认使用 `VpcOriginConfig` 而非 `CustomOriginConfig`
- 检查 VPC Origin 状态是否为 `Deployed`
- 验证 Internal ALB 可正常访问

#### 3. Pod 启动失败
```bash
# 检查 Pod 状态
kubectl describe pod -n hostwo [pod-name]

# 检查配置
kubectl get configmap auto-draw-io-config -n hostwo -o yaml
kubectl get secret auto-draw-io-secret -n hostwo -o yaml
```

### 调试命令
```bash
# 进入容器调试
kubectl exec -it -n hostwo deployment/auto-draw-io -- /bin/sh

# 测试内网连接
kubectl run test-curl --image=curlimages/curl:latest --rm -it --restart=Never -- \
  curl -I http://auto-draw-io-service.hostwo.svc.cluster.local

# 检查环境变量
kubectl exec -n hostwo deployment/auto-draw-io -- env | grep -E "AWS_|AI_"
```
