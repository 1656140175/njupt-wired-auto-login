# -*- coding: utf-8 -*-
"""通用工具：命令执行、网络接口查询、时间同步（仅标准库）。"""
import datetime
import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger("campuspi.utils")

# 系统时间早于该年份即视为"不可信"（树莓派无 RTC，断电重启后会回到 1970）
TIME_ANCHOR_YEAR = 2020


def run(cmd, timeout=15, check=False):
    """执行命令，返回 CompletedProcess；命令缺失/超时返回 None 并记录日志。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        log.warning("命令不存在: %s", cmd[0])
        return None
    except subprocess.TimeoutExpired:
        log.warning("命令超时: %s", " ".join(cmd))
        return None
    if check and proc.returncode != 0:
        log.warning("命令返回 %d: %s (%s)", proc.returncode,
                    " ".join(cmd), proc.stderr.strip()[:300])
    return proc


def iface_exists(name):
    return bool(name) and os.path.isdir(os.path.join("/sys/class/net", name))


def carrier(name):
    """链路状态: True/False；接口不存在返回 None。"""
    try:
        with open(os.path.join("/sys/class/net", name, "carrier"), "r") as f:
            return f.read().strip() == "1"
    except OSError:
        return None


def get_ip(name):
    """获取接口当前 IPv4 地址；无则 None。"""
    proc = run(["ip", "-4", "-o", "addr", "show", "dev", name])
    if not proc or proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split()
        try:
            idx = parts.index("inet")
            return parts[idx + 1].split("/")[0]
        except (ValueError, IndexError):
            continue
    return None


def wait_for_ip(name, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = get_ip(name)
        if ip:
            return ip
        time.sleep(0.5)
    return None


def set_static_ip(name, address, prefix=24):
    """幂等地把静态地址配到接口上。"""
    if not iface_exists(name):
        log.warning("接口 %s 不存在，跳过静态地址配置", name)
        return False
    if get_ip(name) == address:
        return True
    run(["ip", "link", "set", name, "up"], check=True)
    run(["ip", "addr", "flush", "dev", name], check=True)
    run(["ip", "addr", "add", "%s/%d" % (address, prefix), "dev", name], check=True)
    time.sleep(0.3)
    if get_ip(name) == address:
        return True
    log.error("静态地址 %s/%d 配置失败 on %s", address, prefix, name)
    return False


def set_ip_forward(enabled=True):
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("1" if enabled else "0")
        return True
    except OSError as exc:
        log.error("设置 ip_forward 失败: %s", exc)
        return False


def ensure_dhcp(name, wait=30):
    """确保接口通过 DHCP 拿到地址（尽力而为，多个客户端任选其一）。"""
    if get_ip(name):
        return True
    candidates = []
    for tool, args in (("nmcli", ["device", "connect", name]),
                       ("dhcpcd", ["-n", name]),
                       ("dhclient", [name])):
        if shutil.which(tool):
            candidates.append([tool] + args)
    for cmd in candidates:
        log.info("尝试 DHCP: %s", " ".join(cmd))
        run(cmd, timeout=20)
        if wait_for_ip(name, min(wait, 10)):
            return True
    return get_ip(name) is not None


def time_trustworthy():
    """树莓派无 RTC，断电重启后时间可能回到 1970 年。
    时间早于锚点年份即视为不可信（不能据此判断静默时段）。"""
    try:
        return datetime.datetime.now().year >= TIME_ANCHOR_YEAR
    except (OSError, ValueError):
        return False


def sync_time(timeout=15):
    """尽力同步系统时间（树莓派无 RTC，必须靠 NTP）。
    优先 ntpdate 单次同步，失败则重启 systemd-timesyncd 服务。
    返回是否成功。"""
    if shutil.which("ntpdate"):
        proc = run(["ntpdate", "-b", "-u", "pool.ntp.org"], timeout=timeout)
        if proc and proc.returncode == 0:
            log.info("系统时间已通过 ntpdate 同步: %s",
                     time.strftime("%Y-%m-%d %H:%M:%S"))
            return True
    if shutil.which("systemctl"):
        run(["systemctl", "restart", "systemd-timesyncd"], timeout=timeout)
    log.warning("时间同步未确认成功")
    return False


def ipv4_to_int(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    try:
        n = 0
        for part in parts:
            n = (n << 8) | int(part)
        return n
    except ValueError:
        return None


def ip_in_subnet(ip, subnet):
    """subnet 形如 192.168.9.0/24"""
    try:
        net, prefix = subnet.split("/")
        prefix = int(prefix)
    except ValueError:
        return False
    a, b = ipv4_to_int(ip), ipv4_to_int(net)
    if a is None or b is None:
        return False
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return (a & mask) == (b & mask)
