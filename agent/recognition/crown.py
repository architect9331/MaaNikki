"""Recognize the summit page only after its entry popups have closed."""
from __future__ import annotations

from maa.custom_recognition import CustomRecognition
from registration import agent_recognition


@agent_recognition("nikki.crown_ready")
class CrownReadyRecognition(CustomRecognition):
    def analyze(self, context, argv):
        image = argv.image
        if image is None or image.size == 0:
            return None
        # All checks use one image: the summit title remains visible behind
        # both popups and cannot establish that its controls are available.
        for node in ("MaaNikki_Crown_ReportTitle", "MaaNikki_Crown_EntryRewardTitle"):
            result = context.run_recognition(node, image)
            if result and result.hit:
                return CustomRecognition.AnalyzeResult(box=None, detail={"blocking": node})
        result = context.run_recognition("MaaNikki_Crown_SummitFeature", image)
        if result and result.hit:
            return CustomRecognition.AnalyzeResult(
                box=list(result.box), detail={"page": "summit", "entry_popup": False})
        return None
