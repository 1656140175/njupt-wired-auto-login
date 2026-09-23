#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""南邮校园网 Windows 本地自动登录（不依赖树莓派，零依赖标准库）

用法（Windows）：
  python njupt_autologin.py --once        # 检测一次并登录（bat 双击用）
  python njupt_autologin.py --watch       # 常驻：每 15 分钟检测一次
  python njupt_autologin.py --watch --attempts 3   # 开机自启：失败重试 3 次

配置：账号/密码/运营商等全部从同级目录 njupt_autologin.conf 读取
（key=value 格式，# 开头为注释），命令行参数可覆盖。bat 内不硬编码任何信息。

说明：
  - 账号格式 = 学号 + 运营商后缀（移动 @cmcc / 电信 @njxy / 校园网空）
  - 登录请求与 a79.htm 页面表单完全一致（POST + 全部字段）
  - 成功/失败以网关 302 跳转页 Dr.COMWebLoginID_3.htm/_2.htm 判定
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GATEWAY = "http://10.10.244.11:801/eportal/?c=ACSetting&a=Login"
# 注意：探测必须用 HTTP 而非 HTTPS！
# 校园网未认证时 HTTPS 被网关直接阻断（丢包），而 HTTP 会被劫持到认证页
# （返回 302/认证页特征），据此区分"未认证"和"真断网"。
HTTP_PROBE = [("http://www.baidu.com/generate_204", "204"),
              ("http://connect.rom.miui.com/generate_204", "204"),
              ("http://www.qq.com/", "腾讯")]
# 页面 f0 表单的全部字段
FORM_FIELDS = {"0MKKey": "123456", "R1": "0", "R2": "0", "R3": "0",
               "R6": "0", "para": "00", "url": "drappall",
               "buttonClicked": "", "redirect_url": "", "err_flag": "",
               "username": "", "password": "", "user": "", "cmd": "",
               "Login": "", "v6ip": ""}


def decode(body):
    for enc in ("gbk", "utf-8"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "ignore")


CONF_NAME = "njupt_autologin.conf"


def load_conf(path=None):
    """读取同级目录 njupt_autologin.conf（# 注释，key=value）。
    容错：pass 归一化为 password；兼容 BOM 与 GBK/ANSI 编码（记事本保存）。"""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                CONF_NAME)
    out = {}
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return out
    body = None
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            body = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if body is None:
        return out
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower().lstrip(chr(0xFEFF))
        if key == "pass":
            key = "password"
        out[key] = value.strip()
    return out


def _no_proxy_opener():
    """禁用代理直连。Windows 上 urllib 默认读系统代理(注册表)，
    网关/校园网内网地址会被送进代理而连不上——必须强制直连。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _gateway_hostport(gateway=None):
    """从网关 URL 解析 host/port；缺省回退默认值。"""
    m = re.match(r"https?://([^:/]+)(?::(\d+))?", gateway or GATEWAY)
    host = m.group(1) if m else "10.10.244.11"
    port = int(m.group(2)) if m and m.group(2) else 801
    return host, port


def _socket_reachable(host, port, timeout=3):
    """TCP 直连测试（绕过 HTTP 层与代理），用于判断物理链路。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _ping(host, timeout=2):
    """ICMP ping 网关（只查返回码，不看输出）。"""
    if sys.platform.startswith("win"):
        args = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        args = ["ping", "-c", "1", "-W", str(int(timeout)), host]
    try:
        return subprocess.run(args, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=timeout + 1).returncode == 0
    except Exception:
        return False


def _link_up(host, port, timeout=3):
    """物理链路判断：ping 网关；部分网关禁 ICMP，回退 TCP 端口直连。"""
    return _ping(host, timeout) or _socket_reachable(host, port, timeout)


def _is_captive(final_url, body):
    """响应是否被网关劫持（未认证特征：跳到认证页 / 返回认证页内容）。
    端点匹配用 host:port（劫持跳转 URL 必带端口，避免误伤探测 URL 自身）。"""
    text = (final_url + " " + body).lower()
    host, port = _gateway_hostport()
    endpoint = "%s:%d" % (host, port)
    return ("eportal" in text or "loginid" in text or endpoint in text
            or "dr.com" in text or "认证" in text)


