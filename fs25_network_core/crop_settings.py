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


def _state(value: str, label: str) -> int | str:
    text = str(value or "").strip()
    try:
        result = int(text)
    except (TypeError, ValueError):
        normalized = text.upper()
        if not _NAME.fullmatch(normalized):
            raise CropPolicyError(f"{label} must be a non-negative integer or state name")
        return normalized
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
    transitions: tuple[tuple[int | str, int | str], ...] = ()


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
        # Production policy follows the native FruitTypeDesc XML shape:
        # fruit/growth/seasonal/period.  Keep the shorter seasonal form as a
        # compatibility fixture format for deterministic probes.
        seasonal_node = node.find("./growth/seasonal")
        if seasonal_node is None:
            seasonal_node = node.find("./seasonal")
        period_nodes = () if seasonal_node is None else seasonal_node.findall("./period")
        for period in period_nodes:
            period_name = (period.get("name") or "").strip().upper()
            if period_name not in _PERIOD_NAMES:
                raise CropPolicyError(f"invalid period name: {period_name!r}")
            if period_name in period_seen:
                raise CropPolicyError(f"duplicate period policy: {name}/{period_name}")
            period_seen.add(period_name)
            transitions: list[tuple[int | str, int | str]] = []
            transition_seen: set[tuple[int | str, int | str]] = set()
            updates = period.findall("./update")
            if not updates:
                updates = period.findall("./growth/update")
            for update in updates:
                from_value = update.get("fromState") or update.get("startState") or ""
                to_value = update.get("toState") or update.get("endState") or ""
                pair = (_state(from_value, "fromState/startState"),
                        _state(to_value, "toState/endState"))
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


def _resolve_state(descriptor: dict[str, Any], token: int | str) -> int | None:
    if isinstance(token, int):
        return token
    for mapping in (descriptor.get("growthStateIds"), descriptor.get("nameToGrowthState")):
        if isinstance(mapping, dict):
            for key, value in mapping.items():
                if str(key).strip().upper() == token and isinstance(value, int) and value >= 0:
                    return value
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
        harvest_policy: dict[str, bool] = {}
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
                harvest_policy[period.name] = period.harvest_allowed
                # Native FS25 stores the calendar gate as isHarvestable on
                # each seasonal period.  The older mirror fixtures use
                # harvestableInPeriod or harvestAllowed; retain those
                # fallbacks without weakening native validation.
                if isinstance(runtime.get("isHarvestable"), bool):
                    if runtime["isHarvestable"] != period.harvest_allowed:
                        runtime["isHarvestable"] = period.harvest_allowed
                        fruit_changed = True
                elif isinstance(runtime.get("harvestAllowed"), bool) and runtime["harvestAllowed"] != period.harvest_allowed:
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
                        resolved_from = _resolve_state(descriptor, from_state)
                        resolved_to = _resolve_state(descriptor, to_state)
                        if resolved_from is None or resolved_to is None:
                            result.unsupported += 1
                            result.diagnostics.append(
                                f"{name}/{period.name}: growth state name unresolved"
                            )
                            continue
                        if mapping.get(resolved_from) != resolved_to:
                            mapping[resolved_from] = resolved_to
                            fruit_changed = True
        if harvest_policy:
            # The native FS25 descriptor commonly exposes harvestability as
            # getIsHarvestableInPeriod(), not period.harvestAllowed.  The
            # deterministic mirror represents that method's policy gate with
            # this mapping; maturity/readiness remains outside the policy.
            harvest_api = descriptor.get("harvestableInPeriod")
            native_periods = _periods(descriptor) or {}
            if all(isinstance(native_periods.get(period_name), dict) and
                   isinstance(native_periods[period_name].get("isHarvestable"), bool)
                   for period_name in harvest_policy):
                # Already applied above; this branch documents that native
                # period flags are the authoritative representation.
                pass
            elif isinstance(harvest_api, dict):
                for period_name, allowed in harvest_policy.items():
                    if harvest_api.get(period_name) != allowed:
                        harvest_api[period_name] = allowed
                        fruit_changed = True
            elif callable(descriptor.get("getIsHarvestableInPeriod")):
                descriptor["_sin_crop_harvest_policy"] = dict(harvest_policy)
                fruit_changed = True
            elif not any(isinstance(native_periods.get(p), dict)
                         and isinstance(native_periods.get(p, {}).get("harvestAllowed"), bool)
                         for p in harvest_policy):
                result.unsupported += 1
                result.diagnostics.append(f"{name}: harvest period API unsupported")
        descriptor["_sin_crop_policy_version"] = policy.policy_version
        result.applied += 1
        if fruit_changed:
            result.changed += 1
    return result
