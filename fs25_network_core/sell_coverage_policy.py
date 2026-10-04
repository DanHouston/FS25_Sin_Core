"""Deterministic, engine-independent sell-point coverage policy helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

DEFAULT_EXCLUDED = frozenset({
    "WATER", "DIESEL", "DEF", "ELECTRICCHARGE", "METHANE", "AIR",
    "SEEDS", "FERTILIZER", "LIQUIDFERTILIZER", "HERBICIDE", "LIME",
    "ROADSALT",
})
DEFAULT_PRICE_SCALE = 1.0


@dataclass(frozen=True)
class Commodity:
    name: str
    delivery_class: str


@dataclass(frozen=True)
class Station:
    station_id: str
    name: str
    npc: bool
    delivery_classes: frozenset[str]
    accepted: frozenset[str]


def is_eligible(name: str, *, sellable: bool, excluded: Iterable[str] = DEFAULT_EXCLUDED) -> bool:
    return sellable and name.upper() not in {item.upper() for item in excluded}


def needs_assignment(buyer_count: int) -> bool:
    """Coverage is immutable once any valid buyer already exists."""
    return buyer_count == 0


def score_station(commodity: Commodity, station: Station) -> int:
    """Score only compatible NPC stations; deterministic ID handles ties."""
    if not station.npc or commodity.delivery_class not in station.delivery_classes:
        return -1
    accepted = station.accepted
    name = commodity.name.upper()
    score = 1
    if name in {"MILK", "GOATMILK"} and {"MILK", "GOATMILK"} & accepted:
        score += 40
    elif name in {"POTATO", "CARROT", "PARSNIP", "BEETROOT", "SUGARBEET"} and {
        "POTATO", "CARROT", "PARSNIP", "BEETROOT", "SUGARBEET"
    } & accepted:
        score += 30
    elif name in {"PEA", "GREENBEAN", "SPINACH"} and {
        "PEA", "GREENBEAN", "SPINACH", "WHEAT", "BARLEY", "OAT", "MAIZE"
    } & accepted:
        score += 20
    elif name in {"RICE", "RICELONGGRAIN"} and {"WHEAT", "BARLEY", "OAT", "MAIZE", "RICE", "RICELONGGRAIN"} & accepted:
        score += 20
    elif name == "OLIVE" and {"OLIVE", "SUNFLOWER", "CANOLA", "SOYBEAN"} & accepted:
        score += 20
    elif name == "COTTON" and "COTTON" in accepted:
        score += 20
    return score


def choose_station(commodity: Commodity, stations: Iterable[Station], *, override: str | None = None) -> Station | None:
    candidates = [station for station in stations if score_station(commodity, station) >= 0]
    if override is not None:
        return next((station for station in candidates if station.station_id == override), None)
    return min(candidates, key=lambda station: (-score_station(commodity, station), station.station_id)) if candidates else None
