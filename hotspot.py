# -*- coding: utf-8 -*-
"""USB 网卡热点（hostapd）与 DNS/DHCP（dnsmasq）配置生成与守护。

- hostapd: WPA2-PSK，max_num_sta 限制最多接入设备数（默认 4）
- dnsmasq: 仅服务 PC 有线子网与热点子网，上游 DNS 走阿里/腾讯公共 DNS，
  同时为 PC 有线提供 DHCP（Windows 插上网线即得 192.168.9.x 地址）
"""
import hashlib
import logging
import os

from utils import run, iface_exists

log = logging.getLogger("campuspi.hotspot")

HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"
DNSMASQ_CONF = "/etc/dnsmasq.d/campuspi.conf"

UPSTREAM_DNS = ("223.5.5.5", "119.29.29.29")


class Hotspot:
    def __init__(self, app):
        self.app = app
        self._conf_hash = None

    # ------------------------------------------------------------------ 配置
    def hostapd_conf(self):
        h = self.app.config.data["hotspot"]
        return "\n".join([
            "interface=%s" % h["iface"],
            "driver=%s" % h.get("driver", "nl80211"),
            "ssid=%s" % h["ssid"],
            "hw_mode=%s" % h.get("hw_mode", "g"),
            "channel=%d" % h["channel"],
            "ieee80211n=1",
            "wmm_enabled=1",
            "max_num_sta=%d" % h["max_clients"],
            "auth_algs=1",
            "wpa=2",
            "wpa_passphrase=%s" % h["password"],
            "wpa_key_mgmt=WPA-PSK",
            "rsn_pairwise=CCMP",
            "country_code=CN",
            "",
        ])

    def dnsmasq_conf(self):
        cfg = self.app.config.data
        pc = cfg["pc"]
        h = cfg["hotspot"]
        lines = ["no-resolv", "no-hosts", "log-dhcp"]
        lines += ["server=%s" % d for d in UPSTREAM_DNS]
        campus = self.app.net.campus_iface
        if campus:
            lines.append("except-interface=%s" % campus)
        # 劫持 Windows/Chrome 联网探测域名到本机，配合 nla.py 实现
        # "离线自动弹配置页"（强制门户）；在线时则正常返回放行
        lines.append("address=/www.msftconnecttest.com/%s" % pc["gateway"])
        lines.append("address=/connectivitycheck.gstatic.com/%s" % pc["gateway"])
        pc_iface = self.app.net.pc_iface
        if pc_iface:
            lines += [
                "interface=%s" % pc_iface,
                "dhcp-range=interface:%s,%s,%s,255.255.255.0,12h"
                % (pc_iface, pc["dhcp_start"], pc["dhcp_end"]),
                "dhcp-option=interface:%s,3,%s" % (pc_iface, pc["gateway"]),
                "dhcp-option=interface:%s,6,%s" % (pc_iface, pc["gateway"]),
            ]
        if h["enabled"] and iface_exists(h["iface"]):
            lines += [
                "interface=%s" % h["iface"],
                "dhcp-range=interface:%s,%s,%s,255.255.255.0,12h"
                % (h["iface"], h["dhcp_start"], h["dhcp_end"]),
                "dhcp-option=interface:%s,3,%s" % (h["iface"], h["gateway"]),
                "dhcp-option=interface:%s,6,%s" % (h["iface"], h["gateway"]),
            ]
        return "\n".join(lines) + "\n"

    def _current_hash(self):
        content = self.hostapd_conf() + "\n====\n" + self.dnsmasq_conf()
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ 应用
    def apply(self, restart=True):
        """写入配置文件并重启服务。返回 (ok, 说明)。"""
        cfg = self.app.config.data
        h = cfg["hotspot"]
        msgs = []
        wlan = h["iface"]
        if h["enabled"]:
            if not iface_exists(wlan):
                msgs.append("无线接口 %s 不存在，热点未启动" % wlan)
            elif not h.get("password"):
                msgs.append("未设置热点密码，热点未启动")
            else:
                self._write(HOSTAPD_CONF, self.hostapd_conf())
                if restart:
                    self._svc("restart", "hostapd", "热点")
                msgs.append("热点已配置（限 %d 设备）" % h["max_clients"])
        else:
            if restart:
                self._svc("stop", "hostapd", "热点")
            msgs.append("热点已禁用")
        conf = self.dnsmasq_conf()
        if any(line.startswith("interface=") for line in conf.splitlines()):
            self._write(DNSMASQ_CONF, conf)
            if restart:
                self._svc("restart", "dnsmasq", "DNS/DHCP")
        else:
            if restart:
                self._svc("stop", "dnsmasq", "DNS/DHCP")
        self._conf_hash = self._current_hash()
        return True, "；".join(msgs)

    @staticmethod
    def _write(path, content):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    @staticmethod
    def _svc(action, unit, label):
        proc = run(["systemctl", action, unit], timeout=30)
        log.info("%s服务: %s %s", label, unit, action)
        # 服务反复启动失败会触发 systemd StartLimit（restart 永久失效），
        # 重置后再拉起，避免 watchdog 每次重启都被拒
        if action == "restart" and (proc is None or proc.returncode != 0):
            run(["systemctl", "reset-failed", unit], timeout=15)
            run(["systemctl", "start", unit], timeout=30)
            log.warning("%s服务 %s 重启失败，已 reset-failed 后重试", label, unit)

    # ------------------------------------------------------------------ 状态
    def is_active(self, unit):
        proc = run(["systemctl", "is-active", unit], timeout=10)
        return bool(proc and proc.returncode == 0
                    and proc.stdout.strip() == "active")

    def clients(self):
        """当前接入热点的设备 MAC 列表。"""
        h = self.app.config.data["hotspot"]
        if not iface_exists(h["iface"]):
            return []
        proc = run(["iw", "dev", h["iface"], "station", "dump"], timeout=10)
        if not proc or proc.returncode != 0:
            return []
        macs = []
        for line in proc.stdout.splitlines():
            if line.startswith("Station ") and " on " in line:
                macs.append(line.split()[1])
        return macs

    # ------------------------------------------------------------------ 守护
    def watch(self):
        """周期保障：配置变更重写并重启；服务掉线自动拉起。"""
        cfg = self.app.config.data
        h = cfg["hotspot"]
        wlan = h["iface"]
        if h["enabled"]:
            if not iface_exists(wlan):
                log.info("热点未启动：无线接口 %s 不存在", wlan)
            elif not h.get("password"):
                log.info("热点未启动：未设置热点密码")
            else:
                if self._conf_hash != self._current_hash():
                    log.info("热点配置已变更，重新应用")
                    self.apply(restart=True)
                elif not self.is_active("hostapd"):
                    log.warning("hostapd 未运行，尝试拉起")
                    self.apply(restart=True)
        else:
            if self.is_active("hostapd"):
                self._svc("stop", "hostapd", "热点")
        conf = self.dnsmasq_conf()
        if any(line.startswith("interface=") for line in conf.splitlines()):
            if self._conf_hash != self._current_hash():
                log.info("dnsmasq 配置已变更，重新应用")
                self.apply(restart=True)
            elif not self.is_active("dnsmasq"):
                log.warning("dnsmasq 未运行，尝试拉起")
                self.apply(restart=True)
