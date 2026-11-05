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

需要编译并同步本机桌面程序时，运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Action Local -Python F:/MaaNikki/python/python.exe
```

`Local` 构建桌面客户端，核对版本，将根目录旧 `MaaNikki.exe` 备份到 `.build/client-backups/`，替换后校验 SHA-256。同批修改只需构建一次，同步前请退出客户端。前端 `pnpm build` 的产物位于 `client/dist`，桌面程序使用 `Local` 更新。

准备出的运行环境位于 `.build/runtime-<随机编号>/`，客户端产物位于 `client/src-tauri/target/release/mxu.exe`。发布包从锁定依赖与源码组装；下载缓存、构建目录和输出包由 `.gitignore` 排除。

构建工具的 npm 缓存及安装诊断日志保存在 `.build/npm-cache/`。GitHub 自动构建失败时会将其中的日志保存为 `MaaNikki-build-npm-logs` 附件，保留 7 天，便于定位安装失败的具体原因。

## 识别模型更新

`scripts/model_update.py` 负责游戏更新后的祝福闪光种类、等级、角标模型与衣柜服装目录，OCR 模型不在其中。工具只离线工作，不启动游戏。素材路径放在 `.local/model-update/config.json`，产物放在 `.build/model-update/<run>/`，两者都不进入仓库。

按顺序运行，`<run>` 取一个版本名：

| 步骤 | 命令（`python scripts/model_update.py ...`） | 约需 | 人工确认 |
| --- | --- | --- | --- |
| 0. 解包新版本物品表和图标 | 本机私有流程 | 约 20 分钟 | 卡片抽查 |
| 1. 记录基线 | `init --run <run>` | 秒级 | |
| 2. 对比差异 | `discover --run <run>` | 秒级 | 查看 `diff.json` 的新增、移除、改名 |
| 3. 列出缺口 | `assets --run <run>` | 秒级 | 祝福名称和颜色自动取自游戏配置表，配置表缺的项才要手填 `blessing-labels.json`；确实缺图时用 `--accept-gaps "<原因>"` |
| 4. 训练祝福 | `train --run <run> --model identity` | 约 2 分钟 | |
| 5. 更新衣柜目录 | `train --run <run> --model wardrobe` | 秒级 | |
| 6. 对比评估 | `evaluate --run <run> --model blessing`，`--model wardrobe` | 各数分钟 | 不通过时查看 `evaluation/*-gate.json` |
| 7. 打包校验 | `package --run <run>` | 约 1 分钟 | 查看 `package.json` |
| 8. 替换模型 | `promote --run <run> --confirm` | 约 1 分钟 | 由人决定是否替换 |

衣柜读取直接使用图鉴文字识别与目录查表，更新时只扩展部位与服装标签映射，原有类别编号保持稳定。

等级和角标的类别不随新祝福变化，第 6 步会检查它们在新图标上的表现。只有检查不通过或卡片界面变了才需要 `train --model level` 或 `badge`；界面变了还要先取得新的界面捕获。

替换门禁：

- 原有类别的准确率不能下降。下降就拦下；只有配对检验不显著时，才能用 `--waive "<检查名>=<原因>"` 记录原因后放行。
- 新类别与原有类别在同样的合成条件下比较，不能显著偏低，并且必须认得自己的原始卡片。
- 模型文件校验、标签一致性和打包运行环境中的实际加载都必须通过。

门禁指标主要来自合成卡片和同一账号的截图，不代表真实游戏中的准确率。替换后仍需在游戏内核对新衣服和新祝福闪光。第 8 步只改工作区：它会更新模型、`build/resource-models.lock.json`、`model-contract.json` 和祝福闪光图鉴，并运行 `python scripts/release.py check`，失败时自动还原。提交、发版和本地客户端同步要另外进行。

祝福闪光的模板回退图标来自公开 Wiki（`resource/image/blessing/`），需要另行补充。游戏导出素材不能放进仓库。

## 祝福闪光角标模型

发布包包含训练好的三分类模型，运行时使用现有 NumPy 和 OpenCV。需要重新训练时，在开发 Python 中安装 `build/requirements-model-training.txt`，然后运行：

```powershell
python scripts/train_blessing_badge.py --logs logs/blessing-lock --version <模型版本> --update-lock
```

训练使用经锁按钮核验的锁定状态、已确认的堆叠数量，以及整仓容量核对通过的单个未锁定样本。模型保存彩色像素与梯度特征的线性 SVM 权重；训练日志和原图保留在本地。分类分数处于模型间隔内或特征超出训练范围时，仓库读取会改用点击核验。新模型的游戏内表现由实际运行确认。

## GitHub 自动发布

普通提交和推送只同步源码，不触发构建。日常开发时将待发布说明记录在 `CHANGELOG.md` 顶部的 `## [待发布]` 章节下。「Build Windows x64」工作流在每日北京时间 `03:13`（UTC `19:13`）定时检查 `main` 分支：

- 若 `CHANGELOG.md` 没有 `## [待发布]` 内容且顶部版本已公开，前置检查直接跳过，不执行构建。
- 若存在待发布内容，前置任务会自动将项目小版本号递增（如 `0.9.5` → `0.9.6`），在 `client/` 自上一版本以来有改动时同步递增客户端版本号，将 `## [待发布]` 替换为当天日期的版本标题并提交推送到 `main`，随后执行构建与发布。
- 若上次定时任务已定稿版本但构建或发布失败，下次运行会把新的 `## [待发布]` 内容按分类并入该未公开版本，不会产生重复的版本标题。
- 若希望下次定时任务发布大版本（如 `0.10.0`），提前将 `CHANGELOG.md` 顶部标题改为 `## [0.10.0]` 即可；若需立即手动发布，可运行：

```powershell
gh workflow run build-windows.yml --ref main
# 或指定版本规则（patch / minor / 具体版本号）：
gh workflow run build-windows.yml --ref main -f version=minor
```

Windows 构建成功后，发布任务会创建指向本次构建提交的 `v<版本号>` 标签，上传运行包、源码包和 SHA-256 校验文件，核对 GitHub 上的附件哈希后公开 Release。新版本标为预发布，更新说明取自 `CHANGELOG.md` 中对应版本的内容。

Release 公开后，服务器上的 QQ 机器人通过 GitHub Webhook 向交流群发送更新公告，包含版本号、完整更新日志和更新方式，长公告自动分条。程序与维护说明见 [QQ 更新公告](scripts/qq_notifier/README.md)。

发布任务使用 `scripts/incremental.py` 对完整运行包逐文件计算 SHA-256，发布 `MaaNikki-v<版本>-update.json` 与 `MaaNikki-v<目标>-from-v<起始>.patch.zip`。版本清单保留最近 10 个起始版本的文件哈希，分别生成直达本版的增量包；增量包不小于完整包时只保留完整包。首次接入时从上一版本的完整运行包建立基准，后续直接复用上一版本的清单。附件校验文件覆盖完整包、源码包、版本清单和增量包。

客户端根据当前项目版本匹配增量包，安装前核对本地基准、全部变更文件及删除清单，保留配置与账号数据。文件按原路径备份，应用失败时恢复，项目版本最后写入；增量包不适用时自动改下完整包。旧客户端继续识别原来的 Windows 完整运行包，新客户端首次安装后才会选择增量包。

工作流仅通过每日定时任务或手动触发运行，普通推送和拉取请求均不触发软件构建。默认分支构建成功后发布新版本，同一版本已公开时保留原有标签和附件。

### 更新备用线路

线路统一配置在 `build/update-mirrors.json`，由客户端编译时内嵌，发布脚本共用。大于 1 MiB 且有公开 SHA-256 的更新包，下载前并发比较 GitHub 和 GH-Proxy 多节点，每条线路最多读取 512 KiB、等待 6 秒；GH-Proxy 快 20% 以上时优先使用。GitHub 沿用用户的网络代理设置，其他线路直连。下载中每 15 秒评估速度，下一线路测速快 50% 以上，且计入重下成本后预计节省至少 25% 剩余时间时换线；没有测速结果的备用线路保留持续 30 秒低于 8 KiB/s 且进度不足四分之一的换线规则。连接超时、20 秒无数据、文件大小或 SHA-256 不符时也会换线。SourceForge 保留为最后备用，不参与测速；版本查询仍依次尝试各线路。GitHub PAT 仅用于 GitHub API。

GitHub 发布成功后，独立的 `Sync update mirrors` 工作流同步完整包、增量包和校验文件，所有附件上传成功后再写入 `releases.json`。同步失败可单独重新运行此工作流。源码包和逐文件版本清单保留在 GitHub。

- SourceForge：创建项目并添加上传用 SSH 公钥，在配置中填写 `sourceforge_project`。仓库变量 `SOURCEFORGE_USERNAME` 保存用户名；密钥 `SOURCEFORGE_SSH_KEY` 保存私钥，`SOURCEFORGE_KNOWN_HOSTS` 保存核对过官方指纹的 `frs.sourceforge.net` 主机密钥。
首次启用 SourceForge 时，先用 `python scripts/sync_update_mirrors.py sourceforge --tag v<版本>` 同步当前版本，核对公开的 `releases.json` 与附件，再发布包含线路配置的新客户端。尚未开通时将 `sourceforge_project` 设为 `null`。历史附件可按需在托管端清理，保留当前版本目录和根目录的 `releases.json`。

发布失败时，附件不完整的 Release 会保持为草稿。可以在原构建运行中重新运行失败的发布任务，以同一提交恢复上传；也可在默认分支手动启动构建。已有标签或草稿属于其他提交时会停止发布，不移动标签或混用不同提交的附件，应重新运行原构建，或递增版本号发布新的提交。构建失败不会启动发布任务。

## 运行时依赖及模型

Python 3.12.10、MaaFramework SDK 5.14.2 和所有 Windows x64 Python wheels 的下载地址、版本和 SHA-256 在 `build/runtime.lock.json`。Python 依赖清单是 `requirements.txt`；`requirements.lock` 包含同一组版本及哈希。构建脚本直接展开经过校验的 wheels，嵌入式 Python 无需安装 pip。

OCR 模型及字典保留在源码 `resource/model/ocr/`，由 `build/resource-models.lock.json` 校验。修改模型时需要同时更新模型清单中的来源、版本和校验值。模型来自 MaaCommonAssets 的 PP-OCRv6 small，具体来源及许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

发布包保留 Python 和 wheels 自带的许可证、MaaFramework SDK 许可证、`licenses/` 和第三方说明，并收集已安装 JS 包和本次构建下载的 Rust crates 所附许可文件，写入 `licenses/dependencies/` 和清单。Windows WebView2 使用系统安装的运行时，缺失时由客户端引导下载。发布端需要具备相应 Microsoft Visual C++ 运行库；实际启动验证应在干净 Windows 环境完成。

Agent 导入与资源加载检查用于核对运行环境。程序启动、连接与任务效果由用户在游戏中验收。

## 开发与调试

页面入口、导航、截图调度、路线工具和库存字段统一见[开发文档](docs/architecture.md)。游戏内任务由用户运行验收。

## 源码公开范围

源码包含 `agent/`、`client/`、`resource/`、`tasks/`、`options/`、`interface.json`，以及依赖清单、构建脚本、工作流和许可说明。`python scripts/release.py check` 会输出 `.build/source-files.txt` 供核对。

此流程面向 Windows x64。本项目代码采用 AGPL-3.0-only，全文见根目录 `LICENSE`；上游许可原文保存在 `licenses/`。游戏素材和 OCR 模型的许可独立于代码许可证。
