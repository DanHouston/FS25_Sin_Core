"""Pure clock-drift classification shared by tests and policy documentation."""


def signed_drift_minutes(target, game):
    return (float(target) - float(game) + 720) % 1440 - 720


def clock_state(target, game, tolerance):
    drift = signed_drift_minutes(target, game)
    if drift > tolerance:
        return "behind", drift
    if drift < -tolerance:
        return "ahead", drift
    return "synced", drift


def clock_decision(target, game, policy):
    """Return the tier and scale for a validated clock policy."""
    state, drift = clock_state(target, game, policy["tolerance_minutes"])
    if state == "behind" and drift > policy["fast_catchup_threshold_minutes"]:
        return "fast_catchup", drift, policy["fast_catchup_time_scale"]
    if state == "behind":
        return "catchup", drift, policy["catchup_time_scale"]
    if state == "ahead":
        return "ahead", drift, policy["ahead_time_scale"]
    return "synced", drift, policy["normal_time_scale"]


def effective_target_minutes(target, elapsed_seconds):
    """Advance a central minute-of-day target using elapsed real time only."""
    return (float(target) + float(elapsed_seconds) / 60) % 1440
