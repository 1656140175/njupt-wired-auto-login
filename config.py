# -*- coding: utf-8 -*-
"""CampusPi 配置管理：加载 / 合并 / 校验 / 保存。

配置文件默认位于 /etc/campuspi/config.json（树莓派上）。
密码字段通过 masked() 脱敏后才会发给 Web 前端。
"""
import copy
import json
import logging
import os
import tempfile

log = logging.getLogger("campuspi.config")

DEFAULT_CONFIG_PATH = "/etc/campuspi/config.json"

DEFAULTS = {
    "campus": {
        # drcom(南邮哆点,默认) | eportal(锐捷备用) | srun(深澜) | generic(通用表单)
        "adapter": "drcom",
        "protocol": "https",
        "host": "p.njupt.edu.cn",       # 南邮认证门户（srun 适配器用）
        "challenge_path": "/cgi-bin/get_challenge",
        "login_path": "/cgi-bin/srun_portal",
        "username": "",
        "password": "",
        "isp": "mobile",
        # 运营商: username_suffix 会拼在账号后。南邮实际只有移动和电信
        # （移动 @cmcc / 电信 @njxy），无联通。账号后缀错会得到
        # "未绑定运营商账号"——务必在 Web 后台选对运营商。
        # ac_id / extra 为 srun 系高级项。
        "isps": {
            "campus":  {"username_suffix": "", "ac_id": "1", "extra": {}},
            "mobile":  {"username_suffix": "@cmcc", "ac_id": "1", "extra": {}},
            "telecom": {"username_suffix": "@njxy", "ac_id": "2", "extra": {}},
        },
        "enc": "auto",                  # srun 系: auto | md5 | token_md5 | base64 | token_base64
        "check_interval": 1800,         # 无 PC 接入且未连上时的重试间隔（秒），默认 30 分钟
        "iface": "eth0",
        # 在线探测必须用 HTTP：校园网未认证时 HTTPS 被网关阻断（丢包），
        # 而 HTTP 会被劫持到认证页（据此识别"未认证"而非"断网"）
        "probe_urls": [
            {"url": "http://www.baidu.com/generate_204", "marker": "204"},
            {"url": "http://connect.rom.miui.com/generate_204", "marker": "204"},
            {"url": "http://www.qq.com/", "marker": "腾讯"},
        ],
    },
    # 南邮哆点 Dr.COM（来自 a79.htm 页面参数）：
    # GET /eportal/?c=ACSetting&a=Login&DDDDD=<账号>&upass=<密码>（端口 801，明文）
    # 登录后服务器 302 到 Dr.COMWebLoginID_3.htm(成功) / _2.htm(失败)
    "drcom": {
        "host": "10.10.244.11",
        "port": 801,
        "scheme": "http",
        "path": "/eportal/?c=ACSetting&a=Login",
        "logout_path": "/eportal/?c=ACSetting&a=Logout&ver=1.0",
        "user_field": "DDDDD",
        "pass_field": "upass",
        # 页面 f0 表单的全部字段（0MKKey=登录按钮值；其余为空值隐藏字段，
        # 某些网关要求字段存在；url=drappall 来自 authloginparam 配置）
        "extra_params": {
            "0MKKey": "123456", "R1": "0", "R2": "0", "R3": "0", "R6": "0",
            "para": "00", "url": "drappall",
            "buttonClicked": "", "redirect_url": "", "err_flag": "",
            "username": "", "password": "", "user": "", "cmd": "",
            "Login": "", "v6ip": "",
        },
        # 用子串匹配（"webloginid_3" 可同时命中完整名与短名跳转页）
        "success_keys": ["webloginid_3", "认证成功", "success"],
        "fail_keys": ["webloginid_2", "认证失败", "密码错误",
                      "用户名或密码", "账号不存在", "error"],
    },
    # 锐捷 eportal（备用，部分学校网关）：
    # GET /eportal/portal/login?login_method=1&user_account=,0,<账号>&user_password=<密码>
    "eportal": {
        "host": "10.10.244.11",
        "port": 801,
        "scheme": "http",
        "path": "/eportal/portal/login",
        "login_method": "1",
        "fail_keys": ["error", "fail", "密码错误", "用户名或密码", "账号不存在"],
    },
    "pc": {
        "iface": "auto",                # auto = 自动探测 USB 网卡
        "subnet": "192.168.9.0/24",
        "gateway": "192.168.9.1",
        "dhcp_start": "192.168.9.100",
        "dhcp_end": "192.168.9.150",
        "attempts": 3,                  # Windows 设备接入时立即尝试次数
        "attempt_delay": 6,             # 每次尝试间隔（秒）
    },
    "hotspot": {
        "enabled": True,
        "iface": "wlan0",
        "ssid": "CampusPi",
        "password": "",
        "max_clients": 4,               # 最多接入设备数
        "channel": 6,
        "driver": "nl80211",
        "hw_mode": "g",
        "subnet": "192.168.10.0/24",
        "gateway": "192.168.10.1",
        "dhcp_start": "192.168.10.100",
        "dhcp_end": "192.168.10.150",
    },
    "generic": {
        "login_url": "",
        "method": "POST",
        "params": {
            "username": "{username}",
            "password": "{password}",
            "isp": "{isp}",
        },
        "success_key": "ok",
    },
    "web": {
        "port": 8080,
        "bind": "0.0.0.0",
        "username": "admin",
        "password": "",                 # 为空则不要求登录
    },
    # 静默时段：夜间校园网断网（如 00:00-06:00），期间不尝试重连，
    # 窗口一过立即自动尝试。可跨午夜（如 start=23:00 end=06:00）。
    "quiet_hours": {
        "enabled": True,
        "start": "00:00",
        "end": "06:00",
    },
    "log_file": "/var/log/campuspi.log",
}


