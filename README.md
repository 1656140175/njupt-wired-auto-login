# 南邮有线网自动登录

南京邮电大学宿舍**有线校园网自动认证**工具。校园网每晚断网、次日需重新认证，本项目让它全自动完成。

提供两种部署方式，任选其一（也可以两个都用）：

| 方案 | 需要硬件 | 适合谁 |
|---|---|---|
| **A. Windows 纯本地** | 无 | 只想让电脑自己重连，零额外设备 |
| **B. 树莓派全自动** | 树莓派 4 + USB 网卡 + USB Wi-Fi | 想让宿舍所有设备（手机/平板/电脑）共享一条已认证的网 |

两种方案都是**纯 Python 标准库**实现，零第三方依赖。

---

## 方案 A：Windows 纯本地（最简单）

一个 Python 脚本 + 一个配置文件，不需要任何额外硬件，不需要管理员权限。

### 快速开始

```bat
:: 1. 安装 Python 3.6+（务必勾选 Add python.exe to PATH）
::    https://www.python.org/downloads/

:: 2. 双击 njupt_autologin_setup.bat   ← 自动检查环境，缺 Python 会引导安装

:: 3. 复制配置模板并填写
copy njupt_autologin.conf.example njupt_autologin.conf
notepad njupt_autologin.conf

:: 4. 双击 njupt_autologin.bat 验证（看到"登录成功"即 OK）

:: 5. 双击 njupt_autologin_autostart.bat 按 1 → 注册开机自启
```

### 配置文件

```ini
user=                   # 学号（不带后缀）
pass=                   # 密码（原样填写，含 @ . # 等特殊字符无需转义）
suffix=@cmcc            # 移动 @cmcc / 电信 @njxy / 校园网留空
interval=900            # 检测间隔（秒）
attempts=3              # 登录失败重试次数
attempt_delay=6         # 重试间隔（秒）
quiet_start=00:00       # 静默时段：链路断则不尝试
quiet_end=06:00
```

### 运行时行为

```
在线 ──────── 每周期只 ping 网关确认链路（不发登录请求，零打扰）
  │ ping 不通
  ▼
掉线 ──────── 每 30 秒 ping 网关，等网线上线
  │ 网关可达（网线插着）
  ▼
无法访问外网 ─→ 自动尝试登录（失败重试 N 次，间隔 6 秒）
  └ 休眠唤醒 → 立即检查，不用等周期
```

- **有网时零 HTTP 请求**：只发一个 ICMP ping 保活
- **连手机热点时不会乱登录**：网关 ping 不通就不动作
- **切网自动重连**：网线插回、休眠唤醒都会立即检测

### 文件说明

| 文件 | 用途 |
|---|---|
| `njupt_autologin.py` | 主程序 |
| `njupt_autologin.conf.example` | 配置模板（复制成 `.conf` 后填写） |
| `njupt_autologin.bat` | 单次验证 |
| `njupt_autologin_watch.bat` | 后台常驻（静默运行，失败重试 3 次） |
| `njupt_autologin_autostart.bat` | 一键安装/卸载开机自启 |
| `njupt_autologin_stop.bat` | 停止后台常驻进程 |
| `njupt_autologin_setup.bat` | 环境检查与自动配置 |
| `njupt_allow_hotspot_lan.bat` | 放行树莓派热点网段访问本机（方案 B 用） |

---

## 方案 B：树莓派全自动

树莓派接宿舍校园网口，自动认证并把网络分享给电脑和手机。

### 拓扑

```
学校网口 ──(网线)── eth0(树莓派板载) ──NAT──► 校园网
                          │
                 USB 转网口 ──► Windows 电脑 (192.168.9.x)
                          │
                 USB Wi-Fi ──► 热点 WPA2 (192.168.10.x，默认限 4 台)
```

有线和无线**两个子网互相可通**（可直接访问对方设备）。

### 快速开始

```bash
# 1. 把本项目传到树莓派（U 盘 / scp 均可）
# 2. 一键安装（自动装依赖、配镜像源、注册开机自启）
sudo bash install.sh

# 3. 浏览器配置
#    http://192.168.9.1:8080   （电脑插 USB 网线后访问）
```

### 最省事：只传一个文件

