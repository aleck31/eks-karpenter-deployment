#!/usr/bin/env bash
# 从 karpenter-policy.json 模板渲染并更新指定集群的 Karpenter 控制器策略。
#
# 模板按集群参数化，两个集群共用同一份来源，避免各自漂移。
# 策略作用域按 Karpenter 官方推荐收紧：EC2 操作限定 region + cluster tag，
# PassRole 限定该集群的节点角色，SQS 限定该集群的中断队列。
#
# 更新策略前会与线上现行版本逐项比对（action + Resource + Condition）并要求确认，
# 因为仅比对 action 名称会漏掉作用域变化。
#
# 用法：
#   ./apply-karpenter-policy.sh <cluster-name> <region> <policy-name> [--yes]
# 示例：
#   ./apply-karpenter-policy.sh eks-karpenter-env ap-southeast-1 KarpenterControllerPolicy
set -euo pipefail

[[ $# -lt 3 ]] && { echo "用法: $0 <cluster-name> <region> <policy-name> [--yes]" >&2; exit 1; }

CLUSTER_NAME=$1
AWS_REGION=$2
POLICY_NAME=$3
ASSUME_YES=${4:-}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 节点角色与中断队列名各集群可能不同，从实际环境探测而非假定
KARPENTER_NODE_ROLE=$(aws iam list-roles \
  --query "Roles[?starts_with(RoleName,'KarpenterNode') && contains(RoleName,'${CLUSTER_NAME}')].RoleName" \
  --output text | head -1)
[[ -z "$KARPENTER_NODE_ROLE" ]] && { echo "未找到 ${CLUSTER_NAME} 的 Karpenter 节点角色" >&2; exit 1; }

INTERRUPTION_QUEUE=$(aws sqs list-queues --region "$AWS_REGION" \
  --query "QueueUrls[?contains(@,'${CLUSTER_NAME}')]" --output text 2>/dev/null | head -1 | sed 's|.*/||')
[[ -z "$INTERRUPTION_QUEUE" ]] && INTERRUPTION_QUEUE="$CLUSTER_NAME"

echo "集群=${CLUSTER_NAME} 区域=${AWS_REGION}"
echo "  节点角色=${KARPENTER_NODE_ROLE}"
echo "  中断队列=${INTERRUPTION_QUEUE}"

export AWS_ACCOUNT_ID AWS_REGION CLUSTER_NAME KARPENTER_NODE_ROLE INTERRUPTION_QUEUE
RENDERED=$(mktemp /tmp/karpenter-policy-XXXX.json)
envsubst < "${SCRIPT_DIR}/karpenter-policy.json" > "$RENDERED"
python3 -c "import json,sys; json.load(open('$RENDERED'))" || { echo "渲染结果非合法 JSON" >&2; exit 1; }

POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${POLICY_NAME}"
if ! aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  echo "策略不存在，创建中"
  aws iam create-policy --policy-name "$POLICY_NAME" --policy-document "file://${RENDERED}" \
    --query 'Policy.Arn' --output text
  exit 0
fi

# 与线上现行版本比对作用域变化
CUR=$(aws iam get-policy --policy-arn "$POLICY_ARN" --query 'Policy.DefaultVersionId' --output text)
aws iam get-policy-version --policy-arn "$POLICY_ARN" --version-id "$CUR" \
  --query 'PolicyVersion.Document' > /tmp/karpenter-policy-current.json

python3 - "$RENDERED" <<'PY'
import json, sys
def norm(f):
    d = json.load(open(f)); s = set()
    for st in d['Statement']:
        a = st.get('Action', []); a = [a] if isinstance(a, str) else a
        r = st.get('Resource', '*')
        r = json.dumps(sorted(r) if isinstance(r, list) else r, sort_keys=True)
        c = json.dumps(st.get('Condition'), sort_keys=True)
        for x in a:
            s.add((x, r, c))
    return s
cur, new = norm('/tmp/karpenter-policy-current.json'), norm(sys.argv[1])
lost, added = sorted(cur - new), sorted(new - cur)
print(f'  现行 {len(cur)} 项 → 新版 {len(new)} 项')
if lost:
    print(f'  收紧/移除 {len(lost)} 项:')
    for a, r, c in lost[:15]:
        print(f'    - {a}  Resource={r[:60]}')
if added:
    print(f'  新增/放宽 {len(added)} 项:')
    for a, r, c in added[:15]:
        print(f'    + {a}  Resource={r[:60]}' + ('  [带Condition]' if c != 'null' else ''))
if not lost and not added:
    print('  无变化')
PY

if [[ "$ASSUME_YES" != "--yes" ]]; then
  read -rp "  确认应用？(yes/no) " ans
  [[ "$ans" == "yes" ]] || { echo "  已取消"; exit 0; }
fi

# IAM 策略最多 5 个版本，先清理非默认版本
for v in $(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
           --query 'Versions[?!IsDefaultVersion].VersionId' --output text); do
  aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$v"
done

aws iam create-policy-version --policy-arn "$POLICY_ARN" \
  --policy-document "file://${RENDERED}" --set-as-default \
  --query 'PolicyVersion.VersionId' --output text | sed 's/^/  新版本: /'
rm -f "$RENDERED"
