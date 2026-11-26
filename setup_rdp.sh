#!/usr/bin/env bash
# ============================================================
#  CampusPi 一键安装/启动 RDP 远程桌面（xrdp）
#
#  用法（树莓派上）：
#      sudo bash /opt/campuspi/setup_rdp.sh
#  或在 Web 后台点「一键安装/启动 RDP」。
#
#  行为：
#    - 安装 xrdp + xorgxrdp（已装则跳过）
#    - 检测桌面会话（Raspberry Pi OS 桌面 / LXDE / XFCE）
#      没有桌面时安装轻量 XFCE（需要能上网，约 1-3 分钟）
#    - 为每个普通用户写 ~/.xsession（已有则保留）
#    - iptables 限制 3389 只允许 PC 有线/热点子网，不暴露到校园网
#    - 开机自启并立即启动 xrdp
#
#  注意：登录用树莓派系统账号/密码（不是 Web 后台密码）。
# ============================================================
set -u

SCRIPT_PATH=$(readlink -f "$0" 2>/dev/null || echo "$0")
if [ -x /opt/campuspi/setup_rdp.sh ]; then
    SERVICE_SCRIPT=/opt/campuspi/setup_rdp.sh
else
    SERVICE_SCRIPT=$SCRIPT_PATH
fi

log() { echo "[$(date '+%F %T')] $*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo/root 运行：sudo bash $0" >&2
    exit 1
fi

# 只应用防火墙（供开机自启服务调用）
apply_firewall() {
    if command -v iptables >/dev/null 2>&1; then
        iptables -N CAMPUSPI-RDP 2>/dev/null || true
        iptables -F CAMPUSPI-RDP 2>/dev/null || true
        iptables -A CAMPUSPI-RDP -p tcp --dport 3389 \
            -s 192.168.9.0/24 -j ACCEPT
        iptables -A CAMPUSPI-RDP -p tcp --dport 3389 \
            -s 192.168.10.0/24 -j ACCEPT
        iptables -A CAMPUSPI-RDP -j DROP
        iptables -C INPUT -p tcp --dport 3389 -j CAMPUSPI-RDP 2>/dev/null || \
            iptables -I INPUT 1 -p tcp --dport 3389 -j CAMPUSPI-RDP
        log "IPv4 防火墙：3389 仅允许 192.168.9.0/24 与 192.168.10.0/24"
    else
        log "未找到 iptables，跳过 RDP 防火墙限制（请自行确认 3389 未暴露）"
    fi
    # xrdp 同时监听 IPv6；PC/热点用 IPv4 即可，v6 3389 一律封死
    if command -v ip6tables >/dev/null 2>&1; then
        ip6tables -N CAMPUSPI-RDP6 2>/dev/null || true
        ip6tables -F CAMPUSPI-RDP6 2>/dev/null || true
        ip6tables -A CAMPUSPI-RDP6 -j DROP
        ip6tables -C INPUT -p tcp --dport 3389 -j CAMPUSPI-RDP6 \
            2>/dev/null || \
            ip6tables -I INPUT 1 -p tcp --dport 3389 -j CAMPUSPI-RDP6
        log "IPv6 防火墙：3389 已全部拒绝"
    fi
}

if [ "${1:-}" = "--firewall-only" ]; then
    apply_firewall
    exit 0
fi

apt_install() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || log "apt-get update 失败，继续尝试安装"
    apt-get install -y --no-install-recommends "$@"
}

# ------------------------------------------------------------ 1. xrdp
if ! command -v xrdp >/dev/null 2>&1; then
    log "安装 xrdp + xorgxrdp ..."
    if ! apt_install xrdp xorgxrdp; then
        log "xrdp 安装失败，请检查网络/软件源后重试"
        exit 1
    fi
fi
if ! command -v xrdp >/dev/null 2>&1; then
    log "安装后仍找不到 xrdp，退出"
    exit 1
fi
log "xrdp 已安装：$(command -v xrdp)"

# ------------------------------------------------------------ 2. 桌面会话
SESSION=""
for cmd in startlxde-pi startlxde startxfce4 mate-session startplasma-x11; do
    if command -v "$cmd" >/dev/null 2>&1; then
        SESSION="$cmd"
        break
    fi
done

