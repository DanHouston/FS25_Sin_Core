"""Durable Discord activity outbox and publisher."""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
import discord
from pymongo.errors import DuplicateKeyError

LOG = logging.getLogger(__name__)

class ActivityOutbox:
    def __init__(self, database): self.db = database.db
    @staticmethod
    def _scoped_id(source_event_id, server_key, save_key=None, world_id=None):
        return hashlib.sha256("|".join((str(server_key), str(save_key or ""),
                                         str(world_id or "legacy"), str(source_event_id))).encode("utf-8")).hexdigest()

    def enqueue(self, source_event_id, server_key, activity_type, message, save_key=None,
                world_id=None, destination="activity"):
        scoped_id = self._scoped_id(source_event_id, server_key, save_key, world_id)
        doc = {"_id": scoped_id, "activity_id": scoped_id, "source_event_id": source_event_id,
               "server_key": server_key, "save_key": save_key, "activity_type": activity_type, "message": message,
               "destination": str(destination or "activity"),
               "created_at": datetime.now(timezone.utc), "status": "pending", "attempts": 0}
        if world_id:
            doc["world_id"] = str(world_id)
        try: self.db.activity_outbox.insert_one(doc)
        except DuplicateKeyError:
            pass
        return doc

    def enqueue_staff(self, source_event_id, server_key, message, save_key=None, world_id=None):
        """Queue one idempotent operational notification for configured staff."""
        return self.enqueue(source_event_id, server_key, "staff_notification", message,
                            save_key=save_key, world_id=world_id, destination="staff")

class ActivityPublisher:
    def __init__(self, bot, database, interval=2.0, max_attempts=5):
        self.bot, self.outbox, self.interval, self.max_attempts = bot, ActivityOutbox(database), interval, max_attempts
        self.task = None
    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run()); LOG.info("[SiN Activity] publisher started")
            user = getattr(self.bot, "user", None)
            guilds = getattr(self.bot, "guilds", [])
            LOG.info("[SiN Activity] bot=%s connectedGuilds=%s", str(user or "unknown"), len(guilds))
    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
        self.task = None
    async def run(self):
        while True:
            for record in list(self.outbox.db.activity_outbox.find(
                    {"status": "pending", "world_generation_state": {"$ne": "historical"}}).limit(25)):
                await self.publish(record)
            await asyncio.sleep(self.interval)

    async def _channel_member(self, channel):
        guild = getattr(channel, "guild", None)
        if guild is None:
            raise ValueError("activity channel has no guild context")
        member = getattr(guild, "me", None)
        if member is None:
            user = getattr(self.bot, "user", None)
            user_id = getattr(user, "id", None)
            get_member = getattr(guild, "get_member", None)
            member = user_id is not None and get_member is not None and get_member(user_id)
            if member is None and user_id is not None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden):
                    member = None
        if member is None:
            raise ValueError("bot guild member unavailable for activity channel")
        return member
    async def publish(self, record):
        permanent = False
        try:
            if record.get("destination") == "staff":
                try:
                    channel_id = int(self.bot.channels.get("staff"))
                except (AttributeError, TypeError, ValueError):
                    permanent = True
                    raise ValueError("invalid staff channel ID")
            else:
                server = self.outbox.db.sin_servers.find_one({"server_key": record["server_key"]})
                if not server: raise ValueError("server_not_found")
                try:
                    channel_id = int(server["discord_activity_channel_id"])
                except (KeyError, TypeError, ValueError):
                    permanent = True
                    raise ValueError("invalid activity channel ID")
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.NotFound:
                    permanent = True
                    raise ValueError(f"channel_not_found channelId={channel_id}") from None
                except discord.Forbidden:
                    permanent = True
                    raise ValueError(f"channel_forbidden channelId={channel_id}") from None
            if not hasattr(channel, "send"):
                permanent = True
                raise ValueError("resolved activity channel is not sendable")
            permissions_for = getattr(channel, "permissions_for", None)
            if permissions_for is None:
                permanent = True
                raise ValueError("activity channel cannot calculate guild permissions")
            bot_member = await self._channel_member(channel)
            permissions = permissions_for(bot_member)
            missing = [name for name in ("view_channel", "send_messages") if not getattr(permissions, name, False)]
            if missing:
                permanent = True
                raise ValueError("missing channel permissions: " + ", ".join(missing))
            # FS25/Discord-originated text is untrusted community content;
            # never let a mirrored message create Discord mentions.
            await channel.send(record["message"], allowed_mentions=discord.AllowedMentions.none())
            self.outbox.db.activity_outbox.update_one({"_id": record["_id"], "status": "pending"}, {"$set": {"status": "published", "published_at": datetime.now(timezone.utc)}, "$inc": {"attempts": 1}})
            LOG.info("[SiN Activity] published type=%s serverKey=%s", record["activity_type"], record["server_key"])
        except discord.Forbidden as error:
            permanent = True
            self._record_failure(record, error, permanent)
        except discord.NotFound as error:
            permanent = True
            self._record_failure(record, error, permanent)
        except discord.HTTPException as error:
            self._record_failure(record, error, False)
        except Exception as error:
            LOG.exception("[SiN Activity] unexpected publish failure type=%s serverKey=%s", record.get("activity_type"), record.get("server_key"))
            self._record_failure(record, error, permanent)

    def _record_failure(self, record, error, permanent=False):
        attempts = int(record.get("attempts", 0)) + 1
        state = "failed" if permanent or attempts >= self.max_attempts else "pending"
        self.outbox.db.activity_outbox.update_one(
            {"_id": record["_id"], "status": "pending"},
            {"$set": {"status": state, "last_error": str(error),
                      "last_attempt_at": datetime.now(timezone.utc)},
             "$inc": {"attempts": 1}})
        LOG.warning("[SiN Activity] publish %s type=%s serverKey=%s attempt=%s error=%s: %s",
                    "failed" if state == "failed" else "retry", record.get("activity_type"),
                    record.get("server_key"), attempts, type(error).__name__, str(error))
