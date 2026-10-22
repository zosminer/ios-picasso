#!/usr/bin/env python3
"""从 picasso 原厂 boot.img 的内核里取出内置的 Novatek 触控固件。

  picasso-touch-fw.py BOOT.IMG [OUTDIR]     （OUTDIR 默认 research/picasso-touch）

原厂内核用 CONFIG_EXTRA_FIRMWARE 把 novatek_nt36672c_g7a_fw01/02/03.bin（和 mp 测试固件）
编进了内核，/vendor/firmware 里没有。内核里有一张 builtin_fw 表，每项 24 字节
{char *name; void *data; unsigned long size}，name/data 两个指针要靠 .rela 里的
R_AARCH64_RELATIVE 重定位才知道值，所以这里先把重定位表读出来再查表。
只写 fw01..fw03（mp 是产线测试用的，不需要）。输出的是小米的二进制，不要放进 git。
"""
import gzip, hashlib, os, struct, sys

R_AARCH64_RELATIVE = 1027
NAMES = ['novatek_nt36672c_g7a_fw01.bin', 'novatek_nt36672c_g7a_fw02.bin', 'novatek_nt36672c_g7a_fw03.bin']


def kernel_of(boot):
    if boot[:8] != b'ANDROID!':
        raise SystemExit('不是 Android boot.img')
    ksize, page = struct.unpack_from('<I', boot, 8)[0], struct.unpack_from('<I', boot, 36)[0]
    k = boot[page:page + ksize]
    if k[:2] == b'\x1f\x8b':
        k = gzip.decompress(k)
    if k[0x38:0x3c] != b'ARMd':
        raise SystemExit('内核不是 arm64 Image')
    return k


def relocations(k):
    """找最长的一段连续 RELATIVE 重定位（.rela.dyn），返回 {r_offset: addend}。"""
    tag = struct.pack('<Q', R_AARCH64_RELATIVE)
    i = k.find(tag)
    while i >= 0:
        s = i - 8
        if s % 8 == 0:
            n, j = 0, s
            while j + 24 <= len(k) and k[j + 8:j + 16] == tag:
                n, j = n + 1, j + 24
            if n > 1000:
                while s >= 24 and k[s - 16:s - 8] == tag:
                    s -= 24
                rel, j = {}, s
                while j + 24 <= len(k) and k[j + 8:j + 16] == tag:
                    off, _, add = struct.unpack_from('<QQQ', k, j)
                    rel[off] = add
                    j += 24
                return rel
            i = k.find(tag, j)
        else:
            i = k.find(tag, i + 1)
    raise SystemExit('找不到重定位表')


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/picasso-touch')
    k = kernel_of(open(sys.argv[1], 'rb').read())
    text_offset = struct.unpack_from('<Q', k, 8)[0]
    rel = relocations(k)
    by_target = {}
    for off, add in rel.items():
        by_target.setdefault(add, []).append(off)
    # 映像文件偏移 0 对应 _text 的虚拟地址 = KIMAGE_VADDR + text_offset。
    # KIMAGE_VADDR 取所有重定位目标中最小值所在的 16 MiB 边界，下面再用表项内容验证。
    text_va = (min(rel.values()) & ~0xffffff) + text_offset
    os.makedirs(out, exist_ok=True)
    found = 0
    for name in NAMES:
        off = k.find(name.encode() + b'\0')
        if off < 0:
            print(f'{name}：内核里没有这个名字')
            continue
        for slot in by_target.get(text_va + off, []):
            data_va = rel.get(slot + 8)
            if data_va is None:
                continue
            size = struct.unpack_from('<Q', k, slot + 16 - text_va)[0]
            doff = data_va - text_va
            if not (0 < size < 0x100000 and 0 <= doff and doff + size <= len(k)):
                continue
            blob = k[doff:doff + size]
            path = os.path.join(out, name)
            open(path, 'wb').write(blob)
            print(f'{name}：{size} 字节，sha256 {hashlib.sha256(blob).hexdigest()} -> {path}')
            found += 1
            break
        else:
            print(f'{name}：没找到 builtin_fw 表项')
    if found != len(NAMES):
        raise SystemExit(f'只取到 {found}/{len(NAMES)} 个固件')


if __name__ == '__main__':
    main()
