# QQ 更新公告

使用 Cloudflare Workers、D1 和 QQ 官方机器人，在 GitHub Release 发布后自动发送版本号、完整更新日志和更新方式。更新日志取自 Release 正文，对应仓库的 `CHANGELOG.md`。内容较长时按行分条，单条过长再拆分，各条标注序号。

## 运行位置

- Worker：`maanikki-qq-notifier`，源码位于 `worker/`
- 消息与订阅：D1 数据库 `maanikki-qq-notifier`
- GitHub Webhook：`https://qqbot.maanikki.cn/github/webhook`
- QQ 事件回调：`https://qqbot.maanikki.cn/qq/webhook`

GitHub Webhook 订阅 `release` 事件，处理 `published`，包含预发布。提交、Issue、PR 通知继续保留；工作流仅推送失败、超时和需要处理的异常，版本发布统一发送更新公告。机器人直接接收 Webhook，GitHub Actions 使用 `GITHUB_TOKEN` 发布的版本也能触发公告。

## 配置与群绑定

依赖版本见 `worker/package-lock.json`，配置字段见 `worker/cloudflare.config.ts`。QQ 凭据、GitHub Webhook Secret、绑定码和管理令牌通过 Worker Secrets 配置。

将机器人加入 QQ 群 **654174255**，在机器人资料中开启主动消息，然后在群内发送：

```text
@机器人 绑定GitHub 绑定码
@机器人 GitHub状态
```

绑定码使用 Worker 的 `BINDING_CODE`。机器人只绑定一个群，QQ 官方使用 `group_openid` 标识群，群号用于人工核对。更换群时先在原群发送 `@机器人 取消GitHub 绑定码`，再到新群绑定。

默认公告提供客户端内检查更新的入口。QQ 开放平台配置好 GitHub 域名的 URL 权限后，可将 `SHOW_LINKS` 改为 `true`，在通知末尾附上页面链接。

## 消息处理

GitHub Webhook 使用 HMAC SHA-256 验签，QQ 回调使用 Ed25519 验签。公告先写入 D1 队列，每个 Release ID 只入队一次；重新投递 Webhook 也沿用该记录。分条公告逐条发送并保存进度，后续执行从待发送条目继续。Webhook 到达时立即处理队列，每分钟的定时任务接续重试。发送失败逐步延迟重试，每条最多十二次，群内 `GitHub状态` 可查看待发送和失败数量。发送成功后保留记录，已发布版本的说明修改不重复公告。

## 维护

### Cloudflare 部署

在 `worker/` 下安装锁定依赖，使用 `cf deploy --secrets-file <私密配置文件>` 部署。密钥字段见 `cloudflare.config.ts`；日常发送由 `DELIVERY_ENABLED` 控制。

从旧版导入时，用 SQLite 在线备份运行 `worker/import_state.py <备份文件> <新建输出目录>`，将输出的 JSON 按文件名顺序通过 `cf d1 query <数据库ID> --batch @<文件>` 导入。订阅和已发进度可重复导入；群标识和消息批次放在忽略目录。

`GET /health` 查看服务状态；携带管理令牌访问 `GET /admin/status`，可查看群绑定数量、消息队列和 QQ 接口连通状态。

### 旧服务器备份与回退

2026-10-31 迁移至 Cloudflare，沿用原群绑定和发送进度。旧服务 `nonebot-github.service` 已停用并关闭自动启动，程序保留在 `/opt/nonebot-github/`，配置位于 `/etc/nonebot-github/config.json`，数据库位于 `/var/lib/nonebot-github/state.sqlite3`。源码和最终数据库备份位于 `/var/backups/nonebot-github/20261031-cf-migration/`。

需要回退时，先关闭 Worker 的 `DELIVERY_ENABLED`，将 GitHub Webhook 改回 `https://arc9331.top/nonebot-github/github/webhook`，再恢复旧服务：

```sh
systemctl enable --now nonebot-github
systemctl status nonebot-github --no-pager
journalctl -u nonebot-github -n 30 --no-pager
```

QQ 回调配置同时按所选运行端调整。旧版依赖和配置示例见 `requirements.lock`、`config.example.json`。
