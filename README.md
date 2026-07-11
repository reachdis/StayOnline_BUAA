# StayOnline

StayOnline 是一个用于北航校园网网关的 Windows 自动保活/自动登录工具。程序会定期检查网络连通性；如果检测到掉线，会尝试通过校园网网关重新登录。笔记本从睡眠或休眠状态恢复后，程序也会尽快执行一次网络检查和登录尝试。

## 源码来源

本项目的网关登录协议实现主要参考并整理自 [zzdyyy/buaa_gateway_login](https://github.com/zzdyyy/buaa_gateway_login)。

当前版本在此基础上增加了：

- Windows 后台运行支持
- 周期性网络检查
- 掉线后自动重试登录
- 睡眠/休眠恢复后的自动检查
- 日志记录
- Windows 通知提示
- PyInstaller 单文件 exe 打包

## 文件说明

```text
StayOnline.exe      打包好的 Windows 可执行文件
stay_online.py      核心源码
requirements.txt    源码运行所需 Python 依赖
```

## 直接使用 exe

推荐普通用户直接运行 `StayOnline.exe`。不需要安装 Python，也不需要安装 `requirements.txt` 里的依赖。

运行前需要先设置两个环境变量：

| 变量名 | 含义 |
| --- | --- |
| `BUAA_USERNAME` | 校园网账号 |
| `BUAA_PASSWORD` | 校园网密码 |

在 Windows 命令提示符中执行：

```bat
setx BUAA_USERNAME "你的校园网账号"
setx BUAA_PASSWORD "你的校园网密码"
```

设置后需要重新打开终端，或注销/重启后再运行程序。`setx` 写入的是之后新启动进程可见的环境变量。

然后双击运行：

```text
StayOnline.exe
```

程序是后台窗口模式，不会显示控制台窗口。日志会写入 exe 同目录下的：

```text
logs\stay_online.log
```

## 开机自启

如果希望登录 Windows 后自动运行，可以把 `StayOnline.exe` 的快捷方式放到启动目录。

启动目录路径：

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

操作方式：

1. 右键 `StayOnline.exe`，创建快捷方式。
2. 打开上面的启动目录。
3. 把快捷方式放进去。
4. 下次登录 Windows 时程序会自动启动。

## 从源码运行

如果希望直接运行源码，需要本机安装 Python 3.10 或更高版本。

安装依赖：

```bat
pip install -r requirements.txt
```

运行：

```bat
python stay_online.py
```

如果希望后台无窗口运行：

```bat
pythonw stay_online.py
```

## 工作逻辑

程序启动后会立即进入保活循环：

- 每 3 分钟做一次网络连通性检查。
- 如果无法访问外网，尝试登录校园网网关。
- 每 15 秒做一次轻量时间检查，用于判断电脑是否刚从睡眠/休眠状态恢复。
- 如果检测到恢复，会立刻执行一次网络检查和登录尝试。

轻量时间检查不会访问网络，只比较当前时间和上次检查时间，资源占用很低。

## 注意事项

- 账号密码通过环境变量读取，不要把真实账号密码写进源码或提交到 GitHub。
- 程序会在自身目录下写日志，请放在当前用户有写权限的位置，不建议放到 `C:\Program Files`。
- 当前 exe 是 Windows 版本，不能直接在 macOS 或 Linux 上运行。
- 校园网网关参数可能随环境变化。如果登录失败，需要检查源码中的 `AC_ID` 和网关地址是否仍适用于当前网络。

## 致谢

感谢 [zzdyyy/buaa_gateway_login](https://github.com/zzdyyy/buaa_gateway_login) 提供的校园网登录实现参考。
