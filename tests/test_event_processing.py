import unittest
from unittest.mock import MagicMock
from pymongo.errors import DuplicateKeyError

from fs25_network_core.event_processing import (CentralEventProcessor, EventAuthenticationError,
                                                EventRetryableError, EventScopeError, scoped_event_id)


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

    def test_completed_disconnect_repairs_missing_summary_outbox(self):
        self.database.db.processed_server_events.find_one.return_value = {"_id": "processed"}
        self.processor.telemetry_sessions = MagicMock()
        self.processor.telemetry_sessions.disconnected.return_value = {
            "status": "accepted", "duplicate": True, "summary": {
                "total_counted_minutes": 4, "active_minutes": 2,
                "idle_minutes": 2, "afk_minutes": 0}}
        event = self.event("player_disconnected")
        event["payload"].update({"session_id": "session-1", "user_id": "2", "farm_id": "2"})
        result = self.processor.process(event)
        self.assertTrue(result["duplicate"])
        self.database.db.activity_outbox.insert_one.assert_called_once()

    def test_disconnect_retry_after_projection_before_marker_is_idempotent(self):
        self.database.db.processed_server_events.find_one.return_value = None
        self.processor.telemetry_sessions = MagicMock()
        self.processor.telemetry_sessions.disconnected.return_value = {
            "status": "accepted", "duplicate": True, "summary": {
                "total_counted_minutes": 4, "active_minutes": 2,
                "idle_minutes": 2, "afk_minutes": 0}}
        event = self.event("player_disconnected")
        event["payload"].update({"session_id": "session-1", "user_id": "2", "farm_id": "2"})
        self.processor.process(event)
        self.processor.process(event)
        first = self.database.db.activity_outbox.insert_one.call_args_list[0].args[0]
        second = self.database.db.activity_outbox.insert_one.call_args_list[1].args[0]
        self.assertEqual(first["_id"], second["_id"])

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

    def test_disconnect_activity_card_includes_completed_session_summary(self):
        self.processor.telemetry_sessions = MagicMock()
        self.processor.telemetry_sessions.disconnected.return_value = {
            "status": "accepted", "duplicate": False, "save_key": "main-save",
            "summary": {"total_counted_minutes": 14, "active_minutes": 1,
                         "idle_minutes": 11, "afk_minutes": 2},
        }
        event = self.event("player_disconnected")
        event["payload"].update({"session_id": "session-1", "user_id": "2", "farm_id": "2"})
        self.processor.process(event)
        message = self.database.db.activity_outbox.insert_one.call_args.args[0]["message"]
        self.assertIn("Session: 14 min", message)
        self.assertIn("Active: 1 min", message)
        self.assertIn("AFK: 2 min", message)

    def test_disconnect_watermark_waits_for_all_minutes_before_completion(self):
        event = self.event("player_disconnected")
        event["payload"].update({"session_id": "session-1", "final_minute_sequence": 2})
        self.database.db.player_activity_sessions.find_one.return_value = None
        self.database.db.player_activity_minutes.find.return_value = [{"minute_sequence": 1}]
        with self.assertRaises(EventRetryableError):
            self.processor.process(event)
        self.database.db.player_activity_minutes.find.return_value = [
            {"minute_sequence": 1}, {"minute_sequence": 2}]
        result = self.processor.process(event)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.database.db.player_activity_sessions.update_one.call_args.args[1]["$set"]["state"], "completed")

    def test_map_geometry_event_is_validated_and_persisted_for_jin(self):
        event = self.event("map_geometry")
        event["world_id"] = "hobo-world"
        event["payload"] = {"map": {
            "schema_version": 1, "map_id": "synthetic", "map_title": "Synthetic",
            "world_width": 100, "world_depth": 100, "image_width": 32, "image_height": 32,
            "overview_asset_identity": "runtime-generated:synthetic", "version": 1,
            "image_y_inverted": True, "fields": {"22": {
                "field_id": 22, "farmland_id": 22,
                "rings": [[[-10, -10], [10, -10], [0, 10]]], "area_ha": 1,
            }}, "farmlands": {},
        }}
        result = self.processor.process(event)
        self.assertEqual(result["map_id"], "synthetic")
        self.assertEqual(result["map_persistence"], "inserted")
        update = self.database.db.sin_maps.update_one.call_args.args[1]
        self.assertEqual(update["$set"]["map_payload"]["map_id"], "synthetic")
        self.assertEqual(update["$setOnInsert"]["world_id"], "hobo-world")
        self.assertTrue(set(update["$setOnInsert"]).isdisjoint(update["$set"]))

    def test_map_geometry_world_id_at_event_root_is_used_when_runtime_is_already_active(self):
        event = self.event("map_geometry")
        event["world_id"] = "hobo-world"
        event["payload"] = {"map": {
            "schema_version": 1, "map_id": "hobo", "map_title": "Hobo's Hollow",
            "world_width": 100, "world_depth": 100, "image_width": 32, "image_height": 32,
            "overview_asset_identity": "runtime-generated:hobo", "version": 1,
            "fields": {"1": {"field_id": 1, "farmland_id": 22,
                              "rings": [[[-10, -10], [10, -10], [0, 10]]]}},
            "farmlands": {},
        }}
        self.processor.farm_lifecycle.current_world_id = MagicMock(return_value="hobo-world")
        self.processor.farm_lifecycle.require_current_world = MagicMock(return_value="hobo-world")
        result = self.processor.process(event)
        self.assertEqual(result["map_persistence"], "inserted")
        update = self.database.db.sin_maps.update_one.call_args.args[1]
        self.assertEqual(update["$setOnInsert"]["world_id"], "hobo-world")
        self.processor.farm_lifecycle.require_current_world.assert_called_once_with(
            "sin-fs25-01", "main-save", "hobo-world")

    def test_map_geometry_before_snapshot_is_retained_under_its_world_not_legacy_scope(self):
        event = self.event("map_geometry")
        event["world_id"] = "new-world"
        event["payload"] = {"map": {
            "schema_version": 1, "map_id": "new-map", "map_title": "New Map",
            "world_width": 100, "world_depth": 100, "image_width": 32, "image_height": 32,
            "overview_asset_identity": "runtime-generated:new", "version": 1,
            "fields": {"1": {"field_id": 1, "farmland_id": 22,
                              "rings": [[[-10, -10], [10, -10], [0, 10]]]}},
            "farmlands": {},
        }}
        self.processor.farm_lifecycle.current_world_id = MagicMock(return_value=None)
        result = self.processor.process(event)
        self.assertEqual(result["map_persistence"], "inserted")
        update = self.database.db.sin_maps.update_one.call_args.args[1]
        self.assertEqual(update["$setOnInsert"]["world_id"], "new-world")

    def test_newer_map_geometry_waits_for_snapshot_activation(self):
        event = self.event("map_geometry")
        event["world_id"] = "new-world"
        event["payload"] = {
            "source_generation": "19",
            "map": {
                "schema_version": 1, "map_id": "new-map", "map_title": "New Map",
                "world_width": 100, "world_depth": 100, "image_width": 32, "image_height": 32,
                "overview_asset_identity": "runtime-generated:new", "version": 1,
                "fields": {"1": {"field_id": 1, "farmland_id": 22,
                                  "rings": [[[-10, -10], [10, -10], [0, 10]]]}},
                "farmlands": {},
            },
        }
        self.processor.farm_lifecycle.current_world_id = MagicMock(return_value="old-world")
        self.processor.farm_lifecycle.require_current_world = MagicMock(side_effect=ValueError(
            "FS25 world generation is not current for this server/save")
        )
        self.processor.registry.active_runtime.return_value = {
            "runtime_generation": 18, "save_key": "main-save", "world_id": "old-world"}
        with self.assertRaises(EventRetryableError):
            self.processor.process(event)
        self.database.db.sin_maps.update_one.assert_not_called()

    def test_old_map_geometry_remains_a_permanent_scope_error(self):
        event = self.event("map_geometry")
        event["world_id"] = "old-world"
        event["payload"] = {
            "source_generation": "18",
            "map": {
                "schema_version": 1, "map_id": "old-map", "map_title": "Old Map",
                "world_width": 100, "world_depth": 100, "image_width": 32, "image_height": 32,
                "overview_asset_identity": "runtime-generated:old", "version": 1,
                "fields": {"1": {"field_id": 1, "farmland_id": 22,
                                  "rings": [[[-10, -10], [10, -10], [0, 10]]]}},
                "farmlands": {},
            },
        }
        self.processor.farm_lifecycle.current_world_id = MagicMock(return_value="new-world")
        self.processor.farm_lifecycle.require_current_world = MagicMock(side_effect=ValueError(
            "FS25 world generation is not current for this server/save")
        )
        self.processor.registry.active_runtime.return_value = {
            "runtime_generation": 19, "save_key": "main-save", "world_id": "new-world"}
        with self.assertRaises(EventScopeError):
            self.processor.process(event)

    def test_map_geometry_without_world_id_is_rejected(self):
        event = self.event("map_geometry")
        event["payload"] = {"map": {}}
        with self.assertRaises(EventScopeError):
            self.processor.process(event)
