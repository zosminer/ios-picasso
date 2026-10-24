#!/var/jb/bin/sh
# picasso（红米 K30 5G）一个干净的基底，在手机上以 root 跑（USB 网络/SSH 进去后 scp 过去再执行）：
#   sh picasso-clean-base.sh         关掉那些崩溃死循环、或没完没了重启的东西
#   sh picasso-clean-base.sh undo    全部还原
#
# 自从 0095（AMFI）系统能看到 app 扩展后，那些需要这台手机没有的东西（Metal、基带、蓝牙、语音素材、
# 安全隔区）的扩展会一遍遍崩溃：每次崩溃都唤醒 ReportCrash 和 launchd，开机两三分钟后手机就卡死、
# SSH 也进不去（sapphire 2026-10-23：语音合成器各崩 ~30 次、assetsd 16 次、MusicWidgets 11 次、
# AegirPoster 10 次、wifip2pd 一次开机重启 28 次）。
# 扩展移到 $B（origen.txt 记下原位置）；守护进程用 launchctl 禁用（进 launchd 的 disabled 列表，跨开机有效）。
# 两者都能 undo。需要某个且它能工作时，再一个个开回来。
# 源自 ios-sapphire tools/sapphire-clean-base.sh（HackiOS 基底，通用 iOS 16；路径按 Procursus rootless /var/jb）。
export PATH=/var/jb/usr/bin:/var/jb/bin:/var/jb/usr/sbin:/usr/bin:/bin:/usr/sbin
B=/private/var/mobile/disabled-extensions
L=/var/jb/usr/bin/launchctl

EXTENSIONS="
/System/Library/CoreServices/AegirProxyApp.app/PlugIns/AegirPoster.appex
/System/Library/PrivateFrameworks/EmojiPoster.framework/PlugIns/EmojiPosterExtension.appex
/System/Library/PrivateFrameworks/MacinTalk.framework/PlugIns/MacinTalkAUSP.appex
/System/Library/PrivateFrameworks/SiriTTSService.framework/PlugIns/SiriTTSSynthesizerAU.appex
/System/Library/PrivateFrameworks/TextToSpeechKonaSupport.framework/PlugIns/KonaSynthesizer.appex
/System/Library/PrivateFrameworks/MobileTimer.framework/PlugIns/MobileTimerIntents.appex
/System/Library/PrivateFrameworks/CalendarWidget.framework/PlugIns/IntentsExtension.appex
/Applications/Music.app/PlugIns/MusicWidgets.appex
/Applications/Home.app/PlugIns/HomeWidget.appex
/Applications/Home.app/PlugIns/HomeWidgetSingleAccessoryIntent.appex
/Applications/Books.app/PlugIns/BooksWidgetExtension.appex
/Applications/Weather.app/PlugIns/WeatherWidget.appex
/Applications/AppleTV.app/PlugIns/TVWidgetExtension.appex
/Applications/Stocks.app/PlugIns/StocksDetailIntents.appex
/Applications/Passbook.app/PlugIns/PassbookWidgetsExtension-iPhone.appex
/Applications/AppStore.app/PlugIns/AppStoreWidgetsExtension.appex
/System/Library/AppPlaceholders/Music.app/PlugIns/MusicWidgets.appex
/System/Library/AppPlaceholders/Home.app/PlugIns/HomeWidget.appex
/System/Library/AppPlaceholders/Home.app/PlugIns/HomeWidgetSingleAccessoryIntent.appex
/System/Library/AppPlaceholders/Books.app/PlugIns/BooksWidgetExtension.appex
/System/Library/AppPlaceholders/Weather.app/PlugIns/WeatherWidget.appex
/System/Library/AppPlaceholders/AppleTV.app/PlugIns/TVWidgetExtension.appex
/System/Library/AppPlaceholders/Stocks.app/PlugIns/StocksDetailIntents.appex
/System/Library/AppPlaceholders/Passbook.app/PlugIns/PassbookWidgetsExtension-iPhone.appex
"
# wifip2pd：AWDL / Wi-Fi Direct，这里没有这种射频模式；assetsd：照片库守护进程，崩溃死循环（Photos
# 要它，做 Photos 时再开回来）；coreduetd 和 aggregated（使用习惯学习、分析）：第一次网络授时把时钟从
# 1970 跳到 2026 后各占满一个核，手机变慢（这里没有东西需要它们）；dasd（后台任务调度）紧接着也一样占核。
DAEMONS="com.apple.wifip2pd com.apple.assetsd com.apple.coreduetd com.apple.aggregated com.apple.dasd"

if [ "${1:-}" = undo ]; then
	[ -f "$B/origen.txt" ] && while read -r path; do
		name=${path##*/}
		[ -n "$name" ] && [ -d "$B/$name" ] && [ ! -e "$path" ] && mv "$B/$name" "$path" && echo "back: $path"
	done < "$B/origen.txt"
	: > "$B/origen.txt"
	[ -f "$B/more/origen.txt" ] && while read -r file path; do
		[ -n "$file" ] && [ -d "$B/more/$file" ] && [ ! -e "$path" ] && mv "$B/more/$file" "$path" && echo "back: $path"
	done < "$B/more/origen.txt"
	[ -f "$B/more/origen.txt" ] && : > "$B/more/origen.txt"
	for d in $DAEMONS; do
		$L enable "system/$d" && echo "enabled: $d"
	done
	sync
	exit 0
fi

mkdir -p "$B"
for path in $EXTENSIONS; do
	name=${path##*/}
	[ -d "$path" ] || continue
	if [ -e "$B/$name" ]; then
		# 同名 bundle 来自别处（Applications 和 AppPlaceholders）：两个都留
		mkdir -p "$B/more"
		n=0
		while [ -e "$B/more/$n-$name" ]; do n=$((n + 1)); done
		mv "$path" "$B/more/$n-$name" && echo "$n-$name $path" >> "$B/more/origen.txt" && echo "off: $path"
	else
		mv "$path" "$B/$name" && echo "$path" >> "$B/origen.txt" && echo "off: $path"
	fi
done
for d in $DAEMONS; do
	$L disable "system/$d" 2>/dev/null
	$L bootout "system/$d" 2>/dev/null
	echo "disabled: $d"
done
sync