if [ -z "$SESSION" ]; then
    FREE_MB=$(df -m / 2>/dev/null | awk 'NR==2{print $4}')
    if [ -n "${FREE_MB:-}" ] && [ "$FREE_MB" -lt 800 ] 2>/dev/null; then
        log "根分区可用 ${FREE_MB}MB，安装桌面至少需要约 800MB"
        log "请先清理空间，或手动安装桌面后重跑本脚本"
        exit 1
    fi
    log "未检测到桌面环境，安装轻量 XFCE ..."
    if ! apt_install xfce4 xfce4-terminal dbus-x11; then
        log "XFCE 安装失败，请检查网络/软件源后重试"
        exit 1
    fi
    SESSION=startxfce4
fi
log "使用桌面会话：$SESSION"

# ------------------------------------------------------------ 3. ~/.xsession
WROTE=0
while IFS=: read -r user _ uid _ _ home shell; do
    [ -n "${user:-}" ] || continue
    case "${uid:-}" in ''|*[!0-9]*) continue;; esac
    [ "$uid" -ge 1000 ] 2>/dev/null || continue
    [ "$uid" -lt 60000 ] 2>/dev/null || continue
    case "${shell:-}" in *nologin|*false) continue;; esac
    [ -d "${home:-}" ] || continue

    xs="$home/.xsession"
    if [ -s "$xs" ]; then
        log "用户 $user 已有 ~/.xsession，保留原内容"
    else
        printf '#!/bin/sh\nexec %s\n' "$SESSION" > "$xs"
        chown "$user":"$(id -gn "$user" 2>/dev/null || echo "$user")" "$xs" \
            2>/dev/null || chown "$user" "$xs" 2>/dev/null || true
        chmod 755 "$xs" 2>/dev/null || true
        WROTE=$((WROTE + 1))
        log "已为用户 $user 配置桌面：$SESSION"
    fi

    # xrdp 走 PAM，账号没有可用密码时无法登录（不要在这里设置弱密码）
    if command -v passwd >/dev/null 2>&1; then
        st=$(passwd -S "$user" 2>/dev/null | awk '{print $2}')
        case "${st:-}" in
            L|LK) log "警告：$user 密码被锁定，RDP 无法登录；请执行 sudo passwd $user" ;;
            NP)   log "警告：$user 没有密码，RDP 无法登录；请执行 sudo passwd $user" ;;
        esac
    fi
done < /etc/passwd

if [ "$WROTE" -eq 0 ]; then
    log "没有写入新的 ~/.xsession（已有配置，或系统里没有普通用户）"
fi

# ------------------------------------------------------------ 4. 防火墙
# 只允许 PC 有线 / 热点两个子网访问 3389，校园网侧一律 DROP。
apply_firewall

# 开机后由独立服务重新应用（iptables 规则重启即丢）
if [ -d /etc/systemd/system ]; then
    cat > /etc/systemd/system/campuspi-rdp-firewall.service <<EOF
[Unit]
Description=CampusPi RDP firewall (LAN only)
After=network.target
Before=xrdp.service

[Service]
Type=oneshot
ExecStart=$SERVICE_SCRIPT --firewall-only
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable campuspi-rdp-firewall >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------ 5. 启动
systemctl enable xrdp >/dev/null 2>&1 || true
systemctl restart xrdp
sleep 1
if systemctl is-active --quiet xrdp; then
    log "xrdp 已启动（开机自启已启用）"
else
    log "xrdp 启动失败，最近日志："
    journalctl -u xrdp -n 30 --no-pager 2>/dev/null || true
    exit 1
fi

# ------------------------------------------------------------ 6. 提示
log "RDP 监听端口：3389"
log "登录用户（树莓派系统账号，密码为系统登录密码）："
awk -F: '$3>=1000 && $3<60000 && $7 !~ /(nologin|false)$/ {print "  - " $1}' \
    /etc/passwd 2>/dev/null || true
log "本机可用地址："
ip -4 -o addr show 2>/dev/null \
    | awk '$2 != "lo" {print "  - " $2 " " $4}' | sed 's#/[0-9]*##' || true
log "Windows 连接：mstsc /v:192.168.9.1"
log "或在 Web 后台点「下载 .rdp 连接文件」"
