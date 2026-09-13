"""Validation and wall-clock projection for save-scoped clock policies."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS = {"enabled": False, "timezone": "UTC", "offset_minutes": 0,
            "normal_time_scale": 1.0, "catchup_time_scale": 15.0,
            "fast_catchup_threshold_minutes": 60.0, "fast_catchup_time_scale": 360.0,
            "ahead_time_scale": 0.0,
            "tolerance_minutes": 2.0, "hard_resync_threshold_minutes": 180.0,
            "hard_resync_enabled": False, "check_interval_seconds": 60}


def validate_policy(policy):
    value = dict(DEFAULTS, **(policy or {}))
    if not isinstance(value["enabled"], bool):
        raise ValueError("enabled must be boolean")
    if not isinstance(value["timezone"], str) or not value["timezone"].strip():
        raise ValueError("timezone is required")
    try:
        ZoneInfo(value["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone must be a valid ZoneInfo identifier") from None
    if type(value["offset_minutes"]) not in (int, float) or not -1440 <= value["offset_minutes"] <= 1440:
        raise ValueError("offset_minutes must be between -1440 and 1440")
    for name in ("normal_time_scale", "catchup_time_scale"):
        if type(value[name]) not in (int, float) or not 0.1 <= value[name] <= 120:
            raise ValueError(f"{name} must be between 0.1 and 120")
    if value["catchup_time_scale"] < value["normal_time_scale"]:
        raise ValueError("catchup_time_scale must be at least normal_time_scale")
    if type(value["tolerance_minutes"]) not in (int, float) or type(value["fast_catchup_threshold_minutes"]) not in (int, float) or not value["tolerance_minutes"] < value["fast_catchup_threshold_minutes"] <= 720:
        raise ValueError("fast_catchup_threshold_minutes must exceed tolerance and be at most 720")
    if type(value["fast_catchup_time_scale"]) not in (int, float) or not value["catchup_time_scale"] < value["fast_catchup_time_scale"] <= 1200:
        raise ValueError("fast_catchup_time_scale must exceed catchup_time_scale and be at most 1200")
    if type(value["ahead_time_scale"]) not in (int, float) or not 0 <= value["ahead_time_scale"] < value["normal_time_scale"]:
        raise ValueError("ahead_time_scale must be at least 0 and below normal_time_scale")
    if type(value["tolerance_minutes"]) not in (int, float) or not 0 < value["tolerance_minutes"] <= 1440:
        raise ValueError("tolerance_minutes must be between 0 and 1440")
    if type(value["hard_resync_threshold_minutes"]) not in (int, float) or not value["tolerance_minutes"] < value["hard_resync_threshold_minutes"] <= 10080:
        raise ValueError("hard_resync_threshold_minutes must exceed tolerance and be at most 10080")
    if not isinstance(value["hard_resync_enabled"], bool):
        raise ValueError("hard_resync_enabled must be boolean")
    if type(value["check_interval_seconds"]) is not int or not 5 <= value["check_interval_seconds"] <= 3600:
        raise ValueError("check_interval_seconds must be between 5 and 3600")
    return value


def target_game_minutes(policy, now=None):
    value = validate_policy(policy)
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(ZoneInfo(value["timezone"]))
    return int((local.hour * 60 + local.minute + value["offset_minutes"]) % 1440)
