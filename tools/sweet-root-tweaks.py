#!/usr/bin/env python3
"""Apply the SWEET bring-up changes to an iOS root made by tools/mkiosroot.sh.

  sudo sweet-root-tweaks.py MOUNTED_ROOT

MOUNTED_ROOT is the HFS+ image mounted read-write (Linux: run
`fsck.hfsplus -fy` on the image, then `mount -t hfsplus -o loop,force`).
Run as root (it writes root/mobile owned files). Idempotent. This is what
turned the plain mkiosroot.sh output into the images that reach the home
screen (image versions v8-v14 in NOTES.md):

  launchd.plist   _PanicOnCrash -> _PanicOnCrasX (a crashing daemon must not
                  panic the system); CommCenter jobs removed (no baseband: it
                  crash-loops and Setup blocks on its CoreTelephony XPC)
  containers      /private/var/containers/{Bundle,Data/System,Shared/SystemGroup,Temp}
                  (on iOS they live on the data volume)
  migration       LastSystemVersion so DataMigration does not run for an hour
                  on every boot
  audio           VirtualAudio HAL plug-in disabled (mediaserverd crash)
  MediaToolbox    StartRecorderServer=false (camera filter prewarm calls a
                  NULL OpenGL function without a GPU -> mediaserverd crash loop)
  SpringBoard     BKNoWatchdogs (one slow CPU path, no scene-create watchdog
                  kills), no cloud-configuration UI
  Setup           skipped: SetupDone, SetupFinishedAllSteps, SetupVersion 11,
                  mini-buddy flags off
  activation      mobileactivationd data ark seeded; the actual Activated state
                  comes from its Hactivation branch, selected by the /chosen
                  security-status properties in tools/mkadt.py
  network         picasso：SystemConfiguration preferences.plist 里的 "USB Ethernet"
                  服务（ue0 手动 192.168.7.2/24，网关 192.168.7.1，DNS 1.1.1.1 8.8.8.8）；
                  已有 preferences.plist 时不动
"""
import glob, os, plistlib, shutil, sys

BUILD, VERSION = '20H392', '16.7.16'
MOBILE = 501


def load(path):
    with open(path, 'rb') as f:
        return plistlib.load(f)


def save(path, data, uid, gid, mode, fmt=plistlib.FMT_BINARY):
    with open(path, 'wb') as f:
        plistlib.dump(data, f, fmt=fmt)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def merge_pref(root, name, values):
    """Merge keys into /var/mobile/Library/Preferences/NAME.plist (mobile, 0600)."""
    path = os.path.join(root, 'private/var/mobile/Library/Preferences', name + '.plist')
    data = load(path) if os.path.exists(path) else {}
    data.update(values)
    save(path, data, MOBILE, MOBILE, 0o600)
    print('pref', name, sorted(values))


def launchd(root):
    path = os.path.join(root, 'System/Library/xpc/launchd.plist')
    st = os.stat(path)
    plist = load(path)
    jobs = plist['LaunchDaemons']
    renamed = 0
    for job in jobs.values():
        if '_PanicOnCrash' in job:
            job['_PanicOnCrasX'] = job.pop('_PanicOnCrash')
            renamed += 1
    removed = [k for k, j in jobs.items() if j.get('Label') == 'com.apple.CommCenter']
    for k in removed:
        del jobs[k]
    save(path, plist, st.st_uid, st.st_gid, st.st_mode & 0o7777)
    print(f'launchd.plist: {renamed} _PanicOnCrash renamed, {len(removed)} CommCenter jobs removed')


def containers(root):
    base = os.path.join(root, 'private/var/containers')
    for sub, uid in (('Bundle', 33), ('Data/System', 0), ('Shared/SystemGroup', 0), ('Temp', 0)):
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            os.makedirs(d)
            os.chown(d, uid, uid)
            os.chmod(d, 0o755)
            print('mkdir', d)


def find_container(root, kind, identifier):
    for meta in glob.glob(os.path.join(root, 'private/var/containers', kind, '*',
                                       '.com.apple.mobile_container_manager.metadata.plist')):
        if load(meta).get('MCMMetadataIdentifier') == identifier:
            return os.path.dirname(meta)
    return None


def activation(root):
    c = find_container(root, 'Data/System', 'com.apple.mobileactivationd')
    if c is None:
        print('warning: no mobileactivationd system container in the image; skipped data ark')
        return
    d = os.path.join(c, 'Library/internal')
    os.makedirs(d, exist_ok=True)
    os.chown(d, MOBILE, MOBILE)
    os.chmod(d, 0o755)
    save(os.path.join(d, 'data_ark.plist'), {
        '-ActivationState': 'Activated', '-BrickState': False,
        '-BuildVersion': BUILD, '-ProductVersion': VERSION,
        '-DataMigrationCompleted': True, '-PreSydroDataMigrationCompleted': True,
    }, MOBILE, MOBILE, 0o600)
    print('data ark', d)


