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


def _period_list(value: str | None) -> set[str]:
    if not value:
        return set()
    names = set()
    for token in value.split(","):
        name = token.strip().upper()
        if name not in _PERIOD_NAMES:
            raise CropPolicyError(f"invalid period name: {name!r}")
        names.add(name)
    return names


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
    lifecycle: str | None = None
    state_chain: tuple[str, ...] = ()
    preserve_native: bool = False


@dataclass(frozen=True)
class CropPolicy:
    schema_version: int
    policy_version: str
    fruits: tuple[FruitPolicy, ...]
    default_planting_allowed: bool | None = None
    default_harvest_allowed: bool | None = None


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
    default_planting = _boolean(root.get("defaultPlantingAllowed"))
    default_harvest = _boolean(root.get("defaultHarvestAllowed"))
    fruits: list[FruitPolicy] = []
    seen: set[str] = set()
    for node in root.findall("./fruits/fruit"):
        name = normalize_fruit_name(node.get("name", ""))
        if name in seen:
            raise CropPolicyError(f"duplicate fruit policy: {name}")
        seen.add(name)
        enabled = _boolean(node.get("enabled"), default=False)
        planting_periods = _period_list(node.get("plantPeriods"))
        harvest_periods = _period_list(node.get("harvestPeriods"))
        growth_node = node.find("./growth")
        lifecycle = None
        state_chain: tuple[str, ...] = ()
        preserve_native = False
        if growth_node is not None:
            lifecycle_value = (growth_node.get("lifecycle") or "").strip().upper()
            lifecycle = lifecycle_value or None
            chain_value = (growth_node.get("stateChain") or "").strip()
            if chain_value:
                state_chain = tuple(normalize_fruit_name(token) for token in chain_value.split(","))
            preserve_native = _boolean(growth_node.get("preserveNative"), default=False) is True
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
        if (default_planting is not None or default_harvest is not None or
                planting_periods or harvest_periods):
            explicit = {period.name: period for period in periods}
            periods = []
            for period_name in _PERIOD_ORDER:
                prior = explicit.get(period_name)
                periods.append(PeriodPolicy(
                    period_name,
                    (prior.planting_allowed if prior and prior.planting_allowed is not None
                     else period_name in planting_periods or default_planting),
                    (prior.harvest_allowed if prior and prior.harvest_allowed is not None
                     else period_name in harvest_periods or default_harvest),
                    prior.growth_time if prior else None,
                    prior.transitions if prior else (),
                ))
        fruits.append(FruitPolicy(name, bool(enabled), tuple(periods), lifecycle,
                                  state_chain, preserve_native))
    return CropPolicy(schema, policy_version, tuple(fruits), default_planting, default_harvest)


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


def _annual_lifecycle_plan(entry: FruitPolicy, period_map: dict[str, dict[str, Any]],
                           descriptor: dict[str, Any], result: ApplyResult) -> dict[str, dict[int, int]] | None:
    """Validate and build annual mappings without mutating the descriptor."""
    if entry.lifecycle != "ANNUAL" or not entry.state_chain:
        return {}
    planting = {period.name for period in entry.periods if period.planting_allowed is True}
    harvest = {period.name for period in entry.periods if period.harvest_allowed is True}
    if not planting or not harvest:
        result.unsupported += 1
        result.diagnostics.append(f"{entry.name}: annual lifecycle windows unsupported")
        return None
    states = [_resolve_state(descriptor, token) for token in entry.state_chain]
    invisible = _resolve_state(descriptor, "INVISIBLE")
    dead = _resolve_state(descriptor, "DEAD")
    if invisible is None or dead is None or any(state is None for state in states) or len(states) < 2:
        result.unsupported += 1
        result.diagnostics.append(f"{entry.name}: annual lifecycle state chain unsupported")
        return None
    first_plant = min(_PERIOD_ORDER.index(name) + 1 for name in planting)
    harvest_indices = [_PERIOD_ORDER.index(name) + 1 for name in harvest]
    offsets = [((index - first_plant) % 12) for index in harvest_indices]
    last_harvest_offset = max(offsets)
    runtimes: dict[str, dict[str, Any]] = {}
    for period_name in _PERIOD_ORDER:
        runtime = period_map.get(period_name) or period_map.get(period_name.lower())
        if not isinstance(runtime, dict):
            result.unsupported += 1
            result.diagnostics.append(f"{entry.name}/{period_name}: period unavailable")
            return None
        if not isinstance(runtime.get("growthMapping"), dict):
            result.unsupported += 1
            result.diagnostics.append(f"{entry.name}/{period_name}: growthMapping unsupported")
            return None
        runtimes[period_name] = runtime
    replacements: dict[str, dict[int, int]] = {}
    for index, period_name in enumerate(_PERIOD_ORDER, start=1):
        offset = (index - first_plant) % 12
        replacement: dict[int, int] = {}
        if period_name in planting:
            replacement[invisible] = states[0]
        if offset <= last_harvest_offset:
            for state_index in range(len(states) - 2):
                replacement[states[state_index]] = states[state_index + 1]
            if period_name in harvest:
                replacement[states[-2]] = states[-1]
                replacement[states[-1]] = states[-1]
        elif offset == last_harvest_offset + 1:
            replacement[states[-1]] = dead
        replacements[period_name] = replacement
    return replacements


def _apply_annual_lifecycle(entry: FruitPolicy, period_map: dict[str, dict[str, Any]],
                            descriptor: dict[str, Any], result: ApplyResult,
                            plan: dict[str, dict[int, int]] | None = None) -> bool:
    """Apply a prevalidated annual state-chain plan."""
    if plan is None:
        plan = _annual_lifecycle_plan(entry, period_map, descriptor, result)
    if plan is None:
        return False
    changed = False
    for period_name, replacement in plan.items():
        runtime = period_map.get(period_name) or period_map.get(period_name.lower())
        mapping = runtime["growthMapping"]
        if mapping != replacement:
            mapping.clear()
            mapping.update(replacement)
            changed = True
    return changed


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
        annual_plan = _annual_lifecycle_plan(entry, period_map, descriptor, result)
        if entry.lifecycle == "ANNUAL" and entry.state_chain and annual_plan is None:
            # Do not partially apply period gates when the complete annual
            # descriptor cannot be validated.
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
        if _apply_annual_lifecycle(entry, period_map, descriptor, result, annual_plan):
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