def probe_network(timeout=5):
    """三态探测（校园网专用，不依赖"未登录能上网"的错误假设）：
      "online"      链路通且外网可访问（已认证） -> 无事
      "need_login"  链路通但未认证（外网不通）   -> 登录（静默时段也登录）
      "offline"     链路断（ping 网关都不通）    -> 静默时段跳过
    第 0 层 ping 网关判链路；链路通后再用外网 HTTP 探测判认证状态
    （未认证时 HTTP 被网关劫持/阻断，可识别）。
    """
    host, port = _gateway_hostport()
    if not _link_up(host, port, 3):
        return "offline", "ping 网关 %s 不通（链路断）" % host
    # 链路通：外网 HTTP 探测。正常响应=在线；被劫持=未认证。
    opener = _no_proxy_opener()
    for url, marker in HTTP_PROBE:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept-Encoding": "identity"})
            resp = opener.open(req, timeout=timeout)
            status, final, body = resp.getcode(), resp.geturl(), \
                decode(resp.read(1024))
        except urllib.error.HTTPError as exc:
            status, final, body = exc.code, exc.geturl() or url, \
                decode(exc.read(512))
        except Exception:
            continue
        if _is_captive(final, body):
            return "need_login", "%s 被劫持到认证页" % url
        if status == 204 or marker in body:
            return "online", "%s -> %d" % (url, status)
    # 链路通但外网全不通 = 未认证（外网被网关阻断）
    return "need_login", "网关 %s 可达但外网不通（未认证）" % host


def gateway_reachable(gateway=None, timeout=3):
    """网关是否可达（socket 直连，绕过代理与 HTTP 层）。"""
    host, port = _gateway_hostport(gateway)
    return _socket_reachable(host, port, timeout)


