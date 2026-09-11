"""MongoDB storage. Banking requires a replica set for atomic transactions."""
import os

from pymongo import MongoClient
from .config import load_local_environment, required_setting


class Database:
    def __init__(self, uri=None, name=None):
        load_local_environment()
        self.client = MongoClient(uri or required_setting("MONGODB_URI"), serverSelectionTimeoutMS=5000)
        self.db = self.client[name or os.environ.get("MONGODB_DATABASE", "fs25_network")]

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
        self.db.memberships.create_index([("server_id", 1), ("save_id", 1), ("farm_id", 1)],
            unique=True, partialFilterExpression={"desired_role": "farm_manager"}, name="one_manager_per_farm")
        self.db.permission_jobs.create_index([("server_id", 1), ("save_id", 1), ("state", 1)])
        self.db.farm_requests.create_index([("server_id", 1), ("save_id", 1), ("state", 1), ("created_at", 1)])
        self.db.community_applications.create_index([("state", 1), ("submitted_at", 1)])

    def atomic(self, callback):
        with self.client.start_session() as session:
            return session.with_transaction(callback)
