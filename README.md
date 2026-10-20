# MaaNikki

《无限暖暖》PC 自动化助手，支持 Windows 10/11 x64。

## 功能

| 功能 | 内容 |
| --- | --- |
| **祝福闪光** | **一键全收集五星满级颜色**：按缺色选本、刷取升级、锁定新颜色、保存进度；支持仓库整理与仅升级 |
| 日常任务 | 美鸭梨挖掘、朝夕心愿、星海拾光、家园日常、奇迹之旅领奖 |
| 幻境挑战 | 每周幻境、素材激化、祝福闪光、魔物试炼 |
| 商城与巅峰赛 | 免费礼包、每月五抽兑换、巅峰赛搭配挑战与领奖兑换 |
| 开荒辅助 | 自动拾取、对白跳过、动物清洁 |
| 自动钓鱼 | 当前点钓鱼、路线钓鱼 |
| 运行管理 | 启动游戏、多配置、任务预设、账号周期状态、键位设置、定时执行、日志与失败恢复 |

全收集首次需完整运行“一键整理祝福闪光”，再开启「幻境挑战 → 五星祝福闪光全收集」。缺噗灵或材料时保存退出，下次继续。右上角“收集进度”可查看已收集颜色。[查看使用说明](docs/blessing.md)

## 快速开始

1. 从 [Releases](https://github.com/architect9331/MaaNikki/releases) 下载 Windows x64 运行包，完整解压后启动 `MaaNikki.exe`。
2. 使用 16:9 游戏窗口，推荐 1920×1080；核对按键设置，提前解锁任务所需地图、能力和关卡。
3. 默认提供“全套日常”“开荒辅助”“祝福闪光全收集”三个页签，朝夕心愿排在幻境挑战之前。可配置官方启动器路径，启动游戏任务会优先执行。
4. 自动路线运行时保持游戏在前台；定时任务需保持 MaaNikki 运行。开荒辅助单独运行，手动停止。

客户端支持从 GitHub 检查和安装更新。源码包不含运行环境。

## 使用须知

- 祝福闪光升级使用**五星及以下未锁定素材，包括其他五星胚子**；全收集刷取后会分解四星及以下未锁定闪光，已有锁定保留。
- 朝夕心愿和星海拾光仅执行已适配项目，具体范围见[任务说明](docs/usage.md)。
- 星海子任务默认全选，可按需关闭聚会聊天等项目；巅峰赛兑换默认关闭，每月五抽兑换会消耗静谧水滴。

详细说明：[使用与任务范围](docs/usage.md) · [祝福闪光与库存 JSON](docs/blessing.md) · [更新日志](CHANGELOG.md) · [开发与构建](BUILDING.md)

## 来源与致谢

- [Whimbox（奇想盒）](https://github.com/nikkigallery/Whimbox)：部分路线及匹配截图来自该项目。
- [MaaFramework](https://github.com/MaaXYZ/MaaFramework)：自动化框架。
- [MXU](https://github.com/MistEO/MXU)：桌面客户端基于该项目修改。
- [MaaCommonAssets](https://github.com/MaaXYZ/MaaCommonAssets) / [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)：OCR 模型。

本项目代码采用 **AGPL-3.0-only**，第三方组件及素材保留各自的许可与来源说明，详见 [LICENSE](LICENSE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [licenses/](licenses/)。本项目与游戏官方无关联。
