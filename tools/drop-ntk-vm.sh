set -euo pipefail
W=$HOME/iosroot; cd "$W"
sudo mkdir -p /mnt/iosroot
sudo mount -t hfsplus -o loop,force ios16-root.img /mnt/iosroot
trap 'cd /; sudo umount /mnt/iosroot 2>/dev/null || true' EXIT
sudo cat /mnt/iosroot/System/Library/xpc/launchd.plist > /tmp/lp.in
python3 - <<PY
import plistlib
p=plistlib.load(open('/tmp/lp.in','rb')); d=p['LaunchDaemons']
gone=[k for k,j in d.items() if j.get('Label')=='com.apple.nanotimekitcompaniond']
for k in gone: del d[k]
plistlib.dump(p,open('/tmp/lp.out','wb'),fmt=plistlib.FMT_BINARY)
print('removed',len(gone),'job(s); now',len(d),'jobs')
PY
sudo cp /tmp/lp.out /mnt/iosroot/System/Library/xpc/launchd.plist
sudo sync
cd /; sudo umount /mnt/iosroot; trap - EXIT
cd "$W"
sudo fsck.hfsplus -fy ios16-root.img | tail -2 || true
rm -f ios16-root-gpu.simg
img2simg ios16-root.img ios16-root-gpu.simg
cp ios16-root-gpu.simg /Users/yancy/Desktop/小米K30-5G-IOS/src/ios-picasso/research/ios16-root-gpu.simg
sha256sum /Users/yancy/Desktop/小米K30-5G-IOS/src/ios-picasso/research/ios16-root-gpu.simg
echo DROP-DONE
