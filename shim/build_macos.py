#!/usr/bin/env python3
"""在 macOS 上构建 picasso 启动垫片（shim），并打包成 fastboot boot 可用的 boot.img。

原版 shim/build.sh 依赖 Linux 的 aarch64-linux-gnu-gcc、GNU awk 的 strtonum、truncate 和 mkbootimg 命令，
在 Mac 上跑不了。本脚本用 Homebrew 的 LLVM（clang + ld.lld + llvm-objcopy）做同样的事，结果相同：

  1. 交叉编译 start.S 和全部 C 文件为 AArch64 裸机目标（-ffreestanding、无 libc、位置无关的跳转）。
  2. 检查 C 目标文件里没有 ABS64 绝对重定位：ABL 把镜像放在任意地址运行、不做重定位，
     只有 start.S 里 Linux 镜像头的 image_size 允许是绝对值。
  3. 按 shim.ld 链接，转成纯二进制 Image，并补零到 _end（.bss 和各核心的栈必须是实际存在的零字节）。
  4. ramdisk：有参数时用 tools/mkpayload.py 生成的 XNU 负载；没有时用空的 newc cpio（只跑诊断）。
  5. 用 picasso 原厂 boot.img 的镜像头参数打包：v2 头，base 0，内核 0x8000，ramdisk 0x1000000，
     tags 0x100，DTB 0x1f00000，页大小 4096，os_version 11.0.0、补丁级别 2022-10（与原厂一致，
     避免改变 TrustZone 里 keymaster 记录的版本），DTB 用原厂 boot.img 的整段 DTB。

用法：
  python3 shim/build_macos.py                 # 只含诊断的 shim（不带 XNU）
  python3 shim/build_macos.py PAYLOAD.bin     # 带 XNU 负载
可选环境变量：
  PICASSO_DTB   原厂 boot.img 的 DTB 段（默认 ../../firmware/V13.0.3.0.SGICNXM/boot-dtb.bin）
  MKBOOTIMG     mkbootimg.py 路径（默认 ../uefi-picasso/Resources/Scripts/mkbootimg.py）
"""
import gzip
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'out'
WORKSPACE = HERE.parents[2]          # 小米K30-5G-IOS
LLVM = ('/opt/homebrew/opt/llvm/bin', '/opt/homebrew/opt/lld/bin', '/usr/local/opt/llvm/bin', '/usr/local/opt/lld/bin')
SOURCES = ['start.S', 'shim.c', 'console.c', 'mmu.c', 'fdt.c', 'loader.c', 'ufs.c', 'libc.c']
CFLAGS = ['--target=aarch64-none-elf', '-O2', '-ffreestanding', '-nostdlib', '-fno-pic', '-mgeneral-regs-only',
          '-mstrict-align', '-fno-stack-protector', '-Wall', '-Wextra', '-Werror',
          # clang 在 -O2 下会把 switch 改写成“字符串指针查找表”，表里是绝对地址（ABS64 重定位），
          # 而 ABL 不做重定位；GCC 不会这样做。关掉跳转表/查找表，生成的代码与原版 GCC 构建的行为一致。
          '-fno-jump-tables']
# 与 picasso 原厂 boot.img（V13.0.3.0.SGICNXM）的镜像头一致
BOOTIMG = ['--header_version', '2', '--base', '0', '--kernel_offset', '0x8000', '--ramdisk_offset', '0x1000000',
           '--tags_offset', '0x100', '--dtb_offset', '0x1f00000', '--pagesize', '4096',
           '--os_version', '11.0.0', '--os_patch_level', '2022-10', '--cmdline', 'console=null']


def tool(name):
    found = shutil.which(name, path=os.pathsep.join(LLVM)) or shutil.which(name)
    if not found:
        sys.exit(f'build_macos：找不到 {name}（请先 brew install llvm lld）')
    return found


def run(*command, **kwargs):
    result = subprocess.run([str(c) for c in command], capture_output=True, text=True, **kwargs)
    if result.returncode:
        sys.exit(f'build_macos：命令失败：{" ".join(map(str, command))}\n{result.stdout}{result.stderr}')
    return result.stdout


