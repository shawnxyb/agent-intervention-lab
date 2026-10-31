"""Live funding availability with explicitly authorized Credits and no reserve threshold."""

import math
from decimal import Decimal, InvalidOperation

MINIMUM_START_REMAINING = {}


class FundingRequired(RuntimeError):
    pass


def subscription_limit(snapshot: dict) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    limits = snapshot.get("rateLimitsByLimitId")
    if limits is not None and not isinstance(limits, dict):
        return None
    limit = limits.get("codex") if limits else snapshot.get("rateLimits")
    return limit if isinstance(limit, dict) else None


def subscription_status(snapshot: dict) -> str:
    limit = subscription_limit(snapshot)
    if limit is None:
        return "unknown"
    windows = [limit.get(key) for key in ("primary", "secondary")]
    values = [w.get("usedPercent") if isinstance(w, dict) else None for w in windows]
    valid = [
        isinstance(v, (float, int))
        and not isinstance(v, bool)
        and (not isinstance(v, float) or math.isfinite(v))
        and v >= 0
        for v in values
    ]
    if any(ok and v >= 100 for ok, v in zip(valid, values, strict=True)):
        return "exhausted"
    return "available" if all(valid) else "unknown"


def credits_available(snapshot: dict) -> bool:
    limit = subscription_limit(snapshot)
    credits = limit.get("credits") if limit else None
    if not isinstance(credits, dict):
        return False
    if credits.get("unlimited") is True:
        return True
    if credits.get("hasCredits") is not True:
        return False
    balance = credits.get("balance")
    if balance is None:
        return True  # The server's explicit availability flag is authoritative.
    if not isinstance(balance, str):
        return False
    try:
        amount = Decimal(balance)
        return amount.is_finite() and amount > 0
    except InvalidOperation:
        return False


def funding_status(snapshot: dict, allow_existing_credits: bool = False) -> dict:
    subscription = subscription_status(snapshot)
    credit = credits_available(snapshot)
    available = subscription == "available" or (allow_existing_credits and credit)
    limit = subscription_limit(snapshot) or {}
    remaining = {}
    for key, name in (("primary", "five_hour"), ("secondary", "weekly")):
        window = limit.get(key)
        value = window.get("usedPercent") if isinstance(window, dict) else None
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            remaining[name] = max(0, 100 - value)
    return {
        "passed": available,
        "minimum_remaining_percent": {},
        "remaining_percent": remaining,
        "subscription_status": subscription,
        "credits_allowed": allow_existing_credits,
        "credits_available": credit,
        "reason": "Subscription or authorized Credits available; no reserve threshold"
        if available
        else "No confirmed usable subscription quota or authorized Credits",
    }


def experiment_start_status(snapshot: dict, allow_existing_credits: bool = False) -> dict:
    return funding_status(snapshot, allow_existing_credits)


def require_experiment_start(snapshot: dict, allow_existing_credits: bool = False) -> dict:
    check = experiment_start_status(snapshot, allow_existing_credits)
    if not check["passed"]:
        raise FundingRequired("New experiment refused: " + check["reason"])
    return check


def require_subscription(snapshot: dict, allow_existing_credits: bool = False) -> None:
    """Legacy function name; validate the configured funding sources, not just quota."""
    check = funding_status(snapshot, allow_existing_credits)
    if not check["passed"]:
        raise FundingRequired(check["reason"] + "; no new inference is permitted")
