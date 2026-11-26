# -*- coding: utf-8 -*-
"""网络接口管理：USB 网卡探测、静态 IP、DHCP、NAT 转发。"""
import logging
import os
import shutil
import time

from utils import (run, iface_exists, carrier, set_static_ip, get_ip,
                   set_ip_forward)

log = logging.getLogger("campuspi.network")


class Network:
    def __init__(self, app):
        self.app = app
        self._pc_iface = None
        self._pc_scan_ts = 0.0
        self._pc_logged = None
        self._detect_cache = None
        self._detect_ts = 0.0

    @property
    def campus_iface(self):
        return self.app.config.data["campus"].get("iface", "eth0")

    def _physical_ifaces(self):
        """物理以太网接口（排除无线/虚拟/回环）。"""
        out = []
        try:
            names = os.listdir("/sys/class/net")
        except OSError:
            return out
        for name in names:
            if name == "lo" or name.startswith(("veth", "br-", "virbr", "docker", "vlan")):
                continue
            if not os.path.isdir("/sys/class/net/%s/device" % name):
                continue            # 无 device 节点 => 虚拟接口
            if os.path.isdir("/sys/class/net/%s/wireless" % name):
                continue            # 无线接口
            out.append(name)
        return out

    @property
    def pc_iface(self):
        """Windows 有线所接的 USB 网卡接口（30 秒缓存自动探测）。"""
        now = time.time()
        if self._pc_iface is not None and now - self._pc_scan_ts < 30:
            return self._pc_iface
        self._pc_scan_ts = now
        self._pc_iface = None
        configured = self.app.config.data["pc"].get("iface", "auto")
        if configured and configured != "auto":
            self._pc_iface = configured if iface_exists(configured) else None
        else:
            candidates = self._physical_ifaces()
            for name in ("eth1", "eth2", "usb0", "usb1"):
                if name in candidates:
                    self._pc_iface = name
                    break
            if not self._pc_iface:
                for name in sorted(candidates):
                    if name != self.campus_iface:
                        self._pc_iface = name
                        break
        # pc_watch 每 2 秒清缓存重新探测；只在结果变化时打日志，
        # 否则日志会被 "PC 有线接口: eth1" 刷屏（还会增加 SD 卡写入）。
        if self._pc_iface != self._pc_logged:
            if self._pc_iface:
                log.info("PC 有线接口: %s", self._pc_iface)
            elif self._pc_logged:
                log.info("PC 有线接口已断开")
            self._pc_logged = self._pc_iface
        return self._pc_iface

    # ------------------------------------------------------------------ 检测
    def detect_network(self, refresh=False):
        """自动检测所有物理网卡（类型/链路/IP/驱动/角色/AP能力），30秒缓存。

        供 Web 后台状态页与命令行 --detect 使用。Windows 上返回空列表。"""
        now = time.time()
        if not refresh and self._detect_cache is not None \
                and now - self._detect_ts < 30:
            return self._detect_cache
        result = []
        try:
            names = sorted(os.listdir("/sys/class/net"))
        except OSError:
            return result
        # 全局 AP 能力（iw list 是系统级的）
        ap_proc = run(["iw", "list"], timeout=5)
        ap_global = bool(ap_proc and ap_proc.returncode == 0
                         and "AP" in ap_proc.stdout)
        for name in names:
            if name == "lo":
                continue
            info = {"name": name, "carrier": carrier(name), "ip": get_ip(name)}
            if os.path.isdir("/sys/class/net/%s/wireless" % name):
                info["type"] = "无线WiFi"
            elif os.path.isdir("/sys/class/net/%s/device" % name):
                try:
                    devpath = os.readlink("/sys/class/net/%s/device" % name)
                    info["type"] = "USB网卡" if "usb" in devpath else "以太网"
                    drv = os.path.realpath(
                        "/sys/class/net/%s/device/driver" % name)
                    info["driver"] = os.path.basename(drv)
                except OSError:
                    info["type"] = "物理网卡"
                    info["driver"] = ""
            else:
                info["type"] = "虚拟接口"
                info["driver"] = ""
            info["role"] = []
            if name == self.campus_iface:
                info["role"].append("校园网")
            if name == self.pc_iface:
                info["role"].append("PC有线")
            h = self.app.config.data["hotspot"]
            if name == h["iface"] and h["enabled"]:
                info["role"].append("热点")
            if info["type"] == "无线WiFi":
                info["ap_supported"] = ap_global
            result.append(info)
        self._detect_cache = result
        self._detect_ts = now
        return result

    # ------------------------------------------------------------------ 配置
    def setup_interfaces(self):
        """PC 有线 + 热点的静态地址（幂等）。"""
        cfg = self.app.config.data
        pc_iface = self.pc_iface
        pc = cfg["pc"]
        if pc_iface:
            set_static_ip(pc_iface, pc["gateway"],
                          int(pc["subnet"].split("/")[1]))
        else:
            log.info("未检测到 PC 有线接口，跳过")
        h = cfg["hotspot"]
        if h["enabled"] and iface_exists(h["iface"]):
            set_static_ip(h["iface"], h["gateway"],
                          int(h["subnet"].split("/")[1]))

    def ensure_nat(self):
        """把 PC 有线/热点子网 NAT 到校园网接口（幂等）。
        注意：iptables 规则不持久化，且 USB 网卡可能在服务启动后才插入，
        因此本函数会被周期性调用（见 main.hotspot_watch）幂等补齐。"""
        set_ip_forward(True)
        campus = self.campus_iface
        campus_ok = bool(campus) and iface_exists(campus)
        if not campus_ok:
            # 校园网接口异常也要继续：客户端互通的规则不依赖 campus
            log.warning("校园网接口 %s 不存在，跳过 NAT（仍配置客户端互通）",
                        campus or "?")
        run(["iptables", "-P", "FORWARD", "ACCEPT"], timeout=10)
        client_ifaces = []
        if self.pc_iface:
            client_ifaces.append(self.pc_iface)
        h = self.app.config.data["hotspot"]
        if h["enabled"] and iface_exists(h["iface"]):
            client_ifaces.append(h["iface"])

        def add_rule(table, args):
            chk = run(["iptables", "-t", table, "-C"] + args, timeout=10)
            if chk is not None and chk.returncode == 0:
                return
            run(["iptables", "-t", table, "-A"] + args, timeout=10)

        if campus_ok:
            add_rule("nat", ["POSTROUTING", "-o", campus, "-j", "MASQUERADE"])
            for client in client_ifaces:
                add_rule("filter",
                         ["FORWARD", "-i", client, "-o", campus,
                          "-j", "ACCEPT"])
                add_rule("filter",
                         ["FORWARD", "-i", campus, "-o", client,
                          "-m", "state", "--state", "RELATED,ESTABLISHED",
                          "-j", "ACCEPT"])
        # 有线(PC) <-> 无线(热点) 客户端互通：两个子网各自直连在本机，
        # 经内核路由转发即可，这里显式放行双向 FORWARD（不做 NAT，
        # 双方看到真实 IP）。不做此规则时手机访问不到电脑。
        for i, src in enumerate(client_ifaces):
            for dst in client_ifaces[i + 1:]:
                add_rule("filter", ["FORWARD", "-i", src, "-o", dst,
                                    "-j", "ACCEPT"])
                add_rule("filter", ["FORWARD", "-i", dst, "-o", src,
                                    "-j", "ACCEPT"])
        if len(client_ifaces) >= 2:
            log.info("客户端互通规则已就绪（%s）", " <-> ".join(client_ifaces))
        self.ensure_rdp_firewall()
        log.info("NAT 规则已就绪（%s -> %s）", ",".join(client_ifaces) or "无",
                 campus)

    def ensure_rdp_firewall(self):
        """RDP(3389) 只允许 PC 有线 / 热点子网，绝不暴露到校园网。

        幂等：由 ensure_nat() 周期调用（30 秒一次），即使接口/配置变化也
        能在下一轮补齐。setup_rdp.sh 里也有同样一份规则，确保安装完成
        到下一轮周期之间没有裸露窗口。
        """
        cfg = self.app.config.data
        subnets = []
        for section in ("pc", "hotspot"):
            subnet = str((cfg.get(section) or {}).get("subnet") or "").strip()
            if subnet and subnet not in subnets:
                subnets.append(subnet)
        chain = "CAMPUSPI-RDP"
        run(["iptables", "-N", chain], timeout=5)      # 已存在会失败，忽略
        for subnet in subnets:
            check = run(["iptables", "-C", chain, "-p", "tcp", "--dport",
                         "3389", "-s", subnet, "-j", "ACCEPT"], timeout=5)
            if check is None or check.returncode != 0:
                run(["iptables", "-A", chain, "-p", "tcp", "--dport", "3389",
                     "-s", subnet, "-j", "ACCEPT"], timeout=5)
        check = run(["iptables", "-C", chain, "-j", "DROP"], timeout=5)
        if check is None or check.returncode != 0:
            run(["iptables", "-A", chain, "-j", "DROP"], timeout=5)
        check = run(["iptables", "-C", "INPUT", "-p", "tcp", "--dport",
                     "3389", "-j", chain], timeout=5)
        if check is None or check.returncode != 0:
            run(["iptables", "-I", "INPUT", "1", "-p", "tcp", "--dport",
                 "3389", "-j", chain], timeout=5)
        # IPv6 也封死：xrdp 同时监听 v6，PC/热点用 IPv4 访问即可
        if shutil.which("ip6tables"):
            chain6 = "CAMPUSPI-RDP6"
            run(["ip6tables", "-N", chain6], timeout=5)
            check = run(["ip6tables", "-C", chain6, "-j", "DROP"], timeout=5)
            if check is None or check.returncode != 0:
                run(["ip6tables", "-A", chain6, "-j", "DROP"], timeout=5)
            check = run(["ip6tables", "-C", "INPUT", "-p", "tcp", "--dport",
                         "3389", "-j", chain6], timeout=5)
            if check is None or check.returncode != 0:
                run(["ip6tables", "-I", "INPUT", "1", "-p", "tcp", "--dport",
                     "3389", "-j", chain6], timeout=5)