def parse_hhmm(value):
    """把 'HH:MM' 解析为当日分钟数；非法输入返回 None。"""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def in_quiet_hours(now_minutes, start_minutes, end_minutes):
    """当前分钟数是否处于静默窗口。start==end 表示不静默；支持跨午夜。"""
    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes

# 这些字段发给 Web 前端时置空，并附 has_xxx 标记
MASKED_FIELDS = {
    "campus": ("password",),
    "hotspot": ("password",),
    "web": ("password",),
}


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def deep_merge(base, over):
    """递归合并：over 中的 dict 与 base 逐层合并，其余直接覆盖。"""
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, path=None):
        self.path = path or DEFAULT_CONFIG_PATH
        self.data = {}
        self.reload()

    def reload(self):
        loaded = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            log.info("配置文件不存在，使用默认配置: %s", self.path)
        except (ValueError, OSError) as exc:
            log.error("读取配置失败(%s)，使用默认配置", exc)
        self.data = deep_merge(DEFAULTS, loaded)
        self._normalize()
        return self.data

    def _normalize(self):
        c = self.data["campus"]
        c["check_interval"] = max(60, _int(c.get("check_interval"), 1800))
        if not isinstance(c.get("isps"), dict):
            c["isps"] = deep_merge(DEFAULTS["campus"]["isps"], {})
        if c.get("isp") not in c["isps"]:
            c["isp"] = next(iter(c["isps"]), "campus")
        p = self.data["pc"]
        p["attempts"] = min(10, max(1, _int(p.get("attempts"),
                                             DEFAULTS["pc"]["attempts"])))
        p["attempt_delay"] = max(1, _int(p.get("attempt_delay"),
                                         DEFAULTS["pc"]["attempt_delay"]))
        h = self.data["hotspot"]
        h["max_clients"] = min(50, max(1, _int(h.get("max_clients"), 4)))
        h["channel"] = _int(h.get("channel"), 6)
        if not isinstance(h.get("password", ""), str):
            h["password"] = ""
        if not isinstance(h.get("ssid", ""), str):
            h["ssid"] = ""
        w = self.data["web"]
        w["port"] = min(65535, max(1, _int(w.get("port"), 8080)))
        if not isinstance(w.get("password", ""), str):
            w["password"] = ""
        if not isinstance(c.get("password", ""), str):
            c["password"] = ""
        # 防御：enabled 可能是字符串（"false" 也是真值！），统一转布尔
        q = self.data.get("quiet_hours")
        if isinstance(q, dict):
            q["enabled"] = str(q.get("enabled", True)).strip().lower() == "true"

    def save(self, new_data=None):
        if new_data is not None:
            self.data = deep_merge(self.data, new_data)
            self._normalize()
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        log.info("配置已保存: %s", self.path)
        return self.data

    def masked(self):
        """返回给 Web 前端的配置副本：密码置空，附带 has_xxx 标记。"""
        out = copy.deepcopy(self.data)
        for section, fields in MASKED_FIELDS.items():
            for field in fields:
                val = out.get(section, {}).get(field, "")
                out[section]["has_" + field] = bool(val)
                out[section][field] = ""
        return out
