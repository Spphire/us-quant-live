# 托盘启动器使用指南 / Tray Launcher Guide

一键启动美股量化交易系统的 Windows 系统托盘程序。

A single-click Windows tray app to launch the US quant trading system.

---

## 功能 / Features

- 🚀 **一键启动**：双击 .exe 即可启动整个交易系统
- 🛡️ **单例保护**：基于 Windows 命名 mutex，防止重复启动
- 🔄 **自动监督**：scheduler 异常崩溃时自动重启
- 📊 **托盘菜单**：右键托盘图标访问所有功能
- 🖼️ **K线风格图标**：定制的暗色主题蜡烛图图标
- 💾 **进程树清理**：退出时干净杀掉所有子进程（scheduler + executor + dashboard）

---

## 快速开始 / Quick Start

### 方法 1：直接运行 Python 脚本（推荐用于开发）

```bash
cd W:\实验室项目\us-quant-live
source venv/Scripts/activate
python tools/tray_launcher.py
```

启动后会出现：
1. **K 线图标**出现在 Windows 系统托盘（右下角）
2. **弹窗通知**显示 "US Quant Live - Started"
3. **scheduler 后台运行**（每日 12:00/22:00 北京时间自动触发）
4. **dashboard** 自动启动在 `http://127.0.0.1:8766`

### 方法 2：构建独立 .exe（推荐用于生产）

```bash
cd W:\实验室项目\us-quant-live
source venv/Scripts/activate
python tools/build_exe.py
```

输出：`dist/USQuantLive.exe`（约 15-30 MB）

之后只需双击 `USQuantLive.exe` 即可启动，无需 Python 环境（但仍需要 venv 用于 scheduler）。

---

## 托盘菜单 / Tray Menu

右键托盘图标显示菜单：

| 菜单项 | 功能 |
|--------|------|
| 📊 **Open Dashboard** | 浏览器打开 http://127.0.0.1:8766 |
| 📁 **Open Log Folder** | 资源管理器打开日志目录 |
| 📄 **Open Latest Log** | 用默认程序打开 scheduler.out.log |
| ℹ️ **Status** | 显示当前 scheduler PID、运行状态、dashboard URL |
| 🔄 **Restart Scheduler** | 重启 scheduler（保留 launcher） |
| ❌ **Exit** | 完全退出（停止 scheduler + dashboard + executor） |

**双击托盘图标** = Open Dashboard（默认行为）

---

## 单例保护机制 / Single-Instance Protection

启动器使用 **Windows 命名 mutex** (`us-quant-live-tray-launcher-singleton`) 保证同时只有一个实例运行。

- 第一次启动：正常启动
- 重复启动：弹窗提示"Launcher is already running"，进程立即退出（退出码 1）
- 启动器退出后：mutex 自动释放，下次可正常启动

**优势**：基于 Windows 内核对象，比文件锁更可靠；进程崩溃后系统自动清理。

---

## 自动监督 / Auto-Supervision

启动器会监控 scheduler 子进程：

- 每 5 秒检查一次 scheduler 是否存活
- 若 scheduler 死亡且**运行时间 ≥ 30 秒** → 自动重启
- 若 scheduler 死亡且**运行时间 < 30 秒** → 不重启（视为配置错误），弹窗通知

**为什么 30 秒阈值？** 防止配置错误（如错误 API key）导致无限重启循环。需要用户手动修复后再"Restart Scheduler"。

---

## 进程层次结构 / Process Hierarchy

```
USQuantLive.exe (托盘启动器)
└── python.exe (daily_alpaca_scheduler.py)
    ├── python.exe (dashboard_server.py)      ← HTTP 服务在 :8766
    └── python.exe (alpaca_executor.py)        ← 12:00/22:00 触发时启动
```

退出时使用 `taskkill /F /T /PID <launcher_pid>` 清理整个进程树，确保无遗留进程。

---

## 文件位置 / File Locations

```
W:\实验室项目\us-quant-live\
├── tools\
│   ├── tray_launcher.py          ← 启动器主程序
│   ├── tray_icon.ico             ← K 线图标
│   ├── tray_icon_preview.png     ← 图标预览
│   ├── generate_tray_icon.py     ← 图标生成器
│   ├── build_exe.py              ← PyInstaller 打包脚本
│   ├── test_tray_launcher.py     ← 单元测试
│   └── test_tray_launcher_e2e.py ← 端到端测试
├── dist\
│   └── USQuantLive.exe            ← 构建产物（运行 build_exe.py 后生成）
└── artifacts\daily_alpaca_scheduler\
    └── daemon\scheduler.out.log   ← scheduler 主日志
```

---

## 故障排查 / Troubleshooting

### 问题：双击 exe 后没反应

**排查步骤**：
1. 检查 Windows 任务栏的"显示隐藏图标"区域，K 线图标可能在那里
2. 检查 `Task Manager` → 进程列表，看是否有 `USQuantLive.exe`
3. 查看日志 `artifacts/daily_alpaca_scheduler/daemon/scheduler.out.log`

