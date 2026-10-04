"""Private, receipt-gated completion notices for Discord bank commands."""

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

    async def run(self):
        backfilled = False
        while True:
            try:
                if not backfilled:
                    # Upgrade only unresolved requests from earlier JiN builds.
                    # Historical terminal transfers must not get duplicate DMs.
                    for collection in (self.db.deposit_requests, self.db.withdrawals):
                        await asyncio.to_thread(collection.update_many,
                            {"state": "pending", "notification_state": {"$exists": False}},
                            {"$set": {"notification_state": "pending"}})
                    backfilled = True
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
        try:
            summary = await asyncio.to_thread(self.bank.account_summary, record["discord_id"])
            message = completion_message(kind, record, summary["available_balance"])
            user = await self.bot.fetch_user(int(record["discord_id"]))
            await user.send(message)
            await asyncio.to_thread(collection.update_one,
                {"_id": record["_id"], "notification_state": "pending"},
                {"$set": {"notification_state": "sent", "notified_at": datetime.now(timezone.utc)}})
        except (discord.Forbidden, discord.NotFound):
            LOG.warning("Bank completion DM unavailable kind=%s request=%s user=%s; outcome remains visible in /balance",
                        kind, record.get("_id"), record.get("discord_id"))
            await asyncio.to_thread(collection.update_one,
                {"_id": record["_id"], "notification_state": "pending"},
                {"$set": {"notification_state": "blocked"}})
        except Exception:
            LOG.exception("Bank completion notification failed kind=%s request=%s", kind, record.get("_id"))
