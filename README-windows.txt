============================================================
 NJUPT 校园网 Windows 本地自动登录
 （不依赖树莓派，纯标准库，零第三方依赖）
============================================================

一、文件说明
  njupt_autologin.conf          配置模板（唯一要填的文件）
  njupt_autologin.bat           单次验证：检测+登录一次
  njupt_autologin_watch.bat     持久化：后台常驻（每15分钟ping确认，
                                掉线时30秒快速检测，以太网上线且无法
                                访问外网自动尝试登录3次，休眠唤醒立即检查）
  njupt_autologin_stop.bat      停止后台常驻进程
  njupt_autologin_autostart.bat 一键安装/卸载开机自启（任务计划程序）
  njupt_autologin.py            主程序

二、首次使用（3 步）
  1. 编辑 njupt_autologin.conf，填 user（学号）、pass（密码）、
     suffix（移动@cmcc / 电信@njxy / 校园网留空）
  2. 双击 njupt_autologin.bat 验证（看到"登录成功"即 OK）
  3. 双击 njupt_autologin_autostart.bat 按 1 注册开机自启

三、运行环境要求
  - Python 3.6+（下载 https://www.python.org/downloads/ 安装时
    务必勾选 "Add python.exe to PATH"）
  - 无需任何第三方库（pip install 都不用，纯标准库）
  - Windows 自带 ping / curl 即可

四、日常使用
  - 改配置：只改 njupt_autologin.conf
  - 手动查一次：双击 njupt_autologin.bat
  - 停止常驻：双击 njupt_autologin_stop.bat
  - 取消自启：双击 njupt_autologin_autostart.bat 按 2

============================================================
