"""Receipt-gated edits to private Discord bank-command replies."""

import asyncio
import logging
from datetime import datetime, timezone

import discord


LOG = logging.getLogger(__name__)


def completion_message(kind, record, available_balance):
    """Never describe an unconfirmed game mutation as completed or failed."""
    amount = int(record["amount"])
    state = record["state"]
    receipt = record.get("receipt") or {}
    if kind == "deposit":
        if state == "completed":
            result = f"Deposit complete: ${amount:,} moved from your FS25 farm to your SiN bank."
        elif state == "failed":
            result = "Deposit failed: no money was moved. Please try again later."
        else:
            raise ValueError("Deposit has no definitive outcome")
    elif kind == "withdrawal":
        if state == "completed":
            result = f"Withdrawal complete: ${amount:,} delivered to your FS25 farm."
        elif state == "refunded":
            result = "Withdrawal failed: no money was delivered, and the reserved funds were returned. Please try again later."
        else:
            raise ValueError("Withdrawal has no definitive outcome")
    else:
        raise ValueError("Unknown bank operation")
    if state == "completed":
        # The successful receipt includes the native balance read-back that
        # settled this exact operation; a later snapshot could be stale.
        result += f"\nFS25 farm balance after transfer: ${float(receipt['after_balance']):,.0f}."
    return result + f"\nSiN bank available balance: ${int(available_balance):,}."


class BankNotificationPublisher:
    def __init__(self, bot, bank, interval=2.0):
        self.bot, self.bank, self.db, self.interval = bot, bank, bank.database.db, interval
        self.task = None
        # Interaction tokens are short-lived and must not be persisted in Mongo.
        # A restart or late game receipt falls back to the durable /balance view.
        self.interactions = {}

    def track(self, interaction, kind, request_id):
        self.interactions[(str(kind), str(request_id))] = interaction

    def untrack(self, kind, request_id):
        self.interactions.pop((str(kind), str(request_id)), None)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self.interactions.clear()

    async def run(self):
        backfilled = False
        while True:
            try:
                if not backfilled:
                    # Upgrade only unresolved requests from earlier JiN builds.
                    # Historical terminal transfers must not get new notices.
                    for collection in (self.db.deposit_requests, self.db.withdrawals):
                        await asyncio.to_thread(collection.update_many,
                            {"state": "pending", "notification_state": {"$exists": False}},
                            {"$set": {"notification_state": "pending"}})
                    backfilled = True
                for key, interaction in list(self.interactions.items()):
                    if interaction.is_expired():
                        self.interactions.pop(key, None)
                for kind, collection, states in (
                    ("deposit", self.db.deposit_requests, ["completed", "failed"]),
                    ("withdrawal", self.db.withdrawals, ["completed", "refunded"]),
                ):
                    records = await asyncio.to_thread(
                        lambda: list(collection.find({"notification_state": "pending",
                                                      "state": {"$in": states}}).limit(25)))
                    for record in records:
                        await self.publish(kind, collection, record)
            except Exception:
                LOG.exception("Bank completion notification scan failed")
            await asyncio.sleep(self.interval)

    async def publish(self, kind, collection, record):
        key = (str(kind), str(record["_id"]))
        interaction = self.interactions.get(key)
        if interaction is None:
            created_at = record.get("created_at")
            if isinstance(created_at, datetime) and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            # Give the command time to publish its initial ephemeral reply and
            # register the interaction before treating a fast receipt as late.
            if (isinstance(created_at, datetime) and
                    (datetime.now(timezone.utc) - created_at).total_seconds() < 30):
                return
            await self.mark_status_only(collection, record, kind, "no-active-interaction")
            return
        if str(getattr(getattr(interaction, "user", None), "id", "")) != str(record["discord_id"]):
            self.untrack(kind, record["_id"])
            await self.mark_status_only(collection, record, kind, "interaction-owner-mismatch")
            return
        if interaction.is_expired():
            self.untrack(kind, record["_id"])
            await self.mark_status_only(collection, record, kind, "interaction-expired")
            return
        try:
            summary = await asyncio.to_thread(self.bank.account_summary, record["discord_id"])
            message = completion_message(kind, record, summary["available_balance"])
            await interaction.edit_original_response(content=message)
            await asyncio.to_thread(collection.update_one,
                {"_id": record["_id"], "notification_state": "pending"},
                {"$set": {"notification_state": "sent", "notified_at": datetime.now(timezone.utc)}})
            self.untrack(kind, record["_id"])
        except (discord.Forbidden, discord.NotFound):
            self.untrack(kind, record["_id"])
            await self.mark_status_only(collection, record, kind, "private-reply-unavailable")
        except Exception:
            LOG.exception("Bank private-reply update failed kind=%s request=%s", kind, record.get("_id"))

    async def mark_status_only(self, collection, record, kind, reason):
        LOG.info("Bank outcome available via /balance kind=%s request=%s reason=%s",
                 kind, record.get("_id"), reason)
        await asyncio.to_thread(collection.update_one,
            {"_id": record["_id"], "notification_state": "pending"},
            {"$set": {"notification_state": "status_only", "notification_reason": reason}})
