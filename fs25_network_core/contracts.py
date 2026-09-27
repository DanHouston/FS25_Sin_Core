"""Pure helpers for the read-only FS25 contract diagnostics mod.

The game-side Lua remains authoritative. These helpers only define the
documented work-time calculation used by deterministic tests and tooling.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

DEFAULT_EFFICIENCY = 0.70


def estimate_field_work_hours(area_ha: float, working_width_m: float,
                              working_speed_kmh: float,
                              efficiency: float = DEFAULT_EFFICIENCY) -> float:
    """Estimate field hours from area and offered implement performance."""
    values = (area_ha, working_width_m, working_speed_kmh, efficiency)
    if any(not isinstance(value, (int, float)) for value in values):
        raise ValueError("field estimate inputs must be numeric")
    if area_ha <= 0 or working_width_m <= 0 or working_speed_kmh <= 0:
        raise ValueError("area, width and speed must be positive")
    if not 0 < efficiency <= 1:
        raise ValueError("efficiency must be greater than zero and at most one")
    return area_ha * 3.6 / (working_width_m * working_speed_kmh * efficiency)


def equipment_performance(equipment: Iterable[Mapping[str, Any]]) -> tuple[float, float] | None:
    """Choose the widest and slowest positive offered equipment metrics."""
    widths = [float(item["workingWidthM"]) for item in equipment
              if item.get("workingWidthM") is not None and float(item["workingWidthM"]) > 0]
    speeds = [float(item["workingSpeedKmh"]) for item in equipment
              if item.get("workingSpeedKmh") is not None and float(item["workingSpeedKmh"]) > 0]
    if not widths or not speeds:
        return None
    return max(widths), min(speeds)


def estimate_native_dollars_per_hour(reward: float | None, hours: float | None) -> float | None:
    """Return native reward divided by the diagnostic duration estimate."""
    if reward is None or hours is None or hours <= 0:
        return None
    return float(reward) / float(hours)
