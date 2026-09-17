import unittest
from unittest.mock import MagicMock

from fs25_network_core.activity_telemetry import (ActivityMinuteTracker, ActivityTelemetryProcessor,
                                                  ActivitySessionProcessor)


class ActivityMinuteTrackerTests(unittest.TestCase):
    def test_movement_is_active(self):
        tracker = ActivityMinuteTracker()
        self.assertIsNone(tracker.observe_completed_minute((0, 0)))
        result = tracker.observe_completed_minute((1, 0))
        self.assertEqual(result["bucket"], "active")

    def test_stationary_minutes_ten_idle_and_eleven_afk(self):
        tracker = ActivityMinuteTracker()
        tracker.observe_completed_minute((0, 0))
        results = [tracker.observe_completed_minute((0, 0)) for _ in range(11)]
        self.assertEqual([result["bucket"] for result in results[:10]], ["idle"] * 10)
        self.assertEqual(results[10]["bucket"], "afk")
        self.assertEqual(results[10]["inactive_minutes"], 11)

    def test_continued_inactivity_is_afk_without_reclassifying_idle(self):
        tracker = ActivityMinuteTracker()
        tracker.observe_completed_minute((0, 0))
        results = [tracker.observe_completed_minute((0, 0)) for _ in range(15)]
        self.assertEqual(sum(result["bucket"] == "idle" for result in results), 10)
        self.assertEqual(sum(result["bucket"] == "afk" for result in results), 5)
        self.assertEqual(tracker.connected_minutes, 15)
        self.assertEqual(tracker.active_minutes + tracker.idle_minutes + tracker.afk_minutes,
                         tracker.connected_minutes)

    def test_activity_after_afk_resets_inactivity(self):
        tracker = ActivityMinuteTracker()
        tracker.observe_completed_minute((0, 0))
        for _ in range(11):
            tracker.observe_completed_minute((0, 0))
        result = tracker.observe_completed_minute((1, 0))
        self.assertEqual(result["bucket"], "active")
        self.assertEqual(result["inactive_minutes"], 0)
        self.assertEqual(tracker.current_state, "active")

    def test_first_sample_disconnect_and_reconnect_do_not_manufacture_minutes(self):
        tracker = ActivityMinuteTracker()
        self.assertIsNone(tracker.observe_completed_minute((0, 0)))
        tracker.disconnect()
        self.assertIsNone(tracker.observe_completed_minute((1, 0)))
        self.assertEqual(tracker.connected_minutes, 0)
        tracker.connect()
        self.assertIsNone(tracker.observe_completed_minute((5, 5)))
        self.assertEqual(tracker.inactive_minutes, 0)

    def test_jitter_below_tolerance_is_not_activity(self):
        tracker = ActivityMinuteTracker(movement_tolerance=0.5)
        tracker.observe_completed_minute((0, 0))
        result = tracker.observe_completed_minute((0.1, 0.1))
        self.assertEqual(result["bucket"], "idle")


class ActivityTelemetryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.database.atomic.side_effect = lambda callback: callback("session")
        self.db = self.database.db
        self.db.player_activity_minutes.find_one.return_value = None
        self.processor = ActivityTelemetryProcessor(self.database)

    def payload(self, **changes):
        result = {"unique_user_id": "stable-player", "user_id": "2", "farm_id": "1",
                  "display_name": "Observed", "session_id": "session-1",
                  "minute_sequence": 1, "activity_bucket": "active", "inactive_minutes": 0}
        result.update(changes)
        return result

    def test_persists_one_interval_and_cumulative_delta(self):
        result = self.processor.process("server", "save", "event-1", self.payload())
        self.assertFalse(result["duplicate"])
        minute = self.db.player_activity_minutes.insert_one.call_args.args[0]
        self.assertEqual(minute["fs25_unique_user_id"], "stable-player")
        aggregate_update = self.db.player_activity_aggregates.update_one.call_args.args[1]
        self.assertEqual(aggregate_update["$inc"], {"connected_minutes": 1, "active_minutes": 1})
        self.assertTrue(set(aggregate_update["$setOnInsert"]).isdisjoint(aggregate_update["$set"]))
        self.assertTrue(set(aggregate_update["$setOnInsert"]).isdisjoint(aggregate_update["$inc"]))

    def test_duplicate_interval_does_not_double_count(self):
        self.processor.process("server", "save", "event-1", self.payload())
        self.db.player_activity_minutes.find_one.return_value = {"interval_key": "already-seen"}
        result = self.processor.process("server", "save", "event-1-retry", self.payload())
        self.assertTrue(result["duplicate"])
        self.assertEqual(self.db.player_activity_minutes.insert_one.call_count, 1)
        self.assertEqual(self.db.player_activity_aggregates.update_one.call_count, 1)

    def test_same_stable_identity_survives_transient_user_id_change(self):
        self.processor.process("server", "save", "event-1", self.payload(user_id="2"))
        self.db.player_activity_minutes.find_one.return_value = None
        self.processor.process("server", "save", "event-2", self.payload(user_id="9", minute_sequence=2))
        aggregate_filters = [call.args[0] for call in self.db.player_activity_aggregates.update_one.call_args_list]
        self.assertEqual(aggregate_filters[0]["_id"], aggregate_filters[1]["_id"])

    def test_event_storage_id_is_scoped_by_server_and_save(self):
        self.processor.process("server-a", "save", "event-1", self.payload())
        self.processor.process("server-b", "save", "event-1", self.payload())
        documents = [call.args[0] for call in self.db.player_activity_minutes.insert_one.call_args_list]
        self.assertEqual(len(documents), 2)
        self.assertNotEqual(documents[0]["_id"], documents[1]["_id"])

    def test_pseudo_user_is_ignored(self):
        result = self.processor.process("server", "save", "event-server", self.payload(
            user_id="1", farm_id="0", display_name="Server"))
        self.assertTrue(result["ignored"])
        self.db.player_activity_minutes.insert_one.assert_not_called()

    def test_invalid_bucket_or_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            self.processor.process("server", "save", "event-invalid", self.payload(
                activity_bucket="afk", inactive_minutes=10))


class ActivitySessionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.db = self.database.db
        self.db.player_activity_sessions.find_one.return_value = None
        self.processor = ActivitySessionProcessor(self.database)

    def payload(self, **changes):
        result = {"unique_user_id": "stable-player", "user_id": "2", "farm_id": "1",
                  "display_name": "Observed", "session_id": "session-1"}
        result.update(changes)
        return result

    def test_connect_disconnect_creates_one_completed_session(self):
        connected = self.processor.connected("server", "save", "connect-event", self.payload())
        self.assertEqual(connected["session_id"], "session-1")
        self.db.player_activity_sessions.find_one.return_value = {
            "_id": "session-key", "state": "active"}
        disconnected = self.processor.disconnected("server", "save", "disconnect-event", self.payload())
        self.assertFalse(disconnected["duplicate"])
        update = self.db.player_activity_sessions.update_one.call_args.args[1]
        self.assertEqual(update["$set"]["state"], "completed")
        self.db.player_activity_sessions.find_one.return_value = {"state": "completed"}
        duplicate = self.processor.disconnected("server", "save", "disconnect-retry", self.payload())
        self.assertTrue(duplicate["duplicate"])

    def test_dedicated_server_session_is_ignored(self):
        result = self.processor.connected("server", "save", "event", self.payload(
            user_id="1", farm_id="0", display_name="Server"))
        self.assertTrue(result["ignored"])
        self.db.player_activity_sessions.update_one.assert_not_called()

    def test_disconnect_returns_completed_session_summary(self):
        self.db.player_activity_sessions.find_one.side_effect = [
            {"_id": "session-key", "state": "active"},
            {"_id": "session-key", "state": "completed", "total_counted_minutes": 14,
             "active_minutes": 1, "idle_minutes": 11, "afk_minutes": 2},
        ]
        result = self.processor.disconnected("server", "save", "disconnect-event", self.payload())
        self.assertEqual(result["summary"], {"total_counted_minutes": 14, "active_minutes": 1,
                                               "idle_minutes": 11, "afk_minutes": 2})
