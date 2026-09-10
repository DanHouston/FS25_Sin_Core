"""Deterministic speed targets; applying them requires an in-game adapter."""
import math


def speed_multiplier(active_players, factory_count, target_factories_per_player=0.25):
    if type(active_players) is not int or type(factory_count) is not int:
        raise ValueError("Counts must be integers")
    if active_players < 0 or factory_count < 0:
        raise ValueError("Counts cannot be negative")
    if not math.isfinite(target_factories_per_player) or target_factories_per_player <= 0:
        raise ValueError("Target density must be finite and positive")
    if not active_players or not factory_count:
        return 1.0
    return round(max(1.0, min(3.0, active_players * target_factories_per_player / factory_count)), 3)


def targets(active_players, factory_counts, target_factories_per_player=0.25):
    return {category: speed_multiplier(active_players, count, target_factories_per_player)
            for category, count in factory_counts.items()}
