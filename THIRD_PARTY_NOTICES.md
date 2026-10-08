# 上游和第三方组件

部分路线及匹配截图来自 [nikkigallery/Whimbox（奇想盒）](https://github.com/nikkigallery/Whimbox)，在 MaaNikki 中进行了适配。上游仓库使用 GPL-3.0，许可证原文保留在 `licenses/Whimbox-GPL-3.0.txt`；原始项目和素材的权利及许可仍归相应来源。

MaaNikki 桌面客户端基于 [MistEO/MXU](https://github.com/MistEO/MXU)，目前客户端版本标注为 `2.7.1-maanikki.4`。MXU v2.7.1 的许可证原文保留在 `licenses/MXU-AGPL-3.0.txt`。发布修改后的客户端时须满足该版本 AGPL-3.0 的要求并提供对应源码。本项目代码采用 AGPL-3.0-only，全文见根目录 `LICENSE`；本文件不替代第三方各自的许可，也不声明所有图片和模型适用 AGPL。

项目使用 [MaaXYZ/MaaFramework](https://github.com/MaaXYZ/MaaFramework) v5.14.2。许可证原文保留在 `licenses/MaaFramework-LGPL-3.0.md`。发布包使用上游预编译动态库和同版本 Python bindings；上游源码在 [v5.14.2](https://github.com/MaaXYZ/MaaFramework/tree/v5.14.2)。SDK 所附 MaaAgentBinary 许可证也保留在对应目录。

发布运行环境包含 CPython 3.12.10、maafw 5.14.2、MaaAgentBinary 1.0.1、NumPy 1.26.4、OpenCV-Python 4.10.0.84、SciPy 1.13.1 和 StrEnum 0.4.15。各组件的许可原文保留在嵌入式 Python 目录以及 wheel 的 `.dist-info` 或库目录。运行时下载地址及校验值记录在 `build/runtime.lock.json`。

JS 和 Rust 依赖的精确版本见 `client/pnpm-lock.yaml` 和 `client/src-tauri/Cargo.lock`。打包时收集本次已安装/下载的依赖所附许可文件，并保存到发布包的 `licenses/dependencies/`，清单见该目录的 `index.json`。本文件列出主要组件，不替代完整的依赖许可核查。

`resource/model/ocr/` 下的检测模型、识别模型及字典来自 [MaaCommonAssets 的 PP-OCRv6 small 模型](https://github.com/MaaXYZ/MaaCommonAssets/tree/dabcd4681ac990dc4361de26416d986abd80e4aa/OCR/ppocr_v6/small)，检测和识别模型与该上游版本逐字节一致；字典内容一致，仅使用 Windows 换行符。MaaCommonAssets 仓库采用 MIT 许可，模型来源为 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)（Apache-2.0）。许可原文分别保留在 `licenses/MaaCommonAssets-MIT.txt` 和 `licenses/PaddleOCR-Apache-2.0.txt`；模型来源、上游版本和文件校验值记录在 `build/resource-models.lock.json`。

游戏截图与地图用于界面识别和路线定位。《无限暖暖》的名称及相关游戏素材权利归相应权利人，部分路线与匹配截图的项目来源见上述 Whimbox 说明。
