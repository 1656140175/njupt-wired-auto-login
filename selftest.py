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
    check("哆点默认走新版 ePortal JSONP 接口",
          c.data["drcom"]["host"] == "10.10.244.11"
          and c.data["drcom"]["port"] == 801
          and c.data["drcom"]["api_path"] == "/eportal/portal/login"
          and c.data["drcom"]["api_logout_path"] == "/eportal/portal/logout"
          and c.data["drcom"]["account_prefix"] == ",0,")
    check("旧版表单回退参数保留",
          c.data["drcom"]["path"] == "/eportal/?c=ACSetting&a=Login"
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
    p = Portal(None)
    p.local_ip = lambda *a: "10.1.2.3"       # Windows 上没有 ip 命令
    cfg = deep_merge(config_mod.DEFAULTS, {
        "campus": {"adapter": "drcom", "username": "B23010101",
                   "password": "pw123", "isp": "telecom"},
        "drcom": {"host": "10.10.244.11", "port": 801}})
    captured = {}

    def fake_fetch(url, params=None, data=None, timeout=8):
        captured["url"] = url
        captured["params"] = params or {}
        return 'jsonpReturn({"result":1,"msg":"\\u767b\\u5f55\\u6210\\u529f"});'
    p._fetch = fake_fetch
    ok, msg = p._drcom_login(cfg)
    check("新版 ePortal 登录成功", ok, msg)
    check("新版接口路径", captured["url"].endswith("/eportal/portal/login"),
          captured.get("url"))
    check("账号带 ,0, 前缀和后缀",
          captured["params"].get("user_account") == ",0,B23010101@njxy",
          captured["params"].get("user_account"))
    check("密码明文参数", captured["params"].get("user_password") == "pw123")
    check("wlan_user_ip 参数", captured["params"].get("wlan_user_ip") == "10.1.2.3")
    check("jsVersion 参数", captured["params"].get("jsVersion") == "4.1.3")
    # 账号/运营商错误
    p._fetch = lambda *a, **k: (
        'jsonpReturn({"result":0,"ret_code":1,'
        '"msg":"账号错误(运营商登录请检查输入的账号和绑定的运营商账号是否有误)"});')
    ok, msg = p._drcom_login(cfg)
    check("账号错误判失败并透传 msg", (not ok) and "账号错误" in msg, msg)
    check("last_ret_code=1", p.last_ret_code == 1, p.last_ret_code)
    # 终端已在线
    p._fetch = lambda *a, **k: (
        'jsonpReturn({"result":0,"ret_code":2,"msg":"终端IP已经在线"})')
    ok, msg = p._drcom_login(cfg)
    check("终端已在线视为成功", ok and "在线" in msg, msg)
    # 新版路径返回管理页 HTML：必须明确报错，不能当成功
    p._fetch = lambda *a, **k: (
        '<!DOCTYPE html><html><head><title>EPortal</title></head></html>')
    ok, msg = p._drcom_login(cfg)
    check("EPortal 管理页提示接口变更",
          (not ok) and "管理页" in msg, msg)
    # 非 JSON -> 回退旧版表单
    p._fetch = lambda *a, **k: "not json"
    p._fetch_resp = lambda *a, **k: ("", "http://x/Dr.COMWebLoginID_3.htm")
    ok, msg = p._drcom_login(cfg)
    check("新版异常回退旧版成功", ok, msg)
    p._fetch_resp = lambda *a, **k: ("", "http://x/Dr.COMWebLoginID_2.htm")
    ok, msg = p._drcom_login(cfg)
    check("旧版回退失败判定", not ok and "失败" in msg, msg)
    p._fetch_resp = lambda *a, **k: ("密码错误，请重试", "http://x/")
    ok, msg = p._drcom_login(cfg)
    check("旧版正文错误关键字判失败", not ok, msg)
    # 无 IP
    p.local_ip = lambda *a: "0.0.0.0"
    ok, msg = p._drcom_login(cfg)
    check("无 IP 返回明确提示", not ok and "无 IP" in msg, msg)
    # 注销：新版
    p.local_ip = lambda *a: "10.1.2.3"
    p._last_url = None
    def capture(url, params=None, data=None, timeout=8):
        p._last_url = url
        return 'jsonpReturn({"result":1,"msg":"注销成功"});'
    p._fetch = capture
    ok, msg = p.logout(cfg)
    check("新版注销成功", ok and "注销成功" in msg, msg)
    check("新版注销路径", p._last_url.endswith("/eportal/portal/logout"),
          p._last_url)
    # 注销回退
    p._fetch = lambda *a, **k: "not json"
    p._fetch_resp = lambda *a, **k: ("", "http://x/")
    ok, msg = p.logout(cfg)
    check("注销回退旧版", ok and "已发送" in msg, msg)


# ----------------------------------------------------- 安全/探测辅助
def test_helpers():
    import urllib.error
    p = Portal(None)
    cfg = deep_merge(config_mod.DEFAULTS, {})
    check("识别 a79.htm JS 劫持页",
          p._is_captive("http://www.qq.com/",
                        '<script>location.href="http://10.10.244.11/a79.htm"'
                        "</script>", cfg))
    check("正常页面不误判",
          not p._is_captive("http://www.qq.com/", "<html>hello</html>", cfg))
    err = urllib.error.HTTPError(
        "http://x/eportal/portal/login?user_password=secret", 500,
        "err", {}, None)
    check("HTTPError 日志不含密码", "secret" not in Portal._safe_err(err))

    class E(Exception):
        pass
    text = Portal._safe_err(
        E("failed http://x/eportal/portal/login?user_password=secret"))
    check("普通异常日志不含密码", "secret" not in text, text)


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
    check("status 含 RDP 状态",
          isinstance(st.get("rdp"), dict) and "installed" in st["rdp"])
    check("status 含延迟优化状态", isinstance(st.get("tuning"), dict))
    rdp_text = app.rdp_file_text("192.168.9.1")
    check("RDP 文件含连接地址",
          "full address:s:192.168.9.1:3389" in rdp_text)
    check("RDP 文件要求输入密码",
          "prompt for credentials:i:1" in rdp_text)
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
    test_helpers()
    test_ip()
    test_app_save()
    print("\n%d 通过, %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
