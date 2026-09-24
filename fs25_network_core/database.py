"""MongoDB storage. Banking requires a replica set for atomic transactions."""
import os

from pymongo import MongoClient
from .config import load_local_environment, required_setting


class Database:
    def __init__(self, uri=None, name=None):
        load_local_environment()
        self.client = MongoClient(uri or required_setting("MONGODB_URI"), serverSelectionTimeoutMS=5000)
        self.db = self.client[name or os.environ.get("MONGODB_DATABASE", "fs25_network")]
        self.name = self.db.name

    @staticmethod
    def _replace_legacy_unique_index(collection, legacy_keys, desired_keys, name, allow_partial=False, **options):
        """Replace only the known pre-scope unique index, if it exists.

        Older deployments created global uniqueness for event/transfer fields.
        Do not drop an arbitrary index: the key pattern and unique flag must
        match the exact legacy definition before migration is attempted.
        """
        try:
            indexes = collection.index_information()
        except Exception:
            indexes = {}
        for index_name, info in (indexes.items() if isinstance(indexes, dict) else ()):
            if (info.get("unique") and list(info.get("key", [])) == list(legacy_keys)
                    and (allow_partial or not info.get("partialFilterExpression"))):
                collection.drop_index(index_name)
        collection.create_index(desired_keys, name=name, **options)

    def initialize(self):
        hello = self.client.admin.command("hello")
        if not hello.get("setName") and hello.get("msg") != "isdbgrid":
            raise RuntimeError("Banking requires MongoDB configured as a replica set")
        self.db.farm_links.create_index([("server_id", 1), ("save_id", 1), ("discord_id", 1)], unique=True)
        self.db.farm_links.create_index([("server_id", 1), ("save_id", 1), ("farm_id", 1)], unique=True)
        self._replace_legacy_unique_index(
            self.db.transfers,
            [("server_id", 1), ("save_id", 1), ("event_id", 1)],
            [("server_id", 1), ("save_id", 1), ("event_id", 1)],
            name="verified_transfer_event_scope",
            unique=True,
            partialFilterExpression={"event_id": {"$type": "string"}},
        )
        self.db.ledger_entries.create_index("transaction_id", unique=True)
        self.db.wallet_transfers.create_index("transaction_id", unique=True)
        self.db.withdrawals.create_index([("state", 1), ("created_at", 1)])
        self.db.deposit_requests.create_index([("state", 1), ("created_at", 1)])
        self.db.link_codes.create_index("expires_at", expireAfterSeconds=0)
        self.db.game_identities.create_index([("server_id", 1), ("save_id", 1), ("discord_id", 1)], unique=True)
        self.db.game_identities.create_index([("server_id", 1), ("save_id", 1), ("game_player_id", 1)], unique=True)
        self.db.game_identities.create_index([("server_id", 1), ("save_id", 1), ("fs25_unique_user_id", 1)], unique=True, sparse=True)
        self._replace_legacy_unique_index(
            self.db.observed_fs25_identities,
            [("server_id", 1), ("save_id", 1), ("fs25_unique_user_id", 1)],
            [("server_id", 1), ("save_id", 1), ("world_id", 1), ("fs25_unique_user_id", 1)],
            name="observed_identity_world_scope", unique=True)
        self.db.registration_codes.create_index("expires_at", expireAfterSeconds=0)
        self.db.registration_codes.create_index("token_hash", unique=True, sparse=True)
        self._replace_legacy_unique_index(
            self.db.memberships,
            [("server_id", 1), ("save_id", 1), ("farm_id", 1)],
            [("server_id", 1), ("save_id", 1), ("world_id", 1), ("farm_id", 1)],
            name="one_manager_per_farm", unique=True,
            partialFilterExpression={"desired_role": "farm_manager"}, allow_partial=True)
        self.db.permission_jobs.create_index([("server_id", 1), ("save_id", 1), ("state", 1)])
        self.db.farm_requests.create_index([("server_id", 1), ("save_id", 1), ("state", 1), ("created_at", 1)])
        self.db.land_operations.create_index([("server_id", 1), ("save_id", 1), ("request_id", 1)], unique=True)
        self.db.farm_field_reservations.create_index(
            [("server_key", 1), ("save_key", 1), ("world_id", 1), ("farmland_id", 1)], unique=True)
        self.db.farm_operations.create_index([("server_key", 1), ("save_key", 1), ("state", 1), ("created_at", 1)])
        # World replacement is a Mongo transaction. Ensure every collection
        # touched by the generation transition exists before a transaction
        # attempts to update it (MongoDB cannot implicitly create a namespace
        # from inside a transaction on all supported deployments).
        self.db.world_generations.create_index(
            [("server_key", 1), ("save_key", 1), ("state", 1)])
        self.db.world_continuity_migrations.create_index(
            [("server_key", 1), ("save_key", 1), ("source_world_id", 1), ("target_world_id", 1)],
            unique=True)
        self.db.world_continuity_migrations.create_index([("status", 1), ("created_at", -1)])
        self.db.fs25_money_operations.create_index(
            [("server_key", 1), ("save_key", 1), ("world_id", 1), ("state", 1)])
        self.db.bank_bridge_operations.create_index(
            [("server_key", 1), ("save_key", 1), ("world_id", 1), ("state", 1)])
        self.db.farm_financial_provisioning.create_index(
            [("server_key", 1), ("save_key", 1), ("world_id", 1), ("state", 1)])
        self.db.sin_farms.create_index([("server_key", 1), ("save_key", 1), ("farm_type", 1), ("canonical_name", 1)])
        self.db.sin_farms.create_index("source_request_id", unique=True,
            partialFilterExpression={"source_request_id": {"$type": "string"}})
        self._replace_legacy_unique_index(
            self.db.sin_farms,
            [("server_key", 1), ("save_key", 1), ("owner_discord_id", 1)],
            [("server_key", 1), ("save_key", 1), ("world_id", 1), ("owner_discord_id", 1)],
            name="one_personal_farm_per_world", unique=True,
            partialFilterExpression={"owner_discord_id": {"$type": "string"}}, allow_partial=True)
        self._replace_legacy_unique_index(
            self.db.server_snapshots,
            [("server_key", 1), ("save_key", 1)],
            [("server_key", 1), ("save_key", 1), ("world_id", 1)],
            name="snapshot_world_scope", unique=True)
        self.db.community_applications.create_index([("state", 1), ("submitted_at", 1)])
        self.db.sin_servers.create_index("server_key", unique=True)
        self.db.sin_saves.create_index([("server_key", 1), ("fs25_save_id", 1)], unique=True)
        self.db.sin_saves.create_index([("server_key", 1), ("save_key", 1)], unique=True)
        self._replace_legacy_unique_index(
            self.db.sin_maps,
            [("server_key", 1), ("save_key", 1)],
            [("server_key", 1), ("save_key", 1), ("world_id", 1)],
            name="map_world_scope", unique=True)
        self.db.processed_server_events.create_index("processed_at", expireAfterSeconds=604800)
        self.db.activity_outbox.create_index([("status", 1), ("created_at", 1)])
        self.db.server_status_cards.create_index("server_key", unique=True)
        self._replace_legacy_unique_index(
            self.db.activity_outbox,
            [("source_event_id", 1)],
            [("server_key", 1), ("save_key", 1), ("world_id", 1), ("source_event_id", 1)],
            name="activity_event_world_scope",
            unique=True,
        )
        self.db.player_activity_minutes.create_index("interval_key", unique=True)
        self.db.player_activity_minutes.create_index([
            ("server_key", 1), ("save_key", 1), ("fs25_unique_user_id", 1), ("observed_at", 1)])
        self._replace_legacy_unique_index(
            self.db.player_activity_aggregates,
            [("server_key", 1), ("save_key", 1), ("fs25_unique_user_id", 1)],
            [("server_key", 1), ("save_key", 1), ("world_id", 1), ("fs25_unique_user_id", 1)],
            name="activity_aggregate_world_scope", unique=True)
        self.db.player_activity_sessions.create_index([
            ("server_key", 1), ("save_key", 1), ("fs25_unique_user_id", 1), ("connected_at", -1)])
        self.db.player_activity_sessions.create_index([
            ("server_key", 1), ("save_key", 1), ("fs25_unique_user_id", 1), ("state", 1)])
        self._replace_legacy_unique_index(
            self.db.chat_messages,
            [("message_id", 1)],
            [("server_key", 1), ("save_key", 1), ("message_id", 1)],
            name="chat_message_scope",
            unique=True,
        )
        self.db.chat_messages.create_index([("server_key", 1), ("created_at", -1)])
        self.db.contracts.create_index("contract_id", unique=True)
        self.db.contracts.create_index([("status", 1), ("created_at", -1)])
        self.db.invoices.create_index("invoice_id", unique=True)
        self.db.invoices.create_index([("recipient_discord_id", 1), ("status", 1)])
        self.db.community_events.create_index("event_id", unique=True)
        self.db.community_events.create_index([("status", 1), ("scheduled_start", 1)])
        self.db.transfers.create_index("transfer_id", unique=True)
        self.db.transfers.create_index([("kind", 1), ("status", 1), ("created_at", -1)])

    def atomic(self, callback):
        with self.client.start_session() as session:
            return session.with_transaction(callback)
