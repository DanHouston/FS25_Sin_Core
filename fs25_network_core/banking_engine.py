"""Atomic integer-unit ledger. Adapter callbacks are trusted operator interfaces."""
from datetime import datetime, timezone

from .admin_manager import AdminManager


def amount_units(amount):
    if type(amount) is not int or not 0 < amount <= 1_000_000_000:
        raise ValueError("Amount must be a whole game-currency unit between 1 and 1,000,000,000")
    return amount


class BankingEngine:
    def __init__(self, database):
        self.database = database
        self.db = database.db
        self.admin = AdminManager(database)

    def balance(self, discord_id):
        wallet = self.db.wallets.find_one({"_id": str(discord_id)}) or {}
        return wallet.get("balance", 0)

    def credit_verified_transfer(self, server_id, save_id, event_id, discord_id, source_farm_id, amount, evidence):
        """Only call after adapter/operator verifies sender, destination, and finality.

        event_id must be the source system's immutable transaction identifier.
        Never derive it from a Discord interaction or a balance difference.
        """
        amount_units(amount)
        if not event_id or not evidence:
            raise ValueError("A source transaction ID and verification evidence are required")
        user = str(discord_id)
        key = dict(server_id=server_id, save_id=save_id, event_id=event_id)

        def credit(session):
            existing = self.db.transfers.find_one(key, session=session)
            if existing:
                if (existing["discord_id"], existing["source_farm_id"], existing["amount"]) != (user, source_farm_id, amount):
                    raise ValueError("Transfer ID already used with different details")
                return "already_credited"
            link = self.admin.lookup(user, server_id, save_id, session)
            if link["farm_id"] != source_farm_id:
                raise ValueError("Transfer sender does not match the approved farm")
            self.db.transfers.insert_one(dict(**key, discord_id=user, source_farm_id=source_farm_id,
                                             amount=amount, evidence=evidence, created_at=datetime.now(timezone.utc)), session=session)
            self.db.wallets.update_one({"_id": user}, {"$inc": {"balance": amount}}, upsert=True, session=session)
            return "credited"
        return self.database.atomic(credit)

    def request_withdrawal(self, request_id, discord_id, server_id, save_id, amount):
        amount_units(amount)
        user = str(discord_id)
        if not request_id:
            raise ValueError("A stable request ID is required")

        def reserve(session):
            old = self.db.withdrawals.find_one({"_id": request_id}, session=session)
            if old:
                if (old["discord_id"], old["server_id"], old["save_id"], old["amount"]) != (user, server_id, save_id, amount):
                    raise ValueError("Request ID reused with different details")
                return old["state"]
            link = self.admin.lookup(user, server_id, save_id, session)
            result = self.db.wallets.update_one({"_id": user, "balance": {"$gte": amount}}, {"$inc": {"balance": -amount}}, session=session)
            if result.modified_count != 1:
                raise ValueError("Insufficient available balance")
            self.db.withdrawals.insert_one(dict(_id=request_id, discord_id=user, server_id=server_id,
                                               save_id=save_id, farm_id=link["farm_id"], amount=amount,
                                               state="pending", created_at=datetime.now(timezone.utc)), session=session)
            return "pending"
        return self.database.atomic(reserve)

    def settle_withdrawal(self, request_id, outcome, receipt):
        """Trusted adapter: applied or definitively_not_applied. Unknown stays reserved."""
        if outcome not in ("applied", "definitively_not_applied") or not receipt:
            raise ValueError("A definitive outcome and durable receipt are required")
        state = "completed" if outcome == "applied" else "refunded"

        def settle(session):
            record = self.db.withdrawals.find_one({"_id": request_id}, session=session)
            if not record:
                raise ValueError("Unknown withdrawal")
            if record["state"] != "pending":
                if record["state"] != state:
                    raise ValueError("Conflicting settlement")
                return state
            if state == "refunded":
                self.db.wallets.update_one({"_id": record["discord_id"]}, {"$inc": {"balance": record["amount"]}}, session=session)
            self.db.withdrawals.update_one({"_id": request_id}, {"$set": {"state": state, "receipt": receipt}}, session=session)
            return state
        return self.database.atomic(settle)