def login(username, password, gateway=None, timeout=10):
    """POST 哆点网关。返回 (ok, 说明)。"""
    data = {"DDDDD": username, "upass": password}
    data.update(FORM_FIELDS)
    m = re.match(r"(https?://[^/]+)", gateway or GATEWAY)
    referer = (m.group(1) + "/") if m else "http://10.10.244.11:801/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
               "Accept-Encoding": "identity",
               "Referer": referer}
    req = urllib.request.Request(
        gateway or GATEWAY, data=urllib.parse.urlencode(data).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with _no_proxy_opener().open(req, timeout=timeout) as resp:
            body = decode(resp.read(65536))
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        return False, "网关错误: HTTP %d" % exc.code
    except Exception as exc:
        return False, "请求失败: %s" % exc
    low = (body + " " + final_url).lower()
    if "webloginid_2" in low or "认证失败" in low or "密码错误" in low \
            or "fail" in low:
        return False, "登录失败: %s" % body.replace("\n", " ")[:120]
    if "webloginid_3" in low or "认证成功" in low or "success" in low:
        return True, "登录成功"
    return True, "已发送登录请求（等待探测确认）"


def in_quiet(hour, minute, start, end):
    now = hour * 60 + minute
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def main():
    parser = argparse.ArgumentParser(description="南邮校园网本地自动登录")
    parser.add_argument("--user", default="",
                        help="学号（默认读同级 %s）" % CONF_NAME)
    parser.add_argument("--pass", dest="password", default="", help="密码")
    parser.add_argument("--suffix", default="",
                        help="运营商后缀（移动@cmcc/电信@njxy/校园网空）")
    parser.add_argument("--gateway", default=GATEWAY, help="网关地址")
    parser.add_argument("--probe", default=None,
                        help="自定义探测，格式 URL|标记;URL|标记（分号分隔）")
    parser.add_argument("--watch", action="store_true",
                        help="常驻轮询（默认单次）")
    parser.add_argument("--interval", type=int, default=900,
                        help="轮询间隔秒（默认 900=15分钟）")
    parser.add_argument("--attempts", type=int, default=1,
                        help="登录失败重试次数（默认 1，开机自启用 3）")
    parser.add_argument("--attempt-delay", type=int, default=6,
                        help="重试间隔秒（默认 6）")
    parser.add_argument("--once", action="store_true",
                        help="检测并登录后退出（默认行为）")
    parser.add_argument("--test", action="store_true", help="只探测不登录")
    parser.add_argument("--quiet", action="store_true", default=True,
                        help="静默时段不重连（默认 00:00-06:00）")
    parser.add_argument("--no-quiet", action="store_true", help="关闭静默")
    parser.add_argument("--quiet-start", default="00:00", help="静默开始")
    parser.add_argument("--quiet-end", default="06:00", help="静默结束")
    # 同级 njupt_autologin.conf 提供默认值（bat 内不硬编码任何信息）
    conf = load_conf()
    defaults = {}
    for key in ("user", "password", "suffix", "gateway", "interval",
                "attempts", "attempt_delay", "quiet_start", "quiet_end"):
        if key in conf:
            defaults[key] = int(conf[key]) if key in (
                "interval", "attempts", "attempt_delay") else conf[key]
    if defaults:
        parser.set_defaults(**defaults)
    args = parser.parse_args()

    if not args.user or not args.password:
        print("未配置账号/密码：请编辑本目录下的 %s 文件" % CONF_NAME)
        return 1
    args.attempts = max(1, min(args.attempts, 10))
    args.attempt_delay = max(1, args.attempt_delay)

    if args.probe:
        HTTP_PROBE[:] = []
        for item in args.probe.split(";"):
            if "|" in item:
                url, marker = item.split("|", 1)
                HTTP_PROBE.append((url, marker))
            else:
                HTTP_PROBE.append((item, ""))
    quiet_enabled = args.quiet and not args.no_quiet
    start = int(args.quiet_start.split(":")[0]) * 60 + \
        int(args.quiet_start.split(":")[1])
    end = int(args.quiet_end.split(":")[0]) * 60 + \
        int(args.quiet_end.split(":")[1])
    username = args.user + args.suffix

    def one_round():
        state, msg = probe_network()
        label = {"online": "在线", "need_login": "链路通·未认证",
                 "offline": "离线"}[state]
        print("[%s] 探测: %s (%s)" % (time.strftime("%H:%M:%S"),
                                      label, msg))
        if state == "online":
            return True, "已在线"
        if state == "offline":
            # 物理链路断：登录无意义。
            # 静默时段直接跳过；非静默时段等待网络恢复，下轮再试。
            if quiet_enabled and in_quiet(*time.localtime()[3:5], start, end):
                print("[%s] 静默时段且链路断，跳过登录"
                      % time.strftime("%H:%M:%S"))
                return False, "静默"
            print("[%s] 链路不可达，等待网络恢复" % time.strftime("%H:%M:%S"))
            return False, "链路断"
        # need_login：链路通 = 有网（用户要求"静默时段有网就不静默"），
        # 静默时段同样登录。失败按 --attempts 重试（开机自启默认 3 次）
        if quiet_enabled and in_quiet(*time.localtime()[3:5], start, end):
            print("[%s] 静默时段但链路通，尝试登录"
                  % time.strftime("%H:%M:%S"))
        ok, msg2 = False, "未尝试"
        for i in range(1, args.attempts + 1):
            if i > 1:
                print("[%s] 第 %d 次尝试（间隔 %d 秒）"
                      % (time.strftime("%H:%M:%S"), i, args.attempt_delay))
                time.sleep(args.attempt_delay)
            print("[%s] 尝试登录 %s" % (time.strftime("%H:%M:%S"), username))
            ok, msg2 = login(username, args.password, args.gateway)
            print("[%s] 登录: %s" % (time.strftime("%H:%M:%S"), msg2))
            if ok:
                break
        return ok, msg2

    if args.test:
        state, msg = probe_network()
        label = {"online": "在线", "need_login": "链路通·未认证",
                 "offline": "离线"}[state]
        print("%s: %s" % (label, msg))
        return 0 if state == "online" else 1

    one_round()
    if not args.watch:
        return 0
    print("常驻运行中（每 %d 秒一次，Ctrl+C 退出）" % args.interval)
    last_run = time.time()
    online = False
    try:
        while True:
            # 在线：睡满周期；掉线：30 秒快速 ping 等以太网上线
            time.sleep(30 if not online else max(60, args.interval))
            # 电脑休眠/睡眠唤醒后 time.time() 大幅跳跃 -> 立即检测一次
            if time.time() - last_run > args.interval * 2:
                print("[%s] 检测到休眠唤醒，立即检查网络"
                      % time.strftime("%H:%M:%S"))
            last_run = time.time()
            host, port = _gateway_hostport(args.gateway)
            if online:
                # 有网：不探测、不尝试连接，仅 ping 网关确认链路还活着
                if _link_up(host, port, 3):
                    continue
                print("[%s] 网关失联，转为掉线检测"
                      % time.strftime("%H:%M:%S"))
                online = False
                continue
            # 掉线：以太网还没上线（网关 ping 不通）-> 继续等；
            # Wi-Fi 上网/没插网线同样不打扰
            if not _link_up(host, port, 3):
                continue
            # 以太网上线：探测外网；无法访问外网（未认证）-> 尝试登录，
            # 失败按 --attempts 重试（开机自启默认 3 次）
            ok, _ = one_round()
            online = bool(ok)
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
