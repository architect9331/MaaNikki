# MaaNikki 官网

官网地址：https://maanikki.cn，静态页面由 GitHub Pages 托管，DNS 由 Cloudflare 管理。

```powershell
npm ci --prefix website
npm run build --prefix website
python -m http.server 4173 --directory .build/website
```

构建输出在 `.build/website/`。`index.html`、`styles.css` 与 `app.js` 维护主页；用户手册从 `docs/manual.md` 生成，界面截图沿用 `docs/images/`，图标沿用 `resource/icon.png`。

推送网站、手册、截图或图标改动会自动部署。软件构建成功或发布版本后也会刷新下载快照；页面打开时尝试查询 GitHub 获取最新运行包，支持项目使用的预发布版本，失败时沿用快照或发行版列表入口。可手动运行 `Deploy website` 工作流重新部署。

域名使用 GitHub Pages 官方 A / AAAA 记录，`www` 指向 `architect9331.github.io`，保持 DNS only，由 GitHub Pages 提供 HTTPS。
