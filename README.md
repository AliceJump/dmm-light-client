# DMM Light 独立客户端

独立 Windows 桌面程序，用来管理、安装、更新与启动 DMM 游戏。使用自己的数据目录 `%LOCALAPPDATA%\DmmLightClient`，不依赖官方 Player，也不会修改官方 Player 的账号或配置文件。

> 完整操作说明见 [`docs/USAGE.md`](docs/USAGE.md)。

## 功能

- **游戏管理**：刷新游戏目录（一般 + FANZA / R18）、安装、更新、启动；下载支持断点续传与逐文件校验，启动前自动检查更新。
- **原生游戏详情页**：简介、截图、宣传片、运行环境与官方链接，不加载官方网页；普通版与 R18 版可一键互跳。
- **账号**：内置浏览器登录，登录后自动建档，支持多份登录态与随时切换。
- **网络**：内置多节点轮换与故障切换，也可配置 HTTP / SOCKS5 外部代理并测试连通性。
- **目录与卸载**：每款游戏可单独指定或迁移安装目录；卸载默认只移除记录、保留文件。
- **命令行**：`dmmcli` 复用同一份数据、登录态与节点配置，无需图形界面。
- **界面**：跟随系统明暗、支持触摸屏、多个操作排队互不阻塞。

## 快速开始

- **用发布包**：到 **Releases** 下载 `DmmLightClient-<版本>-win64.zip`，解压后双击 `DmmLightClient.exe`。
- **从源码**：安装 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)，运行 `uv sync`，再双击 `start.cmd`。

## 文档

- [`docs/USAGE.md`](docs/USAGE.md) — 面向使用者的完整操作说明：登录、网络与节点、安装 / 更新 / 启动、详情页、账号、目录与卸载、命令行、常见问题与已知限制。

## 开发

```bat
uv sync
uv run python -m unittest discover -s tests
uv run python scripts/build.py     # PyInstaller 打包（onedir + QtWebEngine）
```
