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

    def initialize(self):
        hello = self.client.admin.command("hello")
        if not hello.get("setName") and hello.get("msg") != "isdbgrid":
            raise RuntimeError("Banking requires MongoDB configured as a replica set")
        self.db.farm_links.create_index([("server_id", 1), ("save_id", 1), ("discord_id", 1)], unique=True)
        self.db.farm_links.create_index([("server_id", 1), ("save_id", 1), ("farm_id", 1)], unique=True)
        self.db.transfers.create_index([("server_id", 1), ("save_id", 1), ("event_id", 1)], unique=True)
        self.db.withdrawals.create_index([("state", 1), ("created_at", 1)])
        self.db.link_codes.create_index("expires_at", expireAfterSeconds=0)
        self.db.game_identities.create_index([("server_id", 1), ("save_id", 1), ("discord_id", 1)], unique=True)
        self.db.game_identities.create_index([("server_id", 1), ("save_id", 1), ("game_player_id", 1)], unique=True)
        self.db.game_identities.create_index([("server_id", 1), ("save_id", 1), ("fs25_unique_user_id", 1)], unique=True, sparse=True)
        self.db.observed_fs25_identities.create_index([("server_id", 1), ("save_id", 1), ("fs25_unique_user_id", 1)], unique=True)
        self.db.registration_codes.create_index("expires_at", expireAfterSeconds=0)
        self.db.registration_codes.create_index("token_hash", unique=True, sparse=True)
        self.db.memberships.create_index([("server_id", 1), ("save_id", 1), ("farm_id", 1)],
            unique=True, partialFilterExpression={"desired_role": "farm_manager"}, name="one_manager_per_farm")
        self.db.permission_jobs.create_index([("server_id", 1), ("save_id", 1), ("state", 1)])
        self.db.farm_requests.create_index([("server_id", 1), ("save_id", 1), ("state", 1), ("created_at", 1)])
        self.db.land_operations.create_index([("server_id", 1), ("save_id", 1), ("request_id", 1)], unique=True)
        self.db.farm_operations.create_index([("server_key", 1), ("save_key", 1), ("state", 1), ("created_at", 1)])
        self.db.sin_farms.create_index([("server_key", 1), ("save_key", 1), ("farm_type", 1), ("canonical_name", 1)])
        self.db.sin_farms.create_index("source_request_id", unique=True,
            partialFilterExpression={"source_request_id": {"$type": "string"}})
        self.db.sin_farms.create_index([("server_key", 1), ("save_key", 1), ("owner_discord_id", 1)],
            unique=True, partialFilterExpression={"owner_discord_id": {"$type": "string"}})
        self.db.server_snapshots.create_index([("server_key", 1), ("save_key", 1)], unique=True)
        self.db.community_applications.create_index([("state", 1), ("submitted_at", 1)])
        self.db.sin_servers.create_index("server_key", unique=True)
        self.db.sin_saves.create_index([("server_key", 1), ("fs25_save_id", 1)], unique=True)
        self.db.sin_saves.create_index([("server_key", 1), ("save_key", 1)], unique=True)
        self.db.processed_server_events.create_index("processed_at", expireAfterSeconds=604800)
        self.db.activity_outbox.create_index([("status", 1), ("created_at", 1)])
        self.db.activity_outbox.create_index("source_event_id", unique=True)

    def atomic(self, callback):
        with self.client.start_session() as session:
            return session.with_transaction(callback)
