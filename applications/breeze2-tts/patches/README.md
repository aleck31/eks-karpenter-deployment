# 上游补丁

针对 `breezeblue-ai/breeze-tts` 的构建期补丁。基础镜像构建时 `git clone` 上游源码，
补丁在 `Dockerfile.base` 中应用。

选补丁脚本而非 vendoring 整个文件：只锁定真正需要改的几段，其余跟随上游演进。
每个脚本在上游源码不再匹配时**拒绝应用并使构建失败**，而不是静默跳过 ——
vendoring 的失效是无声的。

| 脚本 | 修什么 | 上游状态 |
|------|--------|----------|
| `release-inference-lock-on-disconnect.py` | 客户端断开时推理锁不释放，服务只能重启恢复 | [issue #20](https://github.com/breezeblue-ai/breeze-tts/issues/20) |

验证已应用：

```bash
python3 patches/<script>.py --check --path <目标文件>
```
