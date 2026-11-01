#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the SWEET xnu needs to boot.

Only what the early boot path actually reads (see NOTES.md); every MMIO
address comes from the sweet live FDT. Usage:  mkadt.py OUT.adt [--dump] [--qemu] [--lowmem]
  [--high-base=0x.. --high-size=0x..] [--touch-fw=DIR | --no-touch-fw]
picasso 默认用真机验证过的高位内存段（0x140000000+3G）；--lowmem 回到旧的低位窗口，--qemu 也用低位。
--file DEST=LOCAL [UID:GID:MODE]（可多次）/ --files-from LIST / --delete DEST / --list PATH：
  把文件放进 /sweet-files 节点，内核（补丁 0114，bsd/dev/sweet_files.c）在 launchd 启动前写到手机根目录，
  不用重刷 userdata。LIST 每行 "DEST=LOCAL [UID:GID:MODE]"、"-DEST"（先删）、"?PATH"（把匹配打印进内核日志），# 开头是注释。
  DEST 只能在 FILE_PREFIXES 之下，最多 64 个文件、48 MiB。
"""
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# 低位窗口（只有 --lowmem / --qemu 用）。**默认不用它**：另一台 K30 5G 的 shim 实测，第一段 DRAM
# 到 0xBCC00000 就结束，0xBCC00000-0xC0000000 是空洞，而这个窗口 0xb0000000-0xfd000000 正好跨过它，
# XNU 会把不存在的内存当 RAM 用 → 很可能死机。真机启动用下面 HIGH_* 的高位段。
PHYS_BASE = 0xb0000000
MEM_SIZE = 0x4d000000
# Panic log buffer, carved from the TOP of the dram range and exposed to XNU as
# /pram + /chosen/embedded-panic-log-size. Without it, pe_init.c's
# check_for_panic_log() leaves gPanicBase == 0, debug_log_init() bails out twice
# and the kernel hangs right after "kdp_core zlib memory" — reproduced on QEMU
# and independently on ios-fajita.
PANIC_LOG_SIZE = 0x100000
# 默认的高位 DDR 段。every fixed reserved-memory carve-out is in the low bank.
# picasso：用另一台 8 GB K30 5G 上**真机验证过**的窗口 0x1_40000000 起 3 GiB（到 0x2_00000000）：
# github.com/minhtritt1996/ios-picasso 用它启动 XNU、挂载 iOS 根分区、跑到 SpringBoard；
# 原厂设备树里位置固定的保留区都在 0xA2400000 以下。那台手机上 ABL 不给设备树，
# 所以真实的内存分布至今没人读到；若诊断 shim 显示了 DRAM banks，可用
# --high-base=0x... --high-size=0x... 覆盖，不必改代码。
HIGH_PHYS_BASE = 0x140000000
HIGH_MEM_SIZE = 0xC0000000
# /arm-io children are relative to this; it must not be 0 (serial_init bails)
SOC_BASE = 0x01000000

GICD, GICD_SIZE = 0x17a00000, 0x10000
GICR, GICR_SIZE = 0x17a60000, 0x100000
# picasso：cont_splash_region <0xa0000000 0x2300000>
FB, FB_SIZE = 0xa0000000, 0x2300000
SWEET_ECID = 0x5049434153534f01  # picasso：'PICASSO' + 1，合成值，不是真实芯片 ID
FB_W, FB_H, FB_STRIDE = 1080, 2400, 1080 * 4
# Internal UFS host (ufshc@1d84000 "ufs_mem"); QEMU virt has none
UFS, UFS_SIZE = 0x01d84000, 0x3000
TIMEBASE = 19200000
VTIMER_INTID = 19
# QEMU virt (-M virt,gic-version=3 -cpu cortex-a76): GICD/GICR and the generic timer's CNTFRQ
QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE = 0x08000000, 0x080a0000, 62500000
QEMU_VTIMER_INTID = 27

BOOT_MANIFEST_HASH = hashlib.sha384(b'picasso boot manifest').digest()

# picasso：Novatek NT36672C 触控固件（内核 bsd/dev/picasso_touch.c，补丁 0082）。
# 芯片没有闪存，每次开机都要下载。文件取自原厂 boot.img 内核里 CONFIG_EXTRA_FIRMWARE
# 内置的三份固件（小米的二进制，不进 git），默认放在 research/picasso-touch/，
# 也可以用 --touch-fw=目录 指定。
TOUCH_FW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/picasso-touch')
TOUCH_FW = [('fw01', 'novatek_nt36672c_g7a_fw01.bin'),
            ('fw02', 'novatek_nt36672c_g7a_fw02.bin'),
            ('fw03', 'novatek_nt36672c_g7a_fw03.bin')]
# 面板编号（ABL 命令行 mdss_dsi_g7a_XX_XX_XX，shim 转成 boot-arg picassopanel=）对应的固件。
# 依据：原厂 DisplayDxe 把 37_02_0a / 37_02_0b / 36_02_0c 依次列为 EBBG / EBBG_815 /
# Tianma 的 NT36672C；原厂设备树 cfg_2（fw03）的 display-maker 是 0x36，
# cfg_0/cfg_1（fw01/fw02）是 0x37、glass-vendor 0/1。36_02_0c -> fw03 比较可靠；
# 37_02_0a -> fw01、37_02_0b -> fw02 是推测（厂商实际按面板 lockdown 信息选）。
# 不对时开机加 picassotouchfw=N 试，改这里不用重编内核。
TOUCH_PANEL_FW = [('37_02_0a', 'fw01'), ('37_02_0b', 'fw02'), ('36_02_0c', 'fw03')]

# picasso：Adreno 620 的固件（内核 bsd/dev/picasso_adreno.c，补丁 0101）。高通的二进制，不进 git；
# tools/picasso-gpu-fw.py 从原厂 vendor.img 取到 research/picasso-gpu-fw/，--gpu-fw=目录 可改。
# 属性名就是文件名；没有目录就不放 /picasso-gpu（内核那边会说缺哪个，GPU 步骤停在那里）。
GPU_FW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/picasso-gpu-fw')
GPU_FW = ['a650_gmu.bin', 'a650_sqe.fw', 'a620_zap.mdt', 'a620_zap.b00', 'a620_zap.b01', 'a620_zap.b02']


def gpu_node(fw_dir, ph):
    props = []
    for fname in GPU_FW:
        path = os.path.join(fw_dir, fname)
        if os.path.exists(path):
            props.append((fname, open(path, 'rb').read()))
        else:
            print(f'warning: {path} missing', file=sys.stderr)
    if not props:
        print(f'warning: no GPU firmware in {fw_dir}, no /picasso-gpu node', file=sys.stderr)
        return None
    return node('picasso-gpu', [ph(), ('compatible', cstr('qcom,adreno-620'))] + props)


def touch_node(fw_dir, ph):
    """/picasso-touch：fw01..fw03 原样放进属性，panel-fw 是 "面板=属性名" 字符串列表。"""
    props = []
    for name, fname in TOUCH_FW:
        path = os.path.join(fw_dir, fname)
        if os.path.exists(path):
            props.append((name, open(path, 'rb').read()))
        else:
            print(f'warning: {path} missing', file=sys.stderr)
    if not props:
        print(f'warning: no touch firmware in {fw_dir}, no /picasso-touch node', file=sys.stderr)
        return None
    have = {n for n, _ in props}
    table = b''.join(cstr(f'{panel}={fw}') for panel, fw in TOUCH_PANEL_FW if fw in have)
    return node('picasso-touch', [ph(), ('compatible', cstr('novatek,nt36672c-spi')),
                                  ('panel-fw', table)] + props)

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW. MobileGestalt
# derives product type, device class, home-button-less UI, artwork etc. from
# the root identity and /product of the real D22AP device tree. That tree is
# an Apple artifact (kept out of git): extract it from the IPSW with
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/Firmware/all_flash/DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/ipsw-x/d22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')

u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n



# 内核（sweet_files.c）接受的目的路径前缀；保持保守：我们的库、调试代理、少数设置目录
FILE_PREFIXES = ('/usr/local/', '/Library/Wallpaper/', '/private/var/', '/System/Library/xpc/',
                 '/Library/Preferences/', '/private/etc/')
FILES_MAX, FILES_BYTES = 64, 48 << 20
def files_node(argv):
    """--file / --files-from / --delete / --list 的参数 -> /sweet-files 节点（没有就返回 None）。
    格式见 bsd/dev/sweet_files.c：fileN = 路径\\0内容，ownerN = uid,gid,mode 三个 32 位字，deleteN / listN = 路径\\0。"""
    wanted = []
    for i, a in enumerate(argv):
        if a == '--file':
            wanted.append(('--file', argv[i + 1]))
        elif a == '--delete':
            wanted.append(('--delete', '-' + argv[i + 1]))
        elif a == '--list':
            wanted.append(('--list', '?' + argv[i + 1]))
        elif a == '--files-from':
            for n, line in enumerate(open(argv[i + 1], encoding='utf-8'), 1):
                line = line.strip()
                if line and not line.startswith('#'):
                    wanted.append((f'{argv[i + 1]} line {n}', line))
    files, deletes, lists = [], [], []
    ok = lambda d: d.startswith(FILE_PREFIXES) and '/../' not in d + '/' and '*' not in d
    for where, spec in wanted:
        if spec.startswith(('-', '?')):
            dest = spec[1:]
            if not dest.startswith(FILE_PREFIXES) or '/../' in dest + '/':
                raise SystemExit(f'{where}: {spec}: 路径要在 ' + ', '.join(FILE_PREFIXES) + ' 之下')
            (deletes if spec[0] == '-' else lists).append(dest)
            continue
        owner = None
        parts = spec.split()
        dest, _, local = parts[0].partition('=')
        if len(parts) > 1:
            uid, gid, mode = parts[1].split(':')
            owner = (int(uid), int(gid), int(mode, 8))
        if not local or not ok(dest):
            raise SystemExit(f'{where}: {spec}: 写成 DEST=LOCAL_FILE [UID:GID:MODE]，DEST 在 ' + ', '.join(FILE_PREFIXES) + ' 之下')
        files.append((dest, open(local, 'rb').read(), owner))
    if not (files or deletes or lists):
        return None
    if len(files) > FILES_MAX or sum(len(d) for _, d, _ in files) > FILES_BYTES:
        raise SystemExit(f'--file: 最多 {FILES_MAX} 个文件、{FILES_BYTES >> 20} MiB')
    for dest, data, _ in files:
        print(f'/sweet-files: {dest} ({len(data)} bytes)', file=sys.stderr)
    return node('sweet-files', [('AAPL,phandle', u32(0x5ff1))]
        + [(f'list{i}', cstr(d)) for i, d in enumerate(lists)]
        + [(f'delete{i}', cstr(d)) for i, d in enumerate(deletes)]
        + [(f'file{i}', cstr(d) + data) for i, (d, data, _) in enumerate(files)]
        + [(f'owner{i}', u32(*o)) for i, (_, _, o) in enumerate(files) if o is not None])
def build(seed=None, qemu=False, highmem=True, touch_fw_dir=TOUCH_FW_DIR, gpu_fw_dir=GPU_FW_DIR):
    """qemu=True: same tree, but with QEMU virt's GIC addresses and CNTFRQ (tools/qemu-sweet.py).
    touch_fw_dir：picasso 触控固件目录（None = 不放 /picasso-touch）。"""
    phys_base, mem_size = (HIGH_PHYS_BASE, HIGH_MEM_SIZE) if highmem else (PHYS_BASE, MEM_SIZE)
    # Carve the panic log off the top so the kernel never allocates over it.
    panic_base = phys_base + mem_size - PANIC_LOG_SIZE
    mem_size -= PANIC_LOG_SIZE
    gicd, gicr, timebase = (QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE) if qemu else (GICD, GICR, TIMEBASE)
    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(PANIC_LOG_SIZE)),
        ('firmware-version', cstr('sweet-shim')),
        ('system-firmware-version', cstr('sweet-shim')),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists (bind mounts /private/preboot/<hex>/usr/standalone/firmware,
        # tolerating a missing directory). Fixed stand-in value.
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm" without
        # it; real A11+ device trees say sha2-384.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: display-scale (u32, default 0) comes
        # from /chosen, the pixel size from IOMobileFramebuffer. Scale 0 makes
        # SpringBoard's FBSDisplayMode "infxinf@0x" invalid. 1080x2400 @3x =
        # 360x800 points (like iPhone X class devices).
        ('display-scale', u32(3)),
        # XNU derives IOPlatformUUID (gethostuuid) from these on arm; without
        # them CFPreferences cannot use kCFPreferencesCurrentHost (no ByHost
        # files), so e.g. DataMigration never records LastSystemVersion and
        # migrates on every boot. Synthetic ECID (not the phone's); chip-id
        # of the A11 that the iPhone X userland expects.
        ('unique-chip-id', struct.pack('<Q', SWEET_ECID)),
        ('chip-id', u32(0x8015)),
        # Security status as on a dev-fused, undemoted board (tuple 0,0,1,1):
        # MobileGestalt reads these from /chosen, and mobileactivationd's
        # DeviceType then takes its native "Hactivation" branch (state
        # Activated without an activation record; no SEP, no baseband here).
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
        # MobileGestalt's Wi-Fi/Bluetooth/Ethernet addresses. Without them every
        # WifiAddress answer waits for an IONetworkController that never comes
        # (once per question), so lsd's first-boot LaunchServices seed takes
        # ~15 minutes (observed on the second K30: SpringBoard in LSContextInit,
        # lsd in MobileGestalt's semaphore). Same fix and cause as ios-flame
        # and the Poco F1 port. Locally administered, not the phone's own.
        ('mac-address-wifi0', bytes([0x02, 0x50, 0x49, 0x43, 0x41, 0x01])),
        ('mac-address-bluetooth0', bytes([0x02, 0x50, 0x49, 0x43, 0x41, 0x02])),
        ('mac-address-ethernet0', bytes([0x02, 0x50, 0x49, 0x43, 0x41, 0x03])),
    ], [node('memory-map', [ph()]),
        # Local boot policy (kern_csr.c): SIP relaxed like `csrutil disable`
        # (lp-sip0: untrusted kexts, unrestricted FS, task_for_pid, kernel
        # debugger, dtrace, nvram), lp-sip1: unauthenticated (unsealed) root.
        # The iOS shared cache is in no trust cache, so dyld can only map it
        # with CSR_ALLOW_UNRESTRICTED_FS (vm_unix.c shared region checks).
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    # SWEET SMP: all eight cores in physical-core order (0-5 Cortex-A55,
    # 6-7 Cortex-A76), one homogeneous cluster. The kernel defaults to one
    # and uses cpus=N to select an initial prefix (N <= 8).
    # QEMU retains its original single-CPU tree: its MPIDRs differ from sweet.
    cpu_nodes = []
    for core in range(1 if qemu else 8):
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cstr('qcom,kryo470') + cstr('qcom,kryo475') + cstr('ARM,v8')),  # picasso：内核 CPU 驱动（补丁 0018/0038）按 qcom,kryo470 匹配，必须保留；kryo475 只是附加描述
            ('reg', u32(core << 8)),  # actual MPIDR Aff1:Aff0, NOT logical ID
            ('cpu-id', u32(core)),   # descriptive: XNU assigns IDs by DT order
            ('cluster-id', u32(0)),  # descriptive; see ml_parse_cpu_topology
            ('cluster-type', u32(0)),  # homogeneous CLUSTER_TYPE_SMP, not AMP
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(core)),  # distinct per-core L2 nodes in live FDT
            ('l3-cache-id', u32(0)),     # shared L3
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(timebase)),
            # picasso：cpu7 2.40 GHz、cpu6 2.208 GHz、cpu0-5 1.8048 GHz（adb 读到的 cpufreq 最高频率；只是描述值）
            ('clock-frequency', u32({7: 2400000000, 6: 2208000000}.get(core, 1804800000))),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1866000000)),
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], cpu_nodes)

    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v3')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(QEMU_VTIMER_INTID if qemu else VTIMER_INTID)),
        ('reg', u64(gicd - SOC_BASE, GICD_SIZE, gicr - SOC_BASE, GICR_SIZE)),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('sweet-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        # No hardware: watchdogd checks IODeviceTree:/arm-io/wdt for no-pmu and
        # otherwise crashes trying to set the PMU boot stage (customer builds
        # ignore the -wdt_no_pmu boot-arg).
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display: IOMobileFramebuffer.framework counts /arm-io children
        # with this device_type and reads the canvas size from the node
        # (the presented iPhone X size, see SweetFramebuffer.cpp);
        # the kernel's AppleCLCD matches the compatible string.
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(1125)),
            ('canvas-height', u32(2436)),
        ])])

    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(FB, FB_SIZE)),
        ('width', u32(FB_W)),
        ('height', u32(FB_H)),
        ('stride', u32(FB_STRIDE)),
    ])

    # bsd/dev/sweet_ufs.c: the UFS host ABL leaves running (absolute physical reg).
    ufs = node('sweet-ufs', [
        ph(),
        ('compatible', cstr('qcom,ufshc')),
        ('reg', u64(UFS, UFS_SIZE)),
    ])

    # pmap_compute_max_asids requires the node even when no overrides are set.
    # An absent pmap-max-asids property selects XNU's own MAX_ASIDS default.
    # No effaceable storage: keybagd then reads /private/var/keybags/systembag.kb
    # as a plain plist instead of decrypting it with the effaceable key.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons home-button-type
    # (D22AP: 2, no Home button; absent = 0 = Home-button device).
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    product_props = d22ap_product()
    if product_props:
        # ios-topaz 3eacb48 / 另一台 K30：iPhone X 的 /product 写的是 P3 广色域，但这块屏是 sRGB。
        # 保留 P3 时 CoreAnimation/WebKit 会往 10 位色深的表面画（我们没有），结果图标/Safari 空白。
        product_props = [(k, cstr('sRGB') if k == 'artwork-display-gamut' else v) for k, v in product_props]
    product = [node('product', [ph()] + product_props)] if product_props else []

    # Panic log region. /pram reg = <phys size>; it sits just above the
    # dram-size we expose, so XNU maps it but never allocates into it.
    pram = node('pram', [ph(), ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    touch = touch_node(touch_fw_dir, ph) if touch_fw_dir and not qemu else None
    gpu = gpu_node(gpu_fw_dir, ph) if gpu_fw_dir and not qemu else None

    # Identity of the iPhone X whose userland runs here: compatible[0] is
    # hw.model/hw.target (D22AP), model is hw.machine/hw.product
    # (iPhone10,3). 'xiaomi,sweet' stays for SweetPlatformExpert matching.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr('xiaomi,sweet') + cstr('xiaomi,picasso') + cstr('qcom,lito') + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product + ([] if qemu else [ufs])
       + ([touch] if touch else []) + ([gpu] if gpu else []))


if __name__ == '__main__':
    # picasso：--high-base=0x... / --high-size=0x... 覆盖高位内存段（按诊断 shim 的 DRAM banks 填写）
    # --touch-fw=目录 指定触控固件目录；--no-touch-fw 不放 /picasso-touch
    touch_dir = TOUCH_FW_DIR
    gpu_dir = GPU_FW_DIR
    for arg in sys.argv:
        if arg.startswith('--high-base='):
            HIGH_PHYS_BASE = int(arg.split('=', 1)[1], 0)
        elif arg.startswith('--high-size='):
            HIGH_MEM_SIZE = int(arg.split('=', 1)[1], 0)
        elif arg.startswith('--touch-fw='):
            touch_dir = arg.split('=', 1)[1]
        elif arg == '--no-touch-fw':
            touch_dir = None
        elif arg.startswith('--gpu-fw='):
            gpu_dir = arg.split('=', 1)[1]
        elif arg == '--no-gpu-fw':
            gpu_dir = None
    # 默认高位内存段（真机验证过）；--lowmem 回到旧低位窗口，--qemu 也用低位。
    highmem = '--lowmem' not in sys.argv and '--qemu' not in sys.argv
    root = build(qemu='--qemu' in sys.argv, highmem=highmem, touch_fw_dir=touch_dir, gpu_fw_dir=gpu_dir)
    extra = files_node(sys.argv)
    if extra is not None:
        root.children.append(extra)
    blob = adt.serialize(root)
    open(sys.argv[1], 'wb').write(blob)
    if '--dump' in sys.argv:
        adt.dump(adt.parse(blob)[0])
    print(f'{sys.argv[1]}: {len(blob)} bytes', file=sys.stderr)
