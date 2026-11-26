# -*- coding: utf-8 -*-
"""校园网认证适配器。

- drcom（哆点 Dr.COM，南邮当前实际使用，默认）：
    2026-11 网关已升级为新版 ePortal（a79.htm 的 page.portal_api）：
    GET http://10.10.244.11:801/eportal/portal/login
        ?callback=jsonpReturn&login_method=1
        &user_account=,0,<学号+运营商后缀>&user_password=<密码>
        &wlan_user_ip=<本机校园网 IP>&wlan_user_mac=<MAC>&jsVersion=4.1.3
    返回 JSONP（jsonpReturn({...})）：result=1/ok 成功；失败看 ret_code + msg。
    旧版 /eportal/?c=ACSetting&a=Login 已经不再处理登录（现在返回 EPortal
    管理页 HTML），仅作为其他学校旧网关的回退。
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

from utils import get_ip, get_mac, iface_exists

log = logging.getLogger("campuspi.portal")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _decode(body):
    """门户响应 UTF-8 / GBK 混杂：先试 UTF-8，失败再按 GBK 解码。
    （顺序不能反：UTF-8 中文按 GBK 通常"能解码"但得到乱码，反不过来。）"""
    for enc in ("utf-8", "gbk"):
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
        # 新版 ePortal 最近一次登录的 ret_code（供上层判断是否值得重试）
        self.last_ret_code = None

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
        端点匹配用 host:port（劫持跳转 URL 必带端口，避免误伤探测 URL 自身）。
        南邮实际劫持页有两种：
          1) <script>location.href="http://10.10.244.11/a79.htm"</script>
             （URL 不变、无 302，只靠 a79.htm 识别！）
          2) 302 到 a79.htm / ePortal 页面
        """
        text = (final_url + " " + body).lower()
        host, port = self._gateway_hostport(cfg)
        endpoint = "%s:%d" % (host, port) if host else ""
        return ("eportal" in text or "loginid" in text
                or "a79.htm" in text
                or "authentication is required" in text
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
        # 校园网接口没有 IP 时，即使别的接口能通网关也不能算在线；
        # 此时登录必然失败（wlan_user_ip 为空/链路本地）。
        iface = (cfg.get("campus") or {}).get("iface", "eth0")
        if iface_exists(iface) and not self.local_ip(iface):
            return "offline", "校园网接口 %s 无 IP（等待 DHCP）" % iface
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
    @staticmethod
    def _safe_err(exc):
        """异常文本脱敏：新版接口密码在 URL 查询串里，绝不能写进日志。"""
        try:
            if isinstance(exc, urllib.error.HTTPError):
                return "HTTP %d" % exc.code
            text = re.sub(r"\?[^\s'\"]*", "?…", str(exc))
            return text[:200]
        except Exception:
            return exc.__class__.__name__

    def _drcom_login(self, cfg):
        """哆点登录：优先新版 ePortal JSONP 接口，接口不可用时回退旧版表单。

        南邮 2026-11 网关升级后，旧路径 /eportal/?c=ACSetting&a=Login
        只会返回 EPortal 管理页 HTML；a79.htm 页面已改用
        page.portal_api + 'login'，即 /eportal/portal/login。
        """
        self.last_ret_code = None
        campus = cfg["campus"]
        base_username = (campus.get("username") or "").strip()
        password = campus.get("password") or ""
        if not base_username or not password:
            return False, "未配置校园网账号/密码"
        dr = cfg.get("drcom") or {}
        host = dr.get("host", "")
        if not host:
            return False, "未配置哆点网关地址(host)"
        isp = (campus.get("isps") or {}).get(campus.get("isp", "campus")) or {}
        username = base_username + str(isp.get("username_suffix") or "")
        # 不要根据 a79.htm 的静态 carrier 列表清空后缀。
        # 实际在线账号可能是 学号@cmcc（移动绑定），即使页面显示“无后缀”：
        # 在线会话里 user_account=<学号>@cmcc，isp_bind_account 也是手机号。
        # 后缀以配置 campus.isp 为准；a79 的 carrier 只用于失败诊断。
        if not str(isp.get("username_suffix") or "").strip():
            log.info("当前配置运营商为无后缀，使用纯学号登录")
        else:
            log.info("使用配置的运营商后缀 %r",
                     isp.get("username_suffix"))
        iface = campus.get("iface", "eth0")
        ip = self.local_ip(iface)
        if not ip or ip == "0.0.0.0":
            return False, ("校园网接口 %s 无 IP：请检查网线/DHCP 后再试"
                           % iface)

        # a79.htm 的 a41.js 里 page.path='/drcom/'、login_method=0，
        # 所以当前网关真正的登录接口是 /drcom/login（带 default_login 的全套
        # 字段），不是 /eportal/portal/login。先试这个，接口不存在再回退。
        drcom_path = str(dr.get("drcom_path") or "").strip()
        if drcom_path:
            result = self._drcom_login_drcom(dr, drcom_path, username,
                                             password, ip, iface)
            if result is not None:
                return result
            log.warning("Dr.COM /drcom/login 无有效响应，回退 ePortal 接口")

        api_path = str(dr.get("api_path") or "").strip()
        if api_path:
            result = self._drcom_login_ep(dr, api_path, username, password,
                                          ip, iface)
            if result is not None:
                ok, msg = result
                if ok:
                    return ok, msg
                # ePortal 的 ret_code=1 信息太笼统（“认证出现异常”）。
                # 旧版表单会返回具体原因，例如：
                #   运营商账号在线数超出限制... (Rad:Limit Users Err)
                #   本账号费用超支，禁止使用
                # 因此再用旧版表单试一次，并把它作为最终结果。
                log.warning("ePortal 登录失败: %s；改用旧版表单获取详细结果",
                            msg)
            else:
                log.warning("新版 ePortal 登录接口无有效响应，回退旧版表单登录")
        else:
            log.warning("未配置 drcom.api_path，使用旧版表单登录")
        return self._drcom_login_legacy(dr, username, password, ip, iface)

    def _carrier_options(self, dr):
        """从 a79.htm 读取网关当前给出的运营商名称/后缀（诊断用）。

        在树莓派的有线网络里读取到的就是该入口真实的选项；账号/运营商
        报错时把它打进日志，用户照着改 Web 后台即可。
        """
        host = dr.get("host", "")
        if not host:
            return []
        try:
            body = self._fetch("http://%s/a79.htm" % host, timeout=5)
        except Exception:
            return []
        match = re.search(r"carrier='([^']+)'", body, re.S)
        if not match:
            return []
        try:
            obj = json.loads(match.group(1))
        except ValueError:
            return []
        out = []
        for item in ((obj.get("yys") or {}).get("data") or []):
            if isinstance(item, dict):
                out.append((str(item.get("name") or ""),
                            str(item.get("suffix") or "")))
        return out

    def _drcom_login_drcom(self, dr, path, username, password, ip, iface):
        """Dr.COM 页面 login_method=0 的 /drcom/login 接口。

        a79.htm 引用的 a41.js 里 page.path = .../drcom/、login_method = 0，
        default_login() 实际请求的是 <host>:801/drcom/login，并携带
        DDDDD/upass/0MKKey/R1/R2/R3/R6/v4ip/v6ip 以及 user_account 等字段。
        这里按页面字段原样发送；接口不存在/非 JSON 时返回 None，交给
        ePortal 接口回退。"""
        scheme = dr.get("scheme", "http")
        host = dr.get("host", "")
        port = dr.get("port", 801)
        url = "%s://%s:%s%s" % (scheme, host, port, path)
        mac = (dr.get("wlan_user_mac") or get_mac(iface)
               or "000000000000").replace(":", "").lower()
        params = {
            "callback": dr.get("callback", "jsonpReturn"),
            "DDDDD": username,
            "upass": password,
            "0MKKey": "123456",
            "R1": "0",
            "R2": "",
            "R3": "0",
            "R6": "0",
            "v4ip": ip,
            "v6ip": "",
            "user_account": str(dr.get("account_prefix", ",0,")) + username,
            "user_password": password,
            "wlan_user_ip": ip,
            "wlan_user_ipv6": "",
            "wlan_user_mac": mac,
            "wlan_ac_ip": "",
            "wlan_ac_name": "",
            "jsVersion": str(dr.get("js_version", "4.1.3")),
            "terminal_type": "1",       # 1=PC
            "lang": "zh",
            "operate": "portal_login",
            "login_method": "0",
        }
        params.update({str(k): str(v)
                       for k, v in (dr.get("drcom_extra_params") or {}).items()})
        try:
            body = self._fetch(url, params, timeout=10)
        except Exception as exc:
            log.warning("Dr.COM /drcom/login 请求失败: %s", self._safe_err(exc))
            return None
        try:
            obj = strip_jsonp(body)
        except Exception:
            preview = body.replace("\n", " ")[:160]
            if "<title>EPortal</title>" in body \
                    or "/eportal/public/static" in body:
                log.warning("Dr.COM /drcom/login 返回管理页 HTML: %s", preview)
            else:
                log.warning("Dr.COM /drcom/login 返回非 JSON: %s", preview)
            return None
        if not isinstance(obj, dict):
            return None
        result = obj.get("result")
        if result in (1, "1", "ok", True):
            return True, "登录成功（Dr.COM /drcom/login）"
        if "result" not in obj and "ret_code" not in obj:
            msg = str(obj.get("msg") or "")
            if "404" in msg and "not found" in msg.lower():
                log.warning("Dr.COM /drcom/login 不存在: %s", msg[:150])
            return None
        ret = obj.get("ret_code")
        try:
            ret_int = int(ret)
        except (TypeError, ValueError):
            ret_int = None
        self.last_ret_code = ret_int
        msg = str(obj.get("msg") or obj.get("error") or "未知错误").strip()
        if ret_int == 2 or "已经在线" in msg:
            return True, "终端 IP 已在线（%s）" % (msg or "ret_code=2")
        log.warning("Dr.COM /drcom/login 登录失败: result=%s ret_code=%s msg=%s",
                    result, ret, msg[:200])
        hint = ""
        if ret_int in (1, 4):
            options = self._carrier_options(dr)
            if options:
                hint = "；可用后缀：" + " / ".join(
                    suffix or "无后缀" for _, suffix in options)
        return False, "登录失败: %s%s" % (msg[:200] or "未知错误", hint)

    def _drcom_login_ep(self, dr, api_path, username, password, ip, iface):
        """新版 ePortal JSONP 登录。
        返回 (ok, msg)；接口不存在/返回非 JSON 时返回 None（交给旧版回退）。"""
        scheme = dr.get("scheme", "http")
        host = dr.get("host", "")
        port = dr.get("port", 801)
        url = "%s://%s:%s%s" % (scheme, host, port, api_path)
        mac = (dr.get("wlan_user_mac") or get_mac(iface)
               or "000000000000").replace(":", "").lower()
        # 与 a79.htm 里 login.login_portal() 的 data 完全一致
        params = {
            "callback": dr.get("callback", "jsonpReturn"),
            "login_method": str(dr.get("login_method", "1")),
            "is_base64encode": "0",
            "user_account": str(dr.get("account_prefix", ",0,")) + username,
            "user_password": password,
            "wlan_user_ip": ip,
            "wlan_user_ipv6": "",
            "wlan_user_mac": mac,
            "wlan_vlan_id": "0",
            "wlan_ac_ip": "",
            "wlan_ac_name": "",
            "authex_enable": "",
            "jsVersion": str(dr.get("js_version", "4.5.1")),
            "terminal_type": "1",       # 1=PC（手机/平板为 2/3）
            "lang": "zh",
            "uuid": "",
        }
        # 新版专用附加参数（不要把旧版表单字段 0MKKey 等塞进来）
        params.update({str(k): str(v)
                       for k, v in (dr.get("api_extra_params") or {}).items()})
        try:
            body = self._fetch(url, params, timeout=10)
        except Exception as exc:
            log.warning("新版 ePortal 登录请求失败: %s", self._safe_err(exc))
            return None
        try:
            obj = strip_jsonp(body)
        except Exception:
            preview = body.replace("\n", " ")[:160]
            if "<title>EPortal</title>" in body \
                    or "/eportal/public/static" in body:
                log.error("新版 ePortal 接口 %s 返回了管理页 HTML："
                          "登录接口地址可能已变更", api_path)
                return False, "网关返回 EPortal 管理页：登录接口地址可能已变更"
            log.warning("新版 ePortal 返回非 JSON: %s", preview)
            return None
        if not isinstance(obj, dict):
            return None
        result = obj.get("result")
        if result in (1, "1", "ok", True):
            return True, "登录成功（新版 ePortal）"
        if "result" not in obj and "ret_code" not in obj:
            msg = str(obj.get("msg") or "")
            if "404" in msg and "not found" in msg.lower():
                log.error("新版 ePortal 登录接口 %s 不存在: %s",
                          api_path, msg[:150])
                return False, "登录接口不存在: %s" % msg[:150]
            return None            # 不认识的结构 -> 交给旧版回退
        ret = obj.get("ret_code")
        try:
            ret_int = int(ret)
        except (TypeError, ValueError):
            ret_int = None
        self.last_ret_code = ret_int
        msg = str(obj.get("msg") or obj.get("error") or "未知错误").strip()
        if ret_int == 2 or "已经在线" in msg:
            return True, "终端 IP 已在线（%s）" % (msg or "ret_code=2")
        log.warning("新版 ePortal 登录失败: result=%s ret_code=%s msg=%s",
                    result, ret, msg[:200])
        hint = ""
        if ret_int in (1, 4):
            options = self._carrier_options(dr)
            if options:
                log.warning(
                    "网关登录页（a79.htm）当前运营商选项：%s",
                    "、".join("%s(%s)" % (name, suffix or "无后缀")
                             for name, suffix in options))
                hint = "；可用后缀：" + " / ".join(
                    suffix or "无后缀" for _, suffix in options)
        return False, "登录失败: %s%s" % (msg[:200] or "未知错误", hint)

    def _drcom_login_legacy(self, dr, username, password, ip="", iface=None):
        """旧版表单登录（a79.htm 的 authloginpath，POST 到 801）。

        除了老网关，这个接口还能给出 ePortal JSONP 不会返回的具体失败原因
        （在线数超限/欠费/账号绑定等），所以也作为 ePortal 失败后的诊断回退。"""
        self.last_ret_code = None
        if iface and not ip:
            ip = get_ip(iface) or ""
        mac = (dr.get("wlan_user_mac") or (get_mac(iface) if iface else "")
               or "").replace(":", "").lower()
        params = {dr.get("user_field", "DDDDD"): username,
                  dr.get("pass_field", "upass"): password}
        params.update({
            "user_account": str(dr.get("account_prefix", ",0,")) + username,
            "user_password": password,
            "wlan_user_ip": ip,
            "wlan_user_ipv6": "",
            "wlan_user_mac": mac,
            "wlan_ac_ip": "",
            "wlan_ac_name": "",
            "jsVersion": str(dr.get("js_version", "4.5.1")),
            "terminal_type": "1",
            "operate": "portal_login",
        })
        params.update({str(k): str(v)
                       for k, v in (dr.get("extra_params") or {}).items()})
        base = "%s://%s:%s%s" % (dr.get("scheme", "http"), dr.get("host", ""),
                                 dr.get("port", 801),
                                 dr.get("path",
                                        "/eportal/?c=ACSetting&a=Login"))
        try:
            body, final_url = self._fetch_resp(base, None, params, timeout=10)
        except Exception as exc:
            return False, "哆点登录请求失败: %s" % self._safe_err(exc)
        low = (body + " " + final_url).lower()
        for key in dr.get("fail_keys") or ["2.htm"]:
            if key.lower() in low:
                body_preview = body.replace("\n", " ")[:200]
                log.warning("哆点旧版登录失败: %s | 网关响应: %s",
                            final_url[:120], body_preview)
                self.last_ret_code = 1
                return False, "哆点返回失败: %s" % body_preview
        for key in dr.get("success_keys") or ["3.htm"]:
            if key.lower() in low:
                self.last_ret_code = None
                return True, "哆点登录成功（%s）" % final_url[:80]
        self.last_ret_code = None
        return True, "哆点登录请求已发送（等待在线探测确认）"

    def _drcom_logout_ep(self, dr, api_path, ip, iface):
        """新版 ePortal 注销。返回 (ok, msg)；接口不可用返回 None。"""
        scheme = dr.get("scheme", "http")
        host = dr.get("host", "")
        port = dr.get("port", 801)
        url = "%s://%s:%s%s" % (scheme, host, port, api_path)
        mac = (dr.get("wlan_user_mac") or get_mac(iface)
               or "000000000000").replace(":", "").lower()
        params = {
            "callback": dr.get("callback", "jsonpReturn"),
            "login_method": "1",
            "user_account": "drcom",
            "user_password": "123",
            "ac_logout": "1",
            "register_mode": str(dr.get("register_mode", "1")),
            "wlan_user_ip": ip,
            "wlan_user_ipv6": "",
            "wlan_vlan_id": str(dr.get("vlan_id", "0")),
            "wlan_user_mac": mac,
            "wlan_ac_ip": "",
            "wlan_ac_name": "",
            "jsVersion": str(dr.get("js_version", "4.1.3")),
        }
        try:
            body = self._fetch(url, params, timeout=8)
        except Exception as exc:
            log.warning("新版 ePortal 注销请求失败: %s", self._safe_err(exc))
            return None
        try:
            obj = strip_jsonp(body)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        if obj.get("result") in (1, "1", "ok", True):
            return True, "注销成功（新版 ePortal）"
        msg = str(obj.get("msg") or obj.get("error") or "未知错误")[:150]
        return False, "注销失败: %s" % msg

    def logout(self, cfg=None):
        """哆点注销：优先新版 ePortal 接口，失败回退旧版路径。"""
        cfg = cfg or self.app.config.data
        dr = cfg.get("drcom", {})
        host = dr.get("host", "")
        if not host:
            return False, "未配置哆点网关地址"
        campus = cfg.get("campus") or {}
        iface = campus.get("iface", "eth0")
        ip = self.local_ip(iface)
        api_path = str(dr.get("api_logout_path") or "").strip()
        if api_path and ip and ip != "0.0.0.0":
            result = self._drcom_logout_ep(dr, api_path, ip, iface)
            if result is not None:
                return result
            log.warning("新版 ePortal 注销接口无有效响应，回退旧版注销")
        url = "%s://%s:%s%s" % (dr.get("scheme", "http"), host,
                                dr.get("port", 801),
                                dr.get("logout_path",
                                       "/eportal/?c=ACSetting&a=Logout&ver=1.0"))
        try:
            body, final_url = self._fetch_resp(url, timeout=8)
        except Exception as exc:
            return False, "注销请求失败: %s" % self._safe_err(exc)
        logging.info("哆点旧版注销已发送: %s", final_url[:100])
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
