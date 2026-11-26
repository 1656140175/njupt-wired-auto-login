#!/usr/bin/env bash
# CampusPi 一键安装脚本（Raspberry Pi OS，需 root）
# 自动完成：环境检查 / 依赖安装 / 网络配置 / 时区时间 / 服务注册 / 自检
# 用法: sudo bash install.sh
set -euo pipefail

error_handler() {
  echo "" >&2
  echo "========================================" >&2
  echo "安装失败（第 $1 行）" >&2
  echo "命令: $2" >&2
  echo "请检查网络/磁盘后重试，或截图反馈" >&2
  echo "========================================" >&2
}
trap 'error_handler $LINENO "$BASH_COMMAND"' ERR

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行: sudo bash install.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/campuspi"

# 国内镜像源（默认清华 TUNA，可切换）
#   sudo MIRROR_URL=https://mirrors.aliyun.com bash install.sh
#   sudo MIRROR_URL=https://mirrors.ustc.edu.cn bash install.sh
MIRROR_URL="${MIRROR_URL:-https://mirrors.tuna.tsinghua.edu.cn}"

# ---------------- EOL 系统检测与修复 ----------------
# Raspbian Buster 及更早已停止维护（官方源/国内镜像均已下架 buster 包，
# 会导致 apt 404 无法安装），自动切换到 archive 存档源。
CODENAME=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release 2>/dev/null)
PRETTY=$(sed -n 's/^PRETTY_NAME="*\([^"]*\)"*/\1/p' /etc/os-release 2>/dev/null)
IS_EOL=0
case "$CODENAME" in
  ""|bookworm|trixie|sid|bullseye) ;;   # 维护中：不需要处理
  *) IS_EOL=1 ;;
esac
echo "==> 系统: ${PRETTY:-未知}（$CODENAME）"
if [ "$IS_EOL" -eq 1 ]; then
  echo "  检测到 EOL 系统（$CODENAME 已停止维护），修复软件源为 archive 存档源..."
  cp -a /etc/apt/sources.list /etc/apt/campuspi-mirror.bak 2>/dev/null || true
  # 1) Raspbian 主仓库（hostapd/dnsmasq 等）-> archive.raspbian.org（官方存档，长期保留）
  cat > /etc/apt/sources.list <<EOF
