"""Atomic integer-unit ledger. Adapter callbacks are trusted operator interfaces."""
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re

from .admin_manager import AdminManager
from .world_generation import WorldGenerationRegistry
from pymongo.errors import DuplicateKeyError


def amount_units(amount):
    if type(amount) is not int or not 0 < amount <= 1_000_000_000:
        raise ValueError("Amount must be a whole game-currency unit between 1 and 1,000,000,000")
    return amount


class BankingEngine:
    # A snapshot uploaded just after settlement can still contain a game
    # export made before the money command. After this grace period, a
    # different balance may represent legitimate later farm activity.
    BALANCE_SNAPSHOT_GRACE = timedelta(seconds=45)

    def __init__(self, database):
        self.database = database
        self.db = database.db
        self.admin = AdminManager(database)
        self.worlds = WorldGenerationRegistry(database)

    def _world_id(self, server_id, save_id, world_id):
        """Require the active generation whenever this touches FS25 cash."""
        active = self.worlds.active_id(server_id, save_id)
        if active:
            return self.worlds.require_active(server_id, save_id, world_id)
        return None

    def balance(self, discord_id):
        wallet = self.db.wallets.find_one({"_id": str(discord_id)}) or {}
        return wallet.get("balance", 0)

    def _authoritative_game_balance(self, discord_id, server_id, save_id, world_id=None):
        """Read one user's native FS25 farm balance from the current snapshot.

        The snapshot is the only central-side source accepted here.  We first
        require the active, mod-confirmed manager relationship, then scope the
        read to the active world generation and the exact farm in that
        relationship.  Wallet activity and pending operations are never used
        as a balance proxy.
        """
        snapshot, farm_id = self._authoritative_farm_snapshot(
            discord_id, server_id, save_id, world_id)
        if snapshot is None:
            return None
        balances = snapshot.get("farm_balances")
        if not isinstance(balances, dict):
            return None
        return self._money_value(balances.get(str(farm_id), balances.get(farm_id)),
                                 allow_negative=True)

    def _authoritative_farm_snapshot(self, discord_id, server_id, save_id, world_id=None):
        """Return only the active world's latest game snapshot for this manager."""
        active_world = self._world_id(server_id, save_id, world_id)
        link = self.admin.lookup(str(discord_id), server_id, save_id, world_id=active_world)
        query = {"server_key": str(server_id), "save_key": str(save_id), "source": "game"}
        if active_world:
            query["world_id"] = str(active_world)
        snapshot = self.db.server_snapshots.find_one(query, sort=[("received_at", -1)])
        if not isinstance(snapshot, dict):
            return None, None
        farms = snapshot.get("farms")
        farm_id = link.get("farm_id")
        if not isinstance(farms, dict) or (str(farm_id) not in farms and farm_id not in farms):
            return None, None
        return snapshot, farm_id

    @staticmethod
    def _money_value(raw, *, allow_negative=False):
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or (not allow_negative and value < 0):
            return None
        return value

    def equity_summary(self, discord_id, server_id=None, save_id=None, world_id=None):
        """Read one manager's checking and live farm resale values, never estimates."""
        result = {"checking_balance": int(self.balance(discord_id) or 0),
                  "game_balance": None, "land_value": None,
                  "structure_value": None, "vehicle_value": None,
                  "structure_fallback_count": 0,
                  "total": None, "farm_name": None, "snapshot_at": None,
                  "unavailable": {"total": "one or more game values are unavailable"}}
        if not server_id or not save_id:
            result["unavailable"].update({name: "no game context selected" for name in
                                          ("game_balance", "land_value", "structure_value", "vehicle_value")})
            return result
        try:
            snapshot, farm_id = self._authoritative_farm_snapshot(
                discord_id, server_id, save_id, world_id)
        except ValueError:
            snapshot, farm_id = None, None
        if snapshot is None:
            result["unavailable"].update({name: "no current, verified farm snapshot" for name in
                                          ("game_balance", "land_value", "structure_value", "vehicle_value")})
            return result

        received_at = snapshot.get("received_at")
        if not isinstance(received_at, datetime):
            result["unavailable"].update({name: "game snapshot timestamp unavailable" for name in
                                          ("game_balance", "land_value", "structure_value", "vehicle_value")})
            return result
        observed_at = received_at.replace(tzinfo=timezone.utc) if received_at.tzinfo is None else received_at
        result["snapshot_at"] = observed_at
        if (datetime.now(timezone.utc) - observed_at).total_seconds() > 120:
            result["unavailable"].update({name: "game snapshot is older than two minutes" for name in
                                          ("game_balance", "land_value", "structure_value", "vehicle_value")})
            return result
        result["farm_name"] = (snapshot["farms"].get(str(farm_id),
                               snapshot["farms"].get(farm_id)))
        balances = snapshot.get("farm_balances")
        result["game_balance"] = self._money_value(
            balances.get(str(farm_id), balances.get(farm_id)) if isinstance(balances, dict) else None,
            allow_negative=True)
        if result["game_balance"] is None:
            result["unavailable"]["game_balance"] = "native farm balance unavailable"

        ownership = snapshot.get("farmlands")
        prices = snapshot.get("farmland_prices")
        if (snapshot.get("farmland_price_source_ready") is True
                and isinstance(ownership, dict) and isinstance(prices, dict)):
            prices_by_id = {str(land_id): price for land_id, price in prices.items()}
            owned_ids = [str(land_id) for land_id, owner in ownership.items()
                         if str(owner) == str(farm_id)]
            values = [self._money_value(prices_by_id.get(land_id)) for land_id in owned_ids]
            if all(value is not None for value in values):
                result["land_value"] = sum(values)
            else:
                result["unavailable"]["land_value"] = "one or more owned parcels lack a native price"
        else:
            result["unavailable"]["land_value"] = "native farmland prices unavailable"

        assets = snapshot.get("farm_asset_values")
        farm_assets = (assets.get(str(farm_id), assets.get(farm_id))
                       if isinstance(assets, dict) else None)
        for kind, result_key in (("structures", "structure_value"), ("vehicles", "vehicle_value")):
            record = farm_assets.get(kind) if isinstance(farm_assets, dict) else None
            value = self._money_value(record.get("sell_value")) if isinstance(record, dict) else None
            count = record.get("count") if isinstance(record, dict) else None
            unpriced = record.get("unpriced") if isinstance(record, dict) else None
            fallback = record.get("fallback", 0) if isinstance(record, dict) else 0
            if (value is not None and type(count) is int and type(unpriced) is int
                    and type(fallback) is int and count >= 0 and 0 <= unpriced <= count
                    and 0 <= fallback <= count - unpriced):
                if unpriced == 0:
                    result[result_key] = value
                    if kind == "structures":
                        result["structure_fallback_count"] = fallback
                else:
                    result["unavailable"][result_key] = f"{unpriced} of {count} assets lack a native sell price"
            else:
                result["unavailable"][result_key] = "native resale valuation unavailable"

        parts = ("checking_balance", "game_balance", "land_value", "structure_value", "vehicle_value")
        if all(result[name] is not None for name in parts):
            result["total"] = sum(result[name] for name in parts)
        else:
            result["unavailable"]["total"] = "one or more game values are unavailable"
        return result

    def account_summary(self, discord_id, server_id=None, save_id=None, world_id=None):
        """Return central wallet state and, when proven, native FS25 money."""
        user_id = str(discord_id)
        wallet = self.db.wallets.find_one({"_id": user_id}) or {}
        deposits = list(self.db.deposit_requests.find({"discord_id": user_id, "state": "pending"}))
        withdrawals = list(self.db.withdrawals.find({"discord_id": user_id, "state": "pending"}))
        game_balance = None
        game_balance_reason = "no game context selected"
        last_verified_game_balance = None
        if server_id and save_id:
            game_balance_reason = "no active, mod-confirmed farm manager mapping"
            mapping_verified = False
            try:
                snapshot, farm_id = self._authoritative_farm_snapshot(
                    user_id, server_id, save_id, world_id=world_id)
                mapping_verified = True
            except ValueError:
                snapshot, farm_id = None, None
            if mapping_verified:
                balances = snapshot.get("farm_balances") if snapshot is not None else None
                if isinstance(balances, dict):
                    game_balance = self._money_value(
                        balances.get(str(farm_id), balances.get(farm_id)), allow_negative=True)
                if game_balance is None:
                    game_balance_reason = "the current FS25 snapshot has no authoritative balance for this farm"
                else:
                    game_balance_reason = None
                    transfer = self._latest_verified_money_transfer(
                        user_id, server_id, save_id, world_id or snapshot.get("world_id"), farm_id)
                    if transfer is not None:
                        after_balance, completed_at = transfer
                        snapshot_at = self._utc_datetime(snapshot.get("received_at"))
                        if (abs(game_balance - after_balance) >= 0.01
                                and (snapshot_at is None
                                     or snapshot_at <= completed_at + self.BALANCE_SNAPSHOT_GRACE)):
                            game_balance = None
                            game_balance_reason = "updating after recent transfer"
                            last_verified_game_balance = after_balance
        result = {
            "available_balance": int(wallet.get("balance", 0) or 0),
            "pending_deposits": sum(int(row.get("amount", 0) or 0) for row in deposits),
            "pending_withdrawals": sum(int(row.get("amount", 0) or 0) for row in withdrawals),
            "game_balance": game_balance,
            "game_balance_reason": game_balance_reason,
        }
        if last_verified_game_balance is not None:
            result["last_verified_game_balance"] = last_verified_game_balance
        return result

    @staticmethod
    def _utc_datetime(value):
        if not isinstance(value, datetime):
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def _latest_verified_money_transfer(self, discord_id, server_id, save_id, world_id, farm_id):
        """Find the most recent receipt for this manager's current farm/world."""
        farm_ids = [farm_id]
        if str(farm_id) != farm_id:
            farm_ids.append(str(farm_id))
        else:
            try:
                farm_ids.append(int(farm_id))
            except (TypeError, ValueError):
                pass
        query = {"discord_id": str(discord_id), "server_id": str(server_id),
                 "save_id": str(save_id), "farm_id": {"$in": farm_ids},
                 "state": "completed"}
        if world_id:
            query["world_id"] = str(world_id)
        candidates = []
        for collection in (self.db.deposit_requests, self.db.withdrawals):
            record = collection.find_one(query, sort=[("completed_at", -1)])
            if not isinstance(record, dict):
                continue
            receipt = record.get("receipt")
            completed_at = self._utc_datetime(record.get("completed_at"))
            after_balance = self._money_value(
                receipt.get("after_balance") if isinstance(receipt, dict) else None,
                allow_negative=True)
            if (completed_at is not None and after_balance is not None
                    and self._receipt_bool(receipt, "authoritative_readback")):
                candidates.append((completed_at, after_balance))
        if not candidates:
            return None
        completed_at, after_balance = max(candidates, key=lambda item: item[0])
        return after_balance, completed_at

    def recent_operation_status(self, discord_id, server_id, save_id, world_id=None):
        """Show the last request when its ephemeral reply is no longer editable."""
        query = {"discord_id": str(discord_id), "server_id": str(server_id),
                 "save_id": str(save_id)}
        active_world = self._world_id(server_id, save_id, world_id)
        if active_world:
            query["world_id"] = str(active_world)
        result = {}
        for kind, collection in (("deposit", self.db.deposit_requests),
                                 ("withdrawal", self.db.withdrawals)):
            record = collection.find_one(query, sort=[("created_at", -1)])
            if isinstance(record, dict):
                result[kind] = {"state": record.get("state"), "amount": int(record.get("amount", 0))}
        return result

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
                expected = (existing.get("payer_id"), existing.get("payee_id"),
                            existing.get("amount"), existing.get("reason"))
                if expected != (payer_id, payee_id, amount, reason):
                    raise ValueError("Wallet transaction ID was reused with different details")
                if existing.get("state") != "completed":
                    raise ValueError("Existing wallet transfer is not complete")
                return "completed"

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

    def pay_player(self, payment_id, payer_id, payee_id, amount, memo):
        """Pay another linked player using only central SiN wallet funds."""
        amount_units(amount)
        payment_id = str(payment_id or "").strip()
        payer_id, payee_id = str(payer_id), str(payee_id)
        memo = str(memo or "").strip()
        if not payment_id:
            raise ValueError("A stable payment ID is required")
        if payer_id == payee_id:
            raise ValueError("You cannot pay yourself")
        if not memo or len(memo) > 200 or re.search(r"[\x00-\x1f\x7f]", memo):
            raise ValueError("Memo must contain 1-200 characters without control characters")

        def pay(session):
            for discord_id, label in ((payer_id, "Your"), (payee_id, "Recipient's")):
                if not self.db.game_identities.find_one({"discord_id": discord_id}, session=session):
                    raise ValueError(f"{label} Discord account is not linked to a Farming Simulator player")
            transfer_id = f"player-pay:{payment_id}"
            is_new = not self.db.wallet_transfers.find_one({"_id": transfer_id}, session=session)
            recipient_before = self.db.wallets.find_one({"_id": payee_id}, session=session) if is_new else None
            previous_balance = (recipient_before or {}).get("balance", 0)
            if is_new and type(previous_balance) is not int:
                raise ValueError("Recipient account balance could not be verified")
            state = self.transfer_wallet(
                transfer_id, payer_id, payee_id, amount,
                f"player payment: {memo}", session=session)
            if state != "completed":
                raise ValueError("Payment did not complete")
            recipient_after = self.db.wallets.find_one({"_id": payee_id}, session=session)
            recipient_balance = (recipient_after or {}).get("balance")
            if (type(recipient_balance) is not int or
                    (is_new and recipient_balance != previous_balance + amount)):
                raise ValueError("Recipient credit could not be verified; contact an operator before retrying")
            wallet = self.db.wallets.find_one({"_id": payer_id}, session=session)
            available_balance = (wallet or {}).get("balance")
            if type(available_balance) is not int:
                raise ValueError("Sender balance could not be verified; contact an operator before retrying")
            return {"state": state, "new": is_new, "recipient_verified": True,
                    "payment_reference": payment_id, "available_balance": available_balance}

        return self.database.atomic(pay)

    def admin_pay_player(self, payment_id, admin_id, payee_id, amount, memo):
        """Issue one audited, system-funded credit to a linked player's wallet."""
        amount_units(amount)
        payment_id = str(payment_id or "").strip()
        admin_id, payee_id = str(admin_id), str(payee_id)
        memo = str(memo or "").strip()
        if not payment_id:
            raise ValueError("A stable payment ID is required")
        if not admin_id or not payee_id:
            raise ValueError("Administrator and recipient IDs are required")
        if admin_id == payee_id:
            raise ValueError("Administrators cannot issue payments to themselves")
        if not memo or len(memo) > 200 or re.search(r"[\x00-\x1f\x7f]", memo):
            raise ValueError("Memo must contain 1-200 characters without control characters")

        def credit(session):
            if not self.db.game_identities.find_one({"discord_id": payee_id}, session=session):
                raise ValueError("Recipient's Discord account is not linked to a Farming Simulator player")
            reference = f"admin-pay:{payment_id}"
            existing = self.db.admin_payments.find_one({"_id": reference}, session=session)
            if existing is not None:
                expected = (existing.get("admin_id"), existing.get("payee_id"),
                            existing.get("amount"), existing.get("memo"))
                if expected != (admin_id, payee_id, amount, memo):
                    raise ValueError("Admin payment ID was reused with different details")
                if existing.get("state") != "completed":
                    raise ValueError("Existing admin payment is not complete")
                return {"state": "completed", "new": False, "recipient_verified": True,
                        "payment_reference": payment_id}

            recipient_before = self.db.wallets.find_one({"_id": payee_id}, session=session) or {}
            previous_balance = recipient_before.get("balance", 0)
            if type(previous_balance) is not int:
                raise ValueError("Recipient account balance could not be verified")
            inserted = self._ledger(
                self.transaction_id("admin-pay", payment_id), payee_id, amount,
                "admin_payment_credit", f"admin payment by {admin_id}: {memo}",
                reference=reference, session=session)
            if not inserted:
                raise ValueError("Admin payment ledger already exists without a completed payment")
            self._project_wallet(payee_id, amount, session=session)
            recipient_after = self.db.wallets.find_one({"_id": payee_id}, session=session) or {}
            new_balance = recipient_after.get("balance")
            if type(new_balance) is not int or new_balance != previous_balance + amount:
                raise ValueError("Recipient credit could not be verified; contact an operator before retrying")
            self.db.admin_payments.insert_one({
                "_id": reference, "payment_id": payment_id, "admin_id": admin_id,
                "payee_id": payee_id, "amount": amount, "memo": memo,
                "state": "completed", "created_at": datetime.now(timezone.utc)}, session=session)
            return {"state": "completed", "new": True, "recipient_verified": True,
                    "payment_reference": payment_id}

        return self.database.atomic(credit)

    def _project_wallet(self, discord_id, amount, session=None):
        self.db.wallets.update_one({"_id": str(discord_id)}, {"$inc": {"balance": amount}},
                                   upsert=True, session=session)

    def credit_verified_transfer(self, server_id, save_id, event_id, discord_id, source_farm_id, amount, evidence,
                                 session=None, world_id=None):
        """Only call after adapter/operator verifies sender, destination, and finality.

        event_id must be the source system's immutable transaction identifier.
        Never derive it from a Discord interaction or a balance difference.
        """
        amount_units(amount)
        world_id = self._world_id(server_id, save_id, world_id)
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
            link = self.admin.lookup(user, server_id, save_id, session, world_id)
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

    def request_withdrawal(self, request_id, discord_id, server_id, save_id, amount, world_id=None):
        amount_units(amount)
        world_id = self._world_id(server_id, save_id, world_id)
        user = str(discord_id)
        if not request_id:
            raise ValueError("A stable request ID is required")

        def reserve(session):
            old = self.db.withdrawals.find_one({"_id": request_id}, session=session)
            if old:
                if (old["discord_id"], old["server_id"], old["save_id"], old["amount"]) != (user, server_id, save_id, amount):
                    raise ValueError("Request ID reused with different details")
                return old["state"]
            link = self.admin.lookup(user, server_id, save_id, session, world_id)
            transaction_id = self.transaction_id("withdrawal", request_id)
            result = self.db.wallets.update_one({"_id": user, "balance": {"$gte": amount}}, {"$inc": {"balance": -amount}}, session=session)
            if result.modified_count != 1:
                raise ValueError("Insufficient available balance")
            operation_id = self.transaction_id("withdrawal-operation", world_id or "legacy", request_id)
            self._ledger(transaction_id, user, -amount, "withdrawal_reservation", "reserved for game delivery",
                         reference=request_id, server_id=server_id, save_id=save_id, session=session)
            now = datetime.now(timezone.utc)
            self.db.farm_operations.update_one(
                {"_id": operation_id},
                {"$setOnInsert": {"_id": operation_id, "operation_id": operation_id,
                                   "operation_type": "withdraw_funds", "server_key": server_id,
                                    "save_key": save_id, **({"world_id": world_id} if world_id else {}), "payload": {"withdrawal_id": request_id,
                                   "farm_id": link["farm_id"], "amount": amount}, "state": "pending",
                                   "attempts": 0, "created_at": now},
                 "$set": {"updated_at": now}}, upsert=True, session=session)
            self.db.withdrawals.insert_one(dict(_id=request_id, discord_id=user, server_id=server_id,
                                                save_id=save_id, farm_id=link["farm_id"], amount=amount,
                                                **({"world_id": world_id} if world_id else {}),
                                               operation_id=operation_id, state="pending", created_at=now,
                                               notification_state="pending"), session=session)
            return "pending"
        return self.database.atomic(reserve)

    def request_deposit(self, request_id, discord_id, server_id, save_id, amount, world_id=None):
        """Queue a game-to-SiN deposit; no central credit occurs before receipt."""
        amount_units(amount)
        world_id = self._world_id(server_id, save_id, world_id)
        user = str(discord_id)
        if not request_id:
            raise ValueError("A stable request ID is required")

        def queue(session):
            old = self.db.deposit_requests.find_one({"_id": request_id}, session=session)
            if old:
                if (old["discord_id"], old["server_id"], old["save_id"], old["amount"]) != (user, server_id, save_id, amount):
                    raise ValueError("Deposit request ID reused with different details")
                return old["state"]
            link = self.admin.lookup(user, server_id, save_id, session, world_id)
            operation_id = self.transaction_id("deposit-operation", world_id or "legacy", request_id)
            now = datetime.now(timezone.utc)
            self.db.farm_operations.update_one(
                {"_id": operation_id},
                {"$setOnInsert": {"_id": operation_id, "operation_id": operation_id,
                                   "operation_type": "deposit_funds", "server_key": server_id,
                                    "save_key": save_id, **({"world_id": world_id} if world_id else {}), "payload": {"deposit_id": request_id,
                                   "farm_id": link["farm_id"], "amount": amount}, "state": "pending",
                                   "attempts": 0, "created_at": now},
                 "$set": {"updated_at": now}}, upsert=True, session=session)
            self.db.deposit_requests.insert_one({"_id": request_id, "deposit_id": request_id,
                "discord_id": user, "server_id": server_id, "save_id": save_id,
                **({"world_id": world_id} if world_id else {}),
                "farm_id": link["farm_id"], "amount": amount, "operation_id": operation_id,
                "state": "pending", "created_at": now,
                "notification_state": "pending"}, session=session)
            return "pending"
        return self.database.atomic(queue)

    @staticmethod
    def _receipt_bool(receipt, field):
        value = receipt.get(field)
        if value is True or value == 1:
            return True
        return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}

    @classmethod
    def _validate_money_receipt(cls, record, receipt, operation_type, outcome, request_id):
        """Require native FS25 balance evidence before settling the wallet.

        XML attributes arrive from the Agent as strings.  A successful receipt
        must identify the exact queued operation/farm/amount and prove that the
        native mutation was performed and read back.  A definitive failure must
        explicitly prove that no mutation was performed; uncertain receipts
        remain pending and are never projected into the wallet.
        """
        if receipt.get("operation_type") != operation_type:
            raise ValueError("Money receipt operation type does not match queued operation")
        if str(receipt.get("farm_id")) != str(record.get("farm_id")):
            raise ValueError("Money receipt farm does not match queued operation")
        try:
            receipt_amount = int(float(receipt.get("amount")))
        except (TypeError, ValueError):
            raise ValueError("Money receipt amount is invalid") from None
        if receipt_amount != int(record.get("amount")):
            raise ValueError("Money receipt amount does not match queued operation")
        expected_request_field = "deposit_id" if operation_type == "deposit_funds" else "withdrawal_id"
        if str(receipt.get(expected_request_field)) != str(request_id):
            raise ValueError("Money receipt request ID does not match queued operation")
        if outcome in {"applied", "already_applied"}:
            if not cls._receipt_bool(receipt, "mutation_performed"):
                raise ValueError("Successful money receipt lacks mutation evidence")
            if not cls._receipt_bool(receipt, "authoritative_readback"):
                raise ValueError("Successful money receipt lacks authoritative readback")
            if operation_type == "deposit_funds" and not receipt.get("source_event_id"):
                raise ValueError("Successful deposit receipt lacks source event evidence")
            try:
                before = float(receipt["before_balance"])
                after = float(receipt["after_balance"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Successful money receipt lacks balance readback") from None
            expected_delta = -receipt_amount if operation_type == "deposit_funds" else receipt_amount
            if abs((after - before) - expected_delta) >= 0.01:
                raise ValueError("Money receipt balance delta does not match requested amount")
        elif outcome == "definitively_not_applied":
            if cls._receipt_bool(receipt, "mutation_performed") or cls._receipt_bool(receipt, "authoritative_readback"):
                raise ValueError("Definitive money failure contains mutation evidence")
        else:
            raise ValueError("Money receipt outcome is not definitive")

    def settle_deposit(self, request_id, outcome, receipt, server_id=None, save_id=None, world_id=None):
        if outcome not in ("applied", "already_applied", "definitively_not_applied") or not receipt:
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
            if record.get("world_id") and record.get("world_id") != self._world_id(server_id, save_id, world_id):
                raise ValueError("Deposit receipt is outside the active FS25 world generation")
            if record["state"] == "completed":
                return "completed"
            if record["state"] == "failed":
                return "failed"
            if record["state"] != "pending":
                raise ValueError("Deposit is not awaiting settlement")
            self._validate_money_receipt(record, receipt, "deposit_funds", outcome, request_id)
            if outcome == "definitively_not_applied":
                result = self.db.deposit_requests.update_one(
                    {"_id": request_id, "state": "pending"},
                    {"$set": {"state": "failed", "receipt": receipt,
                              "completed_at": datetime.now(timezone.utc)}}, session=session)
                if result.modified_count != 1:
                    raise ValueError("Deposit changed before failure could be committed")
                operation = self.db.farm_operations.update_one(
                    {"_id": record["operation_id"], "operation_type": "deposit_funds",
                     "state": {"$in": ["pending", "dispatched"]}},
                    {"$set": {"state": "failed", "receipt": receipt,
                              "updated_at": datetime.now(timezone.utc)}}, session=session)
                if operation.modified_count != 1:
                    raise ValueError("Deposit operation changed before failure could be committed")
                return "failed"
            event_id = receipt.get("source_event_id") or request_id
            evidence = receipt.get("receipt") or receipt
            # Use the existing verified-transfer projection so the immutable
            # transfer/ledger idempotency rules remain the single credit path.
            self.credit_verified_transfer(record["server_id"], record["save_id"], event_id,
                                          record["discord_id"], record["farm_id"], record["amount"], evidence,
                                          session=session, world_id=record.get("world_id"))
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

    def settle_withdrawal(self, request_id, outcome, receipt, server_id=None, save_id=None, world_id=None):
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
            if record.get("world_id") and record.get("world_id") != self._world_id(server_id, save_id, world_id):
                raise ValueError("Withdrawal receipt is outside the active FS25 world generation")
            if record["state"] != "pending":
                if record["state"] != state:
                    raise ValueError("Conflicting settlement")
                return state
            self._validate_money_receipt(record, receipt, "withdraw_funds", outcome, request_id)
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
