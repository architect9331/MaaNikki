"""CLI entry point for generating Gingerbread-Man material routes and route collections."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from navigation.material_route_generator import (  # noqa: E402
    GingerbreadSpec,
    KIND_LABELS,
    MaterialRouteGenerator,
)


def build_full_library(gen: MaterialRouteGenerator, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old_f in out_dir.glob("gb_*.json"):
        old_f.unlink(missing_ok=True)

    saved_routes = 0
    saved_collections = 0

    def save_plans(plans):
        nonlocal saved_routes
        for p in plans:
            rp = out_dir / f"{p.route_doc['id']}.json"
            rp.write_text(json.dumps(p.route_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            saved_routes += 1

    def save_collection(plans, col_id: str, title: str):
        nonlocal saved_collections
        if not plans:
            return
        col_doc = gen.build_route_collection(plans, col_id, title)
        (out_dir / f"{col_id}.json").write_text(
            json.dumps(col_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        saved_collections += 1

    # 1. All-material regional sweep routes
    regional_specs = [
        ("florawish", ["miraland"], ["花愿镇"], 3, "全材料·花愿镇"),
        ("breezy", ["miraland"], ["微风绿野"], 4, "全材料·微风绿野"),
        ("stonewoods", ["miraland"], ["小石树田村"], 3, "全材料·小石树田村"),
        ("abandoned", ["miraland"], ["石树田无人区"], 4, "全材料·石树田无人区"),
        ("wishing", ["miraland"], ["祈愿树林"], 4, "全材料·祈愿树林"),
        ("gianttree", ["miraland"], ["巨木之森"], 5, "全材料·巨木之森"),
        ("snailcity", ["miraland"], ["蜗牛城"], 2, "全材料·蜗牛城"),
        ("boneyard", ["miraland"], ["埋骨地"], 4, "全材料·埋骨地"),
        ("firework", ["firework"], None, 4, "全材料·花焰群岛"),
        ("serenity", ["serenity"], None, 4, "全材料·无忧岛"),
        ("danqing", ["danqing"], None, 4, "全材料·丹青屿"),
        ("wanxiang", ["wanxiang"], None, 4, "全材料·万相境"),
    ]
    by_region: dict[str, list] = {}
    for slug, m_ids, regs, k, label in regional_specs:
        plans = gen.generate_max_yield(
            map_ids=m_ids,
            regions=regs,
            top_k=k,
            route_id_prefix=f"gb_all_{slug}",
            title_label=label,
        )
        save_plans(plans)
        by_region[slug] = plans

    # 2. Category-specific max-yield sweep routes
    cat_specs = [
        ("plant", {"plant": 2.0, "insect": 0.0, "animal": 0.0, "fish": 0.0}, 8, "全植物速刷"),
        ("insect", {"plant": 0.2, "insect": 3.0, "animal": 0.0, "fish": 0.0}, 8, "全昆虫速刷"),
        ("animal", {"plant": 0.2, "insect": 0.0, "animal": 3.5, "fish": 0.0}, 8, "全动物清洁"),
        ("fish", {"plant": 0.1, "insect": 0.0, "animal": 0.0, "fish": 4.0}, 8, "全鱼类垂钓"),
    ]
    by_cat: dict[str, list] = {}
    for slug, weights, k, label in cat_specs:
        plans = gen.generate_max_yield(
            top_k=k,
            kind_weights=weights,
            route_id_prefix=f"gb_cat_{slug}",
            title_label=label,
        )
        save_plans(plans)
        by_cat[slug] = plans

    # 3. Single-material routes for all open-world catalogs (plant, insect, animal)
    by_cid_first: dict[str, object] = {}
    for cid, info in sorted(gen.catalog.catalogs.items(), key=lambda kv: int(kv[0])):
        if cid == "99":
            continue
        pts = [p for plist in gen.catalog.points_by_map.values() for p in plist if p.catalog_id == cid]
        if not pts:
            continue
        cnt = len(pts)
        k = 3 if cnt >= 400 else (2 if cnt >= 25 else 1)
        plans = gen.generate_for_materials(
            [info["name"]],
            top_k=k,
            route_id_prefix=f"gb_mat_{int(cid):03d}",
        )
        save_plans(plans)
        if plans:
            by_cid_first[info["name"]] = plans[0]

    # 4. Single-material routes for all 16 fish species
    fish_species: set[str] = set()
    for plist in gen.catalog.points_by_map.values():
        for p in plist:
            if p.kind == "fish" and p.condition:
                fish_species.update(gen.catalog.parse_fish_species(p.condition))
    for f_idx, f_name in enumerate(sorted(fish_species), 1):
        plans = gen.generate_for_materials(
            [f_name],
            top_k=2,
            route_id_prefix=f"gb_fish_{f_idx:02d}",
        )
        save_plans(plans)
        if plans:
            by_cid_first[f_name] = plans[0]

    # 5. Curated multi-route playlists (route_collection)
    wf_plans = (
        by_region["florawish"] + by_region["breezy"] + by_region["stonewoods"]
        + by_region["abandoned"] + by_region["wishing"]
    )
    iz_plans = by_region["gianttree"] + by_region["snailcity"] + by_region["boneyard"]
    is_plans = by_region["firework"] + by_region["serenity"] + by_region["danqing"] + by_region["wanxiang"]
    global_top = sorted(wf_plans + iz_plans + is_plans, key=lambda r: r.efficiency_per_min, reverse=True)[:24]

    save_collection(global_top, "gb_col_01_all_global", f"【姜饼人合集】全地图高密度大丰收总合集（{len(global_top)}条）")
    save_collection(wf_plans, "gb_col_02_wishfield", f"【姜饼人合集】心愿原野·全区域材料大丰收（{len(wf_plans)}条）")
    save_collection(iz_plans, "gb_col_03_izhan", f"【姜饼人合集】伊赞之土·巨木之森与埋骨地大丰收（{len(iz_plans)}条）")
    save_collection(is_plans, "gb_col_04_islands", f"【姜饼人合集】群岛秘境·花焰/无忧/丹青/万相大丰收（{len(is_plans)}条）")

    legendary_names = [
        "曦光毛团", "天星之羽", "盛宴毛毡", "围兜毛球", "玫色天鹅绒", "夜色天鹅绒",
        "晴日蝶兰/雨滴蝶兰", "烛融花/烛荧花", "日光果/星光果", "纱裙鱼", "拉夫领鱼", "贵族卷毛", "鹿冠毛团", "涅拉羽",
    ]
    leg_plans = [by_cid_first[nm] for nm in legendary_names if nm in by_cid_first]
    save_collection(leg_plans, "gb_col_05_legendary_daily", f"【姜饼人合集】每日传奇动物与稀有材料必刷清单（{len(leg_plans)}条）")
    save_collection(by_cat["plant"], "gb_col_06_fast_plants", f"【姜饼人合集】全植物高密采集跑图合集（{len(by_cat['plant'])}条）")
    save_collection(by_cat["insect"], "gb_col_07_insects", f"【姜饼人合集】全昆虫高密捕捉合集（{len(by_cat['insect'])}条）")
    save_collection(by_cat["animal"], "gb_col_08_animals", f"【姜饼人合集】全动物清洁撸毛合集（{len(by_cat['animal'])}条）")
    save_collection(by_cat["fish"], "gb_col_09_fishing", f"【姜饼人合集】全鱼类高密垂钓合集（{len(by_cat['fish'])}条）")

    print(f"Built full Gingerbread library in {out_dir}: {saved_routes} routes + {saved_collections} collections!")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Gingerbread-Man material collection routes from Pear-Pal 3D spawners and terrain."
    )
    parser.add_argument(
        "--mode",
        choices=("target", "max-yield", "list-materials", "build-library"),
        default="target",
        help="Generation mode: 'target', 'max-yield', 'list-materials', or 'build-library'.",
    )
    parser.add_argument(
        "--materials",
        default="",
        help="Comma-separated material names, catalog IDs, or categories (plant/insect/animal/fish).",
    )
    parser.add_argument(
        "--maps",
        default="",
        help="Optional comma-separated map IDs (miraland,wanxiang,firework,serenity,danqing).",
    )
    parser.add_argument(
        "--regions",
        default="",
        help="Optional comma-separated region or province names (e.g. 花愿镇,巨木之森).",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Max routes to generate.")
    parser.add_argument("--quota", type=int, default=None, help="Target item count quota.")
    parser.add_argument("--radius", type=float, default=13.8, help="Safe Gingerbread cover radius in meters.")
    parser.add_argument("--fish-casts", type=int, default=1, help="Fishing catches per fishing spot.")
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "config/routes"),
        help="Directory to save generated route JSONs and route_collection manifest.",
    )
    parser.add_argument("--prefix", default="gb_auto", help="Route ID prefix.")
    parser.add_argument("--no-save", action="store_true", help="Print summary without writing JSON files.")
    parser.add_argument("--json-stdout", action="store_true", help="Output generated routes and stats as JSON.")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    spec = GingerbreadSpec(
        safe_radius_m=args.radius,
        pass_radius_m=min(15.0, args.radius + 0.7),
        fish_casts_per_spot=args.fish_casts,
    )
    gen = MaterialRouteGenerator(ROOT, spec=spec)

    if args.mode == "list-materials":
        by_kind: dict[str, list[str]] = {}
        for cid, info in sorted(gen.catalog.catalogs.items(), key=lambda kv: int(kv[0])):
            by_kind.setdefault(info["kind"], []).append(f"{info['name']}(id={cid}, {info['count']})")
        for k, items in by_kind.items():
            print(f"=== {KIND_LABELS.get(k, k)} ({len(items)}) ===")
            print("  " + ", ".join(items))
        return 0

    if args.mode == "build-library":
        lib_dir = (
            ROOT / "resource/routes/gingerbread"
            if args.output_dir == str(ROOT / "config/routes")
            else Path(args.output_dir)
        )
        return build_full_library(gen, lib_dir)

    map_ids = [m.strip() for m in args.maps.split(",") if m.strip()] or None
    regions = [r.strip() for r in args.regions.split(",") if r.strip()] or None
    if args.mode == "target":
        queries = [m.strip() for m in args.materials.split(",") if m.strip()]
        if not queries:
            parser.error("--materials is required when --mode=target")
        plans = gen.generate_for_materials(
            queries,
            map_ids=map_ids,
            regions=regions,
            top_k=args.top_k,
            quota=args.quota,
            route_id_prefix=args.prefix,
        )
        col_title = f"【姜饼人速刷】{'/'.join(queries[:2])}（共{len(plans)}条）"
    else:
        plans = gen.generate_max_yield(
            map_ids=map_ids,
            regions=regions,
            top_k=args.top_k,
            route_id_prefix=args.prefix,
        )
        col_title = f"【姜饼人大丰收】高密度综合采集清单（共{len(plans)}条）"

    out_dir = Path(args.output_dir)
    col_id = f"{args.prefix}_collection"
    col_doc = gen.build_route_collection(plans, col_id, col_title) if plans else None
    if not args.no_save and plans:
        out_dir.mkdir(parents=True, exist_ok=True)
        for p in plans:
            rp = out_dir / f"{p.route_doc['id']}.json"
            rp.write_text(json.dumps(p.route_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if col_doc:
            (out_dir / f"{col_id}.json").write_text(
                json.dumps(col_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    if args.json_stdout:
        payload = {
            "routes": [p.route_doc for p in plans],
            "collection": col_doc,
            "summaries": [
                {
                    "id": p.route_doc["id"],
                    "title": p.route_doc["title"],
                    "map": p.checkpoint.map_id,
                    "region": p.checkpoint.region,
                    "checkpoint": p.checkpoint.name,
                    "target_count": p.target_count,
                    "total_count": p.total_count,
                    "counts_by_name": p.counts_by_name,
                    "counts_by_kind": p.counts_by_kind,
                    "walk_distance_m": p.walk_distance_m,
                    "estimated_seconds": p.estimated_seconds,
                    "efficiency_per_min": p.efficiency_per_min,
                    "covered_points": [
                        {
                            "name": cp.name,
                            "kind": cp.kind,
                            "x": cp.x,
                            "y": cp.y,
                            "z": cp.z,
                        }
                        for cp in p.covered_points
                    ],
                }
                for p in plans
            ],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    for idx, p in enumerate(plans, 1):
        top_items = ", ".join(f"{k}×{v}" for k, v in sorted(p.counts_by_name.items(), key=lambda x: -x[1])[:6])
        kinds_str = ", ".join(f"{KIND_LABELS.get(k, k)}×{v}" for k, v in p.counts_by_kind.items())
        print(
            f"[{idx}] {p.route_doc['title']} | map={p.checkpoint.map_id} | "
            f"target={p.target_count}, total={p.total_count} ({kinds_str}) | "
            f"walk={p.walk_distance_m}m, time={p.estimated_seconds}s, rate={p.efficiency_per_min}/min\n"
            f"    明细: {top_items}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
