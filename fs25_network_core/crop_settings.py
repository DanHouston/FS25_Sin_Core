"""Deterministic parser/probe for the standalone SiN FS25 crop policy.

The runtime implementation lives in the FS25 Lua mod.  This small, engine-
independent mirror keeps the policy schema and its fail-closed behavior
testable in CI without pretending to emulate GIANTS' runtime objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from defusedxml import ElementTree


class CropPolicyError(ValueError):
    """The external crop policy is malformed or unsafe."""


_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PERIOD_NAMES = {
    "EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
    "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
    "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER",
}
_PERIOD_ORDER = (
    "EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
    "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
    "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER",
)


def normalize_fruit_name(value: str) -> str:
    """Return the stable runtime fruit key or reject it."""
    normalized = str(value or "").strip().upper()
    if not _NAME.fullmatch(normalized):
        raise CropPolicyError(f"invalid fruit name: {value!r}")
    return normalized


def _boolean(value: str | None, *, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    text = value.strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise CropPolicyError(f"invalid boolean value: {value!r}")


def _state(value: str, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise CropPolicyError(f"{label} must be an integer") from error
    if result < 0:
        raise CropPolicyError(f"{label} must be non-negative")
    return result


def _growth_time(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CropPolicyError("growthTime must be numeric") from error
    if result <= 0:
        raise CropPolicyError("growthTime must be positive")
    return result


@dataclass(frozen=True)
class PeriodPolicy:
    name: str
    planting_allowed: bool | None = None
    harvest_allowed: bool | None = None
    growth_time: float | None = None
    transitions: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class FruitPolicy:
    name: str
    enabled: bool
    periods: tuple[PeriodPolicy, ...] = ()


@dataclass(frozen=True)
class CropPolicy:
    schema_version: int
    policy_version: str
    fruits: tuple[FruitPolicy, ...]


@dataclass
class ApplyResult:
    applied: int = 0
    skipped: int = 0
    conflicts: int = 0
    unsupported: int = 0
    changed: int = 0
    diagnostics: list[str] = field(default_factory=list)


def parse_policy(source: str | bytes | Path) -> CropPolicy:
    """Parse the versioned XML policy and reject ambiguous entries."""
    try:
        if isinstance(source, Path):
            root = ElementTree.parse(source).getroot()
        else:
            root = ElementTree.fromstring(source)
    except (OSError, ElementTree.ParseError, ValueError) as error:
        raise CropPolicyError("invalid crop policy XML") from error
    if root.tag != "cropPolicy":
        raise CropPolicyError("crop policy root must be cropPolicy")
    try:
        schema = int(root.get("schemaVersion", ""))
    except ValueError as error:
        raise CropPolicyError("schemaVersion must be an integer") from error
    if schema != 1:
        raise CropPolicyError(f"unsupported crop policy schema: {schema}")
    policy_version = (root.get("policyVersion") or "").strip()
    if not policy_version:
        raise CropPolicyError("policyVersion is required")
    fruits: list[FruitPolicy] = []
    seen: set[str] = set()
    for node in root.findall("./fruits/fruit"):
        name = normalize_fruit_name(node.get("name", ""))
        if name in seen:
            raise CropPolicyError(f"duplicate fruit policy: {name}")
        seen.add(name)
        enabled = _boolean(node.get("enabled"), default=False)
        periods: list[PeriodPolicy] = []
        period_seen: set[str] = set()
        for period in node.findall("./seasonal/period"):
            period_name = (period.get("name") or "").strip().upper()
            if period_name not in _PERIOD_NAMES:
                raise CropPolicyError(f"invalid period name: {period_name!r}")
            if period_name in period_seen:
                raise CropPolicyError(f"duplicate period policy: {name}/{period_name}")
            period_seen.add(period_name)
            transitions: list[tuple[int, int]] = []
            transition_seen: set[tuple[int, int]] = set()
            for update in period.findall("./growth/update"):
                pair = (_state(update.get("fromState", ""), "fromState"),
                        _state(update.get("toState", ""), "toState"))
                if pair in transition_seen:
                    raise CropPolicyError(f"duplicate transition policy: {name}/{period_name}")
                transition_seen.add(pair)
                transitions.append(pair)
            periods.append(PeriodPolicy(
                period_name,
                _boolean(period.get("plantingAllowed")),
                _boolean(period.get("harvestAllowed")),
                _growth_time(period.get("growthTime")),
                tuple(transitions),
            ))
        fruits.append(FruitPolicy(name, bool(enabled), tuple(periods)))
    return CropPolicy(schema, policy_version, tuple(fruits))


def _periods(descriptor: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    seasonal = descriptor.get("growthDataSeasonal")
    if not isinstance(seasonal, dict):
        return None
    raw = seasonal.get("periods")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {_PERIOD_ORDER[index]: value for index, value in enumerate(raw)
                if index < len(_PERIOD_ORDER) and isinstance(value, dict)}
    return None


def apply_policy(policy: CropPolicy, descriptors: list[dict[str, Any]]) -> ApplyResult:
    """Apply a policy to test descriptors, preserving fail-closed semantics."""
    result = ApplyResult()
    wanted = {entry.name: entry for entry in policy.fruits if entry.enabled}
    for descriptor in descriptors:
        name = normalize_fruit_name(descriptor.get("name", ""))
        entry = wanted.get(name)
        if entry is None:
            continue
        if descriptor.get("_sin_crop_policy_version") == policy.policy_version:
            result.skipped += 1
            result.diagnostics.append(f"{name}: already applied")
            continue
        period_map = _periods(descriptor)
        if period_map is None:
            result.unsupported += 1
            result.diagnostics.append(f"{name}: seasonal descriptor unsupported")
            continue
        fruit_changed = False
        for period in entry.periods:
            runtime = period_map.get(period.name) or period_map.get(period.name.lower())
            if not isinstance(runtime, dict):
                result.skipped += 1
                result.diagnostics.append(f"{name}/{period.name}: period absent")
                continue
            if period.planting_allowed is not None:
                if not isinstance(runtime.get("plantingAllowed"), bool):
                    result.unsupported += 1
                    result.diagnostics.append(f"{name}/{period.name}: planting field unsupported")
                elif runtime["plantingAllowed"] != period.planting_allowed:
                    runtime["plantingAllowed"] = period.planting_allowed
                    fruit_changed = True
            if period.harvest_allowed is not None:
                if not isinstance(runtime.get("harvestAllowed"), bool):
                    result.unsupported += 1
                    result.diagnostics.append(f"{name}/{period.name}: harvest field unsupported")
                elif runtime["harvestAllowed"] != period.harvest_allowed:
                    runtime["harvestAllowed"] = period.harvest_allowed
                    fruit_changed = True
            if period.growth_time is not None:
                if not isinstance(runtime.get("growthTime"), (int, float)):
                    result.unsupported += 1
                    result.diagnostics.append(f"{name}/{period.name}: growthTime field unsupported")
                elif runtime["growthTime"] != period.growth_time:
                    runtime["growthTime"] = period.growth_time
                    fruit_changed = True
            if period.transitions:
                mapping = runtime.get("growthMapping")
                if not isinstance(mapping, dict):
                    result.unsupported += 1
                    result.diagnostics.append(f"{name}/{period.name}: growthMapping unsupported")
                else:
                    for from_state, to_state in period.transitions:
                        if mapping.get(from_state) != to_state:
                            mapping[from_state] = to_state
                            fruit_changed = True
        descriptor["_sin_crop_policy_version"] = policy.policy_version
        result.applied += 1
        if fruit_changed:
            result.changed += 1
    return result
