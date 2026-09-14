"""Minute-granularity player activity telemetry.

This module is deliberately independent from farm authorization.  The game
adapter emits one completed-minute observation; this central component validates
and durably projects it into an interval history and cumulative player totals.
"""
import hashlib
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError


ACTIVITY_EVENT_TYPE = "player_activity_minute"
ACTIVITY_BUCKETS = {"active", "idle", "afk"}
DEFAULT_AFK_THRESHOLD_MINUTES = 10
DEFAULT_MOVEMENT_TOLERANCE_METERS = 0.5


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
        if values is None:
            return {"status": "accepted", "ignored": True, "duplicate": False,
                    "save_key": save_key}
        interval_key = _key(server_key, save_key, values["unique_id"],
                            values["session_id"], values["minute_sequence"])
        aggregate_id = _key(server_key, save_key, values["unique_id"])
        now = datetime.now(timezone.utc)

        def apply(session=None):
            if self.db.player_activity_minutes.find_one({"interval_key": interval_key}, session=session):
                return {"status": "accepted", "duplicate": True, "save_key": save_key}
            self.db.player_activity_minutes.insert_one({
                "_id": str(event_id), "interval_key": interval_key,
                "server_key": server_key, "save_key": save_key,
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
                             "created_at": now}
            if values["bucket"] != "active":
                insert_values["last_activity_at"] = None
            self.db.player_activity_aggregates.update_one(
                {"_id": aggregate_id},
                {"$setOnInsert": insert_values,
                 "$set": current, "$inc": increments}, upsert=True, session=session)
            return {"status": "accepted", "duplicate": False, "save_key": save_key}

        try:
            return self.database.atomic(apply)
        except DuplicateKeyError:
            # A retry may race another delivery using either the same event ID
            # or a different event ID for the same deterministic minute.
            return {"status": "accepted", "duplicate": True, "save_key": save_key}
