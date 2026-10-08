# Windows x64 构建

本文说明开发和发布包的构建流程。用户使用说明见 [README.md](README.md)。

## 构建环境

- Windows 10/11 x64。
- Node.js **24.13.0**，版本记录在 `.node-version`。
- Rust **1.99.0**，由 `rust-toolchain.toml` 固定。安装 Rust 时选择 MSVC 工具链。
- Visual Studio 2022 或 Build Tools，安装「使用 C++ 的桌面开发」及 Windows SDK。
- 构建主机安装 Python **3.11 或更高版本**；发布包使用独立的 Python **3.12.10**。
- PowerShell，以及可以访问 npm、crates.io、PyPI、python.org 和 GitHub 的网络。
- pnpm **10.28.0** 通过 `npx` 使用，无需修改全局 pnpm。JS 依赖使用冻结的 `pnpm-lock.yaml`，Rust 构建使用 `Cargo.lock` 和 `--locked`。

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

准备出的运行环境在 `.build/runtime-<随机编号>/`。下载缓存、构建目录和输出包都被 `.gitignore` 排除。脚本不会读取或打包现有 `config/`、`cache/`、根目录可执行文件和现有 Python/DLL；编译结果保留在 `client/src-tauri/target/release/mxu.exe`。

## 运行时依赖及模型

Python 3.12.10、MaaFramework SDK 5.14.2 和所有 Windows x64 Python wheels 的下载地址、版本和 SHA-256 在 `build/runtime.lock.json`。Python 依赖清单是 `requirements.txt`；`requirements.lock` 包含同一组版本及哈希。构建脚本直接展开经过校验的 wheels，嵌入式 Python 无需安装 pip。

OCR 模型及字典保留在源码 `resource/model/ocr/`，由 `build/resource-models.lock.json` 校验。修改模型时需要同时更新模型清单；模型来源和素材授权仍需在公开发布前核实。

发布包保留 Python 和 wheels 自带的许可证、MaaFramework SDK 许可证、`licenses/` 和第三方说明，并收集已安装 JS 包和本次构建下载的 Rust crates 所附许可文件，写入 `licenses/dependencies/` 和清单。这不替代对许可证条款的核查。Windows WebView2 使用系统安装的运行时，客户端已有缺失时的下载流程，个人 WebView2 缓存不进入发布包。发布端需要具备相应 Microsoft Visual C++ 运行库；实际启动验证应在干净 Windows 环境完成。

Agent 导入及资源加载检查不会连接游戏或执行自动化任务。它们不能代替实际启动、连接游戏和任务执行的人工验收，也不保证编译结果逐字节一致。

## 页面进入与回归检查

页面进入统一使用 `GameUI.enter_page`，页面等待统一使用 `GameUI.wait_page`。默认连续两次识别到局部页面特征才确认到达，不要求整个画面静止。确实需要图像稳定时，显式指定 `stable=True` 和局部 `roi`。

Pipeline 入口在 `custom_action_param` 中配置 `ready`、`source`，必要时配置以秒为单位的 `ready_timeout`。入口默认最多尝试三次（包含第一次）；目标页延迟出现时先等待，来源页丢失时通过已知入口路径恢复主页或上级页面，再重新识别并点击。没有来源页的操作只发送一次。点赞、领取、兑换、传送及追踪切换等操作不因页面等待失败而盲目重复。

Esc 菜单的挖掘、星绘图册、奇迹之冠入口共用 `find_menu_entry`，不使用列表滚轮。进入失败及恢复结果写入 `logs/navigation/*-page.json`；失败帧保存在同目录的 `*-page-attempt*.png`，截图去掉底部账号标识区域。检查新日志时应同时核对首次失败、恢复结果和最终到达状态。

离线回归运行 `python/python.exe -B scripts/test_clicks.py`，覆盖延迟到达、识别抖动、有限重试、主页恢复、停止输入和不重复点赞等情况。Agent 或页面 Pipeline 修改后，还须执行源码检查和运行环境验证，再单独记录游戏内验收结果。

客户端任务启停回归运行 `node scripts/test_task_restart.cjs`（需先安装 `client/` 的锁定依赖）。该检查模拟桌面和浏览器两种客户端，覆盖成功、失败、手动停止后再次启动，运行中拦截、重复启动及启动异常后的锁释放，不连接游戏。启动判断依据 MaaFramework 的实时运行状态；`pendingTaskIds` 保留整轮任务历史，不能用列表非空判断仍在运行。

## 截图、识别与性能回归

