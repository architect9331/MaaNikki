"""Inventory changes have bounded effects; observation failures have none."""
from .blessing import SLOTS


class RecheckSlot(RuntimeError):
    """Acquisition order needs another observation, not a new baseline."""

    def __init__(self, slot, reason):
        super().__init__(reason)
        self.slot = slot


def operation_scope(operation):
    """Return a recoverable change's scope, or None for an unknown mutation.

    Upgrades auto-fill only unlocked materials. Locking a verified maximum
    adds protection; rarity-capped decomposition cannot remove five-stars.
    These operations cannot invalidate unrelated locked collection entries.
    """
    if not isinstance(operation, dict) or operation.get("status") != "submitted_unconfirmed":
        return None
    if operation.get("input_attempted") is False:
        return []
    kind, facts = operation.get("kind"), operation.get("facts")
    evidence = operation.get("evidence") or {}
    if not isinstance(evidence, dict):
        return None
    if kind == "realm" and operation.get("realm") == "bless" and facts is None:
        return []
    if (kind == "decompose" and facts is None and operation.get("rarity_max") == 4
            and evidence.get("source") == "four_star_auto_fill"):
        return []
    if not isinstance(facts, dict) or facts.get("slot") not in SLOTS:
        return None
    if (kind == "upgrade" and facts.get("rarity") in (3, 4, 5)
            and type(facts.get("level")) is int and 0 <= facts["level"] < 10
            and type(operation.get("preview_level")) is int
            and facts["level"] <= operation["preview_level"] <= 10):
        return [facts["slot"]]
    if (kind == "lock" and facts.get("rarity") == 5 and facts.get("level") == 10
            and facts.get("color") is not None
            and evidence.get("source") == "verified_current_selection"):
        return [facts["slot"]]
    return None
