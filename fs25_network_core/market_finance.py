"""One recoverable daily Discord card for observed server-wide FS25 cash flow."""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from .server_registry import ServerRegistry
from .world_generation import WorldGenerationRegistry


LOG = logging.getLogger(__name__)
MARKET_TIMEZONE = ZoneInfo("America/New_York")
PUBLISH_AFTER = time(0, 10)


def report_window(now):
    """Previous completed Eastern calendar day; no partial-day post."""
    local = now.astimezone(MARKET_TIMEZONE)
    if local.time() < PUBLISH_AFTER:
        return None
    day = local.date() - timedelta(days=1)
    start = datetime.combine(day, time.min, MARKET_TIMEZONE)
    end = datetime.combine(day + timedelta(days=1), time.min, MARKET_TIMEZONE)
    return day.isoformat(), start.astimezone(timezone.utc), end.astimezone(timezone.utc)


class DailyMarketFinance:
    def __init__(self, database):
        self.db = database.db
        self.registry = ServerRegistry(database)
        self.worlds = WorldGenerationRegistry(database)

    def summarize(self, now=None):
        window = report_window(now or datetime.now(timezone.utc))
        if window is None:
            return None
        day, start, end = window
        totals = {"income": 0.0, "expense": 0.0, "count": 0}
        servers = []
        for server in self.registry.eligible_servers("reconcile"):
            server_key = server["server_key"]
            save_key = server.get("active_save_key")
            if not save_key and len(server.get("saves") or []) == 1:
                save_key = server["saves"][0]["save_key"]
            if not save_key:
                continue
            world_id = self.worlds.active_id(server_key, save_key)
            if not world_id:
                continue
            match = {"server_key": server_key, "save_key": save_key,
                     "world_id": world_id, "farm_id": {"$gte": 1, "$lte": 254},
                     "observed_at": {"$gte": start, "$lt": end}}
            rows = list(self.db.farm_finance_changes.aggregate([
                {"$match": match},
                {"$group": {"_id": None, "count": {"$sum": 1},
                            "income": {"$sum": {"$cond": [{"$gt": ["$amount", 0]}, "$amount", 0]}},
                            "expense": {"$sum": {"$cond": [{"$lt": ["$amount", 0]},
                                                              {"$multiply": ["$amount", -1]}, 0]}}}},
            ]))
            values = rows[0] if rows else {}
            row = {"name": server.get("display_name") or server_key,
                   "income": float(values.get("income") or 0),
                   "expense": float(values.get("expense") or 0),
                   "count": int(values.get("count") or 0)}
            for key in totals:
                totals[key] += row[key]
            servers.append(row)
        if not servers:
            return None
        return {"day": day, "start": start, "end": end,
                "totals": totals, "servers": servers}

    @staticmethod
    def render(summary):
        totals = summary["totals"]
        def money(value):
            return f"-${-value:,.0f}" if value < 0 else f"${value:,.0f}"
        lines = [f"**Daily server finances — {summary['day']} (Eastern)**",
                 f"Total server revenue / native inflows: **{money(totals['income'])}**",
                 f"Total server expenses / native outflows: **{money(totals['expense'])}**",
                 f"Net observed cash change: **{money(totals['income'] - totals['expense'])}**"]
        if len(summary["servers"]) > 1:
            lines.append("By server:")
            for server in summary["servers"][:12]:
                name = str(server["name"]).replace("\n", " ")[:45]
                lines.append(f"• {name}: +{money(server['income'])} / -{money(server['expense'])}")
        lines.append(f"{int(totals['count']):,} native money change(s) observed. "
                     "Positive/negative farm-balance changes include non-sale categories "
                     "such as loans and purchases; these are cash-flow totals, not profit. "
                     "History is not backfilled, and direct balance edits are not captured.")
        return "\n".join(lines)[:1900]


class DailyMarketFinancePublisher:
    """Update one persistent message per date; recover a send-before-save crash."""

    def __init__(self, bot, database, interval=300):
        self.bot = bot
        self.db = database.db
        self.projection = DailyMarketFinance(database)
        self.interval = float(interval)
        self.task = None
        self.last_cycle_at = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
            LOG.info("[SiN Market] daily finance publisher started")

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def run(self):
        while True:
            try:
                await self.publish_once()
                self.last_cycle_at = datetime.now(timezone.utc).timestamp()
            except Exception:
                LOG.exception("[SiN Market] daily finance publish failed")
            await asyncio.sleep(self.interval)

    async def publish_once(self, now=None):
        channel_id = getattr(self.bot, "channels", {}).get("market")
        if not channel_id:
            return "unconfigured"
        summary = await asyncio.to_thread(self.projection.summarize, now)
        if summary is None:
            return "not-due-or-no-active-server"
        content = self.projection.render(summary)
        record_id = f"{self.bot.guild.id}:{summary['day']}"
        stored = await asyncio.to_thread(self.db.market_daily_finance.find_one, {"_id": record_id})
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(channel_id))
        if not hasattr(channel, "send") or not hasattr(channel, "fetch_message"):
            raise ValueError("market channel must support messages")
        message = None
        if stored and stored.get("message_id") and str(stored.get("channel_id")) == str(channel_id):
            try:
                message = await channel.fetch_message(int(stored["message_id"]))
            except discord.NotFound:
                LOG.warning("[SiN Market] daily card was deleted day=%s; recovering", summary["day"])
            if message is not None and message.content == content:
                return "unchanged"
        if message is None:
            # A process can die after Discord accepts send but before Mongo
            # records the message ID. Search for the stable heading before
            # creating a new card, including when Mongo has no row yet.
            heading = f"**Daily server finances — {summary['day']} (Eastern)**"
            async for candidate in channel.history(limit=500, after=summary["start"]):
                if getattr(candidate, "author", None) == self.bot.user \
                        and str(getattr(candidate, "content", "")).startswith(heading):
                    message = candidate
                    break
        if message is None:
            message = await channel.send(content=content, allowed_mentions=discord.AllowedMentions.none())
            action = "created"
        else:
            await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
            action = "updated"
        await asyncio.to_thread(self.db.market_daily_finance.update_one,
                                {"_id": record_id}, {"$set": {
                                    "channel_id": str(channel_id), "message_id": str(message.id),
                                    "content": content, "day": summary["day"],
                                    "updated_at": datetime.now(timezone.utc)}}, upsert=True)
        LOG.info("[SiN Market] daily finance %s day=%s message=%s", action, summary["day"], message.id)
        return action
