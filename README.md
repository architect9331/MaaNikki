# MaaNikki

《无限暖暖》PC 自动化助手，支持 Windows 10/11 x64。

**这是一个自用项目**，功能和路线持续调整。

## 功能

| 类型 | 内容 |
| --- | --- |
| 日常任务 | 美鸭梨挖掘、朝夕心愿、星海拾光、家园日常、奇迹之旅领奖 |
| 幻境挑战 | 每周幻境、素材激化、祝福闪光、魔物试炼 |
| 祝福闪光 | 祝福闪光整理、仅升级五星、一键全收集 |
| 商城与巅峰赛 | 每周免费礼包、每月五抽兑换、推荐搭配挑战及可选领奖兑换 |
| 开荒与钓鱼 | 自动拾取、对白和清洁辅助、当前点钓鱼、路线钓鱼 |
| 通用功能 | 启动游戏、多配置、任务预设、键位设置、定时执行、日志与失败恢复 |

## 快速开始

1. 从 [Releases](https://github.com/architect9331/MaaNikki/releases) 下载 Windows x64 运行包，完整解压后启动 `MaaNikki.exe`。
2. 使用 16:9 游戏窗口，推荐 1920×1080；核对按键设置，提前解锁任务所需地图、能力和关卡。
3. 选择任务或“日常”预设，朝夕心愿排在幻境挑战之前。可配置官方启动器路径，启动游戏任务会优先执行。
4. 自动路线运行时保持游戏在前台；定时任务需保持 MaaNikki 运行。开荒辅助单独运行，手动停止。

客户端支持从 GitHub 检查和安装更新。源码包不含运行环境。

## 使用须知

- 朝夕心愿和星海拾光仅执行已适配项目，具体范围见[任务说明](docs/usage.md)。
- 聚会聊天、巅峰赛兑换等操作需按需开启；每月五抽兑换会消耗静谧水滴。
- 祝福闪光请先清除筛选并完整运行一次“祝福闪光整理”，建立获得时间基准后再使用五星全收集。五星升级使用**五星及以下未锁定素材**，包括其他五星胚子；已有锁定保留，月桂兔不计入追全目标。
- “仅升级整理”不消耗体力，缺材料或噗灵时保存退出；全收集模式会自动安排幻境补给。

详细说明：[使用与任务范围](docs/usage.md) · [祝福闪光与库存 JSON](docs/blessing.md) · [开发与构建](BUILDING.md)

## 来源与致谢

- [Whimbox（奇想盒）](https://github.com/nikkigallery/Whimbox)：部分路线及匹配截图来自该项目。
- [MaaFramework](https://github.com/MaaXYZ/MaaFramework)：自动化框架。
- [MXU](https://github.com/MistEO/MXU)：桌面客户端基于该项目修改。
- [MaaCommonAssets](https://github.com/MaaXYZ/MaaCommonAssets) / [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)：OCR 模型。

本项目代码采用 **AGPL-3.0-only**，第三方组件及素材保留各自的许可与来源说明，详见 [LICENSE](LICENSE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [licenses/](licenses/)。本项目与游戏官方无关联。
