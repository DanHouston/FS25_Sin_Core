"""Minute-granularity player activity telemetry.

This module is deliberately independent from farm authorization.  The game
adapter emits one completed-minute observation; this central component validates
and durably projects it into an interval history and cumulative player totals.
"""
import hashlib
import logging
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError


ACTIVITY_EVENT_TYPE = "player_activity_minute"
ACTIVITY_BUCKETS = {"active", "idle", "afk"}
DEFAULT_AFK_THRESHOLD_MINUTES = 10
DEFAULT_MOVEMENT_TOLERANCE_METERS = 0.5
LOG = logging.getLogger(__name__)


class ActivityTelemetryError(ValueError):
    """Malformed or unsafe telemetry payload."""


def _key(*parts):
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def is_dedicated_server_user(payload):
    return str(payload.get("user_id", "")) == "1" \
        and str(payload.get("farm_id", "")) == "0" \
        and str(payload.get("display_name", "")).strip().lower() == "server"


class ActivityMinuteTracker:
    """Small reference state machine used to test the FS25_SiN_Server algorithm."""

    def __init__(self, afk_threshold=DEFAULT_AFK_THRESHOLD_MINUTES,
                 movement_tolerance=DEFAULT_MOVEMENT_TOLERANCE_METERS):
        if int(afk_threshold) < 1:
            raise ValueError("AFK threshold must be positive")
        if float(movement_tolerance) <= 0:
            raise ValueError("movement tolerance must be positive")
        self.afk_threshold = int(afk_threshold)
        self.movement_tolerance = float(movement_tolerance)
        self.connect()

    def connect(self):
        self.connected = True
        self.inactive_minutes = 0
        self.connected_minutes = 0
        self.active_minutes = 0
        self.idle_minutes = 0
        self.afk_minutes = 0
        self.current_state = None
        self.previous_position = None

    def disconnect(self):
        self.connected = False
        self.inactive_minutes = 0
        self.current_state = None
        self.previous_position = None

    def observe_completed_minute(self, position):
        """Classify one observed minute, or return ``None`` if unobservable."""
        if not self.connected or position is None:
            return None
        position = (float(position[0]), float(position[1]))
        if self.previous_position is None:
            self.previous_position = position
            return None
        dx = position[0] - self.previous_position[0]
        dz = position[1] - self.previous_position[1]
        moved = dx * dx + dz * dz > self.movement_tolerance ** 2
        self.previous_position = position
        if moved:
            self.inactive_minutes = 0
            bucket = "active"
        else:
            self.inactive_minutes += 1
            bucket = "idle" if self.inactive_minutes <= self.afk_threshold else "afk"
        self.connected_minutes += 1
        setattr(self, bucket + "_minutes", getattr(self, bucket + "_minutes") + 1)
        self.current_state = bucket
        return {"bucket": bucket, "inactive_minutes": self.inactive_minutes,
                "connected_minutes": 1, "active_minutes": int(bucket == "active"),
                "idle_minutes": int(bucket == "idle"), "afk_minutes": int(bucket == "afk")}


