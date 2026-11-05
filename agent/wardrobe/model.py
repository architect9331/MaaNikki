"""Wardrobe Compendium identity catalog."""
from __future__ import annotations

from copy import deepcopy
import json
import re


def _clean_name(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).replace("・", "·")


class WardrobeCatalog:
    """Fast Compendium name lookup backed by deployed wardrobe labels and scoring metadata."""

    def __init__(self, directory, scoring_path=None):
        contract = json.loads((directory / "model-contract.json").read_text(encoding="utf-8"))
        self.version = contract["version"]
        self.fingerprint = contract["label_fingerprint"]
        self.labels = json.loads((directory / "labels.json").read_text(encoding="utf-8"))
        self.parts = json.loads((directory / "parts.json").read_text(encoding="utf-8"))
        if scoring_path is None:
            scoring_path = directory.parents[1] / "data/wardrobe_scoring.json"
        scoring = json.loads(scoring_path.read_text(encoding="utf-8")) if scoring_path.is_file() else {}
        self.game_items = scoring.get("game_items") or {}
        self.by_part_name: dict[str, dict[str, dict]] = {}
        self.by_item_id: dict[int, dict] = {}
        self._build_index()

    def _build_index(self):
        class_by_part_item: dict[tuple[str, int], dict] = {}
        for part, entries in self.labels.items():
            for entry in entries:
                for iid in entry.get("item_ids", []):
                    class_by_part_item[(str(part), int(iid))] = entry

        for part in self.parts:
            by_cname: dict[str, dict] = {}
            part_game_items = [
                (gid, g) for gid, g in self.game_items.items()
                if str(g.get("part")) == str(part)
            ]
            part_game_items.sort(key=lambda kv: (kv[0] != kv[1].get("group"), int(kv[0])))
            for gid, g in part_game_items:
                iid = int(gid)
                cname = _clean_name(g.get("name", ""))
                if not cname:
                    continue
                label_entry = class_by_part_item.get((str(part), iid))
                class_id = int(label_entry["class_id"]) if label_entry else -1
                if cname not in by_cname:
                    rec = {
                        "class_id": class_id,
                        "item_ids": [iid],
                        "names": [g["name"]],
                        "qualities": [int(g.get("quality", 3))],
                    }
                    by_cname[cname] = rec
                else:
                    first_gid = str(by_cname[cname]["item_ids"][0])
                    first_g = self.game_items.get(first_gid, {})
                    if ((g.get("score_id"), g.get("group"), g.get("glow_reward"))
                            == (first_g.get("score_id"), first_g.get("group"), first_g.get("glow_reward"))):
                        if iid not in by_cname[cname]["item_ids"]:
                            by_cname[cname]["item_ids"].append(iid)
                self.by_item_id[iid] = by_cname[cname]

            for entry in self.labels.get(str(part), []):
                for name in entry.get("names", []):
                    cname = _clean_name(name)
                    if not cname:
                        continue
                    if cname not in by_cname:
                        rec = {
                            "class_id": int(entry["class_id"]),
                            "item_ids": list(entry["item_ids"]),
                            "names": [name],
                            "qualities": list(entry["qualities"]),
                        }
                        by_cname[cname] = rec
                    for iid in entry.get("item_ids", []):
                        self.by_item_id.setdefault(int(iid), by_cname[cname])

            self.by_part_name[str(part)] = by_cname

    def lookup(self, part: str, raw_name: str) -> dict | None:
        part = str(part)
        cname = _clean_name(raw_name)
        if not cname:
            return None
        table = self.by_part_name.get(part) or {}
        if cname in table:
            return {**deepcopy(table[cname]), "source": "compendium_ocr"}
        cname_no_ascii = re.sub(r"[A-Za-z]", "", cname)
        if cname_no_ascii and cname_no_ascii != cname:
            cname = cname_no_ascii
            if cname in table:
                return {**deepcopy(table[cname]), "source": "compendium_ocr"}
        # Single-character OCR substitution recovery for names >= 3 chars when
        # strictly one same-length catalog entry in this part is within 1 char
        # and no other candidate is within 2 chars.
        if len(cname) >= 3:
            same_len = [
                (key, rec, sum(a != b for a, b in zip(cname, key)))
                for key, rec in table.items() if len(key) == len(cname)
            ]
            dist1 = [rec for _, rec, d in same_len if d == 1]
            dist2 = [rec for _, rec, d in same_len if d == 2]
            if len(dist1) == 1 and (len(cname) >= 4 or not dist2):
                return {**deepcopy(dist1[0]), "source": "compendium_ocr", "ocr_text": raw_name}
            # Unambiguous 1-char truncation recovery within the same part.
            trunc1 = [rec for key, rec in table.items() if cname in key and len(key) - len(cname) == 1]
            if len(trunc1) == 1:
                return {**deepcopy(trunc1[0]), "source": "compendium_ocr", "ocr_text": raw_name}
        return None

    def infer_suit_handhelds(self, parts_state: dict) -> list[dict]:
        """Infer base ability suit handhelds (TbClothesGallerySpecialAdd) when all sibling suit pieces are owned."""
        owned_gids: set[str] = set()
        existing_278: set[str] = set()
        for part, state in parts_state.items():
            if not isinstance(state, dict):
                continue
            for item in state.get("items") or []:
                for iid in item.get("item_ids") or []:
                    owned_gids.add(str(iid))
                    if str(part) == "278":
                        existing_278.add(str(iid))
        added = []
        for gid, g in sorted(self.game_items.items(), key=lambda kv: int(kv[0])):
            if str(g.get("part")) != "278" or gid in existing_278:
                continue
            if len(gid) != 10 or not gid.startswith("102078"):
                continue
            suffix = gid[-4:]
            siblings = [
                other for other, og in self.game_items.items()
                if other != gid and len(other) == 10
                and other.startswith("1020") and other.endswith(suffix)
                and str(og.get("part")) not in ("278", "413")
            ]
            if len(siblings) >= 6 and all(s in owned_gids for s in siblings):
                rec = self.by_item_id.get(int(gid)) or {
                    "class_id": -1,
                    "item_ids": [int(gid)],
                    "names": [g["name"]],
                    "qualities": [int(g.get("quality", 4))],
                }
                added.append({**deepcopy(rec), "source": "compendium_suit_inferred"})
                existing_278.add(gid)
        return added

