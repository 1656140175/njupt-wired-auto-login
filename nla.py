# -*- coding: utf-8 -*-
"""NLA 强制门户模拟：让 Windows 插线后自动弹出配置页

原理（与校园网/酒店 Wi-Fi 的"自动跳转"完全一致）：
  - dnsmasq 把 Windows/Chrome 的联网探测域名
    （www.msftconnecttest.com / connectivitycheck.gstatic.com）
    解析到 PC 网关 192.168.9.1（见 hotspot.py 的 address= 配置）
  - 本服务监听 80 端口：
      校园网在线 -> /connecttest.txt 返回 "Microsoft Connect Test"，
                    其余路径 204  -> Windows NLA 判定"有网"，不弹窗
      校园网离线 -> 所有请求 302 到 http://192.168.9.1:8080/（配置页）
                    -> Windows 判定为强制门户，自动弹出浏览器打开配置页
  - 仅响应 PC/热点子网来源，不影响树莓派自身与校园网侧流量
"""
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from utils import ip_in_subnet

log = logging.getLogger("campuspi.nla")

CONNECTTEST = b"Microsoft Connect Test"


class NlaHandler(BaseHTTPRequestHandler):
    app = None
    server_version = "CampusPi/1.0"

    def log_message(self, fmt, *args):
        log.debug("nla: " + fmt, *args)

    def _redirect(self):
        cfg = self.app.config.data
        target = "http://%s:%d/" % (cfg["pc"]["gateway"],
                                    cfg["web"]["port"])
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _from_client(self):
        cfg = self.app.config.data
        client = self.client_address[0]
        return (ip_in_subnet(client, cfg["pc"]["subnet"])
                or (cfg["hotspot"]["enabled"]
                    and ip_in_subnet(client, cfg["hotspot"]["subnet"])))

    def do_GET(self):
        if not self._from_client():
            self._reply(404)          # 非 PC/热点来源不响应
            return
        if self.app.online:
            path = self.path.split("?")[0]
            if path.endswith("connecttest.txt"):
                self._reply(200, CONNECTTEST)
            else:
                self._reply(204)      # generate_204 等探测
        else:
            self._redirect()          # 离线 -> 强制门户 -> Windows 自动弹页

    do_HEAD = do_GET


def start(app):
    NlaHandler.app = app
    try:
        server = ThreadingHTTPServer(("0.0.0.0", 80), NlaHandler)
    except OSError as exc:
        log.error("强制门户服务(80端口)启动失败: %s", exc)
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="nla-portal")
    thread.start()
    log.info("强制门户服务已启动：校园网离线时 Windows 将自动弹出配置页")
    return thread