def audio(root):
    src = os.path.join(root, 'Library/Audio/Plug-Ins/HAL/VirtualAudio.plugin')
    dst_dir = os.path.join(root, 'Library/Audio/Plug-Ins/HAL.disabled')
    if os.path.exists(src):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, dst_dir)
        print('VirtualAudio.plugin disabled')
    for conf in glob.glob(os.path.join(root, 'System/Library/Frameworks/MediaToolbox.framework/*/SubServerConfiguration.plist')):
        st = os.stat(conf)
        data = load(conf)
        data['StartRecorderServer'] = False
        save(conf, data, st.st_uid, st.st_gid, st.st_mode & 0o7777, plistlib.FMT_XML)
        print('StartRecorderServer off', os.path.relpath(conf, root))


# picasso：USB 网卡 ue0 的网络服务（内核补丁 0086 让 configd 看见 ue0 之后才起作用）。
# 不写的话 configd 会给新的以太网接口默认用 DHCP；电脑那边没有 DHCP 服务器，
# 手机内核设好的 192.168.7.2 也可能被清掉。这里手动配成 192.168.7.2/24，网关是电脑 192.168.7.1，
# 电脑上开 NAT 后手机就能上网（tools/sweet-usbnet.sh）。键名照 configd 的 genSCPreferences.c。
USBNET_SET = '9E1C5B2A-0F3D-4C7E-9A61-5357454E4554'
USBNET_SERVICE = '6A2D9C4F-8B1E-4F35-A7D0-5553424E4554'
def network(root):
    path = os.path.join(root, 'private/var/preferences/SystemConfiguration/preferences.plist')
    if os.path.exists(path):
        print('network: preferences.plist exists, left alone')
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    service = {
        'UserDefinedName': 'USB Ethernet',
        'Interface': {'DeviceName': 'ue0', 'Hardware': 'Ethernet', 'Type': 'Ethernet',
                      'UserDefinedName': 'USB Ethernet'},
        'IPv4': {'ConfigMethod': 'Manual', 'Addresses': ['192.168.7.2'],
                 'SubnetMasks': ['255.255.255.0'], 'Router': '192.168.7.1'},
        'IPv6': {'ConfigMethod': 'Automatic'},
        'DNS': {'ServerAddresses': ['1.1.1.1', '8.8.8.8']},
        'Proxies': {'ExceptionsList': ['*.local', '169.254/16'], 'FTPPassive': 1},
    }
    prefs = {
        '__VERSION__': 20191120,     # NETWORK_CONFIGURATION_VERSION
        'CurrentSet': '/Sets/' + USBNET_SET,
        'NetworkServices': {USBNET_SERVICE: service},
        'Sets': {USBNET_SET: {
            'UserDefinedName': 'Automatic',
            'Network': {
                'Global': {'IPv4': {'ServiceOrder': [USBNET_SERVICE]}},
                'Service': {USBNET_SERVICE: {'__LINK__': '/NetworkServices/' + USBNET_SERVICE}},
            },
        }},
        'System': {'System': {'ComputerName': 'iPhone', 'HostName': 'iPhone'}},
    }
    save(path, prefs, 0, 0, 0o644, plistlib.FMT_XML)
    print('network: USB Ethernet ue0 192.168.7.2/24 via 192.168.7.1')


# picasso（2026-10-31 真机日志 logs/boot-k5.txt）：这些反复崩溃或没完没了重启，唤醒 ReportCrash/launchd、
# 抢 CPU，系统因此更卡：bluetoothd 80 次、fairplayd.H2 81 次、三个语音合成扩展各 12 次（各崩 36 次）、
# assetsd 崩溃循环、driverkitd 被杀。另外照 ios-sapphire tools/sapphire-clean-base.sh 和它 2026-10-23 的
# NOTES：wifip2pd（没有 AWDL）、coreduetd/aggregated/dasd（时钟从 1970 跳到 2026 后各占满一个核）、
# vmd（等基带）、BTAudioHALPlugin（mediaserverd 每秒约 110 次重连 bluetoothd，让 launchd 空转）。
# 在镜像里离线做：从 launchd.plist 删掉这些任务、把扩展和 HAL 插件挪到 *.disabled，等硬件能用了再挪回来。
CLEAN_JOBS = ('com.apple.bluetoothd', 'com.apple.fairplayd.H2', 'com.apple.wifip2pd', 'com.apple.assetsd',
              'com.apple.coreduetd', 'com.apple.aggregated', 'com.apple.dasd', 'com.apple.voicemail.vmd',
              'com.apple.driverkitd')
