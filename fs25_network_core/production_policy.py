"""Pure, deterministic helpers shared by production-policy validation tests.

The game mod owns runtime discovery and interception.  These helpers make the
identifier and price-policy rules independently testable without FS25.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re


_MOD_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def canonical_production_id(mod_name: str, xml_path: str) -> str:
    """Return ``modName:relative/path.xml`` or fail closed."""
    if not isinstance(mod_name, str) or not _MOD_NAME.fullmatch(mod_name):
        raise ValueError("invalid mod name")
    if not isinstance(xml_path, str):
        raise ValueError("invalid XML path")
    normalized = xml_path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if (not normalized or normalized.startswith("/") or ":" in normalized
            or ".." in normalized.split("/") or not normalized.lower().endswith(".xml")):
        raise ValueError("invalid relative XML path")
    return f"{mod_name}:{normalized}"


def valid_purchase_price(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0 or int(value) != value:
        return None
    return int(value)


@dataclass(frozen=True)
class ProductionDescriptor:
    canonical_id: str
    mod_name: str
    xml_path: str
    source_price: int
    effective_price: int
    recipes: tuple[dict, ...] = ()
    operating_cost: float | None = None


def resolve_effective_price(canonical_id: str, source_price: object,
                            policy: dict[str, dict]) -> int:
    """Resolve exactly once; unknown or invalid overrides retain source."""
    source = valid_purchase_price(source_price)
    if source is None:
        raise ValueError("invalid source price")
    entry = policy.get(canonical_id)
    if not isinstance(entry, dict):
        return source
    override = valid_purchase_price(entry.get("purchasePrice"))
    return source if override is None else override


def describe_production(mod_name: str, xml_path: str, source_price: object,
                        policy: dict[str, dict], recipes: tuple[dict, ...] = (),
                        operating_cost: float | None = None) -> ProductionDescriptor:
    canonical_id = canonical_production_id(mod_name, xml_path)
    source = valid_purchase_price(source_price)
    if source is None:
        raise ValueError("invalid source price")
    return ProductionDescriptor(
        canonical_id=canonical_id,
        mod_name=mod_name,
        xml_path=xml_path.replace("\\", "/"),
        source_price=source,
        effective_price=resolve_effective_price(canonical_id, source, policy),
        recipes=recipes,
        operating_cost=operating_cost,
    )