def empty_cpio():
    """只含 TRAILER!!! 的 newc cpio，再 gzip（与原脚本 `find . | cpio -o -H newc | gzip -9` 等价，内容为空）。"""
    name = b'TRAILER!!!\0'
    header = b'070701' + b''.join(b'%08X' % v for v in (0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(name), 0))
    entry = header + name
    entry += b'\0' * (-len(entry) % 4)
    entry += b'\0' * (-len(entry) % 512)
    return gzip.compress(entry, compresslevel=9, mtime=0)


def main():
    payload = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    dtb = Path(os.environ.get('PICASSO_DTB', WORKSPACE / 'firmware/V13.0.3.0.SGICNXM/boot-dtb.bin'))
    mkbootimg = Path(os.environ.get('MKBOOTIMG', WORKSPACE / 'src/uefi-picasso/Resources/Scripts/mkbootimg.py'))
    for path, what in ((dtb, '原厂 boot.img 的 DTB 段'), (mkbootimg, 'mkbootimg.py')):
        if not path.is_file():
            sys.exit(f'build_macos：缺少{what}：{path}')
    clang, lld, objcopy, readelf, nm = (tool(n) for n in ('clang', 'ld.lld', 'llvm-objcopy', 'llvm-readelf', 'llvm-nm'))
    OUT.mkdir(exist_ok=True)

    objects = []
    for source in SOURCES:
        obj = OUT / (Path(source).stem + '.o')
        run(clang, *CFLAGS, '-c', HERE / source, '-o', obj)
        objects.append(obj)
    relocs = run(readelf, '-r', *[o for o in objects if o.name != 'start.o'])
    absolute = relocs.count('R_AARCH64_ABS64')
    if absolute:
        sys.exit(f'build_macos：C 目标文件中有 {absolute} 个绝对重定位（ABL 不做重定位，必须为 0）')

    elf = OUT / 'shim.elf'
    run(lld, '-static', '-T', HERE / 'shim.ld', '--build-id=none', '-N', *objects, '-o', elf)
    image = OUT / 'Image'
    run(objcopy, '-O', 'binary', elf, image)
    end = next(int(line.split()[0], 16) for line in run(nm, elf).splitlines() if line.split()[-1:] == ['_end'])
    data = image.read_bytes()
    if len(data) > end:
        sys.exit('build_macos：Image 比 _end 还大，链接脚本有问题')
    image.write_bytes(data + bytes(end - len(data)))
    # 镜像头核对：魔数 ARM\x64、image_size = _end
    head = image.read_bytes()[:64]
    if head[56:60] != b'ARM\x64' or struct.unpack_from('<Q', head, 16)[0] != end:
        sys.exit('build_macos：ARM64 镜像头不正确')

    ramdisk = OUT / 'ramdisk.bin'
    if payload:
        shutil.copyfile(payload, ramdisk)
    else:
        ramdisk.write_bytes(empty_cpio())

    boot = OUT / 'shim-boot.img'
    run('python3', mkbootimg, '--kernel', image, '--ramdisk', ramdisk, '--dtb', dtb, *BOOTIMG, '-o', boot)

    # 主机渲染一次屏幕布局（out/host.ppm），确认控制台代码在宿主机上也能跑
    run('cc', '-O1', '-Wall', '-o', OUT / 'host_test', HERE / 'console.c', HERE / 'host_test.c')
    run(OUT / 'host_test', cwd=HERE)
    # 设备树解析自测：原厂 picasso 设备树的 /memory 大小为 0（ABL 启动时才填），应读到 0 个区段
    stock_dtb = WORKSPACE / 'firmware/V13.0.3.0.SGICNXM/dtb/1.dtb'
    if stock_dtb.is_file():
        run('cc', '-O1', '-Wall', '-Wextra', '-Werror', '-o', OUT / 'fdt_host_test', HERE / 'fdt.c', HERE / 'fdt_host_test.c')
        run(OUT / 'fdt_host_test', stock_dtb, '0')
    # 负载搜索自测（ABL 不给设备树时，在它的堆里找负载）
    run('cc', '-O1', '-Wall', '-Wextra', '-Werror', '-o', OUT / 'loader_host_test',
        HERE / 'console.c', HERE / 'fdt.c', HERE / 'loader_host_test.c')
    run(OUT / 'loader_host_test')
    print(f'build_macos：Image {image.stat().st_size} 字节（_end = {end:#x}），'
          f'ramdisk {ramdisk.stat().st_size} 字节，boot.img {boot.stat().st_size} 字节 -> {boot}')


if __name__ == '__main__':
    main()
