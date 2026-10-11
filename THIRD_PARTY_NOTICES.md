# 上游和第三方组件

本项目代码采用 AGPL-3.0-only，全文见根目录 `LICENSE`。第三方组件、图片和模型保留各自的权利与许可。本文件列出主要来源，不替代各组件的许可证或完整的依赖许可核查。

部分路线及匹配截图经适配后用于 MaaNikki，来源见 README 的“来源与致谢”。相关上游仓库采用 GPL-3.0，许可证见 `licenses/Routes-GPL-3.0.txt`。游戏截图与地图用于界面识别和路线定位，《无限暖暖》的名称及相关游戏素材权利归相应权利人。

桌面客户端基于 [MistEO/MXU](https://github.com/MistEO/MXU) v2.7.1，许可证见 `licenses/MXU-AGPL-3.0.txt`。分发修改后的客户端须遵守 AGPL-3.0 并提供对应源码。

项目使用 [MaaXYZ/MaaFramework v5.14.2](https://github.com/MaaXYZ/MaaFramework/tree/v5.14.2) 的预编译动态库和同版本 Python bindings，许可证见 `licenses/MaaFramework-LGPL-3.0.md`。SDK 所附 MaaAgentBinary 许可证保留在对应目录。

发布运行环境包含 CPython 3.12.10、maafw 5.14.2、MaaAgentBinary 1.0.1、NumPy 1.26.4、OpenCV-Python 4.10.0.84、SciPy 1.13.1 和 StrEnum 0.4.15。各组件的许可证保留在嵌入式 Python 目录、wheel 的 `.dist-info` 或库目录；下载地址和校验值见 `build/runtime.lock.json`。

`resource/model/ocr/` 中的模型和字典来自 [MaaCommonAssets 的 PP-OCRv6 small 模型](https://github.com/MaaXYZ/MaaCommonAssets/tree/dabcd4681ac990dc4361de26416d986abd80e4aa/OCR/ppocr_v6/small)。模型文件与该版本一致，字典仅转换为 Windows 换行符。MaaCommonAssets 采用 MIT 许可，模型源自 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)（Apache-2.0）；许可证分别见 `licenses/MaaCommonAssets-MIT.txt` 和 `licenses/PaddleOCR-Apache-2.0.txt`，来源、版本和校验值见 `build/resource-models.lock.json`。

JS 和 Rust 依赖的精确版本见 `client/pnpm-lock.yaml` 和 `client/src-tauri/Cargo.lock`。打包时收集依赖附带的许可证，保存至发布包的 `licenses/dependencies/`，清单见该目录的 `index.json`。

`resource/image/blessing/` 的祝福闪光识别图片来自 [The Infinity Nikki Wiki](https://infinitynikki.wiki/)，保留透明底原图，各条目的来源链接和校验值记录在该目录的 `index.json`；游戏素材权利归相应权利人。
