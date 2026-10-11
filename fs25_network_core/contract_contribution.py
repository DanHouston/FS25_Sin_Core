"""Read-only, active-world reports of directly observed FS25 contract work."""

from .native_contracts import contract_key


def _hours(milliseconds):
    return float(milliseconds or 0) / 3_600_000


def _short_id(value):
    value = str(value or "")
    return value[-8:] if value else "unknown"


def format_contract_contribution_report(report):
    contract = report["contract"]
    lifecycle = report["lifecycle"]
    contributions = report["contributions"]
    lines = [f"**Native contract contribution — {contract['mission_type']} · "
             f"Field {contract['field_id'] or 'unknown'}**",
             f"Mission `{contract['mission_id']}` · {contract['area_ha'] or 0:,.2f} ha · "
             f"Farm {contract['accepting_farm_id'] or 'unknown'} · "
             f"Current state: {contract['lifecycle']}"]
    if lifecycle:
        transitions = []
        for row in lifecycle:
            observed = row.get("observed_at")
            timestamp = (f" <t:{int(observed.timestamp())}:R>"
                         if hasattr(observed, "timestamp") else "")
            transitions.append(f"{row['lifecycle']}{timestamp}")
        lines.append("Lifecycle observed: " + " → ".join(transitions))
    else:
        lines.append("Lifecycle history unavailable.")

    total_ha = sum(row["worked_area_ha"] for row in contributions)
    known_area = sum(row["worked_area_ha"] for row in contributions
                     if row["actor_kind"] in {"player", "ai"})
    total_ms = sum(row["productive_ms"] for row in contributions)
    lines.append(f"Measured work intervals: {report['interval_count']} · {total_ha:,.4f} ha · "
                 f"{_hours(total_ms):,.2f} productive h")
    lines.append(f"Attribution coverage: {known_area / total_ha:.1%} of measured worked area"
                 if total_ha else "Attribution coverage: no measured work intervals yet")
    for kind, label in (("player", "Players"), ("ai", "AI"), ("unknown", "Unknown attribution")):
        rows = [row for row in contributions if row["actor_kind"] == kind]
        if not rows:
            continue
        lines.append(f"{label}: {_hours(sum(row['productive_ms'] for row in rows)):,.2f} h · "
                     f"{sum(row['worked_area_ha'] for row in rows):,.4f} ha")
        for row in rows[:8]:
            if kind == "player":
                actor = row["player_name"]
            elif kind == "ai":
                actor = f"AI job {_short_id(row.get('job_id'))}"
            else:
                actor = row.get("attribution_reason") or "operator unavailable"
            tools = f"vehicle …{_short_id(row.get('vehicle_id'))} / implement …{_short_id(row.get('implement_id'))}"
            lines.append(f"- {actor}: {_hours(row['productive_ms']):,.2f} h · "
                         f"{row['worked_area_ha']:,.4f} ha · {row['activity_type']} · {tools} · "
                         f"{row['intervals']} interval(s)")
    payout = contract.get("actual_payout")
    lines.append(f"Actual payout: ${payout:,.2f}" if payout is not None
                 else "Actual payout: unavailable (no contract-linked native payment evidence)")
    lines.append("Only successful FS25 work-area processing is counted. This is observed work, "
                 "not a reconstruction of all contract progress; work without a direct operator "
                 "or mission-location match remains unlinked and is not assigned.")
    return "\n".join(lines)[:1900]


class ContractContributionReportService:
    def __init__(self, database, identity_resolver=None):
        self.db = database.db
        self.identity_resolver = identity_resolver

    def for_contract(self, server_key, save_key, world_id, mission_id):
        if not all(str(value or "").strip() for value in (server_key, save_key, world_id, mission_id)):
            raise ValueError("Server, save, world generation, and mission ID are required")
        identity = contract_key(server_key, save_key, world_id, mission_id)
        contract = self.db.native_contracts.find_one({"_id": identity})
        if not isinstance(contract, dict):
            raise ValueError("No native FS25 contract record exists for this mission in the active world")
        scope = {"server_key": str(server_key), "save_key": str(save_key),
                 "world_id": str(world_id), "mission_id": str(mission_id)}
        lifecycle = list(self.db.native_contract_lifecycle_events.find(scope).sort("observed_at", 1))
        records = list(self.db.farm_operational_events.find({**scope, "kind": "contract_work"})
                       .sort("observed_at", 1).limit(2000))
        grouped = {}
        for row in records:
            actor = row.get("actor_kind") or "unknown"
            activity = row.get("activity_type") or "unknown"
            vehicle = row.get("vehicle_id") or ""
            implement = row.get("implement_id") or ""
            job_id = row.get("job_id") or ""
            reason = row.get("attribution_reason") or ""
            key = (actor, row.get("unique_user_id") or "", activity, vehicle, implement, job_id, reason)
            result = grouped.setdefault(key, {"actor_kind": actor,
                                               "unique_user_id": row.get("unique_user_id"),
                                               "activity_type": activity, "vehicle_id": vehicle,
                                               "implement_id": implement, "job_id": job_id,
                                               "attribution_reason": reason,
                                               "productive_ms": 0,
                                               "worked_area_ha": 0.0, "intervals": 0})
            result["productive_ms"] += int(row.get("productive_ms") or 0)
            result["worked_area_ha"] += float(row.get("worked_area_ha") or 0)
            result["intervals"] += 1
        contributions = list(grouped.values())
        for row in contributions:
            unique_id = row.get("unique_user_id")
            resolved = (self.identity_resolver(server_key, save_key, unique_id)
                        if unique_id and self.identity_resolver else None)
            row["player_name"] = (resolved or {}).get("canonical_name") or (
                f"Player {unique_id[-8:]}" if unique_id else "Unknown")
        return {"contract": contract, "lifecycle": lifecycle,
                "contributions": contributions, "interval_count": len(records)}
