#!/usr/bin/env python3
"""通过 adb 把 Android 稀疏镜像（.simg）流式写进手机的一个块设备（在 OrangeFox 里用）。

  simg-adb-write.py IMAGE.simg /dev/block/sdaNN [--yes] [--dry-run] [--resume-mib=N]

为什么有它：这台小米的 fastboot 在大文件写入时常常 USB 断开（usb_write failed e00002ed），
OrangeFox 里的 adb 却稳。fastboot 用的稀疏转换工具（simg2img）这里没有，而且原始镜像要 16 GiB，
所以边解析边写，不落盘。

做法：把相邻的 RAW / FILL chunk 展开成连续字节，攒成最多 SEG_MIB 的一段，每段一次
`adb exec-in 'dd of=DEV bs=1M seek=<MiB 偏移> conv=notrunc'`。FILL 块（多半是零）也真的写，
否则目标上残留的旧数据会留在镜像的空洞里。DONT_CARE 块不写（和 fastboot flash 一样）。
段边界一律对齐到 1 MiB，所以 seek 用 MiB 做单位；失败时打印 --resume-mib=N 续写。
每段写完后在手机上回读校验该段的 SHA-256（--no-verify 可关）。
--dry-run 只解析、打印统计，不碰手机。写入前要求目标块设备大小 >= 镜像声明的大小，
没有 --yes 时让用户键入设备路径确认。
"""
import hashlib, struct, subprocess, sys, time

SPARSE_MAGIC = 0xED26FF3A
CHUNK_RAW, CHUNK_FILL, CHUNK_DONT_CARE, CHUNK_CRC32 = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4
MIB = 1 << 20
SEG_MIB = 32            # 小段：这台手机的 USB 在持续大流量下会掉线，段短则失败代价小
SEG_PAUSE = 0.2         # 段间停顿（秒），让 USB 缓冲排空


def parse(path):
    f = open(path, 'rb')
    hdr = f.read(28)
    magic, major, minor, fhsz, chsz, blksz, total_blks, total_chunks, _crc = struct.unpack('<IHHHHIIII', hdr)
    if magic != SPARSE_MAGIC or major != 1:
        sys.exit(f'{path}: 不是稀疏镜像（magic {magic:#x} major {major}）')
    f.seek(fhsz)
    chunks = []
    block = 0
    for i in range(total_chunks):
        ch = f.read(chsz)
        ctype, _res, csize, tsize = struct.unpack('<HHII', ch[:12])
        data_off = f.tell()
        payload = tsize - chsz
        if ctype == CHUNK_RAW:
            if payload != csize * blksz:
                sys.exit(f'chunk {i}: RAW 大小 {payload} != {csize}*{blksz}')
            chunks.append((i, 'raw', block, csize, data_off))
        elif ctype == CHUNK_FILL:
            chunks.append((i, 'fill', block, csize, f.read(4)))
        elif ctype == CHUNK_DONT_CARE:
            chunks.append((i, 'skip', block, csize, None))
        elif ctype == CHUNK_CRC32:
            csize = 0
        else:
            sys.exit(f'chunk {i}: 未知类型 {ctype:#x}')
        block += csize
        f.seek(data_off + payload)
    if block != total_blks:
        sys.exit(f'块数不符：算出 {block}，头部写 {total_blks}')
    return f, blksz, total_blks, chunks