deb http://archive.raspbian.org/raspbian $CODENAME main contrib non-free
EOF
  # 2) 树莓派专用包 -> archive.raspberrypi.org（官方存档）
  if [ -f /etc/apt/sources.list.d/raspi.list ]; then
    sed -i "s|^deb .*|deb http://archive.raspberrypi.org/debian $CODENAME main|" \
      /etc/apt/sources.list.d/raspi.list
  else
    echo "deb http://archive.raspberrypi.org/debian $CODENAME main" \
      > /etc/apt/sources.list.d/raspi.list
  fi
  # 3) 禁用失效的第三方源（TeamViewer/VSCode 等无 buster 包，且缺公钥导致报错）
  for f in /etc/apt/sources.list.d/*.list; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in
      raspi.list|raspi*.list) continue ;;
    esac
    if ! grep -qE "raspbian|raspberrypi|deb\.debian\.org|archive\.debian" "$f"; then
      mv "$f" "$f.disabled" && echo "  已禁用无效第三方源: $(basename "$f")（备份为 .disabled）"
    fi
  done
  echo "  存档源已就绪（原配置备份: /etc/apt/campuspi-mirror.bak）"
else
  echo "==> 配置国内软件源（$MIRROR_URL）"
  # 已配置国内源（含清华/阿里/中科大/华为/网易）则跳过，避免重复替换
  if grep -rqE "mirrors\.(tuna|aliyun|ustc|huaweicloud|163)" \
       /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
    echo "  检测到已配置国内源，跳过"
  else
    BACKUP="/etc/apt/campuspi-mirror.bak"
    cp -a /etc/apt/sources.list "$BACKUP" 2>/dev/null || true
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list \
             /etc/apt/sources.list.d/*.sources; do
      [ -f "$f" ] || continue
      # 域名级替换：保留 signed-by/suite 等其余配置（兼容 deb822 格式）
      sed -i \
        -e "s|http://deb\.debian\.org/debian|$MIRROR_URL/debian|g" \
        -e "s|https://deb\.debian\.org/debian|$MIRROR_URL/debian|g" \
        -e "s|http://archive\.raspberrypi\.org/debian|$MIRROR_URL/raspberrypi|g" \
        -e "s|http://security\.debian\.org/debian-security|$MIRROR_URL/debian-security|g" \
        "$f" 2>/dev/null || true
    done
    echo "  已切换镜像（原配置备份: $BACKUP）"
  fi
fi

echo "==> 环境检查"
# 1) Python3（Raspberry Pi OS 自带，兜底安装）
if ! command -v python3 >/dev/null 2>&1; then
  echo "  未找到 python3，先安装"
  apt-get install -y python3
fi
echo "  python3: $(python3 --version 2>&1)"

# 2) 磁盘空间（hostapd/dnsmasq 等约需 200MB）
FREE_MB=$(df -m / 2>/dev/null | awk 'NR==2{print $4}')
echo "  根分区可用: ${FREE_MB:-?}MB"
if [ -n "${FREE_MB:-}" ] && [ "$FREE_MB" -lt 300 ]; then
  echo "  磁盘空间不足（需 >=300MB），请先清理或扩容" >&2
  exit 1
fi

echo "==> 更新软件源并安装依赖 (hostapd dnsmasq ntpdate iptables iw)"
apt-get update -qq || echo "  (apt update 未成功，继续尝试安装)"
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  hostapd dnsmasq ntpdate iptables iw \
  || { echo "  依赖安装失败，请检查网络后重新运行" >&2; exit 1; }

echo "==> 释放 hostapd 默认屏蔽（Debian 系默认 masked）"
systemctl unmask hostapd 2>/dev/null || true

echo "==> 关键组件版本（确认装的是源里最新版）"
echo "  $(hostapd -v 2>&1 | head -1)"
echo "  $(dnsmasq --version 2>/dev/null | head -1)"
echo "  python3 $(python3 --version 2>&1 | sed 's/Python //')"

# 校园网接口 DHCP 二选一：
#   A) dhcpcd 可用（Bullseye 及更早）-> 由它管理 eth0
#   B) 不可用（Bookworm 起默认 NetworkManager）-> 保留 NM 但只让它管 eth0
echo "==> 配置校园网接口 DHCP（dhcpcd 或 NetworkManager 二选一）"
if apt-get install -y dhcpcd 2>/dev/null; then
  echo "  使用 dhcpcd 管理 eth0"
  if ! grep -q '^allowinterfaces eth0' /etc/dhcpcd.conf 2>/dev/null; then
    echo 'denyinterfaces *' >> /etc/dhcpcd.conf
    echo 'allowinterfaces eth0' >> /etc/dhcpcd.conf
  fi
  systemctl enable --now dhcpcd 2>/dev/null || true
  if systemctl list-unit-files --all 2>/dev/null | grep -q '^NetworkManager'; then
    systemctl stop NetworkManager 2>/dev/null || true
    systemctl disable NetworkManager 2>/dev/null || true
  fi
else
  echo "  dhcpcd 不可用，保留 NetworkManager（仅管理 eth0）"
  mkdir -p /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/campuspi-unmanage.conf <<'EOF'
[keyfile]
unmanaged-devices=interface-name:wlan0;interface-name:eth1;interface-name:eth2;interface-name:usb0;interface-name:usb1;interface-name:enx*;interface-name:wlx*
EOF
  systemctl enable --now NetworkManager 2>/dev/null || true
fi

echo "==> 设置时区与时间同步（树莓派无 RTC，静默时段依赖本地时间）"
timedatectl set-timezone Asia/Shanghai 2>/dev/null || true
systemctl enable --now systemd-timesyncd 2>/dev/null || true
echo "  时区: $(timedatectl show -p Timezone --value 2>/dev/null || echo 未知)"

echo "==> 检测硬件"
# USB Wi-Fi 网卡芯片检测（部分 Realtek 做热点需 dkms 驱动）
if command -v lsusb >/dev/null 2>&1 && lsusb 2>/dev/null | grep -qiE 'realtek|8188|8812|8821|rtl8'; then
  echo "  检测到 Realtek USB 无线网卡（rtl8188/8812 系做热点可能需要驱动）"
  echo "  如热点无法启动，请执行: sudo apt install realtek-rtl88xxau-dkms"
else
  echo "  无线网卡: 未检测到 Realtek 芯片（或 lsusb 不可用）"
fi
# 校园网接口
if ip link show eth0 >/dev/null 2>&1; then
  echo "  校园网接口 eth0: 存在"
else
  echo "  ! 未检测到 eth0（接口名可能不同），"
  echo "    安装后在 Web 后台把'校园网网卡'改成实际接口名（可用 ip link 查看）"
fi

echo "==> 部署程序到 $APP_DIR"
mkdir -p "$APP_DIR/web"
install -m 0644 "$SRC_DIR/main.py" "$SRC_DIR/config.py" "$SRC_DIR/utils.py" \
  "$SRC_DIR/portal.py" "$SRC_DIR/network.py" "$SRC_DIR/hotspot.py" \
  "$SRC_DIR/webui.py" "$SRC_DIR/nla.py" "$SRC_DIR/selftest.py" "$APP_DIR/"
install -m 0644 "$SRC_DIR/web/index.html" "$APP_DIR/web/"
# 一键脚本（Web 后台按钮调用）
install -m 0755 "$SRC_DIR/setup_rdp.sh" "$APP_DIR/"
install -m 0755 "$SRC_DIR/setup_tuning.sh" "$APP_DIR/"
install -m 0755 "$SRC_DIR/repair_desktop.sh" "$APP_DIR/"

echo "==> 安装 systemd 服务（开机自启）"
install -m 0644 "$SRC_DIR/campuspi.service" /etc/systemd/system/campuspi.service
systemctl daemon-reload
systemctl enable campuspi
# 已在运行时必须 restart，否则还是跑旧代码
systemctl restart campuspi || systemctl start campuspi

echo "==> 运行自检（可选）"
python3 "$APP_DIR/selftest.py" || true

echo ""
echo "========================================"
echo "安装完成！环境已自动配置："
echo "  - 依赖: hostapd/dnsmasq/ntpdate/iptables/iw"
echo "  - 时区: Asia/Shanghai + 自动校时"
echo "  - 自启: campuspi 服务已注册（开机自动运行）"
echo "========================================"
IP=$(ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
echo "  1) 浏览器打开 http://${IP:-<树莓派eth0 IP>}:8080"
echo "     （Windows 电脑插上 USB 网卡线后可访问 http://192.168.9.1:8080）"
echo "  2) 填写校园网账号/密码、选择运营商（校园用户/移动@cmcc/电信@njxy/联通@lt，"
echo "     以后缀以 a79.htm 登录页实际显示为准），保存"
echo "  3) 设置热点 SSID 和密码，保存"
echo "  日志: journalctl -u campuspi -f  或  /var/log/campuspi.log"
echo "  卸载/重装: sudo systemctl disable --now campuspi"
