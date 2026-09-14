#!/usr/bin/env bash
# 审计集群对外暴露面：列出所有负载均衡器及其全部安全组的入站来源。
#
# 必须遍历负载均衡器的全部安全组，不能只取第一个。ALB Controller 会同时挂载
# 前端托管安全组与共享后端安全组，两者规则取并集；只看首个会漏掉真正放开的那条。
# 曾因此误判 Portainer 未对外暴露，实际其托管安全组放开了 0.0.0.0/0。
#
# 用法：
#   ./audit-exposure.sh <cluster-name> <region>
set -euo pipefail

[[ $# -lt 2 ]] && { echo "用法: $0 <cluster-name> <region>" >&2; exit 1; }
CLUSTER=$1
REGION=$2

echo "== 集群 ${CLUSTER} (${REGION})"

echo "-- EKS API 端点"
aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
  --query 'cluster.resourcesVpcConfig.[endpointPublicAccess,publicAccessCidrs,endpointPrivateAccess]' \
  --output json | python3 -c "
import sys, json
pub, cidrs, priv = json.load(sys.stdin)
flag = '公网开放' if pub and '0.0.0.0/0' in (cidrs or []) else 'OK'
print(f'   public={pub} cidrs={cidrs} private={priv}  [{flag}]')"

echo "-- 负载均衡器"
aws elbv2 describe-load-balancers --region "$REGION" \
  --query 'LoadBalancers[].[LoadBalancerName,Scheme,Type,DNSName]' --output text |
while read -r name scheme type dns; do
  # 仅审计属于该集群的负载均衡器
  arn=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$name" \
        --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null) || continue
  owner=$(aws elbv2 describe-tags --resource-arns "$arn" --region "$REGION" \
          --query "TagDescriptions[0].Tags[?Key=='elbv2.k8s.aws/cluster'].Value" --output text 2>/dev/null)
  [[ "$owner" != "$CLUSTER" ]] && continue

  echo "   [${name}] ${scheme} ${type}"
  echo "     ${dns}"

  sgs=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$name" \
        --query 'LoadBalancers[0].SecurityGroups' --output text 2>/dev/null)
  [[ -z "$sgs" || "$sgs" == "None" ]] && { echo "     (无安全组, NLB 由 NACL/目标组控制)"; continue; }

  for sg in $sgs; do
    sgname=$(aws ec2 describe-security-groups --group-ids "$sg" --region "$REGION" \
             --query 'SecurityGroups[0].GroupName' --output text 2>/dev/null)
    echo "     ${sg} ${sgname}"
    aws ec2 describe-security-groups --group-ids "$sg" --region "$REGION" \
      --query 'SecurityGroups[0].IpPermissions' --output json 2>/dev/null | python3 -c "
import sys, json
perms = json.load(sys.stdin)
if not perms:
    print('       (无入站规则)')
for p in perms:
    cidr = [r['CidrIp'] for r in p.get('IpRanges', [])]
    v6 = [r['CidrIpv6'] for r in p.get('Ipv6Ranges', [])]
    pl = [x['PrefixListId'] for x in p.get('PrefixListIds', [])]
    sgs = [g['GroupId'] for g in p.get('UserIdGroupPairs', [])]
    open_all = '0.0.0.0/0' in cidr or '::/0' in v6
    port = f\"{p.get('FromPort', 'all')}-{p.get('ToPort', 'all')}\"
    src = cidr + v6 + pl + sgs
    print(f\"       {'!! 全网开放' if open_all else 'OK'} {port}/{p['IpProtocol']} 来源={src}\")"
  done
done

echo "-- Ingress / Service 暴露方式"
CTX=$(kubectl config get-contexts -o name | grep -E "${CLUSTER}\$" | head -1)
if [[ -n "$CTX" ]]; then
  kubectl --context "$CTX" get ingress -A -o json 2>/dev/null | python3 -c "
import sys, json
for i in json.load(sys.stdin).get('items', []):
    a = i['metadata'].get('annotations', {})
    scheme = a.get('alb.ingress.kubernetes.io/scheme', '(默认 internal)')
    cidrs = a.get('alb.ingress.kubernetes.io/inbound-cidrs')
    pls = a.get('alb.ingress.kubernetes.io/security-group-prefix-lists')
    limit = cidrs or pls or '未限制(默认 0.0.0.0/0)'
    flag = '!!' if scheme == 'internet-facing' and not (cidrs or pls) else 'OK'
    print(f\"   {flag} {i['metadata']['namespace']}/{i['metadata']['name']} scheme={scheme} 来源={limit}\")"
  kubectl --context "$CTX" get svc -A --no-headers 2>/dev/null |
    awk '$3=="LoadBalancer"||$3=="NodePort"{printf "   %s %s/%s\n",$3,$1,$2}'
else
  echo "   (未找到 kubectl context, 跳过)"
fi
