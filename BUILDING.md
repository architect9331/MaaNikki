# Windows x64 构建

[返回首页](README.md) · [用户手册](docs/manual.md) · [开发文档](docs/architecture.md)

本文说明 Windows x64 开发环境、客户端编译与发布流程。

## 构建环境

- Windows 10/11 x64。
- Node.js **24.13.0**，版本记录在 `.node-version`。
- Rust **1.99.0**，由 `rust-toolchain.toml` 固定。安装 Rust 时选择 MSVC 工具链。
- Visual Studio 2022 或 Build Tools，安装「使用 C++ 的桌面开发」及 Windows SDK。
- 构建主机安装 Python **3.11 或更高版本**；发布包使用独立的 Python **3.12.10**。
- PowerShell，以及可以访问 npm、crates.io、PyPI、python.org 和 GitHub 的网络。
- pnpm **10.28.0** 由 `client/package.json` 的 `packageManager` 固定。构建脚本通过 npm 直接安装到 `.build/build-tools/`，后续复用该版本。JS 依赖使用冻结的 `pnpm-lock.yaml`，Rust 构建使用 `Cargo.lock` 和 `--locked`。

## 从干净源码构建

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Action Package
```

脚本会验证源码和 OCR 模型，构建前端及桌面客户端，从锁定地址下载运行环境并核对 SHA-256，执行 Python Agent 导入及 MaaFramework 资源加载检查，然后生成：

- `artifacts/MaaNikki-v<interface版本>-win-x86_64.zip`：便携运行包。
- `artifacts/MaaNikki-v<interface版本>-source.zip`：对应源码和构建脚本。
- `artifacts/MaaNikki-v<interface版本>-SHA256SUMS.txt`：上述文件的 SHA-256。

应用项目版本来自 `interface.json`。客户端版本来自 `client/package.json`、`client/src-tauri/Cargo.toml` 和 `client/src-tauri/tauri.conf.json`，三个客户端版本须一致。

单独检查源码：

```powershell
python scripts/release.py check
```

只准备和验证运行环境、不编译客户端：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Action Runtime
```

只编译客户端：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Action Build
```

修改前端或桌面客户端后，默认编译并同步本机程序：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Action Local -Python F:/MaaNikki/python/python.exe
```

`Local` 构建桌面客户端，核对版本，将根目录旧 `MaaNikki.exe` 备份到 `.build/client-backups/`，替换后校验 SHA-256。每轮界面修改完成后执行一次，同步前请退出客户端。前端 `pnpm build` 的产物位于 `client/dist`，桌面程序使用 `Local` 更新。

准备出的运行环境位于 `.build/runtime-<随机编号>/`，客户端产物位于 `client/src-tauri/target/release/mxu.exe`。发布包从锁定依赖与源码组装；下载缓存、构建目录和输出包由 `.gitignore` 排除。

构建工具的 npm 缓存及安装诊断日志保存在 `.build/npm-cache/`。GitHub 自动构建失败时会将其中的日志保存为 `MaaNikki-build-npm-logs` 附件，保留 7 天，便于定位安装失败的具体原因。

## GitHub 自动发布

准备发布新版本时，更新 `interface.json` 中的项目版本号并推送到默认分支 `main`。Windows 自动构建成功后，发布任务会创建指向本次构建提交的 `v<版本号>` 标签，上传运行包、源码包和 SHA-256 校验文件，核对 GitHub 上的附件哈希后公开 Release。新版本标为预发布，更新说明取自 `CHANGELOG.md` 中对应版本的内容。

Release 公开后，服务器上的 QQ 机器人通过 GitHub Webhook 向交流群发送更新公告，包含版本号、完整更新日志和更新方式，长公告自动分条。程序与维护说明见 [QQ 更新公告](scripts/qq_notifier/README.md)。

发布任务使用 `scripts/incremental.py` 对完整运行包逐文件计算 SHA-256，发布 `MaaNikki-v<版本>-update.json` 与 `MaaNikki-v<目标>-from-v<起始>.patch.zip`。版本清单保留最近 10 个起始版本的文件哈希，分别生成直达本版的增量包；增量包不小于完整包时只保留完整包。首次接入时从上一版本的完整运行包建立基准，后续直接复用上一版本的清单。附件校验文件覆盖完整包、源码包、版本清单和增量包。

客户端根据当前项目版本匹配增量包，安装前核对本地基准、全部变更文件及删除清单，保留配置与账号数据。文件按原路径备份，应用失败时恢复，项目版本最后写入；增量包不适用时自动改下完整包。旧客户端继续识别原来的 Windows 完整运行包，新客户端首次安装后才会选择增量包。

同一版本已经公开时，后续普通提交只构建，不覆盖该版本的标签和附件；拉取请求与非默认分支的手动构建不发布。每次需要新安装包供用户更新时，必须递增项目版本号，客户端版本仍只在客户端本身需要更新时调整。

发布失败时，附件不完整的 Release 会保持为草稿。可以在原构建运行中重新运行失败的发布任务，以同一提交恢复上传；也可在默认分支手动启动构建。已有标签或草稿属于其他提交时会停止发布，不移动标签或混用不同提交的附件，应重新运行原构建，或递增版本号发布新的提交。构建失败不会启动发布任务。

## 运行时依赖及模型

Python 3.12.10、MaaFramework SDK 5.14.2 和所有 Windows x64 Python wheels 的下载地址、版本和 SHA-256 在 `build/runtime.lock.json`。Python 依赖清单是 `requirements.txt`；`requirements.lock` 包含同一组版本及哈希。构建脚本直接展开经过校验的 wheels，嵌入式 Python 无需安装 pip。

OCR 模型及字典保留在源码 `resource/model/ocr/`，由 `build/resource-models.lock.json` 校验。修改模型时需要同时更新模型清单中的来源、版本和校验值。模型来自 MaaCommonAssets 的 PP-OCRv6 small，具体来源及许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

发布包保留 Python 和 wheels 自带的许可证、MaaFramework SDK 许可证、`licenses/` 和第三方说明，并收集已安装 JS 包和本次构建下载的 Rust crates 所附许可文件，写入 `licenses/dependencies/` 和清单。Windows WebView2 使用系统安装的运行时，缺失时由客户端引导下载。发布端需要具备相应 Microsoft Visual C++ 运行库；实际启动验证应在干净 Windows 环境完成。

Agent 导入与资源加载检查用于核对运行环境。程序启动、连接与任务效果由用户在游戏中验收。

## 开发与调试

页面入口、导航、截图调度、路线工具和库存字段统一见[开发文档](docs/architecture.md)。游戏内任务由用户运行验收，代码修改遵循 [AGENTS.md](AGENTS.md)。

## 源码公开范围

源码包含 `agent/`、`client/`、`resource/`、`tasks/`、`options/`、`interface.json`，以及依赖清单、构建脚本、工作流和许可说明。`python scripts/release.py check` 会输出 `.build/source-files.txt` 供核对。

此流程面向 Windows x64。本项目代码采用 AGPL-3.0-only，全文见根目录 `LICENSE`；上游许可原文保存在 `licenses/`。游戏素材和 OCR 模型的许可独立于代码许可证。
