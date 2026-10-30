"""Colour evidence for otherwise ambiguous, shape-matched scroll positions."""
from __future__ import annotations

import numpy as np

from .icons import IconMatcher


def distinguish(template, search, offsets, weights, *, threshold, margin):
    """Compare aligned icon bodies, without classifying a named Eureka colour.

    Normalised chroma removes uniform brightness gain. Positive local luma
    contrast supplied by the caller weights bright foreground details rather
    than the smooth card backing; chroma energy suppresses shared white glints.
    Selection tint and translucent backgrounds can still change chroma, so this
    evidence retains the existing icon-colour acceptance and separation limits.
    """
    reference = IconMatcher.chroma(template.astype(np.float32) / 255)
    weights = weights * np.sum(reference ** 2, axis=2)
    total = float(weights.sum() * 3)
    if total <= 0:
        return None, {"accepted": False, "reason": "no_coloured_foreground"}
    candidates = []
    for offset in offsets:
        patch = search[offset:offset + template.shape[0]]
        observed = IconMatcher.chroma(patch.astype(np.float32) / 255)
        distance = float(np.sqrt(np.sum((reference - observed) ** 2 * weights[:, :, None]) / total))
        candidates.append({"offset": offset, "score": float(np.clip(1 - distance, 0, 1))})
    candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
    if len(candidates) < 2:
        return None, {"accepted": False, "reason": "insufficient_candidates"}
    best = candidates[0]
    separation = best["score"] - candidates[1]["score"]
    accepted = best["score"] >= threshold and separation >= margin
    return best["offset"] if accepted else None, {
        "accepted": accepted, "reason": "distinct_colour" if accepted else "colour_uncertain",
        "score": round(best["score"], 4), "margin": round(separation, 4),
        "minimum_score": threshold, "minimum_margin": margin,
        "candidates": [{"offset": candidate["offset"], "score": round(candidate["score"], 4)}
                       for candidate in candidates]}