`release/campuspi-onefile-install.sh` 是自解压的单文件安装包，内嵌了
`campuspi-pi.zip`。把它传到树莓派后执行：

```bash
sudo bash campuspi-onefile-install.sh
```

它会依次完成：安装依赖 → 部署主程序并注册服务 → 安装/启动 RDP →
应用转发延迟优化 → 启动服务 → **用现有账号真实登录一次验证自动连接**。
已有的 `/etc/campuspi/config.json` **不会被覆盖**，账号密码和运营商配置保留。

- 不想装 RDP：`WITH_RDP=0 sudo bash campuspi-onefile-install.sh`
- 只解压检查（不改系统）：`DRY_RUN=1 bash campuspi-onefile-install.sh`

部署完成后验证自动连接：

```bash
sudo python3 /opt/campuspi/main.py --check-login
```

会用当前配置里的账号真实登录一次，并打印网关返回的结果。

`install.sh` 会自动完成：国内镜像源 → 安装 hostapd/dnsmasq/iptables/iw → 时区校时 → 部署到 `/opt/campuspi` → 注册 systemd 服务 → 自检。

### 特性

| 特性 | 说明 |
|---|---|
| 自动重连 | 掉线时每 30 分钟自动尝试；有 PC 接入时立即尝试 3 次 |
| 静默时段 | 默认 00:00–06:00 链路断则不重试，6 点后立即恢复；**网络在线时不受静默影响** |
| 无 RTC 保护 | 树莓派断电后时间归零，此时**不启用静默**，避免永远不重连 |
| 强制门户自动跳转 | 校园网离线时，电脑插线后 **Windows 自动弹出配置页**（dnsmasq 劫持探测域名 + `nla.py`） |
| USB 网线自愈 | 拔插 USB 网卡导致 IP 丢失时，自动重新配置（约 5 秒恢复） |
| 远程桌面 | Web 后台一键装 xrdp，Windows 远程桌面直连；仅 PC/热点子网可访问 |
| 延迟优化 | 一键应用 fq_codel / RPS / performance / 关闭 EEE，开机自动保持 |
| Web 后台 | 账号/运营商/热点/静默时段全在网页配置，密码字段脱敏 |

### Web 后台

访问 `http://192.168.9.1:8080`：

- **校园网**：账号、密码、运营商（校园网/移动 `@cmcc`/电信 `@njxy`，可增删）、检测间隔、网卡
- **热点**：SSID、密码、最大设备数（默认 4）、信道
- **PC 有线**：接入时立即尝试次数（默认 3）、间隔（默认 6 秒）
- **静默时段**：起止时间，支持跨午夜
- **Web 后台**：管理密码（设置后所有接口需 Basic 认证）

### 远程桌面（RDP）与延迟优化

Web 后台新增两个一键功能：

- **🖥 远程桌面（RDP）**：一键安装/启动 `xrdp`。首次安装需要树莓派能上网
  （apt 装 xrdp；没有桌面环境时装轻量 XFCE，约 1-3 分钟）。
  完成后点「下载 .rdp 连接文件」，Windows 双击即可用系统自带的
  “远程桌面连接”打开；也可以直接运行 `mstsc /v:192.168.9.1`。
  登录用**树莓派的系统账号/密码**（不是 Web 后台密码）。
  3389 只允许 `192.168.9.0/24` 和 `192.168.10.0/24` 访问，校园网侧
  直接 DROP；`campuspi-rdp-firewall.service` 开机自动重建规则。
- **⚡ 转发延迟优化**：一键应用 `fq_codel`、RPS/RFS、CPU performance、
  关闭网卡 EEE 与 Wi-Fi 省电；`campuspi-tuning.service` 开机自动重新应用。
  会略增功耗/温度，请做好散热。RDP 是 PC 直连树莓派，不经过校园网转发，
  本身延迟很低。
- **🛠 桌面/任务栏修复**：本地桌面或 RDP 桌面没有任务栏时，一键修复
  LXDE（lxpanel）/XFCE（xfce4-panel）/Wayland（wf-panel-pi）：
  缺包自动安装、面板配置/自启动缺失或损坏时先备份再恢复默认，
  并在当前 X11 会话里重启任务栏。Wayland 需注销/重启，RDP 需断开重连。
  「深度重置」会重置用户面板布局。