CLEAN_EXTENSIONS = (
    'System/Library/CoreServices/AegirProxyApp.app/PlugIns/AegirPoster.appex',
    'System/Library/PrivateFrameworks/EmojiPoster.framework/PlugIns/EmojiPosterExtension.appex',
    'System/Library/PrivateFrameworks/MacinTalk.framework/PlugIns/MacinTalkAUSP.appex',
    'System/Library/PrivateFrameworks/SiriTTSService.framework/PlugIns/SiriTTSSynthesizerAU.appex',
    'System/Library/PrivateFrameworks/TextToSpeechKonaSupport.framework/PlugIns/KonaSynthesizer.appex',
    'System/Library/PrivateFrameworks/MobileTimer.framework/PlugIns/MobileTimerIntents.appex',
    'System/Library/PrivateFrameworks/CalendarWidget.framework/PlugIns/IntentsExtension.appex',
    'Applications/Music.app/PlugIns/MusicWidgets.appex',
    'Applications/Home.app/PlugIns/HomeWidget.appex',
    'Applications/Home.app/PlugIns/HomeWidgetSingleAccessoryIntent.appex',
    'Applications/Books.app/PlugIns/BooksWidgetExtension.appex',
    'Applications/Weather.app/PlugIns/WeatherWidget.appex',
    'Applications/AppleTV.app/PlugIns/TVWidgetExtension.appex',
    'Applications/Stocks.app/PlugIns/StocksDetailIntents.appex',
    'Applications/Passbook.app/PlugIns/PassbookWidgetsExtension-iPhone.appex',
    'Applications/AppStore.app/PlugIns/AppStoreWidgetsExtension.appex',
    'System/Library/AppPlaceholders/Music.app/PlugIns/MusicWidgets.appex',
    'System/Library/AppPlaceholders/Home.app/PlugIns/HomeWidget.appex',
    'System/Library/AppPlaceholders/Home.app/PlugIns/HomeWidgetSingleAccessoryIntent.appex',
    'System/Library/AppPlaceholders/Books.app/PlugIns/BooksWidgetExtension.appex',
    'System/Library/AppPlaceholders/Weather.app/PlugIns/WeatherWidget.appex',
    'System/Library/AppPlaceholders/AppleTV.app/PlugIns/TVWidgetExtension.appex',
    'System/Library/AppPlaceholders/Stocks.app/PlugIns/StocksDetailIntents.appex',
    'System/Library/AppPlaceholders/Passbook.app/PlugIns/PassbookWidgetsExtension-iPhone.appex',
)


def cleanbase(root):
    path = os.path.join(root, 'System/Library/xpc/launchd.plist')
    st = os.stat(path)
    plist = load(path)
    jobs = plist['LaunchDaemons']
    removed = [k for k, j in jobs.items() if j.get('Label') in CLEAN_JOBS]
    for k in removed:
        del jobs[k]
    save(path, plist, st.st_uid, st.st_gid, st.st_mode & 0o7777)
    print(f'clean base: {len(removed)} launchd jobs removed')
    store = os.path.join(root, 'private/var/mobile/disabled-extensions')
    moved = 0
    for rel in CLEAN_EXTENSIONS:
        src = os.path.join(root, rel)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(store, rel)      # 原路径保留在 store 里，挪回去时一目了然
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        moved += 1
    print(f'clean base: {moved} crash-looping extensions moved to /private/var/mobile/disabled-extensions')
    hal = os.path.join(root, 'System/Library/Audio/Plug-Ins/HAL/BTAudioHALPlugin.driver')
    if os.path.exists(hal):
        dst_dir = os.path.join(root, 'System/Library/Audio/Plug-Ins/HAL.disabled')
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(hal, dst_dir)
        print('BTAudioHALPlugin.driver disabled')


def main():
    if len(sys.argv) != 2 or not os.path.isdir(os.path.join(sys.argv[1], 'System/Library/xpc')):
        sys.exit(__doc__)
    root = sys.argv[1]
    launchd(root)
    cleanbase(root)
    containers(root)
    activation(root)
    audio(root)
    network(root)
    merge_pref(root, 'com.apple.migration', {
        'LastSystemVersion': BUILD,
        'DMUserDataDisposition': {'SyntheticDidUpgrade': False, 'buildVersion': BUILD}})
    merge_pref(root, 'com.apple.springboard', {
        'BKNoWatchdogs': True,
        'FBLaunchWatchdogExceptions': {'com.apple.purplebuddy': 100.0},
        'MCForceShowCloudConfigurationUI': False})
    merge_pref(root, 'com.apple.purplebuddy', {
        'SetupDone': True, 'SetupFinishedAllSteps': True, 'SetupVersion': 11,
        'daemonizeNeedsToRun': False})
    merge_pref(root, 'com.apple.purplebuddy.notbackedup', {
        k: False for k in ('ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy',
                           'ShouldRunKeychainSyncMiniBuddy', 'ShouldRunMesaMiniBuddy',
                           'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy',
                           'SilentICDPUpgrade')})
    merge_pref(root, 'com.apple.managedconfiguration.notbackedup', {
        'LockdownActivationIndicatesCloudConfigurationAvailable': False})


if __name__ == '__main__':
    main()
