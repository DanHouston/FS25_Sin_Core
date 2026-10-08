"""Immutable, world-scoped observations of native FS25 farm money changes.

These are game observations, not SiN bank ledger entries.  In particular, a
balance-changing mod that bypasses Mission:addMoney will require reconciliation
against the existing live farm-balance snapshots rather than invented entries.
"""

import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


class FarmFinanceValidationError(ValueError):
    pass


def _finite_decimal(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise FarmFinanceValidationError(f"{field} must be numeric") from None
    if not number.is_finite():
        raise FarmFinanceValidationError(f"{field} must be finite")
    return number


def _optional_game_int(payload, field, minimum, maximum):
    value = payload.get(field)
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise FarmFinanceValidationError(f"{field} must be an integer") from None
    if str(parsed) != str(value) or not minimum <= parsed <= maximum:
        raise FarmFinanceValidationError(f"{field} is invalid")
    return parsed


def validate_finance_change(payload):
    if not isinstance(payload, dict):
        raise FarmFinanceValidationError("finance payload must be an object")
    try:
        farm_id = int(payload.get("farm_id"))
    except (TypeError, ValueError):
        raise FarmFinanceValidationError("farm_id must be an integer") from None
    if not 1 <= farm_id <= 254 or str(farm_id) != str(payload.get("farm_id")):
        raise FarmFinanceValidationError("farm_id is invalid")
    amount = _finite_decimal(payload.get("amount"), "amount")
    before = _finite_decimal(payload.get("balance_before"), "balance_before")
    after = _finite_decimal(payload.get("balance_after"), "balance_after")
    if amount == 0 or abs((after - before) - amount) > Decimal("0.0001"):
        raise FarmFinanceValidationError("finance amount does not match the native balance change")
    money_type = str(payload.get("money_type") or "").strip()
    if not money_type or len(money_type) > 128:
        raise FarmFinanceValidationError("money_type is invalid")
    return {
        "farm_id": farm_id,
        "source_sequence": _optional_game_int(payload, "source_sequence", 1, 2147483647),
        "amount": float(amount),
        "amount_raw": str(amount),
        "balance_before": float(before),
        "balance_before_raw": str(before),
        "balance_after": float(after),
        "balance_after_raw": str(after),
        "money_type": money_type,
        "game_period": _optional_game_int(payload, "game_period", 0, 12),
        "game_day": _optional_game_int(payload, "game_day", 0, 31),
        "game_year": _optional_game_int(payload, "game_year", 0, 10000),
        "game_time_ms": _optional_game_int(payload, "game_time_ms", 0, 86400000),
    }


class FarmFinanceTelemetry:
    def __init__(self, database):
        self.db = database.db

    def ingest(self, server_key, save_key, world_id, event_id, payload):
        if not str(world_id or "").strip():
            raise FarmFinanceValidationError("finance change requires a world identity")
        values = validate_finance_change(payload)
        scope = "|".join((str(server_key), str(save_key), str(world_id), str(event_id)))
        document_id = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        document = {
            "_id": document_id,
            "server_key": str(server_key),
            "save_key": str(save_key),
            "world_id": str(world_id),
            "source_event_id": str(event_id),
            "observed_at": datetime.now(timezone.utc),
            **values,
        }
        self.db.farm_finance_changes.update_one(
            {"_id": document_id}, {"$setOnInsert": document}, upsert=True)
        return document

    def ingest_batch(self, server_key, save_key, world_id, batch_id, payload):
        if not str(world_id or "").strip():
            raise FarmFinanceValidationError("finance batch requires a world identity")
        changes = payload.get("changes") if isinstance(payload, dict) else None
        if not isinstance(changes, list) or not 1 <= len(changes) <= 128:
            raise FarmFinanceValidationError("finance batch must contain 1-128 changes")
        ids = [str(row.get("event_id") or "") if isinstance(row, dict) else "" for row in changes]
        if any(not event_id for event_id in ids) or len(set(ids)) != len(ids):
            raise FarmFinanceValidationError("finance change IDs are missing or duplicated")
        # Reject an invalid batch before persisting any row. The individual
        # upserts then make a partial database failure safe to retry.
        for row in changes:
            validate_finance_change(row)
        return [self.ingest(server_key, save_key, world_id, row["event_id"], row)
                for row in changes]
