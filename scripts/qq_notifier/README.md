# QQ 更新公告

使用 NoneBot 和 QQ 官方机器人，在 GitHub Release 发布后自动发送版本号、完整更新日志和更新方式。更新日志取自 Release 正文，对应仓库的 `CHANGELOG.md`。内容较长时按行分条，单条过长再拆分，各条标注序号。

## 运行位置

- 程序：`/opt/nonebot-github/`
- 配置：`/etc/nonebot-github/config.json`
- 消息与订阅：`/var/lib/nonebot-github/state.sqlite3`
- systemd：`nonebot-github.service`
- GitHub Webhook：`https://arc9331.top/nonebot-github/github/webhook`

GitHub Webhook 订阅 `release` 事件，处理 `published`，包含预发布。现有提交、Issue、PR 和工作流通知继续保留。机器人直接接收 Webhook，GitHub Actions 使用 `GITHUB_TOKEN` 发布的版本也能触发公告。

## 配置与群绑定

依赖版本见 `requirements.lock`，配置字段见 `config.example.json`。实际密钥存放在服务器配置文件。`webhook_secret` 与 GitHub Webhook 的 Secret 一致。

将机器人加入 QQ 群 **654174255**，在机器人资料中开启主动消息，然后在群内发送：

```text
@机器人 绑定GitHub 绑定码
@机器人 GitHub状态
```

绑定码读取服务器配置的 `binding_code`。机器人只绑定一个群，QQ 官方使用 `group_openid` 标识群，群号用于人工核对。更换群时先在原群发送 `@机器人 取消GitHub 绑定码`，再到新群绑定。

默认公告提供客户端内检查更新的入口。QQ 开放平台配置好 GitHub 域名的 URL 权限后，可将 `show_links` 改为 `true`，在通知末尾附上页面链接。

## 消息处理

Webhook 使用 HMAC SHA-256 验签。公告先写入 SQLite 队列，每个 Release ID 只入队一次；重新投递 Webhook 也沿用该记录。分条公告逐条发送并保存进度，重启后从待发送条目继续。发送失败逐步延迟重试，每条最多十二次，群内 `GitHub状态` 可查看待发送和失败数量。发送成功后保留记录，已发布版本的说明修改不重复公告。

## 维护

更新程序前备份服务源码和数据库，将本目录中的 Python 文件同步到 `/opt/nonebot-github/`，再执行：

```sh
systemctl restart nonebot-github
systemctl status nonebot-github --no-pager
journalctl -u nonebot-github -n 30 --no-pager
```

首次部署时，将 `nonebot-github.service` 放到 `/etc/systemd/system/`，按示例创建受限配置，为服务用户 `nonebot-github` 安装虚拟环境依赖，并将 `nginx.conf` 作为 HTTPS 站点的 location 片段引入。执行 `nginx -t` 后加载配置，再启用服务与 GitHub Webhook。
