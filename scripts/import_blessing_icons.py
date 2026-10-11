"""Import original transparent Eureka textures from the public Wiki."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "resource/image/blessing"
CLEAN_WIKI = "https://infinitynikki.wiki"
FANDOM = "https://infinity-nikki.fandom.com"
COLOR_NAMES = {"白": "White", "蓝": "Blue", "绿": "Green", "粉": "Pink", "紫": "Purple",
               "黄": "Yellow", "红": "Red", "彩": "Iridescent"}


def download(url):
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "MaaNikki/1.0 (public icon asset importer)"}),
                         timeout=30) as response:
                return response.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def wiki_query(host, **params):
    return json.loads(download(host + "/api.php?" + urlencode({"action": "query", "format": "json", **params})))


def clean_sources():
    category = wiki_query(FANDOM, list="categorymembers", cmtitle="Category:Eureka Sets", cmlimit=100)
    titles = [p["title"] for p in category["query"]["categorymembers"] if p["ns"] == 0]
    pages = wiki_query(FANDOM, titles="|".join(titles), prop="revisions", rvprop="content")
    names = {}
    for page in pages["query"]["pages"].values():
        text = page.get("revisions", [{}])[0].get("*", "")
        name = re.search(r"\|[ \t]*zhs[ \t]*=[ \t]*([^\r\n]+)", text)
        if name and not name.group(1).startswith("|"):
            names[name.group(1).strip()] = page["title"]
    # Empty or incorrect translations on the English set pages.
    names.update({"稚意酣眠": "Innocent Slumber", "稚意沫影": "Innocent Foamshade",
                  "陨星": "Meteorite", "星芒": "Starlight",
                  "弦中诗": "Verse of Strings", "镜中萤": "Glimmer in Mirrors"})
    return names


def main():
    catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
    english = clean_sources()
    entries = []
    for definition in catalog["sets"]:
        for slot in ("头部", "手部", "脚部"):
            for color in definition["colors"]:
                english_slot = {"头部": "Head", "手部": "Hands", "脚部": "Feet"}[slot]
                clean_title = (f"File:{english[definition['name']]} ({english_slot})"
                               + ("" if definition["rarity"] == 3 else f" {COLOR_NAMES[color]}") + ".png")
                # The Wiki's hand textures for this set use a singular name.
                if definition["name"] == "弦中诗" and slot == "手部":
                    clean_title = clean_title.replace("Verse of Strings", "Verse of String")
                entries.append({"name": definition["name"], "slot": slot, "color": color,
                                "rarity": definition["rarity"], "file": f"{len(entries):03d}.png",
                                "format": "icon", "title": clean_title})
    DESTINATION.mkdir(parents=True, exist_ok=True)
    wanted = [entry["title"] for entry in entries]
    clean = {}
    for offset in range(0, len(wanted), 45):
        query = wiki_query(CLEAN_WIKI, titles="|".join(wanted[offset:offset + 45]),
                           prop="imageinfo", iiprop="url")
        for page in query["query"]["pages"].values():
            if page.get("imageinfo"):
                info = page["imageinfo"][0]
                clean[page["title"]] = info
    for entry in entries:
        title = entry["title"]
        if title not in clean:
            raise ValueError(f"Missing original Wiki texture: {title}")
        info = clean[title]
        entry.update(url=info["url"], page=info["descriptionurl"])

    previous_path = DESTINATION / "index.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    old_entries = {entry["file"]: entry for entry in previous.get("images", [])}

    def save(entry):
        path = DESTINATION / entry["file"]
        # Reuse only a file from the exact same URL, so changing sources never
        # leaves a watermarked card behind under a clean-icon manifest entry.
        old = old_entries.get(entry["file"], {})
        data = path.read_bytes() if path.exists() and old.get("url") == entry["url"] else download(entry["url"])
        if old.get("url") == entry["url"] and sha256(data).hexdigest() != old.get("sha256"):
            data = download(entry["url"])
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"Not a PNG: {entry['title']}")
        path.write_bytes(data)
        return {**entry, "sha256": sha256(data).hexdigest()}

    with ThreadPoolExecutor(max_workers=6) as workers:
        imported = list(workers.map(save, entries))
    index = {"schema_version": 1, "source": "The Infinity Nikki Wiki 透明底原图", "page": CLEAN_WIKI,
             "catalog_version": catalog["version"], "images": imported}
    (DESTINATION / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Imported {len(imported)} Wiki images ({sum(e['format'] == 'icon' for e in imported)} transparent originals)")


if __name__ == "__main__":
    main()
