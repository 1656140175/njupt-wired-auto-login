# -*- coding: utf-8 -*-
"""Web 管理后台：基于标准库 http.server，零第三方依赖。

Windows 电脑通过 USB 网卡直连树莓派后，浏览器访问
http://192.168.9.1:8080 即可配置/查看状态。
所有 /api/* 接口返回 JSON，页面在 web/index.html。
"""
import base64
import hashlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("campuspi.webui")


def _constant_time_eq(a, b):
    return hashlib.sha256(a.encode("utf-8")).digest() == \
        hashlib.sha256(b.encode("utf-8")).digest()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CampusPi/1.0"

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        # ------------------------------------------------------------ 基础
        def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _auth_ok(self):
            web_cfg = app.config.data["web"]
            password = web_cfg.get("password", "")
            if not password:
                return True
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, _, pwd = decoded.partition(":")
            except Exception:
                return False
            user_ok = (not web_cfg.get("username")
                       or user == web_cfg["username"])
            return user_ok and _constant_time_eq(pwd, password)

        def _require_auth(self):
            if not self._auth_ok():
                self.send_response(401)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.send_header("WWW-Authenticate",
                                 'Basic realm="campuspi"')
                self.end_headers()
                return False
            return True

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > (1 << 20):
                    return None
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return None

        def _path(self):
            return urlparse(self.path).path

        def _query(self):
            return parse_qs(urlparse(self.path).query)

        # ------------------------------------------------------------ 路由
        def do_GET(self):
            path = self._path()
            if path in ("/", "/index.html"):
                self._send(200, app.web_index(), "text/html; charset=utf-8")
            elif path == "/api/status":
                if not self._require_auth():
                    return
                self._send(200, json.dumps(app.status(), ensure_ascii=False))
            elif path == "/api/config":
                if not self._require_auth():
                    return
                self._send(200, json.dumps(app.config.masked(),
                                           ensure_ascii=False))
            elif path == "/api/logs":
                if not self._require_auth():
                    return
                try:
                    lines = min(int(self._query().get("lines", ["300"])[0]),
                                1000)
                except ValueError:
                    lines = 300
                self._send(200, json.dumps({"lines": app.tail_logs(lines)},
                                           ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "not found"},
                                           ensure_ascii=False))

        def do_POST(self):
            path = self._path()
            if path == "/api/config":
                if not self._require_auth():
                    return
                data = self._read_json()
                if data is None:
                    self._send(400, json.dumps(
                        {"ok": False, "errors": ["无效的 JSON 请求"]},
                        ensure_ascii=False))
                    return
                errors = app.save_config(data)
                if errors:
                    self._send(400, json.dumps({"ok": False,
                                                "errors": errors},
                                               ensure_ascii=False))
                else:
                    self._send(200, json.dumps({"ok": True},
                                               ensure_ascii=False))
            elif path == "/api/action/login":
                if not self._require_auth():
                    return
                # force: 手动触发无条件发登录请求（探测可能误判，必须直发）
                ok, msg = app.ensure_campus("Web 手动触发", force=True)
                self._send(200, json.dumps({"ok": ok, "msg": msg},
                                           ensure_ascii=False))
            elif path == "/api/action/restart_hotspot":
                if not self._require_auth():
                    return
                ok, msg = app.hotspot.apply(restart=True)
                self._send(200, json.dumps({"ok": ok, "msg": msg},
                                           ensure_ascii=False))
            elif path == "/api/action/logout":
                if not self._require_auth():
                    return
                ok, msg = app.portal.logout()
                self._send(200, json.dumps({"ok": ok, "msg": msg},
                                           ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "not found"},
                                           ensure_ascii=False))

    return Handler


def start(app, port=None, bind=None):
    port = port or app.config.data["web"]["port"]
    bind = bind or app.config.data["web"]["bind"]
    handler = make_handler(app)
    try:
        server = ThreadingHTTPServer((bind, port), handler)
    except OSError as exc:
        log.error("Web 服务启动失败(%s:%d): %s", bind, port, exc)
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="web-server")
    thread.start()
    log.info("Web 管理界面: http://%s:%d （PC 直连请访问 http://%s:%d）",
             bind, port, app.config.data["pc"]["gateway"], port)
    return thread
