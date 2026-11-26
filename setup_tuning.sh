#!/usr/bin/env bash
# ============================================================
#  CampusPi 转发延迟优化（树莓派当路由器时用）
#
#  用法：
#      sudo bash /opt/campuspi/setup_tuning.sh
#  或在 Web 后台点「一键优化延迟」。
#
#  做的事（全部幂等、失败自动跳过，不会把网络弄断）：
#    - 默认队列改为 fq_codel（治 bufferbloat / 负载下延迟暴涨）
#    - 校园网口 + PC 网口应用 fq_codel
#    - 关闭有线网卡 EEE（Energy Efficient Ethernet）
#    - 关闭热点 Wi-Fi 省电
#    - CPU governor 设为 performance（防降频抖动）
#    - 以太网口开启 RPS/RFS（USB 网卡收包分摊到多核）
#    - 注册 campuspi-tuning.service，开机自动重新应用
#
#  注意：会略微增加功耗/温度，树莓派请做好散热。
# ============================================================
set -u

SCRIPT_PATH=$(readlink -f "$0" 2>/dev/null || echo "$0")
# 开机服务优先指向部署目录，避免从 /tmp 运行时把服务指向临时文件
if [ -x /opt/campuspi/setup_tuning.sh ]; then
    SERVICE_SCRIPT=/opt/campuspi/setup_tuning.sh
else
    SERVICE_SCRIPT=$SCRIPT_PATH
fi
APPLIED=""

log() { echo "[$(date '+%F %T')] $*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo/root 运行：sudo bash $0" >&2
    exit 1
fi

# ------------------------------------------------------------ 读取接口
CAMPUS_IF=""
PC_IF=""
WLAN_IF=""
if command -v python3 >/dev/null 2>&1; then
    IFS='|' read -r CAMPUS_IF PC_IF WLAN_IF < <(python3 - <<'PYEOF' 2>/dev/null
import json
try:
    with open("/etc/campuspi/config.json", encoding="utf-8") as f:
        c = json.load(f)
except Exception:
    c = {}
campus = (c.get("campus") or {})
pc = (c.get("pc") or {})
hs = (c.get("hotspot") or {})
print("%s|%s|%s" % (campus.get("iface") or "",
                    pc.get("iface") or "",
                    hs.get("iface") or ""))
PYEOF
    )
fi
[ -n "${CAMPUS_IF:-}" ] || CAMPUS_IF=eth0
[ -n "${WLAN_IF:-}" ] || WLAN_IF=wlan0

# pc.iface 可能是 auto：自动找一个非校园网、非无线的物理以太网口
if [ "${PC_IF:-}" = "auto" ] || [ -z "${PC_IF:-}" ]; then
    PC_IF=""
    for n in /sys/class/net/*; do
        name=$(basename "$n")
        [ "$name" = "lo" ] && continue
        [ "$name" = "$CAMPUS_IF" ] && continue
        [ "$name" = "$WLAN_IF" ] && continue
        [ -d "$n/wireless" ] && continue
        [ -d "$n/device" ] || continue
        PC_IF="$name"
        break
    done
fi
log "接口：校园网=$CAMPUS_IF PC=${PC_IF:-未检测到} 热点=$WLAN_IF"

# ------------------------------------------------------------ 1. sysctl
if [ -d /etc/sysctl.d ]; then
    cat > /etc/sysctl.d/99-campuspi-latency.conf <<'EOF'
# CampusPi 转发延迟优化（由 setup_tuning.sh 生成）
net.core.default_qdisc=fq_codel
net.core.rps_sock_flow_entries=32768
EOF
    sysctl -p /etc/sysctl.d/99-campuspi-latency.conf >/dev/null 2>&1 \
        && APPLIED="$APPLIED sysctl" || true
fi

# ------------------------------------------------------------ 2. fq_codel
if command -v tc >/dev/null 2>&1; then
    for ifc in "$CAMPUS_IF" "${PC_IF:-}"; do
        [ -n "$ifc" ] || continue
        ip link show "$ifc" >/dev/null 2>&1 || continue
        if tc qdisc replace dev "$ifc" root fq_codel 2>/dev/null; then
            APPLIED="$APPLIED fq_codel:$ifc"
        fi
    done
fi

# ------------------------------------------------------------ 3. EEE off
if command -v ethtool >/dev/null 2>&1; then
    for ifc in "$CAMPUS_IF" "${PC_IF:-}"; do
        [ -n "$ifc" ] || continue
        if ethtool --set-eee "$ifc" eee off >/dev/null 2>&1; then
            APPLIED="$APPLIED eee:$ifc"
        fi
    done
fi

# ------------------------------------------------------------ 4. Wi-Fi 省电
if command -v iw >/dev/null 2>&1 && [ -d "/sys/class/net/$WLAN_IF" ]; then
    if iw dev "$WLAN_IF" set power_save off >/dev/null 2>&1; then
        APPLIED="$APPLIED wifi_ps:$WLAN_IF"
    fi
fi

# ------------------------------------------------------------ 5. CPU 调频
if ls /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor \
        >/dev/null 2>&1; then
    ok=0
    for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        if echo performance > "$f" 2>/dev/null; then
            ok=1
        fi
    done
    [ "$ok" = "1" ] && APPLIED="$APPLIED governor"
fi

# ------------------------------------------------------------ 6. RPS/RFS
CORES=$(nproc 2>/dev/null || echo 1)
case "$CORES" in
    ''|*[!0-9]*) CORES=1;;
esac
[ "$CORES" -ge 1 ] || CORES=1
MASK=$(printf '%x' $(( (1 << CORES) - 1 )) 2>/dev/null || echo f)

for ifc in "$CAMPUS_IF" "${PC_IF:-}"; do
    [ -n "$ifc" ] || continue
    [ -d "/sys/class/net/$ifc" ] || continue
    for q in /sys/class/net/"$ifc"/queues/rx-*/rps_cpus; do
        [ -e "$q" ] || continue
        if echo "$MASK" > "$q" 2>/dev/null; then
            APPLIED="$APPLIED rps:$ifc"
        fi
    done
    for q in /sys/class/net/"$ifc"/queues/rx-*/rps_flow_cnt; do
        [ -e "$q" ] || continue
        echo 4096 > "$q" 2>/dev/null || true
    done
done

# ------------------------------------------------------------ 7. 开机自启
if [ -d /etc/systemd/system ]; then
    cat > /etc/systemd/system/campuspi-tuning.service <<EOF
[Unit]
Description=CampusPi forwarding latency tuning
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$SERVICE_SCRIPT
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable campuspi-tuning >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------ 8. 状态/提示
mkdir -p /run 2>/dev/null || true
echo "applied:${APPLIED:- none}" > /run/campuspi-tuning.status 2>/dev/null || true

log "已应用：${APPLIED:-无（可能缺少 tc/ethtool/iw，或接口名不对）}"
if command -v vcgencmd >/dev/null 2>&1; then
    log "CPU 温度：$(vcgencmd measure_temp 2>/dev/null || echo '?')"
    log "降频标志：$(vcgencmd get_throttled 2>/dev/null || echo '?')（0x0 正常）"
fi
log "开机自启：campuspi-tuning.service"
log "提示：performance 调频会增加功耗/温度，请确认有散热片或风扇。"
