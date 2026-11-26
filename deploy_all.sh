#!/usr/bin/env bash
# ============================================================
#  CampusPi 全自动部署（树莓派上运行，需 root）
#
#  在解压后的目录里执行：
#      sudo bash deploy_all.sh
#
#  可选环境变量：
#      WITH_RDP=0  不安装 RDP（默认安装）
#
#  流程：
#    1) install.sh        装依赖、部署到 /opt/campuspi、注册并启动服务
#    2) setup_rdp.sh      安装/启动 xrdp（失败不阻断主程序）
#    3) setup_tuning.sh   应用转发延迟优化（失败不阻断主程序）
# ============================================================
set -u

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo/root 运行：sudo bash $0" >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
WITH_RDP="${WITH_RDP:-1}"

echo "========================================"
echo " CampusPi 全自动部署"
echo " 目录: $HERE"
echo "========================================"

# ------------------------------------------------------------ 0. 备份旧版本
if [ -d /opt/campuspi ]; then
    BAK="/opt/campuspi.bak.$(date +%Y%m%d-%H%M%S)"
    if cp -a /opt/campuspi "$BAK" 2>/dev/null; then
        echo "已备份旧版本到: $BAK"
    else
        echo "警告：旧版本备份失败（磁盘空间不足？），继续部署" >&2
    fi
fi

# ------------------------------------------------------------ 1. 主程序
echo ""
echo "==> [1/4] 安装主程序（依赖/网络/服务）"
if ! bash "$HERE/install.sh"; then
    echo "主程序安装失败，已中止。请检查网络/磁盘后重试。" >&2
    exit 1
fi

# ------------------------------------------------------------ 2. RDP
echo ""
if [ "$WITH_RDP" = "1" ]; then
    echo "==> [2/4] 安装/启动 RDP 远程桌面"
    if ! bash "$HERE/setup_rdp.sh"; then
        echo "RDP 安装失败：不影响校园网自动连接；"
        echo "可稍后在 Web 后台「远程桌面」里重试（需要树莓派能上网）。"
    fi
else
    echo "==> [2/4] 跳过 RDP（WITH_RDP=0）"
fi

# ------------------------------------------------------------ 3. 调优
echo ""
echo "==> [3/4] 应用转发延迟优化"
if ! bash "$HERE/setup_tuning.sh"; then
    echo "延迟优化部分失败：不影响校园网自动连接；"
    echo "可稍后在 Web 后台「转发延迟优化」里重试。"
fi

# ------------------------------------------------------------ 4. 验证自动连接
echo ""
echo "==> [4/4] 验证校园网自动连接"
CONFIGURED=$(python3 - <<'PYEOF' 2>/dev/null || echo 0
import json
try:
    with open("/etc/campuspi/config.json", encoding="utf-8") as f:
        c = json.load(f)
    campus = c.get("campus") or {}
    ok = bool(campus.get("username")) and bool(campus.get("password"))
    print("1" if ok else "0")
except Exception:
    print("0")
PYEOF
)
if [ "$CONFIGURED" = "1" ]; then
    python3 /opt/campuspi/main.py --check-login || true
else
    echo "尚未配置校园网账号/密码：请打开 Web 后台填写，"
    echo "然后点「⚡ 立即测试登录」即可验证。"
fi

# ------------------------------------------------------------ 完成
echo ""
echo "========================================"
echo " 部署完成"
echo "========================================"
echo "  Web 后台 : http://192.168.9.1:8080"
if [ "$WITH_RDP" = "1" ]; then
    echo "  RDP      : mstsc /v:192.168.9.1"
    echo "             （登录用树莓派系统账号/密码）"
fi
echo ""
echo "  自动连接检查（用当前 /etc/campuspi/config.json 里的账号）："
echo "    sudo python3 /opt/campuspi/main.py --check-login"
echo ""
echo "  桌面没有任务栏？"
echo "    Web 后台 → 🛠 桌面/任务栏修复 → 一键修复"
echo ""
echo "  查看日志："
echo "    sudo journalctl -u campuspi -f"
echo "========================================"
