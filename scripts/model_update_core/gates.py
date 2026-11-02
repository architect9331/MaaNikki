"""Replacement rules shared by every model.

Accuracy on existing classes must not fall. A fall is never accepted silently:
it blocks unless a person records a reason, and a statistically significant fall
cannot be waived at all. New classes have no baseline, so they are compared with
existing classes measured on the same recipes. The conventional .05 level only
distinguishes noise from a real difference; it is not tuned to any recorded run.
"""
from __future__ import annotations

import math


ALPHA = .05


def binomial_tail(successes, trials):
    """Exact one-sided P(X >= successes) for a fair coin."""
    if trials <= 0:
        return 1.
    return min(1., sum(math.comb(trials, k) for k in range(successes, trials + 1)) / 2 ** trials)


def mcnemar_regression_p(regressions, fixes):
    """Paired test on samples only one model classified correctly."""
    return binomial_tail(regressions, regressions + fixes)


def _log_choose(n, k):
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_lower_p(new_correct, new_total, old_correct, old_total):
    """One-sided Fisher exact P that new-class accuracy is this low by chance."""
    correct = new_correct + old_correct
    total = new_total + old_total
    if new_total == 0 or old_total == 0:
        return 1.
    denominator = _log_choose(total, new_total)
    tail = 0.
    for k in range(max(0, new_total - (total - correct)), new_correct + 1):
        tail += math.exp(_log_choose(correct, k) + _log_choose(total - correct, new_total - k) - denominator)
    return min(1., tail)


class Gate:
    def __init__(self, model):
        self.model = model
        self.checks = []

    def check(self, name, passed, detail, waivable=False):
        self.checks.append({"name": name, "passed": bool(passed), "waivable": bool(waivable and not passed),
                            "detail": detail})

    def regression(self, name, counts):
        """Counts: samples, baseline_correct, candidate_correct, regressions, fixes."""
        p = mcnemar_regression_p(counts["regressions"], counts["fixes"])
        fell = counts["candidate_correct"] < counts["baseline_correct"]
        self.check(name, not fell, {**counts, "paired_p": p}, waivable=fell and p >= ALPHA)

    def new_classes(self, name, new, old):
        if not new["samples"]:
            self.check(name, True, {"note": "no new-class samples in this corpus"})
            return
        p = fisher_lower_p(new["correct"], new["samples"], old["correct"], old["samples"])
        self.check(name, p >= ALPHA, {"new": new, "existing": old, "fisher_p": p})

    def result(self, waivers=None):
        waivers = waivers or {}
        failed = [row for row in self.checks if not row["passed"]]
        blocking = [row["name"] for row in failed if not (row["waivable"] and row["name"] in waivers)]
        return {"model": self.model, "status": "blocked" if blocking else "pass", "blocking": blocking,
                "waived": {row["name"]: waivers[row["name"]] for row in failed
                           if row["waivable"] and row["name"] in waivers},
                "checks": self.checks}
