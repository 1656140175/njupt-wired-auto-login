#!/usr/bin/env bash
# ============================================================
#  CampusPi 桌面/任务栏自动修复
#
#  用法（树莓派上）：
#      sudo bash /opt/campuspi/repair_desktop.sh
#      RESET_PANEL=1 sudo bash /opt/campuspi/repair_desktop.sh  # 强制重置面板配置
#      DRY_RUN=1 bash /opt/campuspi/repair_desktop.sh           # 只检查不修改
#
#  覆盖：
#    - Raspberry Pi OS LXDE/PIXEL（lxpanel 任务栏）
#    - XFCE（xfce4-panel，RDP 安装的桌面）
#    - Bookworm Wayland（wf-panel-pi）
#
#  修复内容：
#    - 缺包时安装对应面板包
#    - 面板配置缺失/为空/损坏时从系统默认恢复（先备份）
#    - LXDE 自启动里缺 lxpanel 时补上（先备份）
#    - 当前 X11 会话里尝试重启任务栏
#    - Wayland 桌面只能修复文件，需注销/重启后生效
# ============================================================
set -u

RESET_PANEL="${RESET_PANEL:-0}"
DRY_RUN="${DRY_RUN:-0}"
FIXED=0

log() { echo "[$(date '+%F %T')] $*"; }

run_fix() {
    if [ "$DRY_RUN" = "1" ]; then
        log "[DRY_RUN] 将要执行: $*"
        return 0
    fi
    "$@"
}

