#!/usr/bin/env python3
"""比对两个 IAM 角色的有效权限是否一致。

展开角色上的全部托管策略与内联策略，把每条语句归一化为
(Effect, Action, Resource, Condition) 元组集合后比对。

仅比对 action 名称是不够的：Resource 与 Condition 决定实际作用域，
曾因忽略这两项导致 iam:PassRole 作用域被静默改写、Karpenter 无法启动实例。

用法：
  ./compare-role-permissions.py <role-A> <role-B> [--profile me]

退出码：0 权限一致，1 存在差异
"""
import json
import subprocess
import sys


def aws(args, profile):
    cmd = ['aws'] + args + (['--profile', profile] if profile else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'{" ".join(cmd)}\n{r.stderr.strip()}')
    return json.loads(r.stdout) if r.stdout.strip() else None


def aws_text(args, profile):
    cmd = ['aws'] + args + ['--output', 'text'] + (['--profile', profile] if profile else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'{" ".join(cmd)}\n{r.stderr.strip()}')
    return r.stdout.strip()


def statements_of_role(role, profile):
    """返回角色的全部语句，来源含托管策略与内联策略。"""
    out = []
    attached = aws(['iam', 'list-attached-role-policies', '--role-name', role,
                    '--query', 'AttachedPolicies[].PolicyArn'], profile) or []
    for arn in attached:
        ver = aws_text(['iam', 'get-policy', '--policy-arn', arn,
                        '--query', 'Policy.DefaultVersionId'], profile)
        doc = aws(['iam', 'get-policy-version', '--policy-arn', arn, '--version-id', ver,
                   '--query', 'PolicyVersion.Document'], profile)
        out += doc['Statement']

    inline = aws(['iam', 'list-role-policies', '--role-name', role,
                  '--query', 'PolicyNames'], profile) or []
    for name in inline:
        doc = aws(['iam', 'get-role-policy', '--role-name', role, '--policy-name', name,
                   '--query', 'PolicyDocument'], profile)
        out += doc['Statement']
    return out


def normalize(statements):
    """展开为 (effect, action, resource, condition) 集合，便于精确比对。"""
    s = set()
    for st in statements:
        eff = st.get('Effect', 'Allow')
        acts = st.get('Action', st.get('NotAction', []))
        acts = [acts] if isinstance(acts, str) else acts
        res = st.get('Resource', st.get('NotResource', '*'))
        res = json.dumps(sorted(res) if isinstance(res, list) else res, sort_keys=True)
        cond = json.dumps(st.get('Condition'), sort_keys=True)
        for a in acts:
            s.add((eff, a, res, cond))
    return s


def main():
    argv = sys.argv[1:]
    profile = None
    if '--profile' in argv:
        i = argv.index('--profile')
        profile = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith('--')]
    if len(args) != 2:
        print(__doc__)
        return 2

    a, b = args
    sa, sb = normalize(statements_of_role(a, profile)), normalize(statements_of_role(b, profile))

    only_a, only_b = sorted(sa - sb), sorted(sb - sa)
    print(f'  A = {a}')
    print(f'  B = {b}')
    print(f'  语句项数: A={len(sa)}  B={len(sb)}')

    if not only_a and not only_b:
        print('  权限完全一致 ✅')
        return 0

    if only_a:
        print(f'  仅 A 有（切到 B 会丢失）: {len(only_a)} 项')
        for e, act, r, c in only_a[:12]:
            print(f'    {act}  Resource={r[:70]}' + (f'  Cond={c[:60]}' if c != 'null' else ''))
    if only_b:
        print(f'  仅 B 有（切到 B 会新增）: {len(only_b)} 项')
        for e, act, r, c in only_b[:12]:
            print(f'    {act}  Resource={r[:70]}' + (f'  Cond={c[:60]}' if c != 'null' else ''))
    return 1


if __name__ == '__main__':
    sys.exit(main())
