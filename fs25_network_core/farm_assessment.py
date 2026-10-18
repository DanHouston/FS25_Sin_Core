"""Read-only estimates for a possible farm closure; never settle or queue it."""

import csv
import io
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from .world_generation import WorldGenerationRegistry


KINDS = ("equipment", "building", "land", "stock")
LABELS = {"equipment": "Equipment", "building": "Buildings", "land": "Land",
          "stock": "Harvested stock / products", "excluded": "Excluded"}
MAX_AGE_SECONDS = 120


def _decimal(value, *, negative=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        if hasattr(value, "to_decimal"):
            value = value.to_decimal()
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and abs(number) <= Decimal("1e18") and (negative or number >= 0) else None


def _date(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _label(value, limit=160):
    text = str(value or "").replace("\r", " ").replace("\n", " ")[:limit]
    return re.sub(r"([\\`*_\[\]<>|~])", r"\\\1", text).replace("@", "@\u200b")


def _money(value):
    if value is None:
        return "unavailable"
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


class FarmAssessmentService:
    def __init__(self, database):
        self.db = database.db
        self.worlds = WorldGenerationRegistry(database)

    def for_admin(self, server_key, save_key, world_id, farm_id):
        active = self.worlds.require_active(server_key, save_key, world_id)
        if type(farm_id) is not int or not 1 <= farm_id <= 254:
            raise ValueError("Choose a farm from the selected server")
        scope = {"server_key": str(server_key), "save_key": str(save_key), "world_id": active}
        snapshot = self.db.server_snapshots.find_one({**scope, "source": "game"}, sort=[("received_at", -1)])
        farms = snapshot.get("farms") if isinstance(snapshot, dict) else None
        if not isinstance(farms, dict) or str(farm_id) not in {str(key) for key in farms}:
            raise ValueError("No game snapshot confirms this farm in the active world")
        farm_name = farms.get(str(farm_id), farms.get(farm_id))
        issues = []
        now = datetime.now(timezone.utc)
        observed = _date(snapshot.get("farm_assessment_observed_at"))
        received = _date(snapshot.get("received_at"))
        for title, value in (("Assessment inventory", observed), ("Game snapshot", received)):
            age = (now - value).total_seconds() if value is not None else None
            if age is None or age < -30 or age > MAX_AGE_SECONDS:
                issues.append(f"{title} is missing or older than two minutes")

        data = snapshot.get("farm_assessments")
        data = data.get(str(farm_id), data.get(farm_id)) if isinstance(data, dict) else None
        ready = snapshot.get("farm_assessment_ready") is True and isinstance(data, dict) and data.get("schema_version") == 1
        if not ready:
            issues.append("Detailed assessment unavailable; deploy the assessment mod and Agent and wait for a fresh snapshot")
            if snapshot.get("farm_assessment_error"):
                issues.append(str(snapshot["farm_assessment_error"])[:400])
            data = {}
        inventory_issues = data.get("issues")
        if not isinstance(inventory_issues, list):
            issues.append("Assessment coverage information unavailable")
        else:
            issues.extend(str(item)[:400] for item in inventory_issues[:1000])
        cash = _decimal(data.get("cash"), negative=True)
        if cash is None:
            balances = snapshot.get("farm_balances")
            cash = _decimal(balances.get(str(farm_id), balances.get(farm_id)), negative=True) if isinstance(balances, dict) else None
        loan = _decimal(data.get("loan"))
        if cash is None:
            issues.append("Farm cash unavailable")
        if loan is None:
            issues.append("Native farm loan unavailable")

        rows, seen = [], set()
        assets = data.get("assets")
        if not isinstance(assets, list) or len(assets) > 10000:
            assets = []
            issues.append("Itemized asset inventory unavailable or exceeds the limit")
        for raw in assets:
            if not isinstance(raw, dict) or raw.get("kind") not in (*KINDS, "excluded"):
                issues.append("An asset row has an invalid category")
                continue
            row = {key: str(raw.get(key) or "")[:400] for key in
                   ("kind", "id", "name", "location", "unit", "basis", "note")}
            identity = (row["kind"], row["id"])
            if not row["id"] or identity in seen:
                issues.append("Duplicate or unidentified asset requires review")
                continue
            seen.add(identity)
            row.update(value=_decimal(raw.get("value")), quantity=_decimal(raw.get("quantity")),
                       unit_price=_decimal(raw.get("unitPrice")))
            if row["kind"] == "stock" and row["value"] is not None:
                if row["quantity"] is None or row["unit_price"] is None:
                    row["value"] = None
                elif abs(row["quantity"] * row["unit_price"] - row["value"]) > Decimal("0.01"):
                    row["value"] = None
                    issues.append("A stock valuation does not match its quantity and unit price")
            if row["kind"] == "excluded":
                row["value"] = Decimal(0)
            elif row["value"] is None:
                issues.append(f"No valuation for {row['name'] or row['id']}")
            rows.append(row)

        totals = {kind: sum((row["value"] for row in rows if row["kind"] == kind and row["value"] is not None), Decimal(0))
                  for kind in KINDS}
        # Older snapshots can still show useful known asset subtotals, but are
        # explicitly incomplete and can never yield a proposed payout.
        if not ready:
            old_assets = snapshot.get("farm_asset_values")
            old_assets = old_assets.get(str(farm_id), old_assets.get(farm_id)) if isinstance(old_assets, dict) else {}
            for kind, old_key in (("equipment", "vehicles"), ("building", "structures")):
                record = old_assets.get(old_key) if isinstance(old_assets, dict) else None
                value = _decimal(record.get("sell_value")) if isinstance(record, dict) else None
                if value is not None:
                    totals[kind] = value
                    rows.append({"kind": kind, "name": "Known aggregate; itemization pending", "value": value})
            ownership, prices = snapshot.get("farmlands"), snapshot.get("farmland_prices")
            if isinstance(ownership, dict) and isinstance(prices, dict):
                for land_id, owner in ownership.items():
                    if str(owner) == str(farm_id):
                        value = _decimal(prices.get(str(land_id), prices.get(land_id)))
                        if value is not None:
                            totals["land"] += value
                            rows.append({"kind": "land", "name": f"Farmland {land_id}", "value": value})

        mappings = [row for row in self.db.sin_farms.find({**scope, "state": "active"})
                    if str(row.get("fs25_farm_id")) == str(farm_id)]
        recipient = None
        if len(mappings) == 1 and mappings[0].get("farm_type") == "member":
            value = str(mappings[0].get("owner_discord_id") or "")
            recipient = value if value.isdigit() else None
        if recipient is None:
            issues.append("A single canonical member-farm owner could not be established")
        wallet_balance = None
        recipient_name = None
        if recipient is not None:
            application = self.db.community_applications.find_one({"_id": recipient}) or {}
            recipient_name = application.get("server_nickname") or application.get("nickname") or recipient
            wallet = self.db.wallets.find_one({"_id": recipient}) or {}
            wallet_balance = _decimal(wallet.get("balance", 0), negative=True)
            if wallet_balance is None:
                issues.append("Recipient's existing SiN account balance is unreadable")

        pending = []
        relevant_types = {"deposit_funds", "withdraw_funds", "vehicle_transfer", "assign_farmland", "provision_farm"}
        for operation in self.db.farm_operations.find({**scope, "state": {"$nin": ["succeeded", "failed", "superseded", "cancelled"]}}):
            payload = operation.get("payload") or {}
            if not isinstance(payload, dict):
                issues.append("An unsettled operation has unreadable asset scope")
                continue
            if operation.get("operation_type") in relevant_types and any(
                    str(payload.get(key)) == str(farm_id) for key in ("farm_id", "source_farm_id", "destination_farm_id")):
                pending.append(str(operation.get("_id")))
        if pending:
            issues.append(f"{len(pending)} unsettled cash or asset operation(s) require reconciliation")
        active_contracts = list(self.db.native_contracts.find({
            **scope, "accepting_farm_id": {"$in": [str(farm_id), farm_id]},
            "lifecycle": {"$in": ["accepted", "started"]}}))
        if active_contracts:
            issues.append(f"{len(active_contracts)} active native contract(s) may carry completion or cancellation liabilities")

        asset_total = sum(totals.values(), Decimal(0))
        net = cash + asset_total - loan if cash is not None and loan is not None else None
        issues = list(dict.fromkeys(issues))
        complete = ready and not issues
        credit = max(Decimal(0), net).quantize(Decimal(1), rounding=ROUND_FLOOR) if complete and net is not None else None
        return {**scope, "farm_id": farm_id, "farm_name": farm_name, "snapshot_at": received,
                "observed_at": observed, "cash": cash, "loan": loan, "totals": totals,
                "asset_total": asset_total, "net_equity": net, "proposed_credit": credit,
                "recipient": recipient, "recipient_name": recipient_name, "wallet_balance": wallet_balance,
                "projected_wallet_balance": wallet_balance + credit if wallet_balance is not None and credit is not None else None,
                "debt_shortfall": max(Decimal(0), -net) if net is not None else None,
                "complete": complete, "rows": rows, "issues": issues,
                "estimated_rows": sum("estimate" in row.get("basis", "") for row in rows),
                "pending_operations": pending, "active_contracts": active_contracts}


def format_farm_assessment(report):
    lines = [f"**Farm closure assessment — {_label(report['farm_name'], 80)}**",
             "Read-only estimate · harvested stock only",
             f"Server: {_label(report.get('server_name') or report['server_key'], 60)} · Farm {report['farm_id']}",
             f"Farm cash: {_money(report['cash'])}"]
    for kind in KINDS:
        count = sum(row["kind"] == kind for row in report["rows"])
        heading = LABELS[kind] if report["complete"] else "Known " + LABELS[kind].lower()
        lines.append(f"{heading} ({count} rows): {_money(report['totals'][kind])}")
    lines.extend([f"Farm loan to deduct: {_money(report['loan'])}",
                  f"{'Estimated net equity' if report['complete'] else 'Known net (incomplete)'}: {_money(report['net_equity'])}",
                  f"**Proposed SiN account credit: {_money(report['proposed_credit'])}**"])
    if report["recipient"]:
        lines.append(f"Recipient: {_label(report['recipient_name'], 70)}")
        lines.append(f"Existing SiN balance: {_money(report['wallet_balance'])}")
        lines.append(f"Balance after proposed credit: {_money(report['projected_wallet_balance'])}")
    if report["debt_shortfall"]:
        lines.append(f"Uncovered farm debt: {_money(report['debt_shortfall'])}")
    if report["issues"]:
        lines.append("**Incomplete — resolve before using a final settlement figure:**")
        lines.extend(f"• {_label(message, 140)}" for message in report["issues"][:4])
        if len(report["issues"]) > 4:
            lines.append(f"• {len(report['issues']) - 4} further issue(s) in the attachment")
    if report["estimated_rows"]:
        lines.append(f"{report['estimated_rows']} item(s) use a labeled fallback estimate.")
    if report["observed_at"]:
        lines.append(f"Inventory observed: {report['observed_at'].isoformat(timespec='seconds')}")
    lines.append("Full inventory and valuation basis attached. Prices can change; whole-unit credit rounds down.")
    lines.append("Standing crops, forestry/ground heaps, operating fuel and sale/delivery fees are excluded. No assets sold, money credited, or farm closed.")
    return "\n".join(lines)[:1950]


def assessment_csv(report):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    def write(*values):
        safe = []
        for value in values:
            text = "" if value is None else str(value)
            if not isinstance(value, (int, Decimal)) and text.lstrip().startswith(("=", "+", "-", "@", "\t")):
                text = "'" + text
            safe.append(text)
        writer.writerow(safe)
    write("Farm closure assessment", report["farm_name"], "Farm ID", report["farm_id"])
    write("Server", report["server_key"], "Save", report["save_key"], "World", report["world_id"])
    write("Inventory observed", report["observed_at"], "Read-only; harvested stock only")
    write("Recipient account", report["recipient"])
    for name, value in (("Farm cash", report["cash"]), ("Known asset subtotal", report["asset_total"]),
                        ("Farm loan", report["loan"]), ("Net equity", report["net_equity"]),
                        ("Proposed credit", report["proposed_credit"]), ("Existing SiN balance", report["wallet_balance"]),
                        ("Projected SiN balance", report["projected_wallet_balance"])):
        write(name, value)
    write()
    write("Category", "Asset", "Location", "Quantity", "Unit", "Unit price", "Value", "Valuation basis", "Notes", "Asset ID")
    for row in report["rows"]:
        write(LABELS[row["kind"]], row.get("name"), row.get("location"), row.get("quantity"), row.get("unit"),
              row.get("unit_price"), row.get("value"), row.get("basis"), row.get("note"), row.get("id"))
    write()
    for message in report["issues"]:
        write("Unresolved", message)
    for operation in report["pending_operations"]:
        write("Unsettled operation", operation)
    for contract in report["active_contracts"]:
        write("Active native contract", contract.get("mission_id") or contract.get("_id"),
              contract.get("field_id"), contract.get("lifecycle"))
    write("Exclusions", "Standing crops; forestry and ground heaps; operating fuel; sale/delivery fees")
    return stream.getvalue().encode("utf-8-sig")
