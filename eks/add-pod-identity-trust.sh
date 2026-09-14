#!/usr/bin/env bash
# 为现有 IRSA 角色追加 Pod Identity 信任主体（幂等）。
#
# 保留原 OIDC 语句，使迁移可回退：Pod Identity 生效后 OIDC 语句不再被使用，
# 验证稳定后可用 --remove-oidc 清理。
#
# 用法：
#   ./add-pod-identity-trust.sh <role-name> [<role-name> ...]
#   ./add-pod-identity-trust.sh --remove-oidc <role-name> [...]
#
# 依赖：AWS_PROFILE 或 --profile 已配置，jq 或 python3
set -euo pipefail

REMOVE_OIDC=false
if [[ "${1:-}" == "--remove-oidc" ]]; then
  REMOVE_OIDC=true
  shift
fi

[[ $# -eq 0 ]] && { echo "用法: $0 [--remove-oidc] <role-name> [...]" >&2; exit 1; }

for ROLE in "$@"; do
  echo "== ${ROLE}"
  aws iam get-role --role-name "$ROLE" --query 'Role.AssumeRolePolicyDocument' > "/tmp/trust-${ROLE}.json"

  # set -e 下需显式捕获退出码，否则「已存在则跳过」的 exit 3 会终止整个脚本
  rc=0
  python3 - "$ROLE" "$REMOVE_OIDC" <<'PY' || rc=$?
import json, sys, pathlib
role, remove_oidc = sys.argv[1], sys.argv[2] == 'true'
p = pathlib.Path(f'/tmp/trust-{role}.json')
d = json.loads(p.read_text())
stmts = d['Statement']

def is_pi(s):  return s.get('Principal', {}).get('Service') == 'pods.eks.amazonaws.com'
def is_oidc(s): return 'Federated' in s.get('Principal', {})

if remove_oidc:
    before = len(stmts)
    stmts = [s for s in stmts if not is_oidc(s)]
    if not any(is_pi(s) for s in stmts):
        print('  跳过：无 Pod Identity 语句，移除 OIDC 会导致角色不可用'); sys.exit(1)
    print(f'  移除 OIDC 语句 {before - len(stmts)} 条')
else:
    if any(is_pi(s) for s in stmts):
        print('  已存在 Pod Identity 语句，跳过'); sys.exit(3)
    stmts.append({
        "Effect": "Allow",
        "Principal": {"Service": "pods.eks.amazonaws.com"},
        "Action": ["sts:AssumeRole", "sts:TagSession"],
    })
    print('  追加 Pod Identity 语句')

d['Statement'] = stmts
p.write_text(json.dumps(d, indent=2))
PY

  if [[ $rc -eq 3 ]]; then continue; fi
  [[ $rc -ne 0 ]] && { echo "  处理失败" >&2; exit $rc; }

  aws iam update-assume-role-policy --role-name "$ROLE" \
    --policy-document "file:///tmp/trust-${ROLE}.json"
  echo "  已更新"
done
