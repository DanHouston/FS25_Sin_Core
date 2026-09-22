"""Authoritative FS25 world-generation isolation.

``server_key`` and configured ``save_key`` identify a SiN endpoint, not an
immutable FS25 world.  A server mod supplies a persistent opaque marker from
the actual FS25 savegame directory.  Central uses it to keep numeric game
identities (farm, farmland, field, and future asset IDs) out of replacement
worlds which reuse the same configured scope.
"""
from datetime import datetime, timezone


WORLD_BOUND_COLLECTIONS = (
    "sin_farms", "farm_requests", "farm_operations", "land_operations",
    "memberships", "permission_jobs", "server_snapshots", "sin_maps",
    "deposit_requests", "withdrawals", "fs25_money_operations",
    "bank_bridge_operations", "farm_financial_provisioning",
)


class WorldGenerationRegistry:
    """Activate one opaque FS25-save marker per configured server/save."""

    def __init__(self, database):
        self.database, self.db = database, database.db

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _scope(server_key, save_key):
        return {"server_key": str(server_key), "save_key": str(save_key)}

    @staticmethod
    def validate(world_id):
        value = str(world_id or "").strip()
        if not value or len(value) > 160:
            raise ValueError("authoritative FS25 world generation is required")
        return value

    def active_id(self, server_key, save_key):
        row = self.db.world_generations.find_one({**self._scope(server_key, save_key), "state": "active"})
        return str(row["world_id"]) if isinstance(row, dict) and row.get("world_id") else None

    def require_active(self, server_key, save_key, world_id):
        expected = self.active_id(server_key, save_key)
        actual = self.validate(world_id)
        if expected != actual:
            raise ValueError("FS25 world generation is not current for this server/save")
        return actual

    def activate(self, server_key, save_key, world_id, *, evidence=None):
        """Record a game snapshot's marker and archive a replaced world.

        This deliberately retains historical rows.  Pending game mutations are
        marked superseded and all runtime-serving queries additionally require
        the active marker, so a stale message cannot become executable merely
        because a replacement save reused a numeric farm or farmland ID.
        """
        world_id = self.validate(world_id)
        scope, now = self._scope(server_key, save_key), self._now()
        active = self.db.world_generations.find_one({**scope, "state": "active"})
        if isinstance(active, dict) and str(active.get("world_id")) == world_id:
            self.db.world_generations.update_one({"_id": active["_id"]}, {"$set": {
                "last_seen_at": now, "evidence": dict(evidence or {})}})
            return {"world_id": world_id, "changed": False}

        if isinstance(active, dict):
            self.db.world_generations.update_many({**scope, "state": "active"}, {"$set": {
                "state": "historical", "superseded_at": now,
                "superseded_by_world_id": world_id}})
        # Rows created before this protection have no marker.  They are just
        # as unsafe as an explicitly different marker once a real world is
        # observed, so archive both forms rather than silently adopting them.
        # MongoDB's $ne includes documents where the field is absent, which is
        # precisely the legacy case.  A single predicate also keeps this
        # fail-closed migration path compatible with the deterministic memory
        # persistence adapter used by integration tests.
        old_world = {"world_id": {"$ne": world_id}}
        for name in WORLD_BOUND_COLLECTIONS:
            collection = getattr(self.db, name)
            query = {**scope, **old_world}
            collection.update_many(query, {"$set": {
                "world_generation_state": "historical", "world_superseded_at": now,
                "superseded_by_world_id": world_id}})
        for name in ("farm_operations", "permission_jobs", "land_operations",
                     "fs25_money_operations", "bank_bridge_operations"):
            collection = getattr(self.db, name)
            collection.update_many({**scope, **old_world, "state": {"$in": ["pending", "dispatched"]}},
                                   {"$set": {"state": "world_superseded", "updated_at": now,
                                             "world_generation_state": "historical",
                                             "superseded_by_world_id": world_id}})
        generation_id = f"{scope['server_key']}:{scope['save_key']}:{world_id}"
        self.db.world_generations.update_one({"_id": generation_id}, {"$setOnInsert": {
            "_id": generation_id, **scope, "world_id": world_id, "created_at": now}, "$set": {
                "state": "active", "last_seen_at": now, "evidence": dict(evidence or {})}}, upsert=True)
        return {"world_id": world_id, "changed": isinstance(active, dict)}