if [ "$DRY_RUN" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo/root 运行：sudo bash $0" >&2
    exit 1
fi

# ------------------------------------------------------------ 目标用户
TARGET="${SUDO_USER:-}"
if [ -z "$TARGET" ] || [ "$TARGET" = "root" ]; then
    TARGET=$(loginctl list-sessions --no-legend 2>/dev/null \
        | awk '$4 == "seat0" {print $3; exit}')
fi
if [ -z "$TARGET" ] || [ "$TARGET" = "root" ]; then
    TARGET=$(awk -F: '$3 >= 1000 && $3 < 60000 \
        && $7 !~ /(nologin|false)$/ {print $1; exit}' /etc/passwd)
fi
if [ -z "$TARGET" ]; then
    log "找不到普通用户，无法定位桌面配置"
    exit 1
fi
HOME_DIR=$(getent passwd "$TARGET" | cut -d: -f6)
TARGET_GROUP=$(id -gn "$TARGET" 2>/dev/null || echo "$TARGET")
TARGET_UID=$(id -u "$TARGET" 2>/dev/null || echo 1000)
log "目标用户: $TARGET（uid=$TARGET_UID, home=$HOME_DIR）"

# 当前图形会话类型（x11 / wayland）
SID=$(loginctl list-sessions --no-legend 2>/dev/null \
    | awk -v u="$TARGET" '$3 == u {print $1; exit}')
STYPE=""
if [ -n "${SID:-}" ]; then
    STYPE=$(loginctl show-session "$SID" -p Type --value 2>/dev/null || true)
fi
log "图形会话: ${SID:-无} 类型: ${STYPE:-未知}"

backup_file() {
    local f="$1"
    [ -e "$f" ] || return 0
    local bak="$f.bak.$(date +%s)"
    cp -a "$f" "$bak" 2>/dev/null && log "已备份: $bak"
}

# 在用户当前 X11 会话里执行（仅本地 :0；RDP 会话需断开重连）
run_in_x11() {
    [ "$DRY_RUN" = "1" ] && { log "[DRY_RUN] 用户会话执行: $*"; return 0; }
    local auth=""
    if [ -f "$HOME_DIR/.Xauthority" ]; then
        auth="export XAUTHORITY=$HOME_DIR/.Xauthority;"
    fi
    su - "$TARGET" -c \
        "export DISPLAY=:0; export XDG_RUNTIME_DIR=/run/user/$TARGET_UID; $auth $*" \
        2>/dev/null
}

# ------------------------------------------------------------ LXDE / PIXEL
if command -v lxpanel >/dev/null 2>&1 \
        || command -v startlxde-pi >/dev/null 2>&1; then
    log "检测到 LXDE/PIXEL 桌面"

    if ! command -v lxpanel >/dev/null 2>&1; then
        log "lxpanel 未安装，尝试安装..."
        if [ "$DRY_RUN" != "1" ]; then
            apt-get update -qq || true
            if DEBIAN_FRONTEND=noninteractive apt-get install -y lxpanel; then
                log "lxpanel 安装完成"
                FIXED=1
            else
                log "lxpanel 安装失败（检查网络/软件源）"
            fi
        fi
    fi

    # 1) 自启动：优先用户级，其次系统级
    USR_AUTOSTART="$HOME_DIR/.config/lxsession/LXDE-pi/autostart"
    SYS_AUTOSTART="/etc/xdg/lxsession/LXDE-pi/autostart"
    AUTOSTART=""
    if [ -f "$USR_AUTOSTART" ]; then
        AUTOSTART="$USR_AUTOSTART"
    elif [ -f "$SYS_AUTOSTART" ]; then
        AUTOSTART="$SYS_AUTOSTART"
    fi
    if [ -n "$AUTOSTART" ]; then
        if ! grep -qE '^[[:space:]]*@?lxpanel([[:space:]]|$)' "$AUTOSTART"; then
            log "自启动文件缺少 lxpanel: $AUTOSTART"
            backup_file "$AUTOSTART"
            if [ "$DRY_RUN" = "1" ]; then
                log "[DRY_RUN] 将追加: @lxpanel --profile LXDE-pi"
            else
                echo "@lxpanel --profile LXDE-pi" >> "$AUTOSTART"
                FIXED=1
            fi
        else
            log "lxpanel 自启动已存在"
        fi
    else
        log "未找到 LXDE 自启动文件（可能不是 LXDE 会话）"
    fi

    # 2) 面板配置
    PANEL="$HOME_DIR/.config/lxpanel/LXDE-pi/panels/panel"
    if [ "$RESET_PANEL" = "1" ] && [ -e "$PANEL" ]; then
        log "RESET_PANEL=1：重置用户面板配置"
        backup_file "$PANEL"
        [ "$DRY_RUN" = "1" ] || rm -f "$PANEL"
    fi
    if [ ! -s "$PANEL" ]; then
        DEFAULT_PANEL=""
        for cand in \
            /etc/xdg/lxpanel/LXDE-pi/panels/panel \
            /etc/xdg/lxpanel/LXDE/panels/panel \
            /usr/share/lxpanel/profile/LXDE-pi/panels/panel \
            /usr/share/lxpanel/profile/LXDE/panels/panel; do
            if [ -s "$cand" ]; then
                DEFAULT_PANEL="$cand"
                break
            fi
        done
        if [ -n "$DEFAULT_PANEL" ]; then
            log "恢复默认任务栏配置: $DEFAULT_PANEL -> $PANEL"
            if [ "$DRY_RUN" = "1" ]; then
                log "[DRY_RUN] 将复制默认面板配置"
            else
                mkdir -p "$(dirname "$PANEL")"
                backup_file "$PANEL"
                cp "$DEFAULT_PANEL" "$PANEL"
                chown -R "$TARGET:$TARGET_GROUP" "$HOME_DIR/.config/lxpanel" \
                    2>/dev/null || true
                FIXED=1
            fi
        else
            log "未找到系统默认 lxpanel 配置，跳过配置恢复"
        fi
    else
        log "lxpanel 面板配置存在（$PANEL）"
    fi

    # 3) 当前 X11 会话里重启任务栏
    if [ "$STYPE" = "x11" ] || [ -z "$STYPE" ]; then
        if [ "$DRY_RUN" != "1" ]; then
            pkill -u "$TARGET" -x lxpanel 2>/dev/null || true
            sleep 1
            run_in_x11 "nohup lxpanel --profile LXDE-pi >/dev/null 2>&1 &"
            sleep 1
            if pgrep -u "$TARGET" -x lxpanel >/dev/null 2>&1; then
                log "已重启 lxpanel，任务栏应已恢复"
                FIXED=1
            else
                log "lxpanel 未能在当前会话启动（如果是 RDP/远程会话，"
                log "请断开重连；本地桌面请注销或重启后查看）"
            fi
        else
            log "[DRY_RUN] 将重启 lxpanel"
        fi
    else
        log "当前是 Wayland 会话，不热重启 lxpanel（重启后生效）"
    fi
fi

# ------------------------------------------------------------ XFCE
if command -v xfce4-panel >/dev/null 2>&1; then
    log "检测到 XFCE 桌面"
    XFCE_CFG="$HOME_DIR/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"

    if [ "$RESET_PANEL" = "1" ] && [ -e "$XFCE_CFG" ]; then
        log "RESET_PANEL=1：重置 XFCE 面板配置"
        backup_file "$XFCE_CFG"
        [ "$DRY_RUN" = "1" ] || rm -f "$XFCE_CFG"
        FIXED=1
    elif [ -s "$XFCE_CFG" ] && command -v xmllint >/dev/null 2>&1; then
        if ! xmllint --noout "$XFCE_CFG" 2>/dev/null; then
            log "XFCE 面板配置 XML 损坏，备份后重置"
            backup_file "$XFCE_CFG"
            [ "$DRY_RUN" = "1" ] || rm -f "$XFCE_CFG"
            FIXED=1
        fi
    fi

    if [ "$STYPE" = "x11" ] || [ -z "$STYPE" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            log "[DRY_RUN] 将重启 xfce4-panel"
        else
            if pgrep -u "$TARGET" -x xfce4-panel >/dev/null 2>&1; then
                run_in_x11 "nohup xfce4-panel -r >/dev/null 2>&1 &"
            else
                run_in_x11 "nohup xfce4-panel >/dev/null 2>&1 &"
            fi
            sleep 1
            if pgrep -u "$TARGET" -x xfce4-panel >/dev/null 2>&1; then
                log "xfce4-panel 已重启"
                FIXED=1
            else
                log "xfce4-panel 未能在 :0 启动（RDP 请断开重连）"
            fi
        fi
    else
        log "当前是 Wayland 会话，XFCE 面板需注销/重启后生效"
    fi
fi

# ------------------------------------------------------------ Wayland
if [ "$STYPE" = "wayland" ] || command -v wf-panel-pi >/dev/null 2>&1; then
    log "检测到 Wayland / wf-panel-pi 环境"
    if ! command -v wf-panel-pi >/dev/null 2>&1; then
        log "wf-panel-pi 未安装，尝试安装..."
        if [ "$DRY_RUN" != "1" ]; then
            apt-get update -qq || true
            if DEBIAN_FRONTEND=noninteractive \
                    apt-get install -y wf-panel-pi; then
                log "wf-panel-pi 安装完成"
                FIXED=1
            else
                log "wf-panel-pi 安装失败（检查网络/软件源）"
            fi
        fi
    fi
    WF_CFG="$HOME_DIR/.config/wf-panel-pi.ini"
    WF_SYS="/etc/xdg/wf-panel-pi.ini"
    if [ "$RESET_PANEL" = "1" ] && [ -e "$WF_CFG" ]; then
        log "RESET_PANEL=1：重置 wf-panel-pi 配置"
        backup_file "$WF_CFG"
        [ "$DRY_RUN" = "1" ] || rm -f "$WF_CFG"
        FIXED=1
    fi
    if [ ! -s "$WF_CFG" ] && [ -s "$WF_SYS" ]; then
        log "恢复默认 wf-panel-pi 配置: $WF_SYS -> $WF_CFG"
        if [ "$DRY_RUN" = "1" ]; then
            log "[DRY_RUN] 将复制 wf-panel-pi 默认配置"
        else
            mkdir -p "$(dirname "$WF_CFG")"
            cp "$WF_SYS" "$WF_CFG"
            chown "$TARGET:$TARGET_GROUP" "$WF_CFG" 2>/dev/null || true
            FIXED=1
        fi
    fi
    log "Wayland 任务栏无法从 root 安全热重启，请注销/重启后查看"
fi

# ------------------------------------------------------------ 结果
echo ""
if [ "$FIXED" = "1" ]; then
    log "修复完成。本地桌面：任务栏应已恢复；RDP：断开重连；"
    log "Wayland：注销或重启后生效。"
else
    log "没有执行修改（未检测到可修复项，或已经是正常状态）。"
    log "若任务栏仍异常，可试：RESET_PANEL=1 sudo bash $0"
fi
exit 0
