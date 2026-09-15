"""Atomic integer-unit ledger. Adapter callbacks are trusted operator interfaces."""
from datetime import datetime, timezone
import hashlib

from .admin_manager import AdminManager
from pymongo.errors import DuplicateKeyError


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

    def account_summary(self, discord_id):
        """Return central wallet state without pretending it is game money."""
        user_id = str(discord_id)
        wallet = self.db.wallets.find_one({"_id": user_id}) or {}
        deposits = list(self.db.deposit_requests.find({"discord_id": user_id, "state": "pending"}))
        withdrawals = list(self.db.withdrawals.find({"discord_id": user_id, "state": "pending"}))
        return {
            "available_balance": int(wallet.get("balance", 0) or 0),
            "pending_deposits": sum(int(row.get("amount", 0) or 0) for row in deposits),
            "pending_withdrawals": sum(int(row.get("amount", 0) or 0) for row in withdrawals),
            "game_balance": None,
        }

    @staticmethod
    def transaction_id(*parts):
        return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()

    def _ledger(self, transaction_id, actor_id, amount, transaction_type, reason, reference=None,
                server_id=None, save_id=None, session=None):
        """Append one immutable ledger row exactly once."""
        if type(amount) is not int or amount == 0:
            raise ValueError("Ledger amount must be a non-zero whole currency unit")
        row = {"_id": str(transaction_id), "transaction_id": str(transaction_id),
               "actor_discord_id": str(actor_id), "amount": amount,
               "transaction_type": str(transaction_type), "reason": str(reason),
               "reference": reference, "server_id": server_id, "save_id": save_id,
               "created_at": datetime.now(timezone.utc)}
        try:
            self.db.ledger_entries.insert_one(row, session=session)
            return True
        except DuplicateKeyError:
            existing = self.db.ledger_entries.find_one({"_id": str(transaction_id)}, session=session)
            if not existing or (existing.get("amount"), existing.get("actor_discord_id"),
                                existing.get("transaction_type")) != (
                                    amount, str(actor_id), str(transaction_type)):
                raise ValueError("Ledger transaction ID was reused with different details") from None
            return False

    def transfer_wallet(self, transaction_id, payer_id, payee_id, amount, reason, session=None):
        """Transfer projected wallet funds exactly once through the ledger.

        This is used by invoices and other central-only value transfers.  It
        deliberately does not imply a game-world mutation; those use the
        durable operation/receipt path instead.
        """
        amount_units(amount)
        transaction_id = str(transaction_id)
        payer_id, payee_id = str(payer_id), str(payee_id)
        if payer_id == payee_id:
            raise ValueError("Payer and payee must be different")
        reason = str(reason or "wallet transfer")

        def transfer(session):
            existing = self.db.wallet_transfers.find_one({"_id": transaction_id}, session=session)
            if existing:
                expected = (existing.get("payer_id"), existing.get("payee_id"), existing.get("amount"))
                if expected != (payer_id, payee_id, amount):
                    raise ValueError("Wallet transaction ID was reused with different details")
                return existing.get("state", "completed")

            result = self.db.wallets.update_one(
                {"_id": payer_id, "balance": {"$gte": amount}},
                {"$inc": {"balance": -amount}}, session=session)
            if result.modified_count != 1:
                raise ValueError("Insufficient available balance")
            self._ledger(self.transaction_id("wallet-debit", transaction_id), payer_id, -amount,
                         "wallet_transfer_debit", reason, reference=transaction_id, session=session)
            self._ledger(self.transaction_id("wallet-credit", transaction_id), payee_id, amount,
                         "wallet_transfer_credit", reason, reference=transaction_id, session=session)
            self._project_wallet(payee_id, amount, session=session)
            self.db.wallet_transfers.insert_one({
                "_id": transaction_id, "transaction_id": transaction_id,
                "payer_id": payer_id, "payee_id": payee_id, "amount": amount,
                "reason": reason, "state": "completed", "created_at": datetime.now(timezone.utc)},
                session=session)
            return "completed"

        return transfer(session) if session is not None else self.database.atomic(transfer)

    def _project_wallet(self, discord_id, amount, session=None):
        self.db.wallets.update_one({"_id": str(discord_id)}, {"$inc": {"balance": amount}},
                                   upsert=True, session=session)

    def credit_verified_transfer(self, server_id, save_id, event_id, discord_id, source_farm_id, amount, evidence,
                                 session=None):
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
            transaction_id = self.transaction_id("deposit", server_id, save_id, event_id)
            inserted = self._ledger(transaction_id, user, amount, "deposit", "verified game transfer",
                                    reference=event_id, server_id=server_id, save_id=save_id, session=session)
            self.db.transfers.insert_one(dict(**key, transfer_id=transaction_id, kind="deposit",
                                             discord_id=user, source_farm_id=source_farm_id,
                                             amount=amount, evidence=evidence, created_at=datetime.now(timezone.utc)), session=session)
            if inserted:
                self._project_wallet(user, amount, session=session)
            return "credited"
        return credit(session) if session is not None else self.database.atomic(credit)

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
            transaction_id = self.transaction_id("withdrawal", request_id)
            result = self.db.wallets.update_one({"_id": user, "balance": {"$gte": amount}}, {"$inc": {"balance": -amount}}, session=session)
            if result.modified_count != 1:
                raise ValueError("Insufficient available balance")
            operation_id = self.transaction_id("withdrawal-operation", request_id)
            self._ledger(transaction_id, user, -amount, "withdrawal_reservation", "reserved for game delivery",
                         reference=request_id, server_id=server_id, save_id=save_id, session=session)
            now = datetime.now(timezone.utc)
            self.db.farm_operations.update_one(
                {"_id": operation_id},
                {"$setOnInsert": {"_id": operation_id, "operation_id": operation_id,
                                   "operation_type": "withdraw_funds", "server_key": server_id,
                                   "save_key": save_id, "payload": {"withdrawal_id": request_id,
                                   "farm_id": link["farm_id"], "amount": amount}, "state": "pending",
                                   "attempts": 0, "created_at": now},
                 "$set": {"updated_at": now}}, upsert=True, session=session)
            self.db.withdrawals.insert_one(dict(_id=request_id, discord_id=user, server_id=server_id,
                                               save_id=save_id, farm_id=link["farm_id"], amount=amount,
                                               operation_id=operation_id, state="pending", created_at=now), session=session)
            return "pending"
        return self.database.atomic(reserve)

    def request_deposit(self, request_id, discord_id, server_id, save_id, amount):
        """Queue a game-to-SiN deposit; no central credit occurs before receipt."""
        amount_units(amount)
        user = str(discord_id)
        if not request_id:
            raise ValueError("A stable request ID is required")

        def queue(session):
            old = self.db.deposit_requests.find_one({"_id": request_id}, session=session)
            if old:
                if (old["discord_id"], old["server_id"], old["save_id"], old["amount"]) != (user, server_id, save_id, amount):
                    raise ValueError("Deposit request ID reused with different details")
                return old["state"]
            link = self.admin.lookup(user, server_id, save_id, session)
            operation_id = self.transaction_id("deposit-operation", request_id)
            now = datetime.now(timezone.utc)
            self.db.farm_operations.update_one(
                {"_id": operation_id},
                {"$setOnInsert": {"_id": operation_id, "operation_id": operation_id,
                                   "operation_type": "deposit_funds", "server_key": server_id,
                                   "save_key": save_id, "payload": {"deposit_id": request_id,
                                   "farm_id": link["farm_id"], "amount": amount}, "state": "pending",
                                   "attempts": 0, "created_at": now},
                 "$set": {"updated_at": now}}, upsert=True, session=session)
            self.db.deposit_requests.insert_one({"_id": request_id, "deposit_id": request_id,
                "discord_id": user, "server_id": server_id, "save_id": save_id,
                "farm_id": link["farm_id"], "amount": amount, "operation_id": operation_id,
                "state": "pending", "created_at": now}, session=session)
            return "pending"
        return self.database.atomic(queue)

    def settle_deposit(self, request_id, outcome, receipt, server_id=None, save_id=None):
        if outcome not in ("applied", "already_applied") or not receipt:
            raise ValueError("A definitive deposit outcome and durable receipt are required")
        if not isinstance(receipt, dict):
            raise ValueError("A durable deposit receipt object is required")

        def settle(session):
            record = self.db.deposit_requests.find_one({"_id": request_id}, session=session)
            if not record:
                raise ValueError("Unknown deposit")
            if receipt.get("operation_id") != record.get("operation_id"):
                raise ValueError("Deposit receipt does not match the queued operation")
            if server_id is not None and (record.get("server_id") != server_id or record.get("save_id") != save_id):
                raise ValueError("Deposit is outside the authenticated server/save scope")
            if record["state"] == "completed":
                return "completed"
            if record["state"] != "pending":
                raise ValueError("Deposit is not awaiting settlement")
            event_id = receipt.get("source_event_id") or request_id
            evidence = receipt.get("receipt") or receipt
            # Use the existing verified-transfer projection so the immutable
            # transfer/ledger idempotency rules remain the single credit path.
            self.credit_verified_transfer(record["server_id"], record["save_id"], event_id,
                                          record["discord_id"], record["farm_id"], record["amount"], evidence,
                                          session=session)
            result = self.db.deposit_requests.update_one(
                {"_id": request_id, "state": "pending"},
                {"$set": {"state": "completed", "receipt": receipt,
                          "completed_at": datetime.now(timezone.utc)}}, session=session)
            if result.modified_count != 1:
                raise ValueError("Deposit changed before its receipt could be committed")
            operation = self.db.farm_operations.update_one(
                {"_id": record["operation_id"], "operation_type": "deposit_funds",
                 "state": {"$in": ["pending", "dispatched"]}},
                {"$set": {"state": "succeeded", "receipt": receipt,
                          "updated_at": datetime.now(timezone.utc)}}, session=session)
            if operation.modified_count != 1:
                raise ValueError("Deposit operation changed before its receipt could be committed")
            return "completed"
        return self.database.atomic(settle)

    def settle_withdrawal(self, request_id, outcome, receipt, server_id=None, save_id=None):
        """Trusted adapter: applied or definitively_not_applied. Unknown stays reserved."""
        if outcome not in ("applied", "definitively_not_applied") or not receipt:
            raise ValueError("A definitive outcome and durable receipt are required")
        if not isinstance(receipt, dict):
            raise ValueError("A durable withdrawal receipt object is required")
        state = "completed" if outcome == "applied" else "refunded"

        def settle(session):
            record = self.db.withdrawals.find_one({"_id": request_id}, session=session)
            if not record:
                raise ValueError("Unknown withdrawal")
            if receipt.get("operation_id") != record.get("operation_id"):
                raise ValueError("Withdrawal receipt does not match the queued operation")
            if server_id is not None and (record.get("server_id") != server_id or record.get("save_id") != save_id):
                raise ValueError("Withdrawal is outside the authenticated server/save scope")
            if record["state"] != "pending":
                if record["state"] != state:
                    raise ValueError("Conflicting settlement")
                return state
            if state == "refunded":
                transaction_id = self.transaction_id("withdrawal-refund", request_id)
                if self._ledger(transaction_id, record["discord_id"], record["amount"], "withdrawal_refund",
                                "game withdrawal definitively not applied", reference=request_id,
                                server_id=record.get("server_id"), save_id=record.get("save_id"), session=session):
                    self._project_wallet(record["discord_id"], record["amount"], session=session)
            result = self.db.withdrawals.update_one(
                {"_id": request_id, "state": "pending"},
                {"$set": {"state": state, "receipt": receipt,
                          "completed_at": datetime.now(timezone.utc)}}, session=session)
            if result.modified_count != 1:
                raise ValueError("Withdrawal changed before its receipt could be committed")
            operation = self.db.farm_operations.update_one(
                {"_id": record["operation_id"], "operation_type": "withdraw_funds",
                 "state": {"$in": ["pending", "dispatched"]}},
                {"$set": {"state": "succeeded" if state == "completed" else "failed",
                          "receipt": receipt, "updated_at": datetime.now(timezone.utc)}}, session=session)
            if operation.modified_count != 1:
                raise ValueError("Withdrawal operation changed before its receipt could be committed")
            return state
        return self.database.atomic(settle)