命令行等价操作：

```bash
sudo bash /opt/campuspi/setup_rdp.sh        # 安装/启动 RDP
sudo bash /opt/campuspi/setup_tuning.sh     # 延迟优化
sudo bash /opt/campuspi/repair_desktop.sh   # 修复任务栏
RESET_PANEL=1 sudo bash /opt/campuspi/repair_desktop.sh  # 深度重置面板
```

> RDP 只建议在内网使用；树莓派账号请设置强密码，Web 后台也建议设置管理密码。

### 常用命令

```bash
journalctl -u campuspi -f              # 实时日志
tail -f /var/log/campuspi.log          # 日志文件
sudo systemctl restart campuspi        # 重启服务
curl -X POST http://192.168.9.1:8080/api/action/login   # 手动触发登录

# 查看本网络登录页实际提供的运营商后缀（排查"账号错误"）
curl -s http://10.10.244.11/a79.htm | iconv -f gb18030 -t utf-8 \
  | grep -o "carrier='.*'"
```

---

## 认证协议（南邮：哆点 Dr.COM 新版 ePortal，2026-11 起）

南邮网关已升级。`p.njupt.edu.cn/a79.htm` 的 `page.portal_api` 为
`/eportal/portal/`，登录是单步 JSONP（GET）：

```
GET http://10.10.244.11:801/eportal/portal/login
    ?callback=jsonpReturn&login_method=1
    &user_account=,0,<学号+运营商后缀>
    &user_password=<密码>
    &wlan_user_ip=<本机校园网 IP>&wlan_user_mac=<MAC>
    &wlan_ac_ip=&wlan_ac_name=&jsVersion=4.1.3
    &terminal_type=1&lang=zh
```

返回 `jsonpReturn({...})`：

- `result=1`（或 `ok`）= 成功
- `result=0` 时看 `ret_code`：`1/4` 账号或运营商错误、`2` 终端已在线、
  `7` Radius 认证失败……
- 失败时 `msg` 是网关的原始中文提示，日志里会原样显示

要点：

- 旧路径 `/eportal/?c=ACSetting&a=Login` **已经不在处理登录**，现在只会
  返回 EPortal 管理页 HTML（这也是旧版代码日志里出现 `<title>EPortal</title>`
  的原因）；代码保留它仅作旧网关回退。
- 直连网关 IP（绕过 DNS），端口 801，HTTP 明文；Web 后台可改。
- 账号前缀 `,0,` 是 PC 端；手机/平板是 `,1,`。
- **运营商 = 账号后缀**：校园网无后缀 / 移动 `@cmcc` / 电信 `@njxy` /
  联通 `@lt`（不同入口的登录页给出的后缀可能不同，以实际登录页为准）。
  后缀填错会得到"**账号错误(运营商登录请检查输入的账号和绑定的运营商
  账号是否有误)**"。

> 代码内置 **4 种认证适配器**，换学校也能用：
> - `drcom`（默认）：哆点 Dr.COM
> - `eportal`：锐捷 eportal
> - `srun`：深澜（双步 challenge，`enc` 自动嗅探 md5/base64）
> - `generic`：通用表单，填 URL + 参数模板 + 成功标记即可，无需改代码

### 在线探测（三态）

校园网未认证时外网必然不通，所以**不能用"能不能上网"判断是否在线**。本项目用三态探测：

```
ping 网关不通           → offline      （链路断，不尝试登录）
ping 通 + 外网不通       → need_login   （未认证，执行登录）
ping 通 + 外网可访问     → online       （已认证，什么都不做）
```