class ActivityTelemetryProcessor:
    """Authenticate-independent persistence called after event auth/scope."""

    def __init__(self, database, afk_threshold=DEFAULT_AFK_THRESHOLD_MINUTES):
        self.database = database
        self.db = database.db
        self.afk_threshold = int(afk_threshold)

    def validate(self, payload):
        if not isinstance(payload, dict):
            raise ActivityTelemetryError("activity payload must be an object")
        unique_id = str(payload.get("unique_user_id", "")).strip()
        session_id = str(payload.get("session_id", "")).strip()
        bucket = str(payload.get("activity_bucket", "")).strip().lower()
        try:
            minute_sequence = int(payload.get("minute_sequence"))
            inactive_minutes = int(payload.get("inactive_minutes", 0))
        except (TypeError, ValueError):
            raise ActivityTelemetryError("activity minute counters are invalid") from None
        if not unique_id or not session_id or minute_sequence <= 0:
            raise ActivityTelemetryError("activity identity and minute sequence are required")
        if bucket not in ACTIVITY_BUCKETS or inactive_minutes < 0:
            raise ActivityTelemetryError("activity bucket is invalid")
        if bucket == "active" and inactive_minutes != 0:
            raise ActivityTelemetryError("active activity must reset inactivity")
        if bucket == "idle" and not 1 <= inactive_minutes <= self.afk_threshold:
            raise ActivityTelemetryError("idle activity is outside the inactivity threshold")
        if bucket == "afk" and inactive_minutes <= self.afk_threshold:
            raise ActivityTelemetryError("AFK activity has not exceeded the inactivity threshold")
        if is_dedicated_server_user(payload):
            return None
        return {"unique_id": unique_id, "session_id": session_id,
                "minute_sequence": minute_sequence, "bucket": bucket,
                "inactive_minutes": inactive_minutes,
                "transient_user_id": str(payload.get("user_id", "")),
                "farm_id": str(payload.get("farm_id", "0")),
                "display_name": str(payload.get("display_name", ""))}

    def process(self, server_key, save_key, event_id, payload):
        values = self.validate(payload)
        world_id = str(payload.get("world_id") or "legacy")
        if values is None:
            return {"status": "accepted", "ignored": True, "duplicate": False,
                    "save_key": save_key}
        interval_key = _key(server_key, save_key, world_id, values["unique_id"],
                            values["session_id"], values["minute_sequence"])
        aggregate_id = _key(server_key, save_key, world_id, values["unique_id"])
        now = datetime.now(timezone.utc)

        def apply(session=None):
            if self.db.player_activity_minutes.find_one({"interval_key": interval_key}, session=session):
                LOG.info("[SiN Telemetry] duplicate interval ignored serverKey=%s saveKey=%s player=%s session=%s minute=%s",
                         server_key, save_key, values["unique_id"], values["session_id"],
                         values["minute_sequence"])
                return {"status": "accepted", "duplicate": True, "save_key": save_key}
            self.db.player_activity_minutes.insert_one({
                # Event IDs are only unique within a server/save mailbox
                # stream; scope the Mongo _id as well as the interval key.
                "_id": _key(server_key, save_key, world_id, "event", event_id), "interval_key": interval_key,
                "server_key": server_key, "save_key": save_key,
                "world_id": world_id,
                "fs25_unique_user_id": values["unique_id"],
                "session_id": values["session_id"],
                "minute_sequence": values["minute_sequence"],
                "activity_bucket": values["bucket"],
                "inactive_minutes": values["inactive_minutes"],
                "transient_user_id": values["transient_user_id"],
                "observed_display_name": values["display_name"],
                "observed_farm_id": values["farm_id"], "observed_at": now}, session=session)
            increments = {"connected_minutes": 1, values["bucket"] + "_minutes": 1}
            current = {"current_state": values["bucket"],
                       "current_inactive_minutes": values["inactive_minutes"],
                       "last_seen_at": now, "updated_at": now}
            if values["bucket"] == "active":
                current["last_activity_at"] = now
            insert_values = {"_id": aggregate_id, "server_key": server_key,
                             "save_key": save_key, "fs25_unique_user_id": values["unique_id"],
                             "world_id": world_id,
                             "created_at": now}
            if values["bucket"] != "active":
                insert_values["last_activity_at"] = None
            self.db.player_activity_aggregates.update_one(
                {"_id": aggregate_id},
                {"$setOnInsert": insert_values,
                 "$set": current, "$inc": increments}, upsert=True, session=session)
            session_id = values["session_id"]
            session_key = _key(server_key, save_key, world_id, values["unique_id"], session_id)
            session_insert = {"_id": session_key, "server_key": server_key, "save_key": save_key,
                              "world_id": world_id,
                              "fs25_unique_user_id": values["unique_id"], "session_id": session_id,
                              "connected_at": now, "state": "active", "created_at": now}
            self.db.player_activity_sessions.update_one(
                {"_id": session_key},
                {"$setOnInsert": session_insert,
                  "$set": {"last_seen_at": now, "current_state": values["bucket"],
                            "current_inactive_minutes": values["inactive_minutes"],
                            "observed_farm_id": values["farm_id"], "observed_farm_at": now,
                            "observed_display_name": values["display_name"],
                            "transient_user_id": values["transient_user_id"], "updated_at": now},
                  "$inc": dict(increments, total_counted_minutes=1)}, upsert=True, session=session)
            LOG.info("[SiN Telemetry] activity interval accepted serverKey=%s saveKey=%s player=%s session=%s minute=%s bucket=%s",
                     server_key, save_key, values["unique_id"], session_id,
                     values["minute_sequence"], values["bucket"])
            return {"status": "accepted", "duplicate": False, "save_key": save_key}

        try:
            return self.database.atomic(apply)
        except DuplicateKeyError:
            # A retry may race another delivery using either the same event ID
            # or a different event ID for the same deterministic minute.
            return {"status": "accepted", "duplicate": True, "save_key": save_key}

    def status(self, server_key, save_key, unique_user_id, world_id=None):
        """Return aggregate/session diagnostics without exposing coordinates."""
        unique_user_id = str(unique_user_id or "").strip()
        if not unique_user_id:
            raise ActivityTelemetryError("stable player identity is required")
        scope = {"server_key": str(server_key), "save_key": str(save_key),
                 "fs25_unique_user_id": unique_user_id}
        if world_id: scope["world_id"] = str(world_id)
        aggregate = self.db.player_activity_aggregates.find_one({
            "server_key": str(server_key), "save_key": str(save_key),
            **scope})
        sessions = list(self.db.player_activity_sessions.find({
            **scope}).sort("connected_at", -1).limit(5))
        return {"aggregate": aggregate, "sessions": sessions}


