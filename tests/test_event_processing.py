import unittest
from unittest.mock import MagicMock
from pymongo.errors import DuplicateKeyError

from fs25_network_core.event_processing import (CentralEventProcessor, EventAuthenticationError,
                                                EventScopeError, scoped_event_id)


class EventProcessingTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.database.db.processed_server_events.find_one.return_value = None
        self.database.db.sin_servers = MagicMock()
        self.processor = CentralEventProcessor(self.database)
        self.processor.registry = MagicMock()
        self.processor.authorization = MagicMock()
        self.processor.registry.authenticate.return_value = {
            "_id": "sin-fs25-01", "server_key": "sin-fs25-01", "online": False,
            "display_name": "SiN FS25 01"
        }
        self.processor.registry.resolve_save.return_value = "main-save"
        self.processor.authorization.resolve_player_identity.return_value = {"fully_registered": False}

    def event(self, event_type):
        return {"event_id": "event-1", "event_type": event_type, "server_key": "sin-fs25-01",
                "server_credential": "secret", "save_id": "1",
                "payload": {"unique_user_id": "u1", "display_name": "Observed"}}

    def test_supported_events_process_and_enqueue_activity(self):
        for event_type in ("heartbeat", "player_connected", "player_disconnected"):
            with self.subTest(event_type=event_type):
                self.database.db.processed_server_events.find_one.return_value = None
                result = self.processor.process(self.event(event_type))
                self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.processor.registry.resolve_save.call_count, 3)

    def test_authentication_and_save_errors_are_typed(self):
        self.processor.registry.authenticate.side_effect = ValueError("auth")
        with self.assertRaises(EventAuthenticationError):
            self.processor.process(self.event("heartbeat"))
        self.processor.registry.authenticate.side_effect = None
        self.processor.registry.resolve_save.side_effect = ValueError("save")
        with self.assertRaises(EventScopeError):
            self.processor.process(self.event("heartbeat"))

    def test_duplicate_is_accepted_without_new_activity(self):
        self.database.db.processed_server_events.find_one.return_value = {"_id": "event-1"}
        result = self.processor.process(self.event("player_connected"))
        self.assertTrue(result["duplicate"])
        self.database.db.activity_outbox.insert_one.assert_not_called()

    def test_processed_event_id_is_scoped_to_server_and_save(self):
        self.database.db.processed_server_events.find_one.return_value = None
        self.processor.process(self.event("heartbeat"))
        query = self.database.db.processed_server_events.find_one.call_args.args[0]
        self.assertEqual(query["_id"], scoped_event_id("sin-fs25-01", "main-save", "event-1"))
        self.assertNotEqual(query["_id"], scoped_event_id("another-server", "main-save", "event-1"))
        self.assertNotEqual(query["_id"], scoped_event_id("sin-fs25-01", "other-save", "event-1"))

    def test_activity_outbox_receives_save_scope(self):
        self.database.db.processed_server_events.find_one.return_value = None
        self.processor.process(self.event("player_connected"))
        document = self.database.db.activity_outbox.insert_one.call_args.args[0]
        self.assertEqual(document["server_key"], "sin-fs25-01")
        self.assertEqual(document["save_key"], "main-save")
        self.assertEqual(document["_id"], document["activity_id"])

    def test_duplicate_processed_event_key_is_safe(self):
        self.database.db.processed_server_events.insert_one.side_effect = DuplicateKeyError("duplicate key")
        result = self.processor.process(self.event("heartbeat"))
        self.assertEqual(result["status"], "accepted")

    def test_activity_minute_is_persisted_without_authorization_processing(self):
        self.processor.telemetry = MagicMock()
        self.processor.telemetry.process.return_value = {
            "status": "accepted", "duplicate": False, "save_key": "main-save"}
        event = self.event("player_activity_minute")
        event["payload"] = {"unique_user_id": "u1", "session_id": "session-1",
                             "minute_sequence": 1, "activity_bucket": "active", "inactive_minutes": 0}
        result = self.processor.process(event)
        self.assertEqual(result["status"], "accepted")
        self.processor.telemetry.process.assert_called_once_with(
            "sin-fs25-01", "main-save", "event-1", event["payload"])
        self.processor.authorization.activity_message.assert_not_called()

    def test_chat_message_uses_existing_authenticated_event_transport(self):
        self.processor.chat = MagicMock()
        self.processor.chat.ingest_fs25.return_value = {"message_id": "message-1"}
        event = self.event("chat_message")
        event["payload"] = {"message_id": "message-1", "message": "hello", "source": "fs25"}
        result = self.processor.process(event)
        self.assertEqual(result["message_id"], "message-1")
        self.processor.chat.ingest_fs25.assert_called_once_with(
            "sin-fs25-01", "main-save", "event-1", event["payload"])

    def test_lifecycle_event_without_session_id_remains_compatible(self):
        self.processor.telemetry_sessions = MagicMock()
        self.processor.telemetry_sessions.connected.return_value = {"status": "accepted"}
        result = self.processor.process(self.event("player_connected"))
        self.assertEqual(result["status"], "accepted")
        payload = self.processor.telemetry_sessions.connected.call_args.args[3]
        self.assertEqual(payload["session_id"], "event-1")
