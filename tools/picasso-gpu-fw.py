#!/usr/bin/env python3
"""从 picasso 原厂 vendor.img（ext4）里取出 Adreno 620 要用的固件。

  picasso-gpu-fw.py VENDOR.IMG [OUTDIR]     （OUTDIR 默认 research/picasso-gpu-fw）

Linux（drivers/gpu/drm/msm/adreno/a6xx_catalog.c 的 A620）用的是：
  a650_sqe.fw          命令处理器 SQE 的微码
  a650_gmu.bin         GMU（Cortex-M3）的固件；A620 的 GX 域只有 GMU 能开
  a620_zap.mdt/.b00-.b02  签名的 zap shader，经 TrustZone 加载，让 GPU 离开安全模式
都在 vendor.img 的 /firmware 下（原厂设备树 pil_gpu_region@8bc15400，大小 0x2000，是 zap 的位置）。
需要 e2fsprogs 的 debugfs（brew install e2fsprogs），只读打开镜像。
输出的是高通的二进制，不要放进 git（research/ 已在 .gitignore 里）。
"""
import hashlib, os, shutil, subprocess, sys

FILES = ['a650_sqe.fw', 'a650_gmu.bin',
         'a620_zap.mdt', 'a620_zap.b00', 'a620_zap.b01', 'a620_zap.b02']
DEBUGFS_CANDIDATES = ['/opt/homebrew/opt/e2fsprogs/sbin/debugfs',
                      '/usr/local/opt/e2fsprogs/sbin/debugfs', 'debugfs']


def find_debugfs():
    for c in DEBUGFS_CANDIDATES:
        p = shutil.which(c) if os.sep not in c else (c if os.access(c, os.X_OK) else None)
        if p:
            return p
    raise SystemExit('找不到 debugfs：brew install e2fsprogs')


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    img = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'research', 'picasso-gpu-fw')
    os.makedirs(out, exist_ok=True)
    debugfs = find_debugfs()
    for name in FILES:
        dst = os.path.join(out, name)
        # debugfs 默认只读打开；参数用列表传，不经过 shell
        r = subprocess.run([debugfs, '-R', f'dump /firmware/{name} {dst}', img],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
            raise SystemExit(f'{name}: 取不出来\n{r.stderr.strip()}')
        data = open(dst, 'rb').read()
        print(f'{name:14} {len(data):6} 字节  sha256 {hashlib.sha256(data).hexdigest()[:16]}…')
    # zap 的 mdt 是 ELF 头 + 程序头：确认是 32 位 ELF，段数和 .bNN 文件对得上
    mdt = open(os.path.join(out, 'a620_zap.mdt'), 'rb').read()
    if mdt[:4] != b'\x7fELF' or mdt[4] != 1:
        raise SystemExit('a620_zap.mdt 不是 32 位 ELF')
    phnum = int.from_bytes(mdt[44:46], 'little')
    print(f'a620_zap.mdt：{phnum} 个程序头（对应 .b00-.b{phnum - 1:02d}）')
    print(f'已写到 {os.path.abspath(out)}（不要提交）')


if __name__ == '__main__':
    main()
