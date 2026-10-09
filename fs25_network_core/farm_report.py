"""Read-only, world-scoped first farm operations and finance report."""

from datetime import datetime, timedelta, timezone

from .admin_manager import AdminManager
from .world_generation import WorldGenerationRegistry


def _money(value):
    return f"-${-value:,.0f}" if value < 0 else f"${value:,.0f}"


def _top(rows, key, count=5):
    return sorted(rows, key=lambda row: (-row[key], row["name"]))[:count]


def _label(value):
    return str(value).replace("\n", " ").replace("\r", " ")[:40]


class FarmReportService:
    def __init__(self, database):
        self.db = database.db
        self.admin = AdminManager(database)
        self.worlds = WorldGenerationRegistry(database)

    def for_manager(self, discord_id, server_key, save_key, world_id, days=7):
        if type(days) is not int or not 1 <= days <= 30:
            raise ValueError("Report window must be 1-30 days")
        active_world = self.worlds.require_active(server_key, save_key, world_id)
        link = self.admin.lookup(discord_id, server_key, save_key, world_id=active_world)
        farm_id = link.get("farm_id")
        if type(farm_id) is not int or not 1 <= farm_id <= 254:
            raise ValueError("verified farm manager assignment is unavailable")
        snapshot = self.db.server_snapshots.find_one({
            "server_key": str(server_key), "save_key": str(save_key),
            "world_id": active_world, "source": "game"}, sort=[("received_at", -1)])
        farms = snapshot.get("farms") if isinstance(snapshot, dict) else None
        if not isinstance(farms, dict) or str(farm_id) not in {str(key) for key in farms}:
            raise ValueError("no authoritative snapshot confirms this farm in the current world")

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        match = {"server_key": str(server_key), "save_key": str(save_key),
                 "world_id": active_world, "farm_id": farm_id,
                 "observed_at": {"$gte": start, "$lte": end}}
        finance = list(self.db.farm_finance_changes.aggregate([
            {"$match": match},
            {"$group": {"_id": "$money_type", "count": {"$sum": 1},
                        "income": {"$sum": {"$cond": [{"$gt": ["$amount", 0]}, "$amount", 0]}},
                        "expense": {"$sum": {"$cond": [{"$lt": ["$amount", 0]},
                                                          {"$multiply": ["$amount", -1]}, 0]}}}},
        ]))
        operations = list(self.db.farm_operational_events.aggregate([
            {"$match": match},
            {"$group": {"_id": {"kind": "$kind", "fill_type": "$fill_type"},
                        "count": {"$sum": 1}, "liters": {"$sum": "$liters"},
                        "duration_ms": {"$sum": "$duration_ms"},
                        "operating_ms": {"$sum": "$operating_ms"},
                        "distance_estimated_m": {"$sum": "$distance_estimated_m"}}},
        ]))
        finance_rows = [{"name": str(row.get("_id") or "UNKNOWN"),
                         "income": float(row.get("income") or 0),
                         "expense": float(row.get("expense") or 0),
                         "count": int(row.get("count") or 0)} for row in finance]
        operation_rows = [{"kind": (row.get("_id") or {}).get("kind"),
                           "name": str((row.get("_id") or {}).get("fill_type") or ""),
                           "count": int(row.get("count") or 0),
                           "liters": float(row.get("liters") or 0),
                           "duration_ms": float(row.get("duration_ms") or 0),
                           "operating_ms": float(row.get("operating_ms") or 0),
                           "distance_estimated_m": float(row.get("distance_estimated_m") or 0)}
                          for row in operations]
        return {"server_key": str(server_key), "save_key": str(save_key),
                "world_id": active_world, "farm_id": farm_id,
                "farm_name": farms.get(str(farm_id), farms.get(farm_id)),
                "start": start, "end": end, "days": days,
                "finance": finance_rows, "operations": operation_rows}


def format_farm_report(report):
    """Bounded Discord text. Never presents physical storage flow as a sale."""
    finance, operations = report["finance"], report["operations"]
    income = sum(row["income"] for row in finance)
    expense = sum(row["expense"] for row in finance)
    by_kind = lambda kind: [row for row in operations if row["kind"] == kind]
    sales = by_kind("sale")
    storage_in, storage_out = by_kind("storage_in"), by_kind("storage_out")
    ai = by_kind("ai_stopped")
    vehicles = by_kind("vehicle_usage")
    lines = [f"**Farm report — {_label(report['farm_name'])}**",
             f"Last {report['days']} real-world day(s) · through <t:{int(report['end'].timestamp())}:f>",
             f"Native finance changes: +{_money(income)} / -{_money(expense)} "
             f"(net {_money(income - expense)})"]
    if finance:
        for label, field in (("Income", "income"), ("Expenses", "expense")):
            leaders = _top([row for row in finance if row[field] > 0], field)
            if leaders:
                lines.append(f"Top {label.lower()} categories: " + ", ".join(
                    f"{_label(row['name'])} {_money(row[field])}" for row in leaders))
    else:
        lines.append("No native finance changes recorded in this window.")
    lines.append(f"NPC crop/product sales: {sum(row['liters'] for row in sales):,.0f} L "
                 f"across {sum(row['count'] for row in sales):,} observed sale event(s)")
    if sales:
        lines.append("Top sold: " + ", ".join(
            f"{_label(row['name'])} {row['liters']:,.0f} L" for row in _top(sales, "liters")))
    lines.append(f"Storage movement: in {sum(row['liters'] for row in storage_in):,.0f} L / "
                 f"out {sum(row['liters'] for row in storage_out):,.0f} L "
                 "(includes production flows; not harvest yield)")
    for label, rows in (("In", storage_in), ("Out", storage_out)):
        if rows:
            lines.append(f"Top storage {label.lower()}: " + ", ".join(
                f"{_label(row['name'])} {row['liters']:,.0f} L" for row in _top(rows, "liters", 3)))
    lines.append(f"AI work observed: {sum(row['duration_ms'] for row in ai) / 3_600_000:,.1f} h "
                 f"across {sum(row['count'] for row in ai):,} completed job(s)")
    lines.append(f"Vehicle use observed: {sum(row['operating_ms'] for row in vehicles) / 3_600_000:,.1f} h; "
                 f"estimated travel {sum(row['distance_estimated_m'] for row in vehicles) / 1000:,.1f} km")
    lines.append("History starts when telemetry was deployed; no backfill. "
                 "Finance excludes balance changes that bypass native Mission:addMoney.")
    return "\n".join(lines)[:1900]
