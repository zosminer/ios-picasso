#!/bin/bash
# 在 Lima 虚拟机里把 GPU 用户态（Mesa、CoreAnimation 桥、两个测试程序）放进已有的 iOS 根镜像。
# 镜像是 research/build-root-vm.sh 做好的 ~/iosroot/ios16-root.img（已带 cleanbase）。
# 用法：limactl shell iosbuild -- bash /Users/yancy/Desktop/小米K30-5G-IOS/src/ios-picasso/research/add-gpu-userland-vm.sh
# 做完把 research/ios16-root-gpu.simg 刷进 userdata（用户已同意，会清空手机上现有的 iOS 数据）。
set -euo pipefail
REPO=/Users/yancy/Desktop/小米K30-5G-IOS/src/ios-picasso
W=$HOME/iosroot
U=$REPO/sweet-userland
cd "$W"
for f in lib/libOSMesa.8.dylib lib/libsweet-gl.dylib lib/libsweet-ca.dylib sweet-gpu-test sweet-gl-test; do
	[ -f "$U/$f" ] || { echo "缺 $U/$f"; exit 1; }
done
sudo modprobe hfsplus
sudo mkdir -p /mnt/iosroot
mountpoint -q /mnt/iosroot && sudo umount /mnt/iosroot
echo "== fsck (before)"
sudo fsck.hfsplus -fy ios16-root.img || true
sudo mount -t hfsplus -o loop,force ios16-root.img /mnt/iosroot
trap 'sudo umount /mnt/iosroot 2>/dev/null || true' EXIT
M=/mnt/iosroot
[ -f "$M/System/Library/xpc/launchd.plist" ] || { echo "不是 iOS 根"; exit 1; }
sudo mkdir -p "$M/usr/local/lib" "$M/usr/local/bin"
for lib in libOSMesa.8.dylib libsweet-gl.dylib libsweet-ca.dylib; do
	sudo install -m 0755 -o 0 -g 0 "$U/lib/$lib" "$M/usr/local/lib/$lib"
	echo "added /usr/local/lib/$lib"
done
T=$(mktemp)
for pair in sweet-gpu-test:com.sweet.gputest sweet-gl-test:com.sweet.gltest; do
	prog=${pair%%:*}; label=${pair#*:}
	sudo install -m 0755 -o 0 -g 0 "$U/$prog" "$M/usr/local/bin/$prog"
	# python 以普通用户跑（root 写不了别人在 /tmp 里的临时文件）：先把 plist 拷出来，改完再 cp 回去（属主和权限不变）
	sudo cat "$M/System/Library/xpc/launchd.plist" > "$T.in"
	python3 "$REPO/tools/add-launchd-job.py" "$T.in" "$T" "$label" "/usr/local/bin/$prog" >/dev/null
	sudo cp "$T" "$M/System/Library/xpc/launchd.plist"
	echo "added /usr/local/bin/$prog (launchd job $label)"
done
rm -f "$T" "$T.in"
sudo sync
sudo umount /mnt/iosroot
trap - EXIT
echo "== fsck (after)"
sudo fsck.hfsplus -fy ios16-root.img || true
echo "== sparse"
img2simg ios16-root.img ios16-root-gpu.simg
cp ios16-root-gpu.simg "$REPO/research/ios16-root-gpu.simg"
ls -la "$REPO/research/ios16-root-gpu.simg"
sha256sum "$REPO/research/ios16-root-gpu.simg"
echo GPU-USERLAND-DONE