### 问题：弹窗"Cannot find Python interpreter"

**原因**：venv 不存在或路径错误。

**解决**：
```bash
cd <USQuantLive.exe 所在目录>
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt  # 或手动安装 pandas numpy scipy requests pystray pillow
```

### 问题：弹窗"Scheduler Crashed"

**原因**：scheduler 启动后 30 秒内崩溃，通常是：
- Alpaca API key 配置错误（检查 `configs/alpaca_acounts/alpaca_accounts.local.json`）
- 端口 8766 被占用（已有其他程序）
- Python 依赖缺失

**解决**：查看 `scheduler.out.log` 详细错误，修复后右键托盘图标 → Restart Scheduler。

### 问题：Dashboard 显示 "Disconnected"

**原因**：SSE 连接断开。

**解决**：
1. 检查 scheduler 是否还在运行（右键 → Status）
2. 若不在运行，点击 Restart Scheduler
3. 浏览器刷新页面（F5）

### 问题：第二次启动报"already running"，但实际没在运行

**原因**：前一次启动器异常崩溃（如断电），mutex 未释放但实际上已经无效。

**解决**：
- 通常**重启电脑**即可（mutex 是系统对象，重启会清空）
- 或在 Task Manager 中确认没有 `USQuantLive.exe` 后稍等 30 秒重试

---

## 构建 .exe 详细步骤 / Building the .exe

### 前置条件

```bash
cd W:\实验室项目\us-quant-live
source venv/Scripts/activate
pip install pyinstaller pillow pystray
```

### 构建

```bash
python tools/build_exe.py
```

输出：
```
dist/USQuantLive.exe       ← 主程序（~20MB）
build/                      ← PyInstaller 中间产物（可删除）
```

### 部署

将整个项目目录拷贝到目标机器，确保：
- `dist/USQuantLive.exe` 存在
- `tools/daily_alpaca_scheduler.py` 等源码存在
- `venv/` 已配置好依赖
- `configs/alpaca_acounts/alpaca_accounts.local.json` 已配置

然后双击 `dist/USQuantLive.exe` 即可。

### 自启动（可选）

将快捷方式放入 Windows 启动文件夹，开机自动启动：

```
Win+R → shell:startup → 粘贴 USQuantLive.exe 的快捷方式
```

---

## 测试 / Testing

### 单元测试

```bash
python tools/test_tray_launcher.py
```

测试覆盖：
- ✅ 路径解析
- ✅ 图标文件有效性
- ✅ 单例 mutex 正确性（包括释放后可重新获取）
- ✅ SchedulerSupervisor API

### 端到端测试（手动）

1. 启动 launcher：`python tools/tray_launcher.py`
2. 等待 ~20 秒
3. 访问 http://127.0.0.1:8766 应返回 200
4. 再次启动 launcher，应被 mutex 阻止
5. 右键托盘图标 → Exit
6. 验证 dashboard 不再可访问
7. 验证 Task Manager 中无残留 python.exe

---

## 设计决策 / Design Decisions

### 为什么用 pystray 而非 wxPython/tkinter？

- **pystray** 专注于托盘图标，依赖少（仅 ~1MB）
- **wxPython/tkinter** 包含完整 GUI 框架，对仅需托盘图标的应用过于重量级

### 为什么用 Windows 命名 mutex 而非文件锁？

- **mutex** 是内核对象，操作系统自动管理生命周期；进程崩溃后系统自动清理
- **文件锁**需要进程主动释放，崩溃后可能留下"僵尸锁"导致永远无法重启

### 为什么用 30 秒早死阈值？

- scheduler 启动包括：加载 Python 模块、连接 Alpaca API、初始化 lot ledger
- 通常需要 5-15 秒
- 30 秒既能识别真正的配置错误，又能容忍正常的启动延迟

### 为什么需要 taskkill /T 而非 process.terminate()？

- `process.terminate()` 只杀直接子进程
- scheduler 会启动 `dashboard_server.py` 和 `alpaca_executor.py` 等孙进程
- 这些孙进程会成为孤儿进程，继续占用资源
- `taskkill /T` 杀整个进程树，干净彻底

---

## 已修复的 Code Review 问题

本启动器经过 expert code review，修复了以下问题：

1. ✅ **日志文件句柄泄漏**：每次重启时正确关闭 log_fp
2. ✅ **进程树未清理**：使用 `taskkill /T` 杀整个进程树
3. ✅ **monitor 与 manual restart 竞态**：通过 `restart_in_progress` 标志避免双重启动
4. ✅ **PyInstaller frozen 路径检测**：bundle 模式下使用 .exe 所在目录而非临时目录
5. ✅ **Mutex 早期返回路径泄漏**：放在 finally 中保证释放
6. ✅ **早死阈值过低**：从 10 秒提升到 30 秒，避免误判正常启动延迟
7. ✅ **CreateMutex 第二次返回的 handle 未关闭**：release 总是 CloseHandle

---

**完成时间**：2026-06-27
**Dashboard URL**：http://127.0.0.1:8766
**项目根目录**：W:\实验室项目\us-quant-live
