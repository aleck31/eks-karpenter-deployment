#!/usr/bin/env bash
# 用托管前缀列表的条目同步 EKS API Server 的 publicAccessCIDRs。
#
# publicAccessCIDRs 只接受 CIDR 字面量，无法引用前缀列表 ID，因此需要展开同步。
# 前缀列表若由外部工具动态维护，两侧会漂移，本脚本可重复执行以对齐。
#
# 前置条件：集群必须已开启 privateAccess。privateAccess 为 false 时节点经 NAT
# 走公网端点连接 API Server，收窄白名单若未包含 NAT 出口 IP 会导致全部节点失联。
# 脚本会检查该前提并在不满足时拒绝执行。
#
# 用法：
#   ./sync-endpoint-cidrs.sh <cluster-name> <region> <prefix-list-id> [--yes]
set -euo pipefail

[[ $# -lt 3 ]] && { echo "用法: $0 <cluster-name> <region> <prefix-list-id> [--yes]" >&2; exit 1; }
CLUSTER=$1
REGION=$2
PREFIX_LIST=$3
ASSUME_YES=${4:-}

MAX_CIDRS=40   # EKS 配额：Public endpoint access CIDR ranges per cluster

read -r PUB PRIV CUR <<<"$(aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
  --query 'cluster.resourcesVpcConfig.[endpointPublicAccess,endpointPrivateAccess,join(`,`,publicAccessCidrs)]' \
  --output text)"

echo "集群 ${CLUSTER} (${REGION})"
echo "  publicAccess=${PUB} privateAccess=${PRIV}"
echo "  当前 CIDR: ${CUR}"

if [[ "$PRIV" != "True" && "$PRIV" != "true" ]]; then
  echo "  拒绝执行：privateAccess 未开启。" >&2
  echo "  节点当前经 NAT 走公网端点，收窄白名单会导致节点失联。" >&2
  echo "  先执行：eksctl utils update-cluster-vpc-config -f <cluster-config> --approve" >&2
  exit 1
fi

mapfile -t CIDRS < <(aws ec2 get-managed-prefix-list-entries \
  --prefix-list-id "$PREFIX_LIST" --region "$REGION" \
  --query 'Entries[].Cidr' --output text | tr '\t' '\n' | sort -u)

[[ ${#CIDRS[@]} -eq 0 ]] && { echo "  前缀列表为空或不可读" >&2; exit 1; }
if [[ ${#CIDRS[@]} -gt $MAX_CIDRS ]]; then
  echo "  前缀列表有 ${#CIDRS[@]} 条，超出 EKS 上限 ${MAX_CIDRS}" >&2
  exit 1
fi

echo "  前缀列表 ${PREFIX_LIST} 共 ${#CIDRS[@]} 条"

# 校验运维出口是否被覆盖，避免把自己锁在外面
MYIP=$(curl -s --max-time 8 https://checkip.amazonaws.com || true)
if [[ -n "$MYIP" ]]; then
  if python3 -c "
import ipaddress, sys
ip = ipaddress.ip_address('${MYIP}'.strip())
nets = [ipaddress.ip_network(c) for c in '''${CIDRS[*]}'''.split()]
sys.exit(0 if any(ip in n for n in nets) else 1)"; then
    echo "  本机出口 ${MYIP} 在列表内"
  else
    echo "  警告：本机出口 ${MYIP} 不在列表内，应用后将无法从此处访问 API Server" >&2
  fi
fi

JOINED=$(IFS=,; echo "${CIDRS[*]}")
if [[ "$JOINED" == "$CUR" ]]; then
  echo "  已一致，无需更新"
  exit 0
fi

if [[ "$ASSUME_YES" != "--yes" ]]; then
  read -rp "  应用以上 ${#CIDRS[@]} 条 CIDR？(yes/no) " ans
  [[ "$ans" == "yes" ]] || { echo "  已取消"; exit 0; }
fi

UPDATE_ID=$(aws eks update-cluster-config --name "$CLUSTER" --region "$REGION" \
  --resources-vpc-config "publicAccessCidrs=${JOINED}" \
  --query 'update.id' --output text)
echo "  update id: ${UPDATE_ID}"

# 轮询 update 状态而非集群状态：EndpointAccessUpdate 提交后集群可能仍显示 ACTIVE，
# 据此判断会过早认为已完成。
echo "  等待生效"
for _ in $(seq 1 40); do
  s=$(aws eks describe-update --name "$CLUSTER" --update-id "$UPDATE_ID" --region "$REGION" \
      --query 'update.status' --output text)
  [[ "$s" != "InProgress" ]] && break
  sleep 15
done
echo "  update 状态: ${s}"
[[ "$s" != "Successful" ]] && {
  aws eks describe-update --name "$CLUSTER" --update-id "$UPDATE_ID" --region "$REGION" \
    --query 'update.errors' --output json >&2
  exit 1
}
aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
  --query 'length(resourcesVpcConfig.publicAccessCidrs)' --output text 2>/dev/null | sed 's/^/  生效 CIDR 条数: /'
