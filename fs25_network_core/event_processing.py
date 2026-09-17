"""Central processing for authenticated FS25_SiN_Server events."""
import logging
import hashlib
from datetime import datetime, timezone

from .activity import ActivityOutbox
from .authorization import AuthorizationManager
from .server_registry import ServerRegistry
from .farm_lifecycle import FarmLifecycle
from .activity_telemetry import (ACTIVITY_EVENT_TYPE, ActivityTelemetryError,
                                 ActivityTelemetryProcessor, ActivitySessionProcessor)
from .business_workflows import ChatService
from .business_workflows import TransferService
from .banking_engine import BankingEngine
from .map_service import MapModel, MapValidationError
from pymongo.errors import DuplicateKeyError

LOG = logging.getLogger(__name__)
CHAT_EVENT_TYPE = "chat_message"
MAP_EVENT_TYPE = "map_geometry"
SUPPORTED_EVENTS = {"heartbeat", "player_connected", "player_disconnected", ACTIVITY_EVENT_TYPE,
                    CHAT_EVENT_TYPE, MAP_EVENT_TYPE}


def scoped_event_id(server_key, save_key, event_id):
    """Return the durable idempotency key for one server/save event stream."""
    value = "|".join((str(server_key), str(save_key), str(event_id)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EventValidationError(ValueError):
    pass


class EventAuthenticationError(ValueError):
    pass


class EventScopeError(ValueError):
    pass


class EventRetryableError(ValueError):
    """The event is valid but must remain in the Agent mailbox for retry."""


class CentralEventProcessor:
    def __init__(self, database):
        self.database = database
        self.db = database.db
        self.registry = ServerRegistry(database)
        self.authorization = AuthorizationManager(database)
        self.farm_lifecycle = FarmLifecycle(database, self.authorization)
        self.telemetry = ActivityTelemetryProcessor(database)
        self.telemetry_sessions = ActivitySessionProcessor(database)
        self.chat = ChatService(database)
        self.transfers = TransferService(database, self.authorization)
        self.banking = BankingEngine(database)

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
        processed_id = scoped_event_id(record["server_key"], save_key, event_id)
        processed_marker = self.db.processed_server_events.find_one({"_id": processed_id})
        if processed_marker and event_type != "player_disconnected":
            LOG.info("[SiN Events] duplicate event ignored eventId=%s type=%s", event_id, event_type)
            return {"status": "accepted", "duplicate": True, "save_key": save_key}

        now = datetime.now(timezone.utc)
        result = {"status": "accepted", "duplicate": False, "save_key": save_key}
        if event_type == ACTIVITY_EVENT_TYPE:
            raw_payload = event.get("payload") or {}
            if not isinstance(raw_payload, dict):
                raise EventValidationError("event payload must be an object")
            try:
                result = self.telemetry.process(record["server_key"], save_key, event_id, raw_payload)
            except ActivityTelemetryError as error:
                raise EventValidationError(str(error)) from None
        elif event_type == MAP_EVENT_TYPE:
            raw_payload = event.get("payload") or {}
            if not isinstance(raw_payload, dict) or not isinstance(raw_payload.get("map"), dict):
                raise EventValidationError("map geometry payload must contain a map object")
            try:
                model = MapModel.from_dict(raw_payload["map"])
            except MapValidationError as error:
                raise EventValidationError(str(error)) from None
            map_key = hashlib.sha256((record["server_key"] + "|" + save_key).encode("utf-8")).hexdigest()
            self.db.sin_maps.update_one(
                {"_id": map_key},
                {"$setOnInsert": {"_id": map_key, "server_key": record["server_key"],
                                   "save_key": save_key, "created_at": now},
                 "$set": {"map_id": model.map_id, "map_version": model.version,
                           "map_revision": model.revision, "map_payload": model.to_dict(), "updated_at": now}},
                upsert=True)
            result = {"status": "accepted", "map_id": model.map_id,
                      "map_version": model.version, "map_revision": model.revision, "save_key": save_key}
        elif event_type == CHAT_EVENT_TYPE:
            raw_payload = event.get("payload") or {}
            if not isinstance(raw_payload, dict):
                raise EventValidationError("event payload must be an object")
            try:
                chat_record = self.chat.ingest_fs25(record["server_key"], save_key, event_id, raw_payload)
            except (TypeError, ValueError) as error:
                raise EventValidationError(str(error)) from None
            result = {"status": "accepted", "message_id": chat_record.get("message_id") if chat_record else event_id,
                      "save_key": save_key}
            if chat_record:
                observed_name = raw_payload.get("display_name") or chat_record.get("unique_user_id") or "FS25"
                resolved = self.authorization.resolve_player_identity(
                    record["server_key"], save_key, chat_record.get("unique_user_id")) \
                    if chat_record.get("unique_user_id") else {"fully_registered": False}
                sender = resolved.get("canonical_name") if resolved.get("fully_registered") else observed_name
                ActivityOutbox(self.database).enqueue(
                    event_id, record["server_key"], CHAT_EVENT_TYPE,
                    f"💬 {sender}: {chat_record.get('message', raw_payload.get('message', ''))}",
                    save_key=save_key)
        elif event_type == "player_connected":
            raw_payload = event.get("payload") or {}
            if not isinstance(raw_payload, dict):
                raise EventValidationError("event payload must be an object")
            try:
                # Older NetworkLocal builds did not include a session_id on
                # lifecycle events.  Preserve that mailbox contract while
                # giving the session store a stable id for those events.
                raw_payload = dict(raw_payload)
                raw_payload.setdefault("session_id", event_id)
                result = self.telemetry_sessions.connected(record["server_key"], save_key,
                                                           event_id, raw_payload)
            except ActivityTelemetryError as error:
                raise EventValidationError(str(error)) from None
            payload = dict(raw_payload)
            payload["event_type"] = event_type
            message = self.activity_message(record, save_key, payload)
            if message is not None:
                ActivityOutbox(self.database).enqueue(event_id, record["server_key"], event_type, message,
                                                      save_key=save_key)
        elif event_type == "player_disconnected":
            raw_payload = event.get("payload") or {}
            if not isinstance(raw_payload, dict):
                raise EventValidationError("event payload must be an object")
            try:
                raw_payload = dict(raw_payload)
                raw_payload.setdefault("session_id", event_id)
                result = self.telemetry_sessions.disconnected(record["server_key"], save_key,
                                                              event_id, raw_payload)
            except EventRetryableError:
                raise
            except ActivityTelemetryError as error:
                raise EventValidationError(str(error)) from None
            payload = dict(raw_payload)
            payload["event_type"] = event_type
            message = self.activity_message(record, save_key, payload, result.get("summary"))
            # Enqueue is an idempotent repair operation.  It must also run for
            # a completed session whose projection succeeded before the
            # process crashed, and for a processed marker whose outbox insert
            # was lost.  ActivityOutbox's scoped unique key makes retries one
            # durable record.
            if message is not None:
                ActivityOutbox(self.database).enqueue(event_id, record["server_key"], event_type, message,
                                                      save_key=save_key)
        elif event_type == "heartbeat":
            try:
                self.farm_lifecycle.ensure_system_farm(record["server_key"], save_key)
            except Exception:
                LOG.exception("server resource bootstrap could not be queued")
            was_online = bool(record.get("online"))
            self.registry.heartbeat(record["server_key"], event["server_credential"])
            if not was_online:
                name = record.get("display_name") or record["server_key"]
                ActivityOutbox(self.database).enqueue(
                    event_id, record["server_key"], "server_online",
                    f"🟢 Server Online\n{name} is connected to SiN JiN.", save_key=save_key)
        elif event_type not in {"player_connected", "player_disconnected"}:
            raw_payload = event.get("payload") or {}
            if not isinstance(raw_payload, dict):
                raise EventValidationError("event payload must be an object")
            payload = dict(raw_payload)
            payload["event_type"] = event_type
            message = self.activity_message(record, save_key, payload)
            if message is not None:
                ActivityOutbox(self.database).enqueue(
                    event_id, record["server_key"], event_type, message, save_key=save_key)
        try:
            self.db.processed_server_events.insert_one(
                {"_id": processed_id, "event_id": event_id, "server_key": record["server_key"],
                 "save_key": save_key, "processed_at": now})
        except DuplicateKeyError:
            pass
        LOG.info("[SiN Events] processed type=%s serverKey=%s", event_type, record["server_key"])
        result.setdefault("status", "accepted")
        result.setdefault("save_key", save_key)
        return result

    def activity_message(self, server, save_key, payload, session_summary=None):
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
        if verb == "left":
            summary = session_summary if isinstance(session_summary, dict) else {}
            server_name = server.get("display_name") or server.get("server_key") or "the server"
            return (f"{icon} {name} has left {server_name}.{suffix}\n"
                    f"Duration: {int(summary.get('duration_minutes', summary.get('total_counted_minutes', 0)) or 0)} min\n"
                    f"Session: {int(summary.get('total_counted_minutes', 0) or 0)} min\n"
                    f"Active: {int(summary.get('active_minutes', 0) or 0)} min\n"
                    f"Idle: {int(summary.get('idle_minutes', 0) or 0)} min\n"
                    f"AFK: {int(summary.get('afk_minutes', 0) or 0)} min")
        return f"{icon} {name} has {verb} the server.{suffix}"
