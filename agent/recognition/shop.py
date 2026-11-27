"""Shop tab and Resonance Exchange sub-tab state recognizers for the 3.0 shop UI.

The 3.0 shop redesigned the left navigation rail (narrowed to x=0..130, scrollable,
no category icons) and moved Resonance Exchange's Surge/Quiet sub-tabs from a top
horizontal bar with star ornaments to two vertical image cards beside the rail.
These recognizers verify active selection from geometric and photometric contrast
anchored to live OCR boxes rather than fixed theme-sensitive templates.
"""
from __future__ import annotations

import json
import numpy as np

from maa.custom_recognition import CustomRecognition
from registration import agent_recognition


def _mean_luma_bgr(patch: np.ndarray) -> tuple[float, float, float, float]:
    if patch is None or patch.size == 0:
        return 0.0, 0.0, 0.0, 0.0
    bgr = patch.reshape(-1, 3).mean(axis=0)
    b, g, r = float(bgr[0]), float(bgr[1]), float(bgr[2])
    luma = 0.114 * b + 0.587 * g + 0.299 * r
    return luma, b, g, r


@agent_recognition("nikki.shop_tab_selected")
class ShopTabSelectedRecognition(CustomRecognition):
    """Confirm a left-rail shop tab is currently selected via its golden right-edge plate."""

    def analyze(self, context, argv):
        image = argv.image
        if image is None or image.size == 0:
            return None
        param = json.loads(argv.custom_recognition_param or "{}")
        tab_node = param.get("tab_node")
        if not tab_node:
            return None
        page = context.run_recognition("MaaNikki_Shop_PageReady", image)
        if not page or not page.hit:
            return None
        tab = context.run_recognition(tab_node, image)
        if not tab or not tab.hit or not tab.box:
            return None
        bx, by, bw, bh = [int(v) for v in tab.box]
        h, w = image.shape[:2]
        if bx + bw > min(w, 138):
            return None
        y0 = max(80, by - 8)
        y1 = min(h, by + bh + 10)
        x0 = min(w - 1, 116)
        x1 = min(w, 131)
        if y1 <= y0 or x1 <= x0:
            return None
        luma, b, _g, r = _mean_luma_bgr(image[y0:y1, x0:x1])
        # Selected tabs in the 3.0 left rail render a bright warm-gold selection plate
        # ending in a luminous right edge (~luma 205, R ~237, R-B ~87), whereas
        # unselected rows sit over the dark sidebar (~luma 100-120, R ~120-140).
        if luma >= 165.0 and r >= 190.0 and (r - b) >= 45.0:
            return CustomRecognition.AnalyzeResult(
                box=[bx, by, bw, bh],
                detail={"tab": tab_node, "edge_luma": round(luma, 1), "edge_r": round(r, 1), "edge_rb": round(r - b, 1)},
            )
        return CustomRecognition.AnalyzeResult(
            box=None,
            detail={"tab": tab_node, "selected": False, "edge_luma": round(luma, 1), "edge_r": round(r, 1)},
        )


@agent_recognition("nikki.shop_quiet_ready")
class ShopQuietReadyRecognition(CustomRecognition):
    """Triple-locked check that Resonance Shop is specifically on 静谧水滴兑换 (never 澎湃浪花兑换)."""

    def analyze(self, context, argv):
        image = argv.image
        if image is None or image.size == 0:
            return None
        page = context.run_recognition("MaaNikki_Shop_PageReady", image)
        if not page or not page.hit:
            return None
        surge = context.run_recognition("MaaNikki_Shop_SurgeTab", image)
        quiet = context.run_recognition("MaaNikki_Shop_QuietTab", image)
        if not (surge and surge.hit and surge.box and quiet and quiet.hit and quiet.box):
            return None
        sx, sy, sw, sh = [int(v) for v in surge.box]
        qx, qy, qw, qh = [int(v) for v in quiet.box]
        if qy <= sy + 25:
            return None

        h, w = image.shape[:2]
        x0, x1 = max(0, 145), min(w, 262)

        def _card_luma(ty: int, th: int) -> tuple[float, float]:
            body = image[max(85, ty - 52):min(h, ty + th + 4), x0:x1]
            label = image[max(85, ty - 4):min(h, ty + th + 4), x0:x1]
            return _mean_luma_bgr(body)[0], _mean_luma_bgr(label)[0]

        surge_body, surge_label = _card_luma(sy, sh)
        quiet_body, quiet_label = _card_luma(qy, qh)
        body_diff = quiet_body - surge_body
        label_diff = quiet_label - surge_label

        # Photometric flip across the two adjacent sub-tab cards on the same frame:
        # 静谧水滴兑换 selected -> body_diff ~+26, label_diff ~+52;
        # 澎湃浪花兑换 selected -> body_diff ~-27, label_diff ~-31.
        if body_diff < 12.0 or label_diff < 18.0:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={
                    "selected": False,
                    "body_diff": round(body_diff, 1),
                    "label_diff": round(label_diff, 1),
                },
            )

        # Grid content anti-surge guard: never treat 澎湃浪花兑换 as 静谧水滴兑换.
        surge_only = context.run_recognition("MaaNikki_Shop_SurgeOnlyItems", image)
        if surge_only and surge_only.hit:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"selected": False, "blocking": "surge_only_items"},
            )
        quiet_anchor = context.run_recognition("MaaNikki_Shop_QuietGridAnchor", image)
        if not (quiet_anchor and quiet_anchor.hit):
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"selected": False, "missing": "quiet_grid_anchor"},
            )

        return CustomRecognition.AnalyzeResult(
            box=[qx, qy, qw, qh],
            detail={
                "selected": True,
                "body_diff": round(body_diff, 1),
                "label_diff": round(label_diff, 1),
            },
        )
