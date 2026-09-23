#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CampusPi PC Helper —— Windows 端手动跳转助手（零依赖）

说明：插线后的自动跳转由树莓派端完成（强制门户，见 nla.py），
本脚本仅用于 Windows 端手动触发：
  1. 检测树莓派后台是否可达（默认 http://192.168.9.1:8080）
  2. 打开默认浏览器跳到配置页
  3. --login: 校园网离线时手动触发一次登录

用法（Windows，双击 pc_helper.bat 或命令行）：
  python pc_helper.py                   # 单次：检测+打开配置页后退出
  python pc_helper.py --login           # 检测+打开配置页+离线时触发登录
  python pc_helper.py --no-browser      # 只检测和触发登录，不弹浏览器
  python pc_helper.py --watch           # 常驻轮询（一般不需要，自动跳转已由树莓派完成）
  python pc_helper.py --user admin --pass xxxx   # 后台设置了管理密码时
"""
import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
import webbrowser


def _auth_header(user, password):
    if user or password:
        token = base64.b64encode(
            ("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}
    return {}


def fetch_status(base, headers=None, timeout=2):
    """GET /api/status，返回 (状态码, dict)；失败抛异常。"""
    req = urllib.request.Request(base.rstrip("/") + "/api/status",
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def post_login(base, headers=None, timeout=8):
    """POST /api/action/login 触发树莓派登录，返回 (状态码, dict)。"""
    req = urllib.request.Request(base.rstrip("/") + "/api/action/login",
                                 data=b"{}", method="POST",
                                 headers=dict({"Content-Type":
                                               "application/json"},
                                              **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _jump_once(args, headers):
    """单次：检测 -> 跳转 -> 可选触发登录。返回进程退出码。"""
    print("[%s] 正在检测树莓派..." % time.strftime("%H:%M:%S"))
    try:
        status_code, status = fetch_status(args.url, headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("已连接树莓派，但管理密码错误（用 --user/--pass 指定后台账号密码）")
            return 1
        print("未检测到树莓派（%s）" % args.url)
        print("请确认 USB 网卡线已连接，网卡为'自动获取 IP'，稍等几秒再试。")
        return 1
    except Exception:
        print("未检测到树莓派（%s）" % args.url)
        print("请确认 USB 网卡线已连接，网卡为'自动获取 IP'，稍等几秒再试。")
        return 1
    online = status.get("online")
    print("已连接树莓派，校园网状态: %s"
          % ("在线" if online else "离线/未认证"))
    if online is False and args.login:
        try:
            code, result = post_login(args.url, headers)
            print("已触发登录: %s" % result.get("msg", "完成"))
        except Exception as exc:
            print("触发登录失败: %s" % exc)
    if not args.no_browser:
        webbrowser.open(args.url)
        print("已在默认浏览器打开管理页 %s" % args.url)
    print("完成。")
    return 0


def _watch(args, headers):
    """常驻轮询（可选，一般不需要）：检测到连接首次跳转，离线时自动登录。"""
    opened = False
    lost_streak = 0
    interval = max(1.0, args.interval)
    print("CampusPi PC 助手（常驻模式）  目标: %s  间隔: %gs"
          % (args.url, interval))
    while True:
        try:
            status_code, status = fetch_status(args.url, headers)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError, json.JSONDecodeError):
            lost_streak += 1
            if lost_streak in (1, 5, 20):
                print("[%s] 未检测到树莓派（%s）"
                      % (time.strftime("%H:%M:%S"), args.url))
            if lost_streak >= 3:
                opened = False
            time.sleep(interval)
            continue
        lost_streak = 0
        online = status.get("online")
        if online is False and args.login:
            try:
                code, result = post_login(args.url, headers)
                print("[%s] 已自动触发登录: %s"
                      % (time.strftime("%H:%M:%S"),
                         result.get("msg", "完成")))
            except Exception as exc:
                print("[%s] 自动登录请求失败: %s"
                      % (time.strftime("%H:%M:%S"), exc))
        if not opened:
            if not args.no_browser:
                webbrowser.open(args.url)
                print("[%s] 已打开管理页 %s"
                      % (time.strftime("%H:%M:%S"), args.url))
            opened = True
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="CampusPi PC 助手（手动跳转）")
    parser.add_argument("--url", default="http://192.168.9.1:8080",
                        help="树莓派管理后台地址")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="--watch 常驻模式的检测间隔秒数（默认 5）")
    parser.add_argument("--login", action="store_true",
                        help="校园网离线时触发一次登录")
    parser.add_argument("--no-browser", action="store_true",
                        help="不打开浏览器（仅检测/触发登录）")
    parser.add_argument("--watch", action="store_true",
                        help="常驻轮询（自动跳转已由树莓派完成，一般不需要）")
    parser.add_argument("--user", default="", help="后台管理账号（设置了密码时）")
    parser.add_argument("--pass", dest="password", default="",
                        help="后台管理密码")
    args = parser.parse_args()
    headers = _auth_header(args.user, args.password)
    if args.watch:
        _watch(args, headers)
    else:
        return _jump_once(args, headers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
