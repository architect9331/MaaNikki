# 上游和第三方组件

部分路线及匹配截图来自 [nikkigallery/Whimbox（奇想盒）](https://github.com/nikkigallery/Whimbox)，在 MaaNikki 中进行了适配。上游仓库使用 GPL-3.0，许可证原文保留在 `licenses/Whimbox-GPL-3.0.txt`；原始项目和素材的权利及许可仍归相应来源。

MaaNikki 桌面客户端基于 [MistEO/MXU](https://github.com/MistEO/MXU)，目前客户端版本标注为 `2.7.1-maanikki.4`。MXU v2.7.1 的许可证原文保留在 `licenses/MXU-AGPL-3.0.txt`。发布修改后的客户端时须满足该版本 AGPL-3.0 的要求并提供对应源码。本项目代码采用 AGPL-3.0-only，全文见根目录 `LICENSE`；本文件不替代第三方各自的许可，也不声明所有图片和模型适用 AGPL。

项目使用 [MaaXYZ/MaaFramework](https://github.com/MaaXYZ/MaaFramework) v5.14.2。许可证原文保留在 `licenses/MaaFramework-LGPL-3.0.md`。发布包使用上游预编译动态库和同版本 Python bindings；上游源码在 [v5.14.2](https://github.com/MaaXYZ/MaaFramework/tree/v5.14.2)。SDK 所附 MaaAgentBinary 许可证也保留在对应目录。

发布运行环境包含 CPython 3.12.10、maafw 5.14.2、MaaAgentBinary 1.0.1、NumPy 1.26.4、OpenCV-Python 4.10.0.84、SciPy 1.13.1 和 StrEnum 0.4.15。各组件的许可原文保留在嵌入式 Python 目录以及 wheel 的 `.dist-info` 或库目录。运行时下载地址及校验值记录在 `build/runtime.lock.json`。

JS 和 Rust 依赖的精确版本见 `client/pnpm-lock.yaml` 和 `client/src-tauri/Cargo.lock`。打包时收集本次已安装/下载的依赖所附许可文件，并保存到发布包的 `licenses/dependencies/`，清单见该目录的 `index.json`。本文件列出主要组件，不替代完整的依赖许可核查。

《无限暖暖》的名称、游戏截图、地图及其他素材的权利归相应权利人。保留在 `resource/` 中的图片和 OCR 模型须单独核实来源及可分发条件。
