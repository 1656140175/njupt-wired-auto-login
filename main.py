#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CampusPi —— 树莓派校园网自动连接 + Windows 有线直连 + USB 热点

拓扑：eth0(板载) -> 学校网口；USB 网卡 -> Windows 电脑；USB Wi-Fi -> 热点(限4设备)
行为：
  - 无 PC 接入且未连上时每 30 分钟自动尝试重连校园网（可配置）
  - Windows 设备通过 USB 网卡接入时，立即尝试登录（默认 3 次）
  - 静默时段（默认 00:00-06:00）不尝试重连，窗口一过立即自动尝试
  - 树莓派无 RTC：时间不可信时不做静默判断，并尝试 NTP 同步
  - Web 管理后台：http://192.168.9.1:8080（Windows 浏览器访问）

入口：systemd 服务 campuspi.service -> python3 /opt/campuspi/main.py
"""
import collections
import copy
import datetime
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, parse_hhmm, in_quiet_hours
from hotspot import Hotspot
from network import Network
from portal import Portal
from utils import (carrier, get_ip, iface_exists, ensure_dhcp, sync_time,
                   time_trustworthy)
import nla
import webui

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingHandler(logging.Handler):
    """内存环形日志（Web /api/logs 读取）。"""

    def __init__(self, buf):
        super().__init__()
        self.buf = buf
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    def emit(self, record):
        try:
            self.buf.append(self.format(record))
        except Exception:
            pass


class App:
    def __init__(self):
        self.config = Config()
        self.login_lock = threading.Lock()
        self.running = True
        self.net = Network(self)
        self.portal = Portal(self)
        self.hotspot = Hotspot(self)

        self.online = None
        self.last_probe = {"ts": "", "ok": None, "msg": ""}
        self.last_login = {"ts": "", "ok": None, "msg": ""}
        self.pc_state = None
        self._last_sync = 0.0

        self.logs = collections.deque(maxlen=600)
        self._ring = RingHandler(self.logs)

    # ------------------------------------------------------------- 日志/工具
    def setup_logging(self):
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        fmt = logging.Formatter(LOG_FORMAT)
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
        path = self.config.data.get("log_file", "/var/log/campuspi.log")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
            logging.info("日志文件: %s", path)
        except OSError:
            logging.info("无法写入 %s，仅输出到标准输出", path)
        root.addHandler(self._ring)

    def tail_logs(self, lines):
        return "\n".join(list(self.logs)[-lines:])

    def _sleep(self, seconds):
        end = time.time() + seconds
        while self.running and time.time() < end:
            time.sleep(1)

    # ------------------------------------------------------------- 静默时段
    def quiet_hours_active(self):
        """00:00-06:00 夜间断网窗口判断。
        系统时间不可信（无 RTC 断电后回到 1970）时不做静默，
        否则可能导致永远不重连。"""
        q = self.config.data.get("quiet_hours", {})
        if not q.get("enabled", True):
            return False
        if not time_trustworthy():
            self._maybe_sync_time()
            return False
        start = parse_hhmm(q.get("start", "00:00"))
        end = parse_hhmm(q.get("end", "06:00"))
        if start is None or end is None:
            return False
        now = datetime.datetime.now()
        return in_quiet_hours(now.hour * 60 + now.minute, start, end)

    def _maybe_sync_time(self, cooldown=900):
        """节流地同步系统时间（登录前/时间不可信时调用）。"""
        now = time.time()
        if now - self._last_sync < cooldown:
            return
        self._last_sync = now
        if not time_trustworthy():
            logging.warning("系统时间不可信（树莓派无 RTC），尝试 NTP 同步")
        sync_time()

    # ------------------------------------------------------------- 探测/登录
    def probe_now(self):
        """三态探测并记录。返回 state: online / need_login / offline。"""
        state, msg = self.portal.probe_network()
        self.online = state == "online"
        self.last_probe = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "state": state, "ok": self.online, "msg": msg}
        logging.debug("在线探测: %s (%s)", state, msg)
        return state

    def ensure_campus(self, reason="周期检测", force=False):
        """确保校园网在线：探测 -> 必要时登录 -> 再探测。
        链路断(offline)时周期检测不登录（无意义）；
        force=True（Web 手动触发/PC 接入）无条件尝试——探测可能误判，
        手动请求必须发出，成不成功由网关说了算。
        未配置账号密码时不尝试（安静等待 Web 后台配置）。"""
        if not force:
            state = self.probe_now()
            if state == "online":
                logging.info("[%s] 校园网已在线", reason)
                return True, "校园网已在线"
            if state == "offline":
                logging.info("[%s] 链路不可达，登录无意义，等待网络恢复",
                             reason)
                return False, "链路不可达"
        cfg = self.config.data["campus"]
        if not (cfg.get("username") or "").strip() or not cfg.get("password"):
            logging.info("[%s] 未配置校园网账号/密码，跳过登录", reason)
            return False, "未配置账号密码"
        logging.info("[%s] 校园网未认证，开始尝试登录", reason)
        self._maybe_sync_time()
        iface = self.config.data["campus"]["iface"]
        ip = self.portal.local_ip()
        if not ip or ip == "0.0.0.0":
            logging.info("校园网接口 %s 无 IP，先请求 DHCP", iface)
            ensure_dhcp(iface)
        with self.login_lock:
            ok, msg = self.portal.login()
        self.last_login = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "ok": ok, "msg": msg}
        logging.info("[%s] 登录结果: %s", reason, msg)
        if ok:
            self.probe_now()
            threading.Thread(target=sync_time, daemon=True,
                             name="sync-time-after-login").start()
        return ok, msg

    # ------------------------------------------------------------- 后台线程
    def campus_loop(self):
        """周期自动连接（无 PC 接入时默认 30 分钟重试）。
        三态语义：online 无事；need_login（链路通）无论静默与否都登录；
        offline（链路断）在静默时段暂停、网络恢复后立即继续。"""
        while self.running:
            try:
                state = self.probe_now()
                if state == "online":
                    pass                      # 正常，走周期休眠
                elif state == "offline":
                    if self.quiet_hours_active():
                        # 物理断网窗口：暂停重连，但持续轻探测（2 分钟）
                        logging.info("静默时段且链路断，暂停重连")
                        self._sleep(120)
                        continue
                    logging.info("链路不可达（非静默时段），等待网络恢复")
                else:                          # need_login：链路通=有网，
                    self.ensure_campus("周期检测")   # 静默时段也登录
            except Exception:
                logging.exception("周期检测异常")
            interval = self.config.data["campus"]["check_interval"]
            self._sleep(interval)

    def pc_watch(self):
        """检测 Windows 有线接入（USB 网卡链路上升沿），立即尝试登录。
        自愈：USB 网线拔插导致接口重建/丢 IP 时，自动重新配置静态地址，
        否则 Windows 拿不到 DHCP（192.168.9.x），管理界面打不开。"""
        last_heal = 0.0
        while self.running:
            try:
                # 绕过 30 秒缓存重新探测 PC 接口（接口可能被重建/改名）
                self.net._pc_iface = None
                self.net._pc_scan_ts = 0.0
                iface = self.net.pc_iface
                # 接口在但无 IP = 拔插后配置丢失，重新配置（节流 5 秒）
                if iface and not get_ip(iface) and time.time() - last_heal > 5:
                    last_heal = time.time()
                    logging.warning("PC 接口 %s 无 IP（疑似拔插），重新配置",
                                    iface)
                    self.net.setup_interfaces()
                    self.net.ensure_nat()
                    self.hotspot.apply(restart=True)   # 接口名可能变化，重写 DHCP
                current = carrier(iface) if iface else None
                if self.pc_state is None:
                    if current:
                        self._pc_connected()
                elif current is True and self.pc_state is False:
                    logging.info("检测到 Windows 有线设备接入 %s", iface)
                    self._pc_connected()
                self.pc_state = current
            except Exception:
                logging.exception("pc_watch 异常")
            time.sleep(2)

    def _pc_connected(self):
        # 先探测：链路断(offline)时登录无意义；
        # need_login（链路通）在静默时段也照常尝试（"有网就不静默"）
        state = self.probe_now()
        if state == "online":
            logging.info("PC 接入时校园网已在线，跳过立即尝试")
            return
        if state == "offline" and self.quiet_hours_active():
            logging.info("PC 接入触发被静默时段跳过（时段内且链路断）")
            return
        cfg = self.config.data["pc"]
        attempts = cfg.get("attempts", 3)
        delay = cfg.get("attempt_delay", 6)
        for i in range(1, attempts + 1):
            if not self.running or self.online:
                break
            logging.info("PC 接入触发登录（第 %d/%d 次）", i, attempts)
            self.ensure_campus("PC 接入触发", force=True)
            if self.online:
                break
            if i < attempts:
                self._sleep(delay)

    def hotspot_watch(self):
        while self.running:
            try:
                self.hotspot.watch()
            except Exception:
                logging.exception("热点守护异常")
            self._sleep(30)

    # ------------------------------------------------------------- 状态/配置
    def status(self):
        h = self.config.data["hotspot"]
        pc = self.net.pc_iface
        clients = self.hotspot.clients() if h["enabled"] else []
        return {
            "online": self.online,
            "last_probe": self.last_probe,
            "last_login": self.last_login,
            "campus_iface": self.net.campus_iface,
            "campus_ip": self.portal.local_ip(),
            "interfaces": self.net.detect_network(),
            "pc_iface": pc,
            "pc_link": carrier(pc) if pc else None,
            "quiet_hours": self.quiet_hours_active(),
            "quiet_enabled": bool(self.config.data.get("quiet_hours", {})
                                 .get("enabled", True)),
            "quiet_window": "%s-%s" % (
                self.config.data.get("quiet_hours", {}).get("start", "00:00"),
                self.config.data.get("quiet_hours", {}).get("end", "06:00")),
            "time_trustworthy": time_trustworthy(),
            "system_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hotspot": {
                "enabled": h["enabled"],
                "iface": h["iface"],
                "iface_present": iface_exists(h["iface"]),
                "password_set": bool(h.get("password", "")),
                "running": self.hotspot.is_active("hostapd"),
                "clients": len(clients),
                "client_list": clients[:10],
                "max_clients": h["max_clients"],
            },
        }

    def save_config(self, incoming):
        """合并并校验 Web 前端提交的配置；空密码表示保持原值。
        在配置副本上修改，全部校验通过后才写回并落盘。
        返回错误列表（空 = 成功）。"""
        errors = []
        cfg = copy.deepcopy(self.config.data)

        def masked_update(section, fields):
            for field in fields:
                value = incoming.get(section, {}).get(field)
                if value is None:
                    continue
                if field.endswith("password") and value == "":
                    continue            # 空密码 = 保持原密码
                cfg[section][field] = value

        masked_update("campus", ["username", "password", "isp", "host",
                                 "protocol", "adapter", "enc",
                                 "challenge_path", "login_path", "iface",
                                 "check_interval"])
        dr = incoming.get("drcom")
        if isinstance(dr, dict):
            for field in ("host", "port", "scheme", "path", "user_field",
                          "pass_field", "extra_params"):
                if field in dr:
                    cfg["drcom"][field] = dr[field]
        ep = incoming.get("eportal")
        if isinstance(ep, dict):
            for field in ("host", "port", "scheme", "path", "login_method"):
                if field in ep:
                    cfg["eportal"][field] = ep[field]
        masked_update("hotspot", ["enabled", "ssid", "password",
                                  "max_clients", "channel", "driver",
                                  "hw_mode", "iface"])
        masked_update("pc", ["iface", "subnet", "gateway", "dhcp_start",
                             "dhcp_end", "attempts", "attempt_delay"])
        masked_update("web", ["username", "password", "port", "bind"])
        qh = incoming.get("quiet_hours")
        if isinstance(qh, dict):
            cfg["quiet_hours"]["enabled"] = bool(qh.get("enabled", True))
            if parse_hhmm(qh.get("start")) is not None:
                cfg["quiet_hours"]["start"] = qh.get("start")
            if parse_hhmm(qh.get("end")) is not None:
                cfg["quiet_hours"]["end"] = qh.get("end")
        # 运营商列表
        isps = incoming.get("campus", {}).get("isps")
        if isinstance(isps, dict) and isps:
            cleaned = {}
            for name, item in isps.items():
                if not isinstance(item, dict):
                    continue
                cleaned[str(name)] = {
                    "username_suffix": str(item.get("username_suffix", "")),
                    "ac_id": str(item.get("ac_id", "1")),
                    "extra": item.get("extra") or {},
                }
            if cleaned:
                cfg["campus"]["isps"] = cleaned
        if cfg["campus"].get("isp") not in cfg["campus"]["isps"]:
            cfg["campus"]["isp"] = next(iter(cfg["campus"]["isps"]), "campus")

        # 数字字段
        for section, field, minimum, maximum in (
                ("campus", "check_interval", 60, 86400),
                ("hotspot", "max_clients", 1, 50),
                ("hotspot", "channel", 0, 14),
                ("pc", "attempts", 1, 10),
                ("pc", "attempt_delay", 1, 300),
                ("web", "port", 1, 65535),
                ("drcom", "port", 1, 65535),
                ("eportal", "port", 1, 65535)):
            try:
                value = int(cfg[section][field])
            except (TypeError, ValueError):
                errors.append("%s.%s 必须是整数" % (section, field))
                continue
            if not (minimum <= value <= maximum):
                errors.append("%s.%s 需在 %d-%d 之间"
                              % (section, field, minimum, maximum))
            else:
                cfg[section][field] = value
        # web.bind 必须是合法 IP（或 localhost），避免被改成域名/恶意地址
        bind = cfg["web"].get("bind", "0.0.0.0")
        if not isinstance(bind, str) or not (
                bind in ("localhost", "0.0.0.0", "::")
                or all(part.isdigit() and 0 <= int(part) <= 255
                       for part in bind.split("."))):
            errors.append("web.bind 必须是合法 IP 地址")

        # 内容校验
        if not cfg["campus"].get("username"):
            errors.append("校园网用户名不能为空")
        if not cfg["campus"].get("password"):
            errors.append("校园网密码不能为空")
        h = cfg["hotspot"]
        if h.get("enabled"):
            if not h.get("ssid") or len(h["ssid"]) > 32:
                errors.append("热点 SSID 长度需为 1-32")
            # 热点密码为空允许保存（热点暂不启动），填写时须 8-63 位
            if h.get("password") and not (8 <= len(h["password"]) <= 63):
                errors.append("热点密码长度需为 8-63")
        if errors:
            return errors            # 校验失败：副本丢弃，原配置不受影响
        self.config.data = cfg
        self.config.save()
        self._apply_config()
        return errors

    def _apply_config(self):
        logging.info("应用新配置")
        self.net.setup_interfaces()
        self.hotspot.apply(restart=True)
        self.net.ensure_nat()
        threading.Thread(target=self.ensure_campus,
                         kwargs={"reason": "配置变更"},
                         daemon=True, name="apply-config-login").start()

    def apply_all(self):
        self.net.setup_interfaces()
        self.hotspot.apply(restart=True)
        self.net.ensure_nat()

    # ------------------------------------------------------------- Web 页面
    def web_index(self):
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "web", "index.html")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return "<h1>CampusPi</h1><p>web/index.html 缺失</p>"

    # ------------------------------------------------------------- 主流程
    def _env_check(self):
        """启动环境自检：依赖缺失时明确警告。"""
        missing = [tool for tool in ("systemctl", "hostapd", "dnsmasq",
                                     "iw", "iptables", "ip")
                   if shutil.which(tool) is None]
        if missing:
            logging.warning("环境自检：缺少命令 %s —— 相关功能不可用，"
                            "请运行 install.sh 安装依赖", "、".join(missing))
        else:
            logging.info("环境自检：依赖完整")

    def run(self):
        self.setup_logging()
        logging.info("==== CampusPi 启动 ====")
        self._env_check()
        self._maybe_sync_time(cooldown=0)
        self.apply_all()
        self.probe_now()
        threads = [
            threading.Thread(target=self.campus_loop, daemon=True,
                             name="campus-loop"),
            threading.Thread(target=self.pc_watch, daemon=True,
                             name="pc-watch"),
            threading.Thread(target=self.hotspot_watch, daemon=True,
                             name="hotspot-watch"),
        ]
        for thread in threads:
            thread.start()
        webui.start(self)
        nla.start(self)
        logging.info("服务运行中。Windows 直连后访问 "
                     "http://%s:%d 进行配置",
                     self.config.data["pc"]["gateway"],
                     self.config.data["web"]["port"])
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        logging.info("CampusPi 退出")


def detect_command():
    """命令行网卡检测：python3 main.py --detect（不启动服务）"""
    app = App()
    print("=" * 64)
    print("CampusPi 网卡检测")
    print("=" * 64)
    print("%-8s %-10s %-6s %-16s %-12s %s" % (
        "接口", "类型", "链路", "IP", "驱动", "角色"))
    for info in app.net.detect_network(refresh=True):
        print("%-8s %-10s %-6s %-16s %-12s %s" % (
            info["name"],
            info.get("type", "?"),
            "已连接" if info.get("carrier") else
            ("未插入" if info.get("carrier") is False else "无"),
            info.get("ip") or "-",
            info.get("driver", "") or "-",
            "、".join(info.get("role", [])) or "未分配"))
        if info.get("type") == "无线WiFi":
            ap = info.get("ap_supported")
            print("          AP模式: %s" % (
                "支持（可做热点）" if ap else
                ("不支持（无法做热点！）" if ap is False else "未知")))
    print("=" * 64)
    print("提示: 链路=已连接 表示网线/WiFi 正常；'未分配'的网卡可在 Web 后台配置角色")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--detect":
        detect_command()
        return
    app = App()

    def stop(signum, frame):
        logging.info("收到信号 %d，正在退出", signum)
        app.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app.run()


if __name__ == "__main__":
    main()
