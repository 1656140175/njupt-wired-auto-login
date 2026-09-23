# -*- coding: utf-8 -*-
"""校园网认证适配器。

- drcom（哆点 Dr.COM，南邮当前实际使用，默认）：
    GET http://10.10.244.11:801/eportal/?c=ACSetting&a=Login
        &DDDDD=<学号+运营商后缀>&upass=<密码>&0MKKey=123456
    单步、明文、无 challenge。登录后服务器 302 跳转：
    Dr.COMWebLoginID_3.htm=成功 / Dr.COMWebLoginID_2.htm=失败（以最终 URL 判定）。
    参数名/路径来自 a79.htm 页面配置，可在 Web 后台修改。
- eportal（锐捷，备用）：
    GET http://host:801/eportal/portal/login?login_method=1&user_account=,0,<账号>&user_password=<密码>
- srun（深澜）：
    1) GET  {host}/cgi-bin/get_challenge  -> 取 challenge / acid / enc
    2) POST {host}/cgi-bin/srun_portal     -> res=ok 即成功
    密码编码（enc）支持 auto 自动嗅探：md5 / token$md5 / base64 / token$base64 顺序尝试。
- generic（通用表单）：完全由配置文件驱动 login_url + 占位符参数，兜底用。
"""
import base64
import hashlib
import json
import logging
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from utils import get_ip

log = logging.getLogger("campuspi.portal")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _decode(body):
    """门户响应多为 GBK(gb2312)，部分为 UTF-8：按序尝试。"""
    for enc in ("gbk", "utf-8"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "ignore")


def strip_jsonp(text):
    """去掉 jsonp 回调包装返回解析对象；纯 JSON 也兼容。"""
    stripped = text.strip()
    match = re.search(r"^[\w$.\[\]'\" ]*\((\{.*\})\)\s*;?\s*$", stripped, re.S)
    if match:
        return json.loads(match.group(1))
    return json.loads(stripped)


