# QQ 更新公告与群助手

使用 Cloudflare Workers、D1 和 QQ 官方机器人，在 GitHub Release 发布后自动发送版本号、完整更新日志和更新方式，同时提供《无限暖暖》活动提醒、官方兑换码推送以及群聊智能答疑服务。

## 功能概览

1. **GitHub 仓库通知**：订阅 `release`（`published`，含预发布）、提交、Issue、PR 及工作流异常告警，Release 长公告自动分条编号发送。
2. **《无限暖暖》活动与兑换码推送**：
   - 定时同步 SRA 公共 API（`https://starrailassistant.top/api/v1/activity/infinitynikki.json`），在新版本或新活动开启时推送通知，并在活动结束前 72 小时 / 24 小时（北京时间 09:00–22:00 之间）发出到期提醒。
   - 双源监测《无限暖暖》B站百科兑换码合集与微博兑换码转存频道（`infinity_nikki_weibo_code`），自动过滤推广贴并去重合并后在群内推送新兑换码及游戏内兑换路径。
   - 首次上线时静默建立历史基线，避免对过往活动和旧兑换码刷屏。
3. **群聊智能答疑与快捷指令（需 `@机器人`）**：
   - `活动`：查看无限暖暖当前版本与限时活动剩余时间
   - `兑换码`：查看近期收录的福利兑换码与兑换方式
   - `帮助` / `迎新` / `安静 [分钟]` / `说话` / `重置`：群助手管理与答疑指令

## 运行位置

- Worker：`maanikki-qq-notifier`，源码位于 `worker/`
- 消息与订阅：D1 数据库 `maanikki-qq-notifier`
- GitHub Webhook：`https://qqbot.maanikki.cn/github/webhook`
- QQ 事件回调：`https://qqbot.maanikki.cn/qq/webhook`

## 配置与群绑定

依赖版本见 `worker/package-lock.json`，配置字段见 `worker/cloudflare.config.ts`。QQ 凭据、GitHub Webhook Secret、绑定码、管理令牌及 LLM 接口配置通过 Worker Secrets 配置。

将机器人加入 QQ 群 **654174255**，在机器人资料中开启主动消息，然后在群内发送：

```text
@机器人 绑定GitHub 绑定码
@机器人 GitHub状态
```

绑定码使用 Worker 的 `BINDING_CODE`。机器人只绑定一个群，QQ 官方使用 `group_openid` 标识群，群号用于人工核对。更换群时先在原群发送 `@机器人 取消GitHub 绑定码`，再到新群绑定。

默认公告提供客户端内检查更新的入口。QQ 开放平台配置好对应域名的 URL 权限后，可将 `SHOW_LINKS` 改为 `true`。

## 消息处理

GitHub Webhook 使用 HMAC SHA-256 验签，QQ 回调使用 Ed25519 验签。通知先写入 D1 队列，每个事件键只入队一次；分条公告逐条发送并保存进度。触发到达时立即处理队列，每分钟的定时任务负责外源轮询（每 10 分钟检查一次，直播发码期间自动短延迟聚合）与失败重试（每条最多十二次）。

## 维护

### Cloudflare 部署

在 `worker/` 下安装锁定依赖，确保 `worker/schema.sql` 已同步到 D1 后，使用 `cf deploy --secrets-file <私密配置文件>` 部署。密钥字段见 `cloudflare.config.ts`；日常发送由 `DELIVERY_ENABLED` 控制。

从旧版导入时，用 SQLite 在线备份运行 `worker/import_state.py <备份文件> <新建输出目录>`，将输出的 JSON 按文件名顺序通过 `cf d1 query <数据库ID> --batch @<文件>` 导入。订阅和已发进度可重复导入；群标识和消息批次放在忽略目录。

`GET /health` 查看服务状态；携带管理令牌访问 `GET /admin/status`，可查看群绑定数量、消息队列、外部活动/兑换码同步状态和 QQ 接口连通状态。

### 旧版回退

旧部署的地址、服务配置和备份位置由维护者保存在私密部署记录中。需要回退时，先关闭 Worker 的 `DELIVERY_ENABLED`，保留当前订阅与发送进度的备份，再按部署记录恢复旧服务，并将 GitHub Webhook 与 QQ 回调切换到对应地址。确认接收正常后再恢复发送，避免重复公告。

旧版依赖和配置示例见 `requirements.lock`、`config.example.json`。
