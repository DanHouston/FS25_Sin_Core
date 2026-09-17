import json
import tempfile
import unittest
from pathlib import Path

from fs25_network_core.integration_campaign import (SCENARIOS, SEMANTIC_SCENARIOS,
                                                     AUTHORITATIVE_SCENARIOS, BoundaryCentral,
                                                     authoritative_manifest, run_named_scenario)
from scripts.run_integration_campaign import run


class IntegrationCampaignTests(unittest.TestCase):
    def test_registry_contains_required_named_scenarios(self):
        self.assertEqual(SCENARIOS.names(), (
            "offline-full", "artifact-and-snapshot", "mailbox-roundtrip",
            "event-ordering", "registration-roundtrip", "map-geometry-boundary"))

    def test_authoritative_manifest_declares_shared_adapter_contracts(self):
        manifest = authoritative_manifest()
        self.assertEqual(manifest["required_scenarios"], list(AUTHORITATIVE_SCENARIOS.names()))
        self.assertEqual({adapter["id"] for adapter in manifest["adapters"]}, {
            "mailbox_xml", "central_http", "central_processor", "memory_persistence",
            "activity_publisher", "map_service", "networkbot_presentation"})
        self.assertEqual(manifest["scenario_boundaries"]["map_contract"], [
            "mailbox_xml", "central_http", "central_processor", "memory_persistence",
            "map_service", "networkbot_presentation"])

    def test_each_named_scenario_returns_structured_pass_report(self):
        for name in SCENARIOS.names():
            report = run_named_scenario(name)
            self.assertEqual(report["scenario"], name)
            self.assertEqual(report["report_schema"], "sin.integration/1")
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["checks"])

    def test_semantic_registry_contains_six_operator_workflows(self):
        self.assertEqual(tuple(SEMANTIC_SCENARIOS), (
            "server-pairing", "farm-lifecycle", "player-registration",
            "activity-telemetry", "map-discovery", "release-evidence"))
        for name in SEMANTIC_SCENARIOS:
            self.assertEqual(run_named_scenario(name)["status"], "passed")

    def test_authoritative_registry_contains_required_scenarios(self):
        self.assertEqual(AUTHORITATIVE_SCENARIOS.names(), (
            "registration", "control_plane_backlog", "activity_disconnect",
            "map_contract", "contract_scope", "authority_regression"))
        for name in AUTHORITATIVE_SCENARIOS.names():
            if name == "control_plane_backlog":
                # The dedicated composed-backlog test below owns the measured
                # 10,000-event fixture; avoid paying for it twice in this
                # registry-shape test.
                continue
            report = run_named_scenario(name)
            self.assertEqual(report["scenario"], name)
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["checks"])

    def test_authoritative_reports_prove_replay_and_authentication_boundaries(self):
        activity = run_named_scenario("activity_disconnect")
        mapping = run_named_scenario("map_contract")
        registration = run_named_scenario("registration")
        self.assertTrue(activity["activity_disconnect"]["replay_is_idempotent"])
        self.assertTrue(mapping["map_contract"]["replay_is_idempotent"])
        self.assertGreater(registration["registration"]["authenticated_requests"], 0)

    def test_activity_disconnect_report_proves_composed_lifecycle_and_delivery(self):
        evidence = run_named_scenario("activity_disconnect")["activity_disconnect"]
        self.assertEqual(evidence["forwarded_event_types"].count("player_connected"), 1)
        self.assertEqual(evidence["forwarded_event_types"].count("player_activity_minute"), 12)
        self.assertEqual(evidence["forwarded_event_types"].count("player_disconnected"), 4)
        self.assertEqual(evidence["expected_totals"], {
            "connected_minutes": 12, "active_minutes": 1,
            "idle_minutes": 10, "afk_minutes": 1})
        self.assertEqual(evidence["observed_totals"], evidence["expected_totals"])
        self.assertEqual(evidence["lifecycle"]["disconnect_watermark"], 12)
        self.assertEqual(evidence["lifecycle"]["afk_sequences"], [12])
        self.assertTrue(evidence["retryable_early_disconnect"])
        self.assertEqual(evidence["early_disconnect_outbox_count"], 0)
        self.assertTrue(evidence["outbox_repaired"])
        self.assertTrue(evidence["duplicate_disconnect_idempotent"])
        self.assertTrue(evidence["crash_repair"]["completed_session_preserved"])
        self.assertEqual(evidence["outbox_published"], {
            "count": 1, "status": "published", "captured_messages": 1})
        message = evidence["networkbot_completed_session"]["message"]
        for line in ("Duration: 12 min", "Session: 12 min", "Active: 1 min",
                     "Idle: 10 min", "AFK: 1 min"):
            self.assertIn(line, message)

    def test_composed_backlog_and_central_map_boundaries(self):
        backlog = run_named_scenario("control_plane_backlog")
        mapping = run_named_scenario("map_contract")
        evidence = backlog["control_plane_backlog"]
        self.assertEqual(evidence["backlog_size"], 10000)
        self.assertEqual(evidence["processed_events"], 10000)
        self.assertEqual(mapping["map_contract"]["central_result"]["map_id"], "campaign-map")

    def test_map_contract_report_proves_real_vertical_slice(self):
        evidence = run_named_scenario("map_contract")["map_contract"]
        self.assertEqual(evidence["input"]["field_ids"], [22, 47])
        self.assertEqual(evidence["agent"]["forwarded_field_ids"], ["22", "47"])
        self.assertEqual(evidence["agent"]["forwarded_server_key"], "sin-campaign")
        self.assertEqual(evidence["agent"]["forwarded_save_id"], "1")
        self.assertTrue(evidence["agent"]["replay_is_idempotent"])
        self.assertEqual(evidence["central"]["fields"], ["22", "47"])
        self.assertEqual(evidence["downstream_load"]["source"], "persisted_sin_maps")
        self.assertTrue(evidence["downstream_load"]["stable_refresh_avoided"])
        self.assertTrue(evidence["downstream_load"]["changed_refresh_performed"])
        self.assertEqual(evidence["contract"]["requested_field"], "22")
        self.assertTrue(evidence["contract"]["persisted"])
        self.assertEqual(evidence["render"]["highlighted_field"], [22])
        self.assertTrue(evidence["render"]["irregular_field_47_not_highlighted"])
        self.assertTrue(evidence["presentation"]["path"].endswith("publish_contract_card"))
        self.assertEqual(evidence["presentation"]["attachment_filename"], "sin-map.png")
        self.assertTrue(evidence["presentation"]["png_signature"])
        self.assertTrue(evidence["presentation"]["png_valid"])
        self.assertGreater(evidence["presentation"]["attachment_bytes"], 0)
        self.assertTrue(evidence["presentation"]["text_only_fallback"])
        self.assertTrue(evidence["invalid_geometry"]["rejected_and_quarantined"])
        self.assertTrue(evidence["invalid_geometry"]["prior_valid_state_preserved"])
        self.assertTrue(evidence["invalid_geometry"]["presentation_unchanged"])
        self.assertTrue(evidence["filesystem_safety"]["unsafe_identity_rejected"])
        self.assertTrue(evidence["filesystem_safety"]["arbitrary_path_lookup_rejected"])

    def test_contract_scope_report_proves_context_selection_persistence_and_fallback(self):
        evidence = run_named_scenario("contract_scope")["contract_scope"]
        single = evidence["single"]
        self.assertEqual(single["eligible_context_count"], 1)
        self.assertEqual(single["selection_mode"], "automatic")
        self.assertEqual(single["persisted_scope"], {
            "scope": "server", "server_key": "server-a", "save_key": "save-a"})
        multiple = evidence["multiple"]
        self.assertEqual(multiple["eligible_context_count"], 2)
        self.assertEqual(multiple["result"], "selection_required")
        self.assertFalse(multiple["contract_created"])
        self.assertEqual(multiple["persisted_contract_count_change"], 0)
        self.assertEqual(multiple["presentation_count_change"], 0)
        explicit = evidence["explicit_selector"]
        self.assertEqual(explicit["selector"], "server-b")
        self.assertEqual(explicit["persisted_scope"], {
            "scope": "server", "server_key": "server-b", "save_key": "save-b"})
        self.assertFalse(explicit["other_context_selected"])
        self.assertFalse(evidence["invalid_selector"]["contract_created"])
        network = evidence["network_wide"]
        self.assertTrue(network["explicit_request"])
        self.assertEqual(network["persisted_scope"], {
            "scope": "network", "server_key": None, "save_key": None})
        self.assertTrue(evidence["jobs_channel"]["matched"])
        self.assertEqual(evidence["presentation"]["path"], "NetworkBot.publish_contract_card")
        self.assertTrue(evidence["fallback"]["render_failure"])
        self.assertTrue(evidence["fallback"]["scope_preserved"])
        self.assertTrue(evidence["fallback"]["text_published"])
        self.assertEqual(evidence["fallback"]["attachment_count"], 0)

    def test_central_boundary_rejects_wrong_credentials(self):
        from urllib.request import Request
        central = BoundaryCentral()
        request = Request("http://offline/api/server/events", data=b"{}", method="POST",
                          headers={"X-SiN-Server-Key": "wrong", "Authorization": "Bearer wrong"})
        with self.assertRaises(AssertionError):
            central(request)

    def test_offline_campaign_writes_secret_free_pass_report(self):
        with tempfile.TemporaryDirectory() as folder:
            report_path = Path(folder) / "campaign.json"
            report = run(report_path)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["mailbox"]["central_receipts"], 1)
            self.assertEqual(report["events"]["central_event_types"],
                             ["player_activity_minute", "player_disconnected"])
            self.assertNotIn("campaign-secret", report_path.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)


if __name__ == "__main__":
    unittest.main()
