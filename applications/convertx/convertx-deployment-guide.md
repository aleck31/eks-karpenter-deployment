# ConvertX 部署指南 (EKS)

## 项目概述

ConvertX 是一个自托管的在线文件转换器，支持 1000+ 种文件格式转换。基于 TypeScript、Bun 和 Elysia 框架构建。

### 主要特性
- 支持 1000+ 文件格式转换
- 批量文件处理
- 密码保护和多用户支持
- 自托管，完全控制数据
- 现代化 Web 界面
- 自动清理临时文件（默认24小时）

### 支持的转换器
- **FFmpeg**: 视频/音频 (~472 输入格式, ~199 输出格式)
- **ImageMagick**: 图像 (245 输入格式, 183 输出格式)
- **Pandoc**: 文档 (43 输入格式, 65 输出格式)
- **Calibre**: 电子书 (26 输入格式, 19 输出格式)
- **Inkscape**: 矢量图像 (7 输入格式, 17 输出格式)
- 以及更多专业转换工具...

## 部署架构

```
Internet → CloudFront → Internal ALB → ConvertX Pod (ARM64)
                                    ↓
                                  EBS 存储（gp3）
```

## 部署步骤

### 1. 创建命名空间
```bash
kubectl create namespace convertx
```

### 2. 部署存储和配置
```bash
kubectl apply -f convertx-ebs-pvc.yaml
kubectl apply -f convertx-secret.yaml
kubectl apply -f convertx-configmap.yaml
```

### 3. 部署 ConfigMap
```bash
kubectl apply -f convertx-configmap.yaml
```

### 4. 部署应用
```bash
kubectl apply -f convertx-deployment.yaml
```

### 5. 验证部署
```bash
# 检查 Pod 状态
kubectl get pods -n convertx

# 检查服务状态
kubectl get svc -n convertx

# 查看应用日志
kubectl logs -n convertx deployment/convertx
```

## 配置说明

### 环境变量配置
- `JWT_SECRET`: JWT 签名密钥（必须设置）
- `ACCOUNT_REGISTRATION`: 是否允许用户注册（默认 false）
- `HTTP_ALLOWED`: 是否允许 HTTP 连接（仅本地开发）
- `AUTO_DELETE_EVERY_N_HOURS`: 自动删除文件间隔（默认 24 小时）
- `TZ`: 时区设置（默认 UTC）

### 资源配置
- **CPU**: 500m-2000m（转换任务需要较多 CPU）
- **内存**: 1Gi-4Gi（处理大文件需要更多内存）
- **存储**: GP3 EBS 存储 (30GB, 000 IOPS, 125 MiB/s 吞吐量)

## 访问方式

### 内部访问
```bash
# 端口转发测试
kubectl port-forward -n convertx svc/convertx 3000:3000
```
然后访问 http://localhost:3000

### 外部访问
通过 CloudFront 分发访问：https://your-domain.com

## 监控和维护

### 查看资源使用情况
```bash
kubectl top pods -n convertx
```

### 查看存储使用情况
```bash
kubectl exec -n convertx deployment/convertx -- df -h /app/data
```

### 重启应用
```bash
kubectl rollout restart deployment/convertx -n convertx
```

## 故障排除

### 常见问题

1. **Pod 启动失败**
   ```bash
   kubectl describe pod -n convertx <pod-name>
   kubectl logs -n convertx <pod-name>
   ```

2. **存储权限问题**
   - 检查 EFS 挂载权限
   - 确认 SecurityContext 配置正确

3. **转换任务失败**
   - 检查文件格式支持
   - 查看应用日志中的错误信息
   - 确认资源限制是否足够

4. **访问问题**
   - 检查 Ingress 配置
   - 确认 CloudFront 设置
   - 验证 JWT_SECRET 配置

### 性能优化

1. **CPU 密集型任务**
   - 增加 CPU 资源限制
   - 考虑使用 GPU 加速（如支持）

2. **内存优化**
   - 根据处理文件大小调整内存限制
   - 监控内存使用情况

3. **存储优化**
   - 定期清理临时文件
   - 调整自动删除间隔

## 安全注意事项

1. **JWT 密钥安全**
   - 使用强随机密钥
   - 定期轮换密钥

2. **网络安全**
   - 仅通过 HTTPS 访问
   - 配置适当的网络策略

3. **文件安全**
   - 限制上传文件大小
   - 扫描恶意文件

## 备份和恢复

### 数据备份
```bash
# 备份用户数据和配置
kubectl exec -n convertx deployment/convertx -- tar -czf /tmp/backup.tar.gz /app/data
kubectl cp convertx/<pod-name>:/tmp/backup.tar.gz ./convertx-backup-$(date +%Y%m%d).tar.gz
```

### 数据恢复
```bash
# 恢复数据
kubectl cp ./convertx-backup.tar.gz convertx/<pod-name>:/tmp/
kubectl exec -n convertx deployment/convertx -- tar -xzf /tmp/backup.tar.gz -C /
```

## 更新升级

### 应用更新
```bash
# 更新到最新版本
kubectl set image deployment/convertx -n convertx convertx=ghcr.io/c4illin/convertx:latest
kubectl rollout status deployment/convertx -n convertx
```

### 回滚版本
```bash
kubectl rollout undo deployment/convertx -n convertx
```
