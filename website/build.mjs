import {
  mkdir,
  readFile,
  writeFile,
  copyFile,
  readdir,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { marked } from "marked";

const directory = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(directory, "..");
const output = path.join(root, ".build", "website");
const repo = "https://github.com/architect9331/MaaNikki";
const escape = (value) =>
  String(value).replace(
    /[&<>"]/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[character],
  );
await mkdir(path.join(output, "assets"), { recursive: true });
await mkdir(path.join(output, "manual"), { recursive: true });

const icons = `<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
<symbol id="icon-download" viewBox="0 0 24 24"><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/></symbol>
<symbol id="icon-arrow" viewBox="0 0 24 24"><path d="M4 12h16m-6-6 6 6-6 6"/></symbol>
<symbol id="icon-star" viewBox="0 0 24 24"><path d="m12 3 2.8 5.7 6.3.9-4.6 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L2.9 9.6l6.3-.9Z"/></symbol>
<symbol id="icon-theme" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></symbol>
<symbol id="icon-github" viewBox="0 0 24 24"><path d="M8 20v-3c-4 1-4-2-5-2m13 5v-3a4 4 0 0 0-1-3c3-.4 5-1.5 5-5a4 4 0 0 0-1-3 4 4 0 0 0 0-3c-2 0-3 1-3 1a11 11 0 0 0-8 0s-1-1-3-1a4 4 0 0 0 0 3 4 4 0 0 0-1 3c0 3.5 2 4.6 5 5a4 4 0 0 0-1 3"/></symbol>
<symbol id="icon-sparkles" viewBox="0 0 24 24"><path d="m13 3 2.5 6.5L22 12l-6.5 2.5L13 21l-2.5-6.5L4 12l6.5-2.5ZM4 3v4M2 5h4M4 18v4m-2-2h4"/></symbol>
<symbol id="icon-calendar" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18m-14 5 3 3 6-6"/></symbol>
<symbol id="icon-compass" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m16 8-2.5 5.5L8 16l2.5-5.5Z"/></symbol>
<symbol id="icon-gem" viewBox="0 0 24 24"><path d="m3 8 4-5h10l4 5-9 13ZM3 8h18M8 3l4 18 4-18"/></symbol>
<symbol id="icon-route" viewBox="0 0 24 24"><circle cx="6" cy="5" r="2"/><circle cx="18" cy="19" r="2"/><path d="M8 5h8a4 4 0 0 1 0 8H8a3 3 0 0 0 0 6h8"/></symbol>
<symbol id="icon-book" viewBox="0 0 24 24"><path d="M12 5C9 3 5 3 2 4v16c3-1 7-1 10 1 3-2 7-2 10-1V4c-3-1-7-1-10 1Zm0 0v16M6 8h2m-2 4h2m8-4h2m-2 4h2"/></symbol>
</svg>`;
const header = (manual = false) =>
  `<a class="skip-link" href="#main">跳到正文</a><header class="site-header"><div class="header-inner"><a class="brand" href="/" aria-label="MaaNikki 首页"><img src="/assets/icon.png" alt="" width="39" height="39"><span>MaaNikki</span></a><nav class="site-nav" aria-label="主导航"><a class="nav-features" href="/#features">功能</a><a href="/manual/"${manual ? ' aria-current="page"' : ""}>用户手册</a><a class="nav-community" href="/#community">交流</a><a class="nav-github" href="${repo}" target="_blank" rel="noopener noreferrer" aria-label="GitHub 仓库"><svg aria-hidden="true"><use href="#icon-github"></use></svg><span>GitHub</span></a><button class="theme-toggle" type="button" data-theme-toggle aria-label="切换深色主题" aria-pressed="false"><svg aria-hidden="true"><use href="#icon-theme"></use></svg></button></nav></div></header>`;
const footer = `<footer class="site-footer"><div class="footer-inner section-width"><div><a class="footer-brand" href="/">MaaNikki</a><p>Powered by <a href="https://github.com/MaaXYZ/MaaFramework">MaaFramework</a> &amp; <a href="https://github.com/MistEO/MXU">MXU</a></p></div><nav class="footer-links" aria-label="页脚导航"><a href="${repo}">项目源码</a><a href="${repo}/releases">更新日志</a><a href="${repo}/issues">问题反馈</a><a href="https://maa.plus/">MAA</a><a href="https://maaend.com/">MaaEnd</a></nav></div><div class="footer-note section-width"><span>由社区共同维护 · AGPL-3.0-only</span><span>《无限暖暖》游戏相关内容与商标归其权利方所有。</span></div></footer>`;
let homepage = (await readFile(path.join(directory, "index.html"), "utf8"))
  .replace("<!-- ICONS -->", icons)
  .replace("<!-- HEADER -->", header())
  .replace("<!-- FOOTER -->", footer);
let snapshot = null;
try {
  const headers = {
    Accept: "application/vnd.github+json",
    "User-Agent": "MaaNikki-website",
  };
  if (process.env.GH_TOKEN)
    headers.Authorization = `Bearer ${process.env.GH_TOKEN}`;
  const response = await fetch(
    "https://api.github.com/repos/architect9331/MaaNikki/releases?per_page=10",
    { headers, signal: AbortSignal.timeout(15000) },
  );
  if (!response.ok) throw new Error(`GitHub returned ${response.status}`);
  const releases = await response.json();
  const release = releases.find(
    (item) =>
      !item.draft &&
      item.assets.some((asset) =>
        /^MaaNikki-v.+-win-x86_64\.zip$/.test(asset.name),
      ),
  );
  if (release) {
    const asset = release.assets.find((item) =>
      /^MaaNikki-v.+-win-x86_64\.zip$/.test(item.name),
    );
    snapshot = {
      tag_name: release.tag_name,
      published_at: release.published_at,
      assets: [
        {
          name: asset.name,
          size: asset.size,
          browser_download_url: asset.browser_download_url,
        },
      ],
    };
    homepage = homepage
      .replace(
        /(class="button primary download-link"\s+href=")[^"]*"/,
        (_, prefix) => `${prefix}${escape(asset.browser_download_url)}"`,
      )
      .replace(
        "data-release-version>Windows 10 / 11 · x64",
        `data-release-version>${escape(release.tag_name)} · Windows 10 / 11 · x64 · ${Math.round(asset.size / 1024 / 1024)} MB`,
      );
  }
} catch (error) {
  console.warn(
    `Release snapshot unavailable; keeping release-list link: ${error.message}`,
  );
}
await writeFile(
  path.join(output, "release.json"),
  `${JSON.stringify(snapshot, null, 2)}\n`,
);
await writeFile(path.join(output, "index.html"), homepage);
await copyFile(
  path.join(directory, "styles.css"),
  path.join(output, "styles.css"),
);
await copyFile(path.join(directory, "app.js"), path.join(output, "app.js"));
await copyFile(
  path.join(root, "resource", "icon.png"),
  path.join(output, "assets", "icon.png"),
);
for (const file of await readdir(path.join(root, "docs", "images"))) {
  if (/\.(png|webp|jpg|svg)$/i.test(file))
    await copyFile(
      path.join(root, "docs", "images", file),
      path.join(output, "assets", file),
    );
}

const toc = [];
const usedIds = new Map();
marked.use({
  renderer: {
    heading({ tokens, depth }) {
      const content = this.parser.parseInline(tokens);
      const base = content
        .replace(/<[^>]*>/g, "")
        .toLowerCase()
        .replace(/[^\p{L}\p{N}_\-\s]/gu, "")
        .replace(/\s/g, "-");
      const count = usedIds.get(base) || 0;
      usedIds.set(base, count + 1);
      const id = count ? `${base}-${count}` : base;
      if (depth === 2 && base !== "目录") toc.push({ id, content });
      return `<h${depth} id="${escape(id)}">${content}</h${depth}>\n`;
    },
    link({ href, title, tokens }) {
      if (href === "../README.md") href = "/";
      else if (href && !/^(?:https?:|mailto:|#|\/)/.test(href))
        href = `${repo}/blob/main/${path.posix.normalize(`docs/${href}`)}`;
      return `<a href="${escape(href || "")}"${title ? ` title="${escape(title)}"` : ""}>${this.parser.parseInline(tokens)}</a>`;
    },
    image({ href, title, text }) {
      if (href?.startsWith("images/")) href = `/assets/${href.slice(7)}`;
      return `<img src="${escape(href || "")}" alt="${escape(text)}" loading="lazy"${title ? ` title="${escape(title)}"` : ""}>`;
    },
  },
});
// The website supplies its own navigation; the Markdown source keeps its GitHub-friendly TOC.
const manualSource = (
  await readFile(path.join(root, "docs", "manual.md"), "utf8")
).replace(/^## 目录\r?\n[\s\S]*?(?=^## )/m, "");
const content = marked.parse(manualSource);
const shell = (title, description, url, body, manual = false) =>
  `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${escape(title)} · MaaNikki</title><meta name="description" content="${escape(description)}"><link rel="canonical" href="https://maanikki.cn${url}"><link rel="icon" href="/assets/icon.png"><link rel="stylesheet" href="/styles.css"><script src="/app.js" defer></script></head><body>${icons}${header(manual)}${body}${footer}</body></html>`;
await writeFile(
  path.join(output, "manual", "index.html"),
  shell(
    "用户手册",
    "MaaNikki 使用说明：快速开始、日常任务、祝福闪光全收集、探索辅助与常见问题。",
    "/manual/",
    `<main id="main" class="manual-layout section-width"><aside class="manual-sidebar"><strong>用户手册</strong><nav aria-label="手册目录">${toc.map((item) => `<a href="#${escape(item.id)}">${item.content}</a>`).join("")}</nav></aside><article class="manual-content">${content}</article></main>`,
    true,
  ),
);
await writeFile(
  path.join(output, "404.html"),
  shell(
    "页面未找到",
    "返回 MaaNikki 首页。",
    "/",
    '<main id="main" class="not-found section-width"><h1>404</h1><p>这页还没找到，先回首页看看吧。</p><a class="button primary" href="/">返回首页 →</a></main>',
  ),
);
await writeFile(path.join(output, "CNAME"), "maanikki.cn\n");
await writeFile(path.join(output, ".nojekyll"), "");
await writeFile(
  path.join(output, "robots.txt"),
  "User-agent: *\nAllow: /\nSitemap: https://maanikki.cn/sitemap.xml\n",
);
await writeFile(
  path.join(output, "sitemap.xml"),
  '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://maanikki.cn/</loc></url><url><loc>https://maanikki.cn/manual/</loc></url></urlset>\n',
);
console.log(
  `Built homepage, manual (${toc.length} sections), and assets → ${path.relative(root, output)}`,
);
console.log(`Download: ${snapshot?.tag_name || "GitHub release list"}`);