外网探测**必须用 HTTP 而非 HTTPS**：未认证时 HTTP 会被网关劫持到认证页（可识别），HTTPS 则直接被阻断。所有请求强制绕过系统代理。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 日志出现 `<title>EPortal</title>` / 返回管理页 HTML | 网关已升级，旧登录接口失效。更新到 v1.1.0+（新版 `/eportal/portal/login`） |
| 提示"账号错误(运营商登录请检查输入的账号和绑定的运营商账号是否有误)" | 账号/密码错，或运营商后缀不对。打开 `http://10.10.244.11/a79.htm` 看页面实际给出的运营商后缀再选 |
| 提示"未绑定运营商账号" | 运营商后缀选错。移动 `@cmcc`、电信 `@njxy`、联通 `@lt`、校园网留空（以 a79.htm 实际显示为准） |
| 提示"认证出现异常！" `ret_code=1`，且 a79.htm 全部显示"无后缀" | 网关当前不要求后缀。把 Web 后台运营商改为"校园用户/无后缀"，账号只填学号；新版代码检测到全部无后缀时会自动忽略本地配置的 `@cmcc` 等后缀 |
| 提示 `AC999` | AC 侧认证失败。常见原因：欠费、短时间内频繁尝试触发风控、密码错误。**停掉所有重试冷却 30 分钟**，再用浏览器手动登录一次验证；多校文档反馈"改密码"能解决多数情况 |
| `'xxx' is not recognized` | bat 编码问题。本项目所有 bat 为**纯 ASCII**（中文由 Python 输出），若出现说明文件被 UTF-8 保存过，重新拷贝即可 |
| 热点起不来 | `journalctl -u hostapd -e`；确认网卡支持 AP 模式（`iw list` 看 Supported interface modes） |
| 手机连不上热点 | 信道别用 12/13（国内手机不支持），用 1/6/11 |
| 拔插 USB 网线后打不开管理页 | 新版本已自愈（约 5 秒）。若仍不行：`ipconfig /renew` 或在树莓派上 `sudo systemctl restart campuspi` |
| 插着树莓派时 Wi-Fi 能连但上不了网 | 默认只在校园网在线时把 `192.168.9.1` 设为 PC 的网关/DNS；离线时不下发默认网关，DNS 改为 `223.5.5.5,119.29.29.29`。执行一次 `ipconfig /renew` 即可走 Wi-Fi。需要旧行为时在 `/etc/campuspi/config.json` 的 `pc` 段设置 `"share_online_only": false` |
| 离线时想看树莓派状态 | 直接访问 `http://192.168.9.1:8080`。这是 `192.168.9.0/24` 直连路由，不依赖默认网关或 DNS；后台设置密码时用该密码登录 |
| 手机访问不到电脑 | 在 Windows 上以管理员运行 `njupt_allow_hotspot_lan.bat` 放行热点网段 |

---

## 目录结构

```
【Windows 方案】
njupt_autologin.py        主程序（三态探测 + 登录重试 + 常驻状态机）
njupt_autologin.conf      配置文件（.example 为模板，实际配置需自行创建）
njupt_autologin*.bat      单次/常驻/自启/停止/环境配置

【树莓派方案】
install.sh                一键安装脚本
campuspi.service          systemd 服务
main.py                   主程序（周期检测 / PC 接入触发 / 静默时段 / 时间同步）
config.py                 配置加载保存（/etc/campuspi/config.json）
portal.py                 认证适配器（drcom / eportal / srun / generic）
network.py                接口探测、静态 IP、NAT、客户端互通
hotspot.py                hostapd + dnsmasq 配置生成与守护
webui.py + web/index.html Web 管理后台（含 RDP/延迟优化一键按钮）
nla.py                    强制门户：离线时让 Windows 自动弹配置页
setup_rdp.sh              一键安装/启动 xrdp 远程桌面
setup_tuning.sh           一键转发延迟优化（fq_codel/RPS/performance）
repair_desktop.sh         一键修复任务栏（LXDE/XFCE/Wayland）
deploy_all.sh             全自动部署编排（install + RDP + 延迟优化）
connect_pi_rdp.bat        Windows 一键打开树莓派远程桌面
selftest.py               自检（Windows 上也能跑）

【旧版 Windows 助手】
pc_helper.py              手动检测/打开配置页/触发登录
```

---

## 安全提示

- 本工具**不包含任何账号密码**，所有凭据由使用者自行填写在本地配置文件中（已被 `.gitignore` 排除）
- 树莓派上的 `/etc/campuspi/config.json` 明文保存密码（权限 0600，仅 root 可读）
- Web 后台默认无密码，**建议设置管理密码**（Basic 认证，明文传输，仅限局域网）

## 免责声明

仅供学习交流与个人在自有账号上使用。请遵守学校网络管理规定，账号密码由使用者自行负责保管。

## License

MIT
