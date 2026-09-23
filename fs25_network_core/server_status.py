"""Persistent JiN server-status cards.

Status cards are a presentation projection, deliberately separate from the
activity outbox.  The projection reads the authoritative active runtime and
the snapshot for that exact world; it never falls back to a historical save or
to a Discord channel selected by name.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone

import discord


LOG = logging.getLogger(__name__)
STATUS_COLLECTION = "server_status_cards"


def _as_text(value, fallback="unknown"):
    if value is None or str(value).strip() == "":
        return fallback
    return str(value)


def _format_timestamp(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return f"<t:{int(value.timestamp())}:R>"
    return _as_text(value, "unknown")


def _is_recent_timestamp(value, now, threshold_seconds=60):
    """Return whether a successful update is strictly newer than the threshold."""
    if not isinstance(value, datetime):
        return False
    observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    current = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
    return (current - observed).total_seconds() < threshold_seconds


def _format_game_time(snapshot):
    """Render optional authoritative game-clock observations compactly."""
    month = snapshot.get("current_month")
    day = snapshot.get("current_day")
    minutes = snapshot.get("day_time_minutes")
    if minutes is None:
        minutes = snapshot.get("day_time")
        if minutes is not None:
            try:
                minutes = float(minutes) / 60000.0
            except (TypeError, ValueError):
                minutes = None
    try:
        if minutes is not None:
            total = int(float(minutes)) % 1440
            clock = f"{total // 60:02d}:{total % 60:02d}"
        else:
            clock = "unknown"
    except (TypeError, ValueError):
        clock = "unknown"
    month_day = ""
    if month is not None or day is not None:
        month_day = f"month {_as_text(month)}, day {_as_text(day)}"
    return f"{month_day + ', ' if month_day else ''}{clock}"


class ServerStatusProjection:
    """Build deterministic, current-world-only status content."""

    def __init__(self, database, now=None, online_after_seconds=90):
        self.db = database.db
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.online_after_seconds = int(online_after_seconds)

    def _current_snapshot(self, server, runtime):
        save_key = runtime.get("save_key") if isinstance(runtime, dict) else None
        world_id = runtime.get("world_id") if isinstance(runtime, dict) else None
        if not save_key or not world_id:
            return None
        return self.db.server_snapshots.find_one(
            {"server_key": server.get("server_key"), "save_key": str(save_key),
             "world_id": str(world_id)}, sort=[("received_at", -1)])

    def project(self, server):
        runtime = server.get("active_runtime") if isinstance(server, dict) else None
        runtime = runtime if isinstance(runtime, dict) else {}
        snapshot = self._current_snapshot(server, runtime) or {}
        # Heartbeats are the liveness source.  Snapshot timestamps are the
        # source for displayed game state and may legitimately lag while the
        # Agent is retrying a snapshot upload.
        last_seen = server.get("last_seen_at") or runtime.get("last_seen_at")
        online = False
        if isinstance(last_seen, datetime):
            observed = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
            online = (self.now() - observed).total_seconds() <= self.online_after_seconds
        elif server.get("online") and last_seen:
            # Test and legacy adapters may expose a non-datetime marker.  The
            # marker is useful for presentation, but cannot prove freshness.
            online = bool(server.get("online"))
        state = "online" if online else "offline"
        if not runtime:
            state = "offline / no active runtime"
        players = snapshot.get("players") if isinstance(snapshot.get("players"), dict) else {}
        names = []
        for identity, raw in players.items():
            details = raw if isinstance(raw, dict) else {"name": raw}
            name = str(details.get("name") or identity or "Unnamed player").strip()
            names.append(name or "Unnamed player")
        names.sort(key=str.casefold)
        return {
            "server_key": _as_text(server.get("server_key")),
            "display_name": _as_text(server.get("display_name"), server.get("server_key") or "SiN server"),
            "state": state,
            "save_key": _as_text(runtime.get("save_key")),
            "world_id": _as_text(runtime.get("world_id")),
            "map_id": _as_text(snapshot.get("map_id")),
            "game_time": _format_game_time(snapshot),
            "time_scale": _as_text(snapshot.get("time_scale")),
            "players": names,
            "last_update": _format_timestamp(snapshot.get("received_at") or runtime.get("last_seen_at")),
            "last_update_fresh": _is_recent_timestamp(
                snapshot.get("received_at") or runtime.get("last_seen_at"), self.now(), 60
            ),
        }

    @staticmethod
    def render(projection):
        players = projection["players"]
        player_text = ", ".join(players) if players else "None"
        state_light = "🟢" if projection.get("state") == "online" else "🔴"
        update_light = "🟢" if projection.get("last_update_fresh") else "🔴"
        return (f"**{projection['display_name']}** (`{projection['server_key']}`)\n"
                f"State: {state_light}\n"
                f"Active save: `{projection['save_key']}`\n"
                f"World: `{projection['world_id']}`\n"
                f"Map: `{projection['map_id']}`\n"
                f"Game time: {projection['game_time']}\n"
                f"Time scale: `{projection['time_scale']}x`\n"
                f"Players ({len(players)}): {player_text}\n"
                f"Last successful update: {update_light}")

    @staticmethod
    def content_hash(content):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ServerStatusStore:
    """Durable message-ID/hash store, one row per registered server."""

    def __init__(self, database):
        self.db = database.db

    def get(self, server_key):
        return self.db.server_status_cards.find_one({"server_key": str(server_key)})

    def save(self, server_key, channel_id, message_id, content_hash, *, updated_at=None):
        updated_at = updated_at or datetime.now(timezone.utc)
        self.db.server_status_cards.update_one(
            {"server_key": str(server_key)},
            {"$set": {"server_key": str(server_key), "channel_id": str(channel_id),
                      "message_id": str(message_id), "content_hash": str(content_hash),
                      "updated_at": updated_at},
             "$setOnInsert": {"created_at": updated_at}}, upsert=True)


class ServerStatusPublisher:
    """Synchronize one persistent dashboard message per registered server."""

    def __init__(self, bot, database, interval=30.0):
        self.bot = bot
        self.store = ServerStatusStore(database)
        self.projection = ServerStatusProjection(database)
        self.db = database.db
        self.interval = float(interval)
        self.task = None
        self._needs_recovery = True

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
            LOG.info("[SiN Status] dashboard publisher started")

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
            except Exception:
                LOG.exception("[SiN Status] dashboard refresh failed")
            await asyncio.sleep(self.interval)

    def _servers(self):
        return list(self.db.sin_servers.find({"enabled": True}))

    async def _channel(self, channel_id):
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(channel_id))
        if not hasattr(channel, "send"):
            raise ValueError("configured server-status channel is not sendable")
        return channel

    async def sync_server(self, server, channel_id=None, verify_existing=False):
        """Refresh one card; return ``created``, ``edited``, or ``unchanged``."""
        if channel_id is None:
            channel_id = getattr(self.bot, "channels", {}).get("server_status")
        if not channel_id:
            raise ValueError("Configure channels.server_status for the JiN status dashboard")
        projection = self.projection.project(server)
        content = self.projection.render(projection)
        digest = self.projection.content_hash(content)
        previous = self.store.get(server.get("server_key"))
        same_card = (previous and str(previous.get("channel_id")) == str(channel_id)
                     and previous.get("content_hash") == digest)
        if same_card and not verify_existing:
            return "unchanged"
        channel = await self._channel(channel_id)
        message = None
        if previous and previous.get("message_id") \
                and str(previous.get("channel_id")) == str(channel_id):
            try:
                message = await channel.fetch_message(int(previous["message_id"]))
            except discord.NotFound:
                LOG.info("[SiN Status] stored card missing server=%s; creating replacement",
                         server.get("server_key"))
            except discord.Forbidden:
                raise ValueError("stored server-status message is unavailable due to Discord permissions")
            except discord.HTTPException:
                raise
            if same_card and message is not None:
                return "unchanged"
        if message is not None:
            await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
            action = "edited"
        else:
            message = await channel.send(content=content, allowed_mentions=discord.AllowedMentions.none())
            action = "created"
        message_id = getattr(message, "id", None)
        if message_id is None:
            raise ValueError("Discord status message did not return an ID")
        self.store.save(server.get("server_key"), channel_id, message_id, digest)
        LOG.info("[SiN Status] %s server=%s message=%s", action, server.get("server_key"), message_id)
        return action

    async def publish_once(self):
        channel_id = getattr(self.bot, "channels", {}).get("server_status")
        if not channel_id:
            LOG.warning("[SiN Status] channels.server_status is not configured; dashboard paused")
            return []
        results = []
        verify_existing = self._needs_recovery
        try:
            for server in self._servers():
                try:
                    results.append((server.get("server_key"), await self.sync_server(
                        server, channel_id, verify_existing=verify_existing)))
                except (discord.Forbidden, discord.NotFound, discord.HTTPException, ValueError) as error:
                    LOG.warning("[SiN Status] refresh failed server=%s error=%s", server.get("server_key"), error)
            return results
        finally:
            self._needs_recovery = False
