#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CampusPi 纯函数自检（无需树莓派、无需网络、无需 root）。

在 Windows 上也能运行：python selftest.py
覆盖：配置合并/脱敏/回读、静默时段判断、srun 密码编码、JSONP 解析、
      IP/子网计算、通用适配器、App 保存合并逻辑。
"""
import base64
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config as config_mod
from config import parse_hhmm, in_quiet_hours, deep_merge
from portal import Portal, strip_jsonp
from utils import ipv4_to_int, ip_in_subnet

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s   %s" % (name, detail))


# ---------------------------------------------------------------- 配置
def test_config():
    tmp = os.path.join(tempfile.mkdtemp(), "config.json")
    c = config_mod.Config(tmp)
    check("默认适配器为南邮哆点 drcom", c.data["campus"]["adapter"] == "drcom")
    check("哆点参数与 a79.htm 一致",
          c.data["drcom"]["host"] == "10.10.244.11"
          and c.data["drcom"]["port"] == 801
          and c.data["drcom"]["path"] == "/eportal/?c=ACSetting&a=Login"
          and c.data["drcom"]["user_field"] == "DDDDD"
          and c.data["drcom"]["pass_field"] == "upass"
          and c.data["drcom"]["success_keys"][0] == "webloginid_3")
    check("默认静默时段 00:00-06:00", c.data["quiet_hours"]["end"] == "06:00")
    c.save({"campus": {"username": "stu01", "password": "p@ss", "isp": "telecom"}})
    c2 = config_mod.Config(tmp)
    check("配置保存/回读", c2.data["campus"]["username"] == "stu01")
    check("运营商保留", c2.data["campus"]["isp"] == "telecom")
    m = c2.masked()
    check("密码脱敏", m["campus"]["password"] == ""
          and m["campus"]["has_password"] is True)
    check("脱敏副本不改原配置", c2.data["campus"]["password"] == "p@ss")
    check("合并覆盖", deep_merge({"a": 1, "b": {"x": 1}},
                                {"b": {"y": 2}}) == {"a": 1, "b": {"x": 1, "y": 2}})
    c3 = config_mod.Config(tmp)
    c3.save({"campus": {"check_interval": "abc"}})
    check("非法数字回退默认", c3.data["campus"]["check_interval"] == 1800)


# ------------------------------------------------------------ 静默时段
def test_quiet_hours():
    check("parse_hhmm 正常", parse_hhmm("23:45") == 1425)
    check("parse_hhmm 非法", parse_hhmm("25:99") is None and parse_hhmm("abc") is None)
    check("同段内静默", in_quiet_hours(360, 0, 360) is False)  # 06:00 整点=结束
    check("00:00 在静默内", in_quiet_hours(5, 0, 360))
    check("01:00 在静默内", in_quiet_hours(60, 0, 360))
    check("12:00 不在静默内", in_quiet_hours(720, 0, 360) is False)
    check("跨午夜静默", in_quiet_hours(25, 1380, 360))       # 00:25, 23:00-06:00
    check("跨午夜非静默", in_quiet_hours(720, 1380, 360) is False)
    check("start==end 不静默", in_quiet_hours(60, 60, 60) is False)
    check("边界=开始", in_quiet_hours(0, 0, 360))


# ----------------------------------------------------------------- srun
def test_srun():
    p = Portal(None)
    variants = dict(p._pwd_variants("auto", "secret", "tok"))
    md5_expect = hashlib.md5(b"secrettok").hexdigest()
    check("auto 含 md5 变体", variants["md5"] == md5_expect)
    check("auto 含 token$md5 变体",
          variants["token$md5"] == "tok$" + md5_expect)
    b64_expect = base64.b64encode(b"secrettok").decode()
    check("auto 含 base64 变体", variants["base64"] == b64_expect)
    check("显式 md5 只出一种", len(p._pwd_variants("md5", "a", "b")) == 1)
    check("未知编码回退 auto", len(p._pwd_variants("zzz", "a", "b")) == 4)
    check("JSONP 解析", strip_jsonp('jsonp({"a":1})') == {"a": 1})
    check("JSONP 带分号/换行",
          strip_jsonp('jQuery1234({"a":{"b":2}});') == {"a": {"b": 2}})
    check("纯 JSON 解析", strip_jsonp('{"a":1}') == {"a": 1})
    # 通用适配器（mock _fetch）
    cfg = deep_merge(config_mod.DEFAULTS, {
        "campus": {"adapter": "generic", "username": "u1", "password": "p1",
                   "isp": "cmcc", "host": "x"},
        "generic": {"login_url": "http://portal/login",
                    "params": {"user": "{username}@x", "pass": "{password}",
                               "isp": "{isp}", "acid": "{acid}"},
                    "success_key": "ok"}})
    p._fetch = lambda url, params=None, data=None, timeout=8: "ok logged in"
    ok, msg = p._generic_login(cfg)
    check("通用适配器成功", ok)
    p._fetch = lambda *a, **k: "denied"
    ok, msg = p._generic_login(cfg)
    check("通用适配器失败检测", not ok and "成功标记" in msg)


# ------------------------------------------------------------- eportal
def test_eportal():
    p = Portal(None)
    cfg = deep_merge(config_mod.DEFAULTS, {
        "campus": {"adapter": "eportal", "username": "B23010101",
                   "password": "pw123", "isp": "telecom"},
        "eportal": {"host": "10.10.244.11", "port": 801}})
    captured = {}
    p._fetch = lambda url, timeout=8: captured.setdefault("url", url) or "ok"
    ok, msg = p._eportal_login(cfg)
    check("eportal 登录成功", ok, msg)
    check("URL 含运营商后缀账号", ",0,B23010101@njxy" in captured["url"],
          captured.get("url"))
    check("URL 含密码", "user_password=pw123" in captured["url"])
    check("URL 含 login_method", "login_method=1" in captured["url"])
    p._fetch = lambda url, timeout=8: "用户名或密码错误"
    ok, msg = p._eportal_login(cfg)
    check("eportal 错误响应检测", not ok and "错误" in msg, msg)
    # 校园网(无后缀)
    cfg2 = deep_merge(cfg, {"campus": {"isp": "campus"}})
    p._fetch = lambda url, timeout=8: captured.__setitem__("url2", url) or "ok"
    p._eportal_login(cfg2)
    check("校园网无后缀", ",0,B23010101&" in captured["url2"], captured["url2"])
    # 无账号密码
    cfg3 = deep_merge(cfg, {"campus": {"username": "", "password": ""}})
    ok, msg = p._eportal_login(cfg3)
    check("未配置账号返回提示", not ok and "未配置" in msg)


# -------------------------------------------------------------- drcom
def test_drcom():
    import urllib.parse
    p = Portal(None)
    cfg = deep_merge(config_mod.DEFAULTS, {
        "campus": {"adapter": "drcom", "username": "B23010101",
                   "password": "pw123", "isp": "telecom"},
        "drcom": {"host": "10.10.244.11", "port": 801}})
    captured = {}

    def fake(base, params=None, data=None, timeout=8):
        captured["url"] = base
        captured["data"] = urllib.parse.urlencode(data) if data else ""
        return ("ok", "http://10.10.244.11/")
    p._fetch_resp = fake
    ok, msg = p._drcom_login(cfg)
    check("drcom 登录成功(POST)", ok, msg)
    check("POST 到 801 网关路径", captured["url"] ==
          "http://10.10.244.11:801/eportal/?c=ACSetting&a=Login",
          captured["url"])
    check("POST 数据含 DDDDD 账号+后缀", "DDDDD=B23010101%40njxy" in captured["data"],
          captured["data"][:150])
    check("POST 数据含 upass 明文密码", "upass=pw123" in captured["data"])
    check("POST 数据含附加参数", "0MKKey=123456" in captured["data"]
          and "url=drappall" in captured["data"])
    check("POST 数据含空值字段", "buttonClicked=" in captured["data"]
          and "v6ip=" in captured["data"])
    # 302 到成功页判定
    p._fetch_resp = lambda *a, **k: ("", "http://x/Dr.COMWebLoginID_3.htm")
    ok, msg = p._drcom_login(cfg)
    check("跳转 3.htm 判成功", ok, msg)
    # 302 到失败页判定
    p._fetch_resp = lambda *a, **k: ("", "http://x/Dr.COMWebLoginID_2.htm")
    ok, msg = p._drcom_login(cfg)
    check("跳转 2.htm 判失败", not ok and "失败" in msg, msg)
    # 响应正文错误关键字
    p._fetch_resp = lambda *a, **k: ("密码错误，请重试", "http://x/")
    ok, msg = p._drcom_login(cfg)
    check("正文错误关键字判失败", not ok, msg)
    # 注销
    p._last_url = None
    def capture(url, params=None, data=None, timeout=8):
        p._last_url = url
        return ("", "http://x/")
    p._fetch_resp = capture
    ok, msg = p.logout(cfg)
    check("注销成功", ok and "注销请求已发送" in msg, msg)
    check("注销路径正确", "a=Logout&ver=1.0" in p._last_url, p._last_url)


# --------------------------------------------------------------- IP/子网
def test_ip():
    check("IPv4 转整数", ipv4_to_int("192.168.9.1") == 0xC0A80901)
    check("非法 IPv4", ipv4_to_int("a.b.c") is None)
    check("子网内", ip_in_subnet("192.168.9.55", "192.168.9.0/24"))
    check("子网外", ip_in_subnet("192.168.10.1", "192.168.9.0/24") is False)
    check("子网格式错误", ip_in_subnet("1.2.3.4", "bad") is False)


# ------------------------------------------------------- App 保存合并逻辑
def test_app_save():
    tmp = os.path.join(tempfile.mkdtemp(), "config.json")
    config_mod.DEFAULT_CONFIG_PATH = tmp
    from main import App
    app = App()
    app._apply_config = lambda: None   # 避免在 Windows 上执行系统命令
    errs = app.save_config({"campus": {"username": "u1", "password": "p1"},
                            "hotspot": {"enabled": True, "ssid": "Pi",
                                        "password": "123"}})
    check("热点密码过短被拒绝",
          any("热点密码长度需为 8-63" in e for e in errs))
    errs = app.save_config({"campus": {"username": "u1", "password": "p1"},
                            "hotspot": {"enabled": True, "ssid": "Pi",
                                        "password": "12345678"}})
    check("合法配置保存成功", not errs)
    check("ISP 回退到第一个", app.config.data["campus"]["isp"] in
          app.config.data["campus"]["isps"])
    errs = app.save_config({"campus": {"password": ""}})  # 空密码=保持
    check("空密码保持原值", not errs
          and app.config.data["campus"]["password"] == "p1")
    check("数字越界拒绝", "campus.check_interval 需在 60-86400 之间"
          in app.save_config({"campus": {"check_interval": 5}}))
    check("用户名必填", "校园网用户名不能为空"
          in app.save_config({"campus": {"username": ""}}))
    check("drcom 端口非法拒绝", "drcom.port 必须是整数"
          in app.save_config({"drcom": {"port": "abc"},
                              "campus": {"username": "u", "password": "p"}}))
    check("drcom 端口越界拒绝", "drcom.port 需在 1-65535 之间"
          in app.save_config({"drcom": {"port": 99999},
                              "campus": {"username": "u", "password": "p"}}))
    check("web.bind 非法拒绝", "web.bind 必须是合法 IP 地址"
          in app.save_config({"web": {"bind": "evil.example.com"},
                              "campus": {"username": "u", "password": "p"}}))
    check("web.bind 合法通过", not app.save_config(
        {"web": {"bind": "192.168.9.1"},
         "campus": {"username": "u", "password": "p"}}))
    check("校验失败不污染内存配置",
          app.config.data["drcom"]["port"] == 801
          and app.config.data["web"]["bind"] == "192.168.9.1")
    # 状态接口在 Windows 上也不应崩溃
    st = app.status()
    check("status() 可用", isinstance(st, dict) and "online" in st)
    # 静默时段判断（时间可信时）
    app.config.data["quiet_hours"]["enabled"] = True
    check("quiet_hours_active() 可调用",
          isinstance(app.quiet_hours_active(), bool))


def main():
    test_config()
    test_quiet_hours()
    test_drcom()
    test_eportal()
    test_srun()
    test_ip()
    test_app_save()
    print("\n%d 通过, %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
