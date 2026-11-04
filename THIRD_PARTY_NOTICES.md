# 上游和第三方组件

地图标记参考 [Whimbox](https://github.com/nikkigallery/Whimbox/tree/2a75df818c28e9f4af2b3e81c0db12d7694a20d1) 的美鸭梨接口、坐标转换和 Snappy 解码实现（GPL-3.0，许可证见 licenses/Routes-GPL-3.0.txt）。透明遮罩和 Tauri 登录界面在本项目中实现。

共鸣记录接口和导出格式参考 [Gongeo Infinity Nikki Tracker](https://github.com/dastrokes/gongeo.us-nikki-tracker)。内置卡池目录、中文物品名及星级摘取自该项目，更新于 2026-10-31；采用 MIT 许可，全文见 `licenses/Gongeo-MIT.txt`。

衣柜读取与祝福闪光整理在全量扫描阶段使用的无边框超长竖屏（`1440 × 5760`）窗口拉伸与恢复思路参考 [CountingMomo](https://github.com/ChanIok/CountingMomo)（作者 ChanIok），具体窗口管理、坐标归一化及卡片识别逻辑由本项目独立实现。

本项目代码采用 AGPL-3.0-only，全文见根目录 `LICENSE`。第三方组件、图片和模型保留各自的权利与许可。本文件列出主要来源，不替代各组件的许可证或完整的依赖许可核查。

部分路线及匹配截图经适配后用于 MaaNikki，来源见 README 的“来源与致谢”。相关上游仓库采用 GPL-3.0，许可证见 `licenses/Routes-GPL-3.0.txt`。游戏截图与地图用于界面识别和路线定位，《无限暖暖》的名称及相关游戏素材权利归相应权利人。

桌面客户端基于 [MistEO/MXU](https://github.com/MistEO/MXU) v2.7.1，许可证见 `licenses/MXU-AGPL-3.0.txt`。分发修改后的客户端须遵守 AGPL-3.0 并提供对应源码。

项目使用 [MaaXYZ/MaaFramework v5.14.2](https://github.com/MaaXYZ/MaaFramework/tree/v5.14.2) 的预编译动态库和同版本 Python bindings，许可证见 `licenses/MaaFramework-LGPL-3.0.md`。SDK 所附 MaaAgentBinary 许可证保留在对应目录。

发布运行环境包含 CPython 3.12.10、maafw 5.14.2、MaaAgentBinary 1.0.1、NumPy 1.26.4、OpenCV-Python 4.10.0.84、SciPy 1.13.1 和 StrEnum 0.4.15。各组件的许可证保留在嵌入式 Python 目录、wheel 的 `.dist-info` 或库目录；下载地址和校验值见 `build/runtime.lock.json`。

衣柜识别使用 ONNX Runtime 1.20.1（MIT）运行模型，使用 Pillow 10.4.0（HPND）预处理图片；相关传递依赖与许可证随运行包收集，精确版本及校验值见同一运行环境清单。

`resource/model/ocr/` 中的模型和字典来自 [MaaCommonAssets 的 PP-OCRv6 small 模型](https://github.com/MaaXYZ/MaaCommonAssets/tree/dabcd4681ac990dc4361de26416d986abd80e4aa/OCR/ppocr_v6/small)。模型文件与该版本一致，字典仅转换为 Windows 换行符。MaaCommonAssets 采用 MIT 许可，模型源自 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)（Apache-2.0）；许可证分别见 `licenses/MaaCommonAssets-MIT.txt` 和 `licenses/PaddleOCR-Apache-2.0.txt`，来源、版本和校验值见 `build/resource-models.lock.json`。

JS 和 Rust 依赖的精确版本见 `client/pnpm-lock.yaml` 和 `client/src-tauri/Cargo.lock`。打包时收集依赖附带的许可证，保存至发布包的 `licenses/dependencies/`，清单见该目录的 `index.json`。

`resource/image/blessing/` 的祝福闪光识别图片来自 [The Infinity Nikki Wiki](https://infinitynikki.wiki/)，保留透明底原图，各条目的来源链接和校验值记录在该目录的 `index.json`；游戏素材权利归相应权利人。

祝福闪光种类与颜色模型基于 [TorchVision](https://github.com/pytorch/vision) 的 MobileNetV3 Small 预训练权重进行迁移训练，使用游戏图标、界面合成图及本地识别截图。TorchVision 采用 BSD-3-Clause 许可，全文见 `licenses/TorchVision-BSD-3-Clause.txt`。等级与角标模型由本项目训练，模型和标签的校验值记录在 `build/resource-models.lock.json`。

衣柜种类模型基于 TorchVision 的 ConvNeXt Tiny 预训练权重训练，采用共享骨干与部位分类头。训练使用 NikkiModel 整理的游戏图标与界面合成素材，另有少量解包缺图的类别使用 [BWIKI](https://wiki.biligame.com/wxnn) 公开图标，素材不随仓库分发，游戏素材权利归相应权利人；部署文件及标签校验值见 `build/resource-models.lock.json`，模型目录内保留部位目录、预处理约定与素材覆盖名单。

搭配计算的服装属性、成长曲线、套装及关卡资料参考[无暖助手](https://nuan5.pro/score-calculate)，按本地游戏物品目录中的名称、部位和明确的变色对应关系建立映射。评分逻辑由本项目独立实现，参考该站公开的属性、标签、加成和取整规则。资料日期、来源链接及参考脚本的 SHA-256 记录于 `resource/data/wardrobe_scoring.json`；游戏数据权利归相应权利人。