class Portal:
    def __init__(self, app):
        self.app = app

    # ------------------------------------------------------------------ 探测
    @staticmethod
    def _no_proxy_opener():
        """禁用代理直连（树莓派上一般无代理，双保险）。"""
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _gateway_hostport(self, cfg):
        """从配置解析网关 host/port（drcom 优先，eportal 兜底）。"""
        dr = cfg.get("drcom") or {}
        host = dr.get("host") or (cfg.get("eportal") or {}).get("host") or ""
        port = int(dr.get("port") or (cfg.get("eportal") or {}).get("port") or 801)
        return host, port

    def _is_captive(self, final_url, body, cfg):
        """响应是否被网关劫持（未认证特征：跳到认证页/返回认证页内容）。
        端点匹配用 host:port（劫持跳转 URL 必带端口，避免误伤探测 URL 自身）。"""
        text = (final_url + " " + body).lower()
        host, port = self._gateway_hostport(cfg)
        endpoint = "%s:%d" % (host, port) if host else ""
        return ("eportal" in text or "loginid" in text
                or (endpoint and endpoint in text)
                or "dr.com" in text or "认证" in text)

    @staticmethod
    def _socket_reachable(host, port, timeout=3):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False

    @staticmethod
    def _ping(host, timeout=2):
        """ICMP ping 网关（只查返回码）。"""
        if shutil.which("ping") is None:
            return False
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

    def _link_up(self, host, port, timeout=3):
        """物理链路判断：ping 网关；部分网关禁 ICMP，回退 TCP 端口直连。"""
        return self._ping(host, timeout) or \
            self._socket_reachable(host, port, timeout)

    def probe_network(self, cfg=None, timeout=5):
        """三态探测（校园网专用，不依赖"未登录能上网"的错误假设）：
          "online"      链路通且外网可访问（已认证） -> 无事
          "need_login"  链路通但未认证（外网不通）   -> 登录（静默时段也登录）
          "offline"     链路断（ping 网关都不通）    -> 静默时段跳过
        第 0 层 ping 网关判链路；链路通后再用外网 HTTP 探测判认证状态
        （未认证时 HTTP 被网关劫持/阻断，可识别）。
        """
        cfg = cfg or self.app.config.data
        host, port = self._gateway_hostport(cfg)
        if not host or not self._link_up(host, port, 3):
            return "offline", "ping 网关 %s 不通（链路断）" % (host or "?")

        opener = self._no_proxy_opener()
        urls = cfg["campus"].get("probe_urls") or []
        for item in urls:
            if isinstance(item, str):
                url, marker = item, None
            else:
                url = item.get("url", "")
                marker = item.get("marker")
            if not url:
                continue
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
                resp = opener.open(req, timeout=timeout)
                status, final, body = resp.getcode(), resp.geturl(), \
                    _decode(resp.read(1024))
            except urllib.error.HTTPError as exc:
                status, final, body = exc.code, exc.geturl() or url, \
                    _decode(exc.read(512))
            except Exception as exc:
                log.debug("探测 %s 失败: %s", url, exc)
                continue
            if self._is_captive(final, body, cfg):
                return "need_login", "%s 被劫持到认证页" % url
            ok = (status == 204 or marker in body
                  or (marker is None and status < 400 and len(body) >= 200))
            if ok:
                return "online", "探测成功 %s (%d)" % (url, status)
            log.debug("探测 %s 返回 %d，未判定在线", url, status)
        # 链路通但外网全不通 = 未认证（外网被网关阻断）
        return "need_login", "网关 %s 可达但外网不通（未认证）" % host

    def probe_online(self, cfg=None, timeout=5):
        """兼容旧接口：是否在线（online=True，其余 False）。"""
        state, msg = self.probe_network(cfg, timeout)
        return state == "online", msg

    # ------------------------------------------------------------------ 登录
    def login(self, cfg=None):
        cfg = cfg or self.app.config.data
        campus = cfg["campus"]
        adapter = campus.get("adapter", "drcom")
        if adapter == "generic":
            return self._generic_login(cfg)
        if adapter == "srun":
            return self._srun_login(cfg)
        if adapter == "eportal":
            return self._eportal_login(cfg)
        return self._drcom_login(cfg)

    def _fetch_resp(self, url, params=None, data=None, timeout=8):
        """同 _fetch，但返回 (body, 最终URL)；用于哆点 302 跳转页判定。"""
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        # 部分哆点网关校验 Referer，带上同源站点根更稳
        m = re.match(r"(https?://[^/]+)", url)
        headers["Referer"] = (m.group(1) + "/") if m else url
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode(data).encode("utf-8"),
                headers=headers, method="POST")
        else:
            req = urllib.request.Request(url, headers=headers)
        with self._no_proxy_opener().open(req, timeout=timeout) as resp:
            return _decode(resp.read(65536)), resp.geturl()

    # --------------------------------------------------------------- drcom
    def _drcom_login(self, cfg):
        """南邮哆点 Dr.COM：GET 单步、明文，302 跳转页判定。"""
        campus = cfg["campus"]
        base_username = (campus.get("username") or "").strip()
        password = campus.get("password") or ""
        if not base_username or not password:
            return False, "未配置校园网账号/密码"
        dr = cfg.get("drcom", {})
        host = dr.get("host", "")
        if not host:
            return False, "未配置哆点网关地址(host)"
        isp = (campus.get("isps") or {}).get(campus.get("isp", "campus")) or {}
        username = base_username + str(isp.get("username_suffix") or "")
        params = {dr.get("user_field", "DDDDD"): username,
                  dr.get("pass_field", "upass"): password}
        params.update({str(k): str(v) for k, v in (dr.get("extra_params") or {}).items()})
        # 页面 f0 表单 method=post：与浏览器一致用 POST 提交
        base = "%s://%s:%s%s" % (dr.get("scheme", "http"), host,
                                 dr.get("port", 801),
                                 dr.get("path", "/eportal/?c=ACSetting&a=Login"))
        try:
            body, final_url = self._fetch_resp(base, None, params, timeout=10)
        except Exception as exc:
            return False, "哆点登录请求失败: %s" % exc
        low = (body + " " + final_url).lower()
        for key in dr.get("fail_keys") or ["2.htm"]:
            if key.lower() in low:
                # 网关返回的正文是诊断关键，完整打进日志
                body_preview = body.replace("\n", " ")[:200]
                log.warning("哆点登录失败: %s | 网关响应: %s",
                            final_url[:120], body_preview)
                return False, "哆点返回失败: %s" % body_preview
        for key in dr.get("success_keys") or ["3.htm"]:
            if key.lower() in low:
                return True, "哆点登录成功（%s）" % final_url[:80]
        # 无明确标志也视为已受理，最终以登录后探测为准
        return True, "哆点登录请求已发送（等待在线探测确认）"

    def logout(self, cfg=None):
        """哆点注销（配置里 authlogoutpath）。返回 (ok, 说明)。"""
        cfg = cfg or self.app.config.data
        dr = cfg.get("drcom", {})
        host = dr.get("host", "")
        if not host:
            return False, "未配置哆点网关地址"
        url = "%s://%s:%s%s" % (dr.get("scheme", "http"), host,
                                dr.get("port", 801),
                                dr.get("logout_path",
                                       "/eportal/?c=ACSetting&a=Logout&ver=1.0"))
        try:
            body, final_url = self._fetch_resp(url, timeout=8)
        except Exception as exc:
            return False, "注销请求失败: %s" % exc
        logging.info("哆点注销已发送: %s", final_url[:100])
        return True, "注销请求已发送"

    # -------------------------------------------------------------- eportal
    def _eportal_login(self, cfg):
        campus = cfg["campus"]
        base_username = (campus.get("username") or "").strip()
        password = campus.get("password") or ""
        if not base_username or not password:
            return False, "未配置校园网账号/密码"
        ep = cfg.get("eportal", {})
        host = ep.get("host", "")
        if not host:
            return False, "未配置 eportal 网关地址(host)"
        isp = (campus.get("isps") or {}).get(campus.get("isp", "campus")) or {}
        username = base_username + str(isp.get("username_suffix") or "")
        # 账号后缀中的 @ 保持原样（锐捷网关对 URL 解码不严格，参考官方脚本直发）
        url = "%s://%s:%s%s?login_method=%s&user_account=,0,%s&user_password=%s" % (
            ep.get("scheme", "http"), host, ep.get("port", 801),
            ep.get("path", "/eportal/portal/login"),
            ep.get("login_method", "1"),
            urllib.parse.quote(username, safe="@"),
            urllib.parse.quote(password, safe=""))
        try:
            body = self._fetch(url, timeout=10)
        except Exception as exc:
            return False, "eportal 登录请求失败: %s" % exc
        low = body.lower()
        fail_keys = ep.get("fail_keys") or ["error", "fail"]
        for key in fail_keys:
            if key.lower() in low:
                return False, "eportal 返回错误: %s" % body[:150]
        # 响应无错误关键字即视为已受理，最终以登录后探测为准
        return True, "eportal 登录请求已发送（等待在线探测确认）"

    def local_ip(self, iface=None):
        if iface is None and self.app is not None:
            iface = self.app.config.data["campus"]["iface"]
        return get_ip(iface) or "0.0.0.0"

    def _fetch(self, url, params=None, data=None, timeout=8):
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode(data).encode("utf-8"),
                headers=headers, method="POST")
        else:
            req = urllib.request.Request(url, headers=headers)
        with self._no_proxy_opener().open(req, timeout=timeout) as resp:
            return _decode(resp.read(65536))

    # ------------------------------------------------------------------ srun
    def _srun_login(self, cfg):
        campus = cfg["campus"]
        base_username = (campus.get("username") or "").strip()
        password = campus.get("password") or ""
        if not base_username or not password:
            return False, "未配置校园网账号/密码"
        host = campus.get("host", "")
        if not host:
            return False, "未配置认证服务器地址(host)"
        base = "%s://%s" % (campus.get("protocol", "https"), host)
        ip = self.local_ip()
        if ip == "0.0.0.0":
            return False, "校园网接口 %s 无 IP" % campus.get("iface", "eth0")

        isp = (campus.get("isps") or {}).get(campus.get("isp", "campus")) or {}
        # 运营商 = 账号后缀（南邮机制）；ac_id / extra 兼容其他部署
        username = base_username + str(isp.get("username_suffix") or "")
        acid = str(isp.get("ac_id", "1"))
        extra = isp.get("extra") or {}

        # 1) 获取 challenge
        ch_url = base + campus.get("challenge_path", "/cgi-bin/get_challenge")
        ch_params = {"callback": "jsonp", "username": username, "ip": ip,
                     "_": int(time.time() * 1000)}
        try:
            obj = strip_jsonp(self._fetch(ch_url, ch_params))
        except Exception as exc:
            return False, "获取 challenge 失败: %s" % exc
        challenge = obj.get("challenge")
        if not challenge:
            return False, "challenge 为空: %s" % str(obj)[:200]
        if obj.get("acid"):
            acid = str(obj["acid"])

        # 2) 计算候选密码编码
        variants = self._pwd_variants(campus.get("enc", "auto"),
                                      password, challenge)
        server_enc = obj.get("enc")
        if (campus.get("enc", "auto") == "auto"
                and server_enc in ("md5", "base64")):
            # 服务器指定了编码方式 -> 优先尝试，其余兜底
            preferred = [v for v in variants if v[0].endswith(server_enc)]
            variants = preferred + [v for v in variants if v not in preferred]

        info = base64.b64encode(json.dumps({
            "username": username, "password": password, "ip": ip,
            "acid": acid, "enc_ver": "srun_bx1",
        }).encode("utf-8")).decode("ascii")

        login_url = base + campus.get("login_path", "/cgi-bin/srun_portal")
        last_err = "未知错误"
        for label, pwd_param in variants:
            data = {
                "callback": "jsonp", "action": "login", "username": username,
                "password": pwd_param, "ac_id": acid, "ip": ip, "info": info,
                "n": "200", "type": "1", "os": "Windows", "name": "Windows",
                "double_stack": "0", "_": int(time.time() * 1000),
            }
            data.update({str(k): str(v) for k, v in extra.items()})
            try:
                obj = strip_jsonp(self._fetch(login_url, None, data))
            except Exception as exc:
                last_err = "登录请求异常: %s" % exc
                continue
            if obj.get("res") == "ok":
                return True, "登录成功（%s，运营商=%s）" % (label, campus.get("isp"))
            last_err = str(obj.get("error_msg") or obj.get("error")
                           or "res=%s" % obj.get("res"))[:200]
        return False, "登录失败: %s" % last_err

    @staticmethod
    def _pwd_variants(enc, password, challenge):
        md5p = hashlib.md5((password + challenge).encode("utf-8")).hexdigest()
        b64p = base64.b64encode(
            (password + challenge).encode("utf-8")).decode("ascii")
        table = {
            "md5": [("md5", md5p)],
            "token_md5": [("token$md5", "%s$%s" % (challenge, md5p))],
            "base64": [("base64", b64p)],
            "token_base64": [("token$base64", "%s$%s" % (challenge, b64p))],
            "auto": [("md5", md5p),
                     ("token$md5", "%s$%s" % (challenge, md5p)),
                     ("base64", b64p),
                     ("token$base64", "%s$%s" % (challenge, b64p))],
        }
        return table.get(enc, table["auto"])

    # -------------------------------------------------------------- 通用表单
    def _generic_login(self, cfg):
        campus = cfg["campus"]
        gen = cfg.get("generic", {})
        username = (campus.get("username") or "").strip()
        password = campus.get("password") or ""
        if not username or not password:
            return False, "未配置校园网账号/密码"
        url = gen.get("login_url", "")
        if not url:
            return False, "未配置通用登录 URL"
        isp = campus.get("isp", "")
        acid = str(((campus.get("isps") or {}).get(isp) or {}).get("ac_id", "1"))
        ip = self.local_ip(campus.get("iface", "eth0"))

        def substitute(value):
            return str(value).replace("{username}", username) \
                .replace("{password}", password) \
                .replace("{isp}", isp) \
                .replace("{acid}", acid) \
                .replace("{ip}", ip)

        params = {str(k): substitute(v) for k, v in (gen.get("params") or {}).items()}
        try:
            if str(gen.get("method", "POST")).upper() == "GET":
                body = self._fetch(url, params)
            else:
                body = self._fetch(url, None, params)
        except Exception as exc:
            return False, "登录请求失败: %s" % exc
        key = gen.get("success_key", "")
        if not key or key in body:
            return True, "登录成功（通用适配器）"
        return False, "响应未包含成功标记 %r: %s" % (key, body[:150])
