const repository = "https://github.com/architect9331/MaaNikki";
const themeButton = document.querySelector("[data-theme-toggle]");
let chosenTheme;
try {
  chosenTheme = localStorage.getItem("maanikki-theme");
} catch {
  /* Browsers may restrict local storage. The system theme still works. */
}
const systemTheme = matchMedia("(prefers-color-scheme: dark)");
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  themeButton?.setAttribute(
    "aria-label",
    theme === "dark" ? "切换浅色主题" : "切换深色主题",
  );
  themeButton?.setAttribute("aria-pressed", String(theme === "dark"));
}
setTheme(
  chosenTheme === "dark" || chosenTheme === "light"
    ? chosenTheme
    : systemTheme.matches
      ? "dark"
      : "light",
);
themeButton?.addEventListener("click", () => {
  chosenTheme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  setTheme(chosenTheme);
  try {
    localStorage.setItem("maanikki-theme", chosenTheme);
  } catch {
    /* Keep the choice for this page. */
  }
});
systemTheme.addEventListener("change", (event) => {
  if (!chosenTheme) setTheme(event.matches ? "dark" : "light");
});
for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const status = button
      .closest(".community-links")
      ?.querySelector("[role=status]");
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      if (status) status.textContent = "群号已复制";
    } catch {
      if (status) status.textContent = `QQ群：${button.dataset.copy}`;
    }
  });
}
function showRelease(release) {
  if (!release || !Array.isArray(release.assets)) return;
  const asset = release.assets.find((item) =>
    /^MaaNikki-v.+-win-x86_64\.zip$/.test(item.name),
  );
  if (!asset || typeof asset.browser_download_url !== "string") return;
  const url = new URL(asset.browser_download_url);
  if (
    url.origin !== "https://github.com" ||
    !url.pathname.startsWith("/architect9331/MaaNikki/releases/download/")
  )
    return;
  document.querySelectorAll(".download-link").forEach((link) => {
    link.href = url.href;
  });
  document.querySelectorAll("[data-release-version]").forEach((label) => {
    label.textContent = `${release.tag_name} · Windows 10 / 11 · x64${asset.size ? ` · ${Math.round(asset.size / 1024 / 1024)} MB` : ""}`;
  });
}
async function loadRelease() {
  if (!document.querySelector(".download-link")) return;
  // A same-origin snapshot keeps the download usable when GitHub's API is slow or rate limited.
  try {
    const response = await fetch("/release.json", {
      signal: AbortSignal.timeout(5000),
    });
    if (response.ok) showRelease(await response.json());
  } catch {
    /* The static link already points to the release list. */
  }
  try {
    // This project publishes prereleases, so /releases/latest would omit its current builds.
    const response = await fetch(
      "https://api.github.com/repos/architect9331/MaaNikki/releases?per_page=10",
      { signal: AbortSignal.timeout(5000) },
    );
    if (!response.ok) return;
    const releases = await response.json();
    if (!Array.isArray(releases)) return;
    const release = releases.find(
      (item) =>
        !item.draft &&
        item.assets?.some((asset) =>
          /^MaaNikki-v.+-win-x86_64\.zip$/.test(asset.name),
        ),
    );
    showRelease(release);
  } catch {
    /* Keep the release snapshot or the release-list link. */
  }
}
void loadRelease();