同一轮只读判断使用 `Runtime.observe()`，其中 `Runtime.frame()`、默认的识别和图像匹配复用一张截图。截图完成后最多复用 250 毫秒，慢识别使画面超过该时限时会重新截图；该作用域结束后不保留画面。输入、等待、移走鼠标或显式 `capture()` 都会使作用域中的画面失效。`capture()` 始终请求新截图，`wait_page()` 的两次确认分别使用新截图，不能把同一张图计为两次到达。作用域只用于连续、独立的特征判断，不应跨越导航、手动控制或直接调用控制器的操作。

模板统一由 `agent/vision.py` 准备；解码和预处理缓存分别限制为 64、128 项。缓存区分路径、文件修改时间与大小、缩放、亮度、颜色及灰度解码方式，缓存像素只读；模板替换、参数调整或删除后不沿用原模板。图册搜索将滚动后已经截取的画面直接用于下一轮识别，不额外截取相同状态。

开荒辅助以实际经过时间安排检查，不按循环次数调度；一轮只提交一个输入，下一轮重新截图。无匹配时拾取、跳过动画、对话分别间隔 100、300、500 毫秒检查；成功拾取、对话后分别保留至少 20、250 毫秒冷却。跳过动画后每 300 毫秒检查特征是否消失，消失前不重复发送。连续拾取不会独占循环，前台检查、停止检查和输入释放仍然生效。间隔是检测完成后的调度目标，实际频率还取决于截图与识别耗时。

自定义动作结束时将耗时统计写入 `logs/performance/*.json`；持续开荒辅助每 60 秒更新同一个报告。报告记录已接入统计的 Python 路径中的截图、识别、各资产的模板匹配、输入及等待耗时，以及截图复用、失败和模板缓存计数；不保存截图或文字内容。各阶段可能包含彼此的耗时，不能直接相加；`p95_upper_ms` 是固定直方图估计的上界。原生 Pipeline 内部和其他线程回调的每个阶段不会自动单独计时。记录使用固定数量的桶，不保存不断增长的逐帧样本，日志写入失败不改变任务结果。

运行 `python/python.exe -B scripts/test_performance.py` 检查截图失效、同帧复用、独立到达确认、搜索截图复用、模板更新、缓存大小、辅助调度公平性、操作冷却、失去焦点及停止保护。运行 `python/python.exe -B scripts/benchmark_performance.py --output .build/recognition-benchmark.json` 可对真实模板及正常 ROI 下的可复现合成场景做离线耗时对照，包含正、负样本；使用 `--baseline-ui <保存的旧版 daily/ui.py>` 对照同一组场景。该基准不连接游戏，其结果不代表整项游戏任务提速比例；实际首次成功率和端到端耗时须另做游戏验收。

每日积分与领奖沿用性能优化前的流程：先确认局部页面特征，积分最多读取三次，每次之间间隔 0.4 秒，连续两次相同才采用；没有固定动画等待，也不按预计积分延长等待。领奖直接识别可领取按钮，提交后必须确认奖励弹层和返回每日页面。

星光结晶在摇铃前准备地图区域和最大缩放；摇铃后限时打开地图、最多进行三次大幅移动，不等待整屏静止。必须在同一张截图中取得三个结晶标记和可信地图位置，再把标记偏移换算为路线坐标；右侧永久星形图标不参与匹配。相邻岛屿位置不明确时重新摇铃，最多检查三个已登记视野，不放宽路线距离阈值。定位事件写入 `logs/navigation/*-starsea.json`。

运行 `python/python.exe -B scripts/test_gameplay.py` 检查积分两次一致、三次读取上限、停止保护、结晶同帧换算与地图回中、相邻路线歧义、摆饰放置确认和巅峰赛图标 OCR。再次挖掘直接确认返回挖掘页，仅“收取”分支检查领奖弹窗。摆饰以放置操作提示确认进入模式；明确无法放置时才更换角度重试，提交后结果不明不会再次放置，失败截图保存到 `logs/daily/*-place-*.png`。这些回归不操作游戏，地图拖动时长、摆饰提示区域及每日动画时间仍须游戏内验收。

## 源码公开范围

源码包含 `agent/`、`client/`、`resource/`、`tasks/`、`options/`、`interface.json`，以及依赖清单、构建脚本、工作流和许可说明。`python scripts/release.py check` 会输出 `.build/source-files.txt` 供核对。

此流程支持 Windows x64，尚未声明支持 Linux、macOS 或 Windows ARM64。本项目代码采用 AGPL-3.0-only，全文见根目录 `LICENSE`；上游许可原文保存在 `licenses/`。游戏素材和 OCR 模型的许可独立于代码许可证。
