"""Central processing for authenticated NetworkLocal server events."""
import logging
from datetime import datetime, timezone

from .activity import ActivityOutbox
from .authorization import AuthorizationManager
from .server_registry import ServerRegistry

LOG = logging.getLogger(__name__)
SUPPORTED_EVENTS = {"heartbeat", "player_connected", "player_disconnected"}


class EventValidationError(ValueError):
    pass


class EventAuthenticationError(ValueError):
    pass


class EventScopeError(ValueError):
    pass


class CentralEventProcessor:
    def __init__(self, database):
        self.database = database
        self.db = database.db
        self.registry = ServerRegistry(database)
        self.authorization = AuthorizationManager(database)

    def process(self, event):
        if not isinstance(event, dict):
            raise EventValidationError("event must be an object")
        required = {"event_id", "event_type", "server_key", "server_credential", "save_id"}
        if not required.issubset(event) or any(not str(event.get(key, "")).strip() for key in required):
            raise EventValidationError("event is missing required fields")
        event_id = str(event["event_id"])
        event_type = str(event["event_type"])
        if event_type not in SUPPORTED_EVENTS:
            raise EventValidationError("unsupported server event")
        try:
            record = self.registry.authenticate(str(event["server_key"]), str(event["server_credential"]))
        except ValueError as error:
            raise EventAuthenticationError(str(error)) from None
        try:
            save_key = self.registry.resolve_save(record["server_key"], event["save_id"])
        except ValueError as error:
            raise EventScopeError(str(error)) from None
        if self.db.processed_server_events.find_one({"_id": event_id}):
            return {"status": "accepted", "duplicate": True, "save_key": save_key}

        now = datetime.now(timezone.utc)
        if event_type == "heartbeat":
            was_online = bool(record.get("online"))
            self.registry.heartbeat(record["server_key"], event["server_credential"])
            if not was_online:
                name = record.get("display_name") or record["server_key"]
                ActivityOutbox(self.database).enqueue(
                    event_id, record["server_key"], "server_online",
                    f"🟢 Server Online\n{name} is connected to SiN JiN.")
        else:
            raw_payload = event.get("payload") or {}
            if not isinstance(raw_payload, dict):
                raise EventValidationError("event payload must be an object")
            payload = dict(raw_payload)
            payload["event_type"] = event_type
            message = self.activity_message(record, save_key, payload)
            if message is not None:
                ActivityOutbox(self.database).enqueue(
                    event_id, record["server_key"], event_type, message)
        try:
            self.db.processed_server_events.insert_one(
                {"_id": event_id, "server_key": record["server_key"], "processed_at": now})
        except Exception as error:
            if "duplicate" not in str(error).lower():
                raise
        LOG.info("[SiN Events] processed type=%s serverKey=%s", event_type, record["server_key"])
        return {"status": "accepted", "duplicate": False, "save_key": save_key}

    def activity_message(self, server, save_key, payload):
        if str(payload.get("user_id", "")) == "1" and str(payload.get("farm_id", "")) == "0" \
                and str(payload.get("display_name", "")).strip().lower() == "server":
            return None
        unique_id = payload.get("unique_user_id")
        resolved = self.authorization.resolve_player_identity(server["server_key"], save_key, unique_id) if unique_id else {"fully_registered": False}
        registered = resolved.get("linked", False)
        name = resolved.get("canonical_name") if resolved.get("fully_registered", False) else payload.get("display_name", "Player")
        verb = "joined" if payload.get("event_type") == "player_connected" else "left"
        icon = "🟢" if verb == "joined" else "🔴"
        suffix = "" if registered else " (SiN Registration: Required)"
        return f"{icon} {name} has {verb} the server.{suffix}"