class ActivitySessionProcessor:
    """Idempotent connection/session lifecycle projection for telemetry."""

    def __init__(self, database):
        self.database = database
        self.db = database.db

    @staticmethod
    def _identity(payload):
        if not isinstance(payload, dict):
            raise ActivityTelemetryError("session payload must be an object")
        unique_id = str(payload.get("unique_user_id", "")).strip()
        session_id = str(payload.get("session_id", "")).strip()
        if not unique_id or not session_id:
            raise ActivityTelemetryError("session identity is required")
        if is_dedicated_server_user(payload):
            return None
        return unique_id, session_id

    def connected(self, server_key, save_key, event_id, payload):
        values = self._identity(payload)
        if values is None:
            return {"status": "accepted", "ignored": True, "duplicate": False, "save_key": save_key}
        unique_id, session_id = values
        world_id = str(payload.get("world_id") or "legacy")
        now = datetime.now(timezone.utc)
        session_key = _key(server_key, save_key, world_id, unique_id, session_id)
        self.db.player_activity_sessions.update_one(
            {"_id": session_key},
            {"$setOnInsert": {"_id": session_key, "server_key": server_key,
                               "save_key": save_key, "world_id": world_id, "fs25_unique_user_id": unique_id,
                               "session_id": session_id, "connected_at": now,
                               "state": "active", "created_at": now},
             "$set": {"last_seen_at": now, "updated_at": now,
                        "transient_user_id": str(payload.get("user_id", "")),
                        "observed_farm_id": str(payload.get("farm_id", "0")),
                        "observed_farm_at": now,
                        "observed_display_name": str(payload.get("display_name", ""))}},
            upsert=True)
        # A new authoritative connection supersedes an older session that
        # never delivered a normal disconnect. Preserve the historical record
        # and identify the reconciliation source rather than fabricating a
        # normal completion summary for the stale session.
        self.db.player_activity_sessions.update_many(
            {"server_key": server_key, "save_key": save_key, "world_id": world_id,
             "fs25_unique_user_id": unique_id, "state": "active",
             "session_id": {"$ne": session_id}},
            {"$set": {"state": "reconciled", "reconciled_at": now,
                       "reconciled_by_session_id": session_id, "updated_at": now}})
        LOG.info("[SiN Telemetry] session observed/started serverKey=%s saveKey=%s player=%s session=%s",
                 server_key, save_key, unique_id, session_id)
        return {"status": "accepted", "duplicate": False, "save_key": save_key,
                "session_id": session_id}

    def disconnected(self, server_key, save_key, event_id, payload):
        values = self._identity(payload)
        if values is None:
            return {"status": "accepted", "ignored": True, "duplicate": False, "save_key": save_key}
        unique_id, session_id = values
        world_id = str(payload.get("world_id") or "legacy")
        watermark_value = payload.get("final_minute_sequence")
        if watermark_value is not None and str(watermark_value).strip() != "":
            try:
                watermark = int(watermark_value)
            except (TypeError, ValueError):
                raise ActivityTelemetryError("disconnect minute watermark is invalid") from None
            if watermark < 0:
                raise ActivityTelemetryError("disconnect minute watermark is invalid")
        else:
            # Backward-compatible policy for legacy mods: a disconnect without
            # a watermark closes the session using the minutes already
            # committed. New runtimes always send the durable watermark.
            watermark = None
        now = datetime.now(timezone.utc)
        session_key = _key(server_key, save_key, world_id, unique_id, session_id)
        existing = self.db.player_activity_sessions.find_one({"_id": session_key})
        if existing and existing.get("state") == "completed":
            LOG.info("[SiN Telemetry] duplicate session completion ignored serverKey=%s saveKey=%s player=%s session=%s",
                     server_key, save_key, unique_id, session_id)
            return {"status": "accepted", "duplicate": True, "save_key": save_key,
                    "session_id": session_id, "summary": self._summary(existing)}
        if watermark is not None:
            committed = list(self.db.player_activity_minutes.find({
                "server_key": server_key, "save_key": save_key, "world_id": world_id,
                "fs25_unique_user_id": unique_id, "session_id": session_id,
                "minute_sequence": {"$gte": 1, "$lte": watermark}}))
            sequences = {int(row.get("minute_sequence")) for row in committed
                         if row.get("minute_sequence") is not None}
            missing = [sequence for sequence in range(1, watermark + 1)
                       if sequence not in sequences]
            if missing:
                from .event_processing import EventRetryableError
                raise EventRetryableError(
                    f"disconnect waits for committed activity minutes: {missing[:5]}")
        self.db.player_activity_sessions.update_one(
            {"_id": session_key},
            {"$setOnInsert": {"_id": session_key, "server_key": server_key,
                               "save_key": save_key, "world_id": world_id, "fs25_unique_user_id": unique_id,
                               "session_id": session_id, "connected_at": now,
                               "created_at": now},
             "$set": {"state": "completed", "disconnected_at": now,
                       "last_seen_at": now, "updated_at": now}}, upsert=True)
        completed = self.db.player_activity_sessions.find_one({"_id": session_key}) or {}
        LOG.info("[SiN Telemetry] session completed serverKey=%s saveKey=%s player=%s session=%s",
                 server_key, save_key, unique_id, session_id)
        return {"status": "accepted", "duplicate": False, "save_key": save_key,
                "session_id": session_id, "summary": self._summary(completed)}

    @staticmethod
    def _summary(session):
        """Return only cumulative session counters for the disconnect card."""
        session = session if isinstance(session, dict) else {}
        return {key: int(session.get(key, 0) or 0) for key in (
            "total_counted_minutes", "active_minutes", "idle_minutes", "afk_minutes")}
