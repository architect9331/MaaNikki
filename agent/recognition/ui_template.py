"""MAA recognition for UI glyphs whose brightness is part of their identity."""
from __future__ import annotations

import json
import cv2
import numpy as np
from registration import agent_recognition
from maa.custom_recognition import CustomRecognition

from daily.settings import ROOT
from vision import prepared_template


@agent_recognition("nikki.ui_template")
class UITemplateRecognition(CustomRecognition):
    def analyze(self, context, argv):
        del context
        try:
            param = argv.custom_recognition_param
            param = json.loads(param or "{}") if isinstance(param, str) else param
            image = argv.image
            if image is None or image.size == 0:
                raise ValueError("游戏截图为空。")
            roi = param.get("roi", [0, 0, image.shape[1], image.shape[0]])
            if (not isinstance(roi, list) or len(roi) != 4
                    or any(type(v) is not int for v in roi)):
                raise ValueError("页面图像区域无效。")
            x, y, width, height = roi
            if (x < 0 or y < 0 or width <= 0 or height <= 0
                    or x+width > image.shape[1] or y+height > image.shape[0]):
                raise ValueError("页面图像区域超出截图。")
            templates = param["template"]
            templates = templates if isinstance(templates, list) else [templates]
            thresholds = param.get("threshold", .8)
            thresholds = thresholds if isinstance(thresholds, list) else [thresholds]*len(templates)
            if not templates or len(templates) != len(thresholds):
                raise ValueError("页面模板与识别阈值数量不一致。")
            gray = param.get("gray_limit", [210, 255])
            if (not isinstance(gray, list) or len(gray) != 2
                    or any(type(v) is not int for v in gray) or not 0 <= gray[0] < gray[1] <= 255):
                raise ValueError("页面图像亮度范围无效。")
            resource = ROOT / "resource"
            image_root = (resource / "image").resolve()
            target = cv2.threshold(cv2.cvtColor(image[y:y+height, x:x+width], cv2.COLOR_BGR2GRAY),
                                   gray[0], gray[1], cv2.THRESH_BINARY)[1]
            best_score, best_box, best_template = -1.0, None, None
            for name, threshold in zip(templates, thresholds):
                if not isinstance(name, str) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
                    raise ValueError("页面模板或阈值无效。")
                path = (image_root / name).resolve()
                if not path.is_relative_to(image_root):
                    raise ValueError("页面图像资源路径无效。")
                template = prepared_template(path, grayscale=True, gray=gray).image
                if not template.size:
                    raise ValueError("页面图像没有可识别的亮色特征。")
                th, tw = template.shape[:2]
                if tw > width or th > height:
                    continue
                rates = cv2.matchTemplate(target, template, cv2.TM_CCORR_NORMED)
                rates = np.nan_to_num(rates, copy=False, nan=-1, posinf=-1, neginf=-1)
                _, score, _, point = cv2.minMaxLoc(rates)
                if score > best_score:
                    best_score, best_template = float(score), name
                if score >= threshold:
                    best_box = [x+point[0], y+point[1], tw, th]
                    return CustomRecognition.AnalyzeResult(box=best_box, detail={
                        "score": float(score), "template": name, "gray_limit": gray, "method": 3})
            return CustomRecognition.AnalyzeResult(box=None, detail={
                "score": best_score, "template": best_template, "gray_limit": gray, "method": 3})
        except (OSError, ValueError, TypeError, KeyError, cv2.error) as error:
            return CustomRecognition.AnalyzeResult(box=None, detail={"error": str(error)})
