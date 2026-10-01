"""Deterministic arithmetic for the standalone FS25 vehicle pricing policy."""

from __future__ import annotations

from dataclasses import dataclass


class VehiclePricingPolicyError(ValueError):
    """Raised when a descriptor cannot safely receive a policy price."""


@dataclass(frozen=True)
class VehiclePriceDescriptor:
    canonical_id: str
    category: str
    source_price: int
    base_hp: int
    maximum_hp: int
    maximum_engine_surcharge: int
    dollars_per_hp: int
    effective_price: int


def canonical_vehicle_id(mod_name: str, xml_path: str) -> str:
    if not mod_name or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_" for char in mod_name):
        raise VehiclePricingPolicyError("invalid mod name")
    path = xml_path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or ".." in path.split("/") or not path.lower().endswith(".xml"):
        raise VehiclePricingPolicyError("invalid XML path")
    return f"{mod_name}:{path}"


def effective_vehicle_price(base_hp: int, maximum_engine_surcharge: int, dollars_per_hp: int) -> int:
    if any(type(value) is not int or value < 0 for value in (base_hp, maximum_engine_surcharge, dollars_per_hp)):
        raise VehiclePricingPolicyError("price inputs must be non-negative integers")
    if base_hp <= 0 or dollars_per_hp <= 0:
        raise VehiclePricingPolicyError("base HP and rate must be positive")
    return base_hp * dollars_per_hp + maximum_engine_surcharge


def describe_vehicle(mod_name: str, xml_path: str, category: str, source_price: int,
                     base_hp: int, maximum_engine_surcharge: int, rates: dict[str, int], maximum_hp: int | None = None) -> VehiclePriceDescriptor | None:
    if type(source_price) is not int or source_price < 0 or not isinstance(category, str):
        return None
    rate = rates.get(category)
    if type(rate) is not int or rate <= 0:
        return None
    return VehiclePriceDescriptor(
        canonical_id=canonical_vehicle_id(mod_name, xml_path), category=category,
        source_price=source_price, base_hp=base_hp,
        maximum_hp=maximum_hp if type(maximum_hp) is int and maximum_hp > 0 else base_hp,
        maximum_engine_surcharge=maximum_engine_surcharge, dollars_per_hp=rate,
        effective_price=effective_vehicle_price(base_hp, maximum_engine_surcharge, rate),
    )


def normalize_equal_max_hp_prices(descriptors: list[VehiclePriceDescriptor]) -> list[VehiclePriceDescriptor]:
    """Apply the deterministic same-category/same-max-HP mean price rule."""
    groups: dict[tuple[str, int], list[VehiclePriceDescriptor]] = {}
    for descriptor in descriptors:
        groups.setdefault((descriptor.category, descriptor.maximum_hp), []).append(descriptor)
    normalized: list[VehiclePriceDescriptor] = []
    for descriptor in descriptors:
        peers = groups[(descriptor.category, descriptor.maximum_hp)]
        if len(peers) < 2:
            normalized.append(descriptor)
            continue
        mean_price = int(sum(item.effective_price for item in peers) / len(peers) + 0.5)
        normalized.append(VehiclePriceDescriptor(
            canonical_id=descriptor.canonical_id, category=descriptor.category,
            source_price=descriptor.source_price, base_hp=descriptor.base_hp,
            maximum_hp=descriptor.maximum_hp,
            maximum_engine_surcharge=descriptor.maximum_engine_surcharge,
            dollars_per_hp=descriptor.dollars_per_hp, effective_price=mean_price,
        ))
    return normalized
