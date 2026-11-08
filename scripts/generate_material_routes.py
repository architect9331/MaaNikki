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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Gingerbread-Man material collection routes from Pear-Pal 3D spawners and terrain."
    )
    parser.add_argument(
        "--mode",
        choices=("target", "max-yield", "list-materials"),
        default="target",
        help="Generation mode: 'target' for specific materials, 'max-yield' for all-in-one harvest.",
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
    parser.add_argument("--top-k", type=int, default=5, help="Max routes to generate.")
    parser.add_argument("--quota", type=int, default=None, help="Target item count quota.")
    parser.add_argument("--radius", type=float, default=13.5, help="Safe Gingerbread cover radius in meters.")
    parser.add_argument("--fish-casts", type=int, default=1, help="Fishing catches per fishing spot.")
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "config/routes"),
        help="Directory to save generated route JSONs and route_collection manifest.",
    )
    parser.add_argument("--prefix", default="gb_auto", help="Route ID prefix.")
    parser.add_argument("--no-save", action="store_true", help="Print summary without writing JSON files.")
    args = parser.parse_args()

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

    map_ids = [m.strip() for m in args.maps.split(",") if m.strip()] or None
    if args.mode == "target":
        queries = [m.strip() for m in args.materials.split(",") if m.strip()]
        if not queries:
            parser.error("--materials is required when --mode=target")
        plans = gen.generate_for_materials(
            queries,
            map_ids=map_ids,
            top_k=args.top_k,
            quota=args.quota,
            route_id_prefix=args.prefix,
        )
        col_title = f"【姜饼人速刷】{'/'.join(queries[:2])}（共{len(plans)}条）"
    else:
        plans = gen.generate_max_yield(
            map_ids=map_ids,
            top_k=args.top_k,
            route_id_prefix=args.prefix,
        )
        col_title = f"【姜饼人大丰收】高密度综合采集清单（共{len(plans)}条）"

    out_dir = Path(args.output_dir)
    if not args.no_save and plans:
        out_dir.mkdir(parents=True, exist_ok=True)
        for p in plans:
            rp = out_dir / f"{p.route_doc['id']}.json"
            rp.write_text(json.dumps(p.route_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        col_id = f"{args.prefix}_collection"
        col_doc = gen.build_route_collection(plans, col_id, col_title)
        (out_dir / f"{col_id}.json").write_text(
            json.dumps(col_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

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
