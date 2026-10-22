#!/usr/bin/env python3
"""picasso 新增：在本机按 CI（blacktop/darwin-xnu-build）的规则，检查内核补丁能否全部应用到 xnu-8796.141.3。

为什么需要它：darwin-xnu-build 对 `git apply --check` 不通过的补丁只打一行警告就跳过，构建还会继续，
结果是内核缺了某个补丁却看起来“编译成功”。改动板级常量后，必须先确认 100% 的补丁都能应用。

模拟 CI 的顺序：
  1. 复制一份干净的 xnu 源码（不改动原件）。
  2. 在源码根目录依次运行 patches 目录里的 *.sh（下载并放入 libpthread、BearSSL、HFS 等源码），
     环境变量与 CI 相同：MACHINE_CONFIG=SWEET、MACOS_VERSION=13.5。
  3. 按文件名顺序合并“构建脚本自带补丁”和本仓库 xnu/patches/*.patch，逐个 git apply --check 后应用。

用法：
  tools/check-patch-series.py --xnu 干净的xnu目录 --dxb darwin-xnu-build目录 [--keep 输出目录]
任何一个补丁被跳过，退出码为 1。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--xnu', type=Path, required=True, help='干净的 xnu-8796.141.3 检出目录（不会被修改）')
    parser.add_argument('--dxb', type=Path, required=True, help='blacktop/darwin-xnu-build 固定提交的检出目录')
    parser.add_argument('--keep', type=Path, help='把打完补丁的源码树保留在这个目录（用于后续单文件编译检查）')
    args = parser.parse_args()
    # 补丁要在源码副本目录里应用，路径必须是绝对路径
    args.xnu, args.dxb = args.xnu.resolve(), args.dxb.resolve()
    if args.keep:
        args.keep = args.keep.resolve()

    ours = sorted((ROOT / 'xnu/patches').glob('*'))
    theirs = sorted(p for p in (args.dxb / 'patches').iterdir() if p.is_file() and p.suffix in ('.patch', '.sh'))
    # CI 把我们的补丁复制进构建脚本的 patches 目录，两者混在一起按文件名排序
    merged = {p.name: p for p in theirs}
    merged.update({p.name: p for p in ours if p.suffix in ('.patch', '.sh')})
    scripts = [merged[n] for n in sorted(merged) if n.endswith('.sh')]
    patches = [merged[n] for n in sorted(merged) if n.endswith('.patch')]

    work = args.keep or Path(tempfile.mkdtemp(prefix='xnu-check-'))
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(args.xnu, work, symlinks=True)
    env = dict(os.environ, MACHINE_CONFIG='SWEET', MACOS_VERSION='13.5', KDKROOT='/nonexistent')
    print(f'源码副本：{work}')
    for script in scripts:
        result = subprocess.run(['bash', str(script)], cwd=work, env=env, capture_output=True, text=True)
        status = '完成' if result.returncode == 0 else f'失败（{result.returncode}）'
        print(f'  脚本 {script.name}：{status}')
        if result.returncode:
            print(result.stdout[-2000:] + result.stderr[-2000:])
            sys.exit(1)
    skipped = []
    for patch in patches:
        check = subprocess.run(['git', 'apply', '--check', str(patch)], cwd=work, capture_output=True, text=True)
        if check.returncode:
            skipped.append((patch.name, check.stderr.strip().splitlines()[:3]))
            continue
        subprocess.run(['git', 'apply', str(patch)], cwd=work, check=True)
    own = {p.name for p in ours}
    own_skipped = [(n, w) for n, w in skipped if n in own]
    applied = len(patches) - len(skipped)
    print(f'补丁：共 {len(patches)} 个，应用 {applied} 个，跳过 {len(skipped)} 个')
    print(f'  其中本仓库补丁 {sum(1 for p in patches if p.name in own)} 个，跳过 {len(own_skipped)} 个')
    for name, why in skipped:
        tag = '【本仓库补丁，必须修复】' if name in own else '（构建脚本自带、针对其他 macOS 版本的补丁，CI 里同样会跳过）'
        print(f'  跳过 {name}{tag}：' + ' | '.join(why[:1]))
    if not args.keep:
        shutil.rmtree(work)
    sys.exit(1 if own_skipped else 0)


if __name__ == '__main__':
    main()
