#!/usr/bin/env bash
# Buster -> Bullseye 原地升级脚本（Raspberry Pi OS）
# 免读卡器/显示器/键盘：SSH 运行即可。
# 原理：把 EOL 的 buster 存档源换成维护中的 bullseye 源，然后 apt 大版本升级。
#
# 用法: sudo bash upgrade_bullseye.sh
# 注意:
#   - 升级过程约 30-60 分钟，SSH 会在重启时断开，重启后重新连接即可
#   - 升级前自动备份 /etc/apt/sources*，失败可回退
#   - 升级中途切勿断电！建议插着电源线操作
set -euo pipefail

error_handler() {
  echo "升级失败（第 $1 行）: $2" >&2
  echo "可尝试恢复备份: sudo cp /root/apt-sources-backup/* /etc/apt/ 2>/dev/null || true" >&2
}
trap 'error_handler $LINENO "$BASH_COMMAND"' ERR

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行: sudo bash upgrade_bullseye.sh" >&2
  exit 1
fi

CODENAME=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release 2>/dev/null)
if [ "$CODENAME" != "buster" ]; then
  echo "当前系统是 $CODENAME，不是 buster，无需本脚本（本脚本仅 buster -> bullseye）" >&2
  exit 1
fi

echo "========================================"
echo "  Buster -> Bullseye 原地升级"
echo "  升级期间请勿断电！SSH 重启后会断开"
echo "========================================"

echo "==> 1/6 备份软件源配置"
mkdir -p /root/apt-sources-backup
cp -a /etc/apt/sources.list /root/apt-sources-backup/ 2>/dev/null || true
cp -a /etc/apt/sources.list.d /root/apt-sources-backup/ 2>/dev/null || true
echo "  已备份到 /root/apt-sources-backup/"

echo "==> 2/6 切换软件源到 bullseye（清华镜像）"
# 国内镜像（默认清华；可切换: sudo MIRROR_URL=https://mirrors.aliyun.com bash upgrade_bullseye.sh）
MIRROR_URL="${MIRROR_URL:-https://mirrors.tuna.tsinghua.edu.cn}"
# Raspbian 主仓库（海量软件包，走国内镜像）
cat > /etc/apt/sources.list <<EOF
deb $MIRROR_URL/raspbian/raspbian bullseye main contrib non-free rpi
EOF
# 树莓派专属仓库（官方 archive.raspberrypi.org/debian 对应清华 /raspberrypi，
# 国内镜像同样很快，避免官方源几天下不动）
if [ -f /etc/apt/sources.list.d/raspi.list ]; then
  sed -i "s|^deb .*|deb $MIRROR_URL/raspberrypi bullseye main|" \
    /etc/apt/sources.list.d/raspi.list
else
  echo "deb $MIRROR_URL/raspberrypi bullseye main" \
    > /etc/apt/sources.list.d/raspi.list
fi
echo "  sources.list / raspi.list 已切换到 bullseye（$MIRROR_URL）"

echo "==> 3/6 更新包索引（首次走新源，可能较慢）"
apt-get update -qq || echo "  (apt update 有警告，继续尝试)"

echo "==> 4/6 升级现有软件包到 bullseye"
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends

echo "==> 5/6 大版本升级（dist-upgrade，核心步骤）"
DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y --no-install-recommends

echo "==> 6/6 清理并提示重启"
apt-get autoremove -y 2>/dev/null || true
echo ""
echo "========================================"
echo "升级完成！即将重启（15 秒后自动执行，Ctrl+C 可取消）"
echo "重启后检查: cat /etc/os-release | grep PRETTY"
echo "重启后如要继续安装 CampusPi: sudo bash install.sh"
echo "========================================"
sleep 15
reboot