def segments(f, blksz, chunks):
    """产生 (字节偏移, bytes)，每段 <= SEG_MIB，起点 1 MiB 对齐；遇到 DONT_CARE 就断开。"""
    buf = bytearray()
    start = None
    seg = SEG_MIB * MIB
    for (_i, kind, blk, cnt, extra) in chunks:
        if kind == 'skip':
            if buf:
                yield start, bytes(buf)
                buf, start = bytearray(), None
            continue
        off = blk * blksz
        n = cnt * blksz
        if start is None:
            start = off
        if kind == 'raw':
            f.seek(extra)
            data = f.read(n)
        else:
            data = (extra * (n // 4))
        buf += data
        while len(buf) >= seg:
            # 先把对齐到 1 MiB 的前缀取出来写
            cut = (len(buf) // MIB) * MIB if len(buf) < 2 * seg else seg
            cut = min(cut, seg)
            yield start, bytes(buf[:cut])
            del buf[:cut]
            start += cut
    if buf:
        yield start, bytes(buf)


def adb(*args, **kw):
    return subprocess.run(['adb', *args], capture_output=True, **kw)


def main():
    pos = [x for x in sys.argv[1:] if not x.startswith('--')]
    flags = [x for x in sys.argv[1:] if x.startswith('--')]
    if len(pos) != 2:
        sys.exit(__doc__)
    path, dev = pos
    resume = 0
    for x in flags:
        if x.startswith('--resume-mib='):
            resume = int(x.split('=', 1)[1])
    f, blksz, total_blks, chunks = parse(path)
    raw = sum(c[3] for c in chunks if c[1] == 'raw')
    print(f'{path}: 块 {blksz} 字节，共 {total_blks*blksz/2**30:.2f} GiB，{len(chunks)} 个 chunk，'
          f'RAW {raw*blksz/2**30:.2f} GiB，FILL {sum(1 for c in chunks if c[1]=="fill")} 个，'
          f'DONT_CARE {sum(1 for c in chunks if c[1]=="skip")} 个')
    if '--dry-run' in flags:
        n = 0
        total = 0
        for off, data in segments(f, blksz, chunks):
            assert off % MIB == 0, off
            n += 1
            total += len(data)
        print(f'将分成 {n} 段（每段 <= {SEG_MIB} MiB），共 {total/2**30:.2f} GiB；所有段起点 1 MiB 对齐')
        return
    out = adb('shell', f'blockdev --getsize64 {dev}').stdout.decode().strip()
    if not out.isdigit():
        sys.exit(f'读不到 {dev} 的大小：{out!r}（手机在 OrangeFox 里吗？）')
    size = int(out)
    need = total_blks * blksz
    print(f'目标 {dev}：{size} 字节；镜像需要 {need} 字节')
    if size < need:
        sys.exit('目标比镜像小，停止')
    if '--yes' not in flags:
        if input(f'要往 {dev} 写入吗？键入设备路径确认：').strip() != dev:
            sys.exit('已取消')
    t0 = time.time()
    done = 0
    for off, data in segments(f, blksz, chunks):
        if off // MIB < resume:
            continue
        seek = off // MIB
        for attempt in range(3):
            p = subprocess.run(['adb', 'exec-in', f'dd of={dev} bs=1048576 seek={seek} conv=notrunc 2>/dev/null'],
                               input=data, capture_output=True)
            ok = p.returncode == 0
            if ok and '--no-verify' not in flags:
                want = hashlib.sha256(data).hexdigest()
                r = adb('shell', f'dd if={dev} bs=1048576 skip={seek} count={(len(data)+MIB-1)//MIB} 2>/dev/null | head -c {len(data)} | sha256sum')
                got = r.stdout.decode().split()[0] if r.stdout.strip() else ''
                ok = got == want
                if not ok:
                    print(f'  回读校验不一致（{seek} MiB），重试 {attempt+1}/3', flush=True)
            if ok:
                break
        else:
            sys.exit(f'段 {seek} MiB 写入或校验连续失败。修好连接后用 --resume-mib={seek} 续写')
        done += len(data)
        time.sleep(SEG_PAUSE)
        print(f'  {seek:6d} MiB  +{len(data)//MIB:3d} MiB  共 {(off+len(data))/2**30:6.2f} GiB  {time.time()-t0:6.0f}s', flush=True)
    adb('shell', 'sync')
    print(f'完成：写入 {done/2**30:.2f} GiB，用时 {time.time()-t0:.0f}s')


main()
