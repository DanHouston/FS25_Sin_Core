"""Central farm lifecycle state and durable game-operation coordination.

This module owns the SiN side of farm bootstrap/provisioning.  It never assumes
an FS25 numeric farm ID; IDs are learned from authenticated game receipts.
"""
from datetime import datetime, timezone
import hashlib
import logging
from pymongo.errors import DuplicateKeyError
from .database import Database
from .world_generation import WorldGenerationRegistry
from .map_service import MapStore, MapValidationError


SYSTEM_FARM_NAME = "SiN Harvest"
SYSTEM_FARM_TYPE = "system"
MEMBER_FARM_TYPE = "member"
FIRST_FIELD_MAX_PRICE = 750_000
OPERATION_STATES = {"pending", "dispatched", "succeeded", "failed", "reconciliation_required"}
PERSONAL_FARM_STATES = {"provisioned", "active"}
PERSONAL_FARM_REQUEST_STATES = {
    "requested", "pending", "provisioning", "land_pending", "land_assigning",
    "awaiting_manager", "manager_authorization_required", "financial_capability_required",
    "reconciliation_required", "active",
}


def _operation_id(*parts):
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


LOG = logging.getLogger(__name__)


class FarmLifecycle:
    """Idempotent central coordinator for system and member farms."""

    def __init__(self, database, authorization=None):
        self.database = database
        self.db = database.db
        if authorization is None:
            from .authorization import AuthorizationManager
            authorization = AuthorizationManager(database)
        self.authorization = authorization
        self.worlds = WorldGenerationRegistry(database)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def current_world_id(self, server_key, save_key):
        return self.worlds.active_id(server_key, save_key)

    @staticmethod
    def _continuity_migration_id(server_key, save_key, source_world_id, target_world_id,
                                 farm_id, farmland_id, discord_id, unique_user_id):
        return _operation_id("same-physical-world-continuity", server_key, save_key,
                             source_world_id, target_world_id, farm_id, farmland_id,
                             discord_id, unique_user_id)

    @staticmethod
    def _optional_session(session):
        return {"session": session} if session is not None else {}

    @staticmethod
    def _parse_positive_int(value, label):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a positive integer") from None
        if parsed <= 0:
            raise ValueError(f"{label} must be a positive integer")
        return parsed

    def _continuity_plan(self, server_key, save_key, source_world_id, target_world_id,
                         farm_id, farm_name, farmland_id, discord_id, unique_user_id,
                         *, session=None):
        """Validate the bounded same-physical-save migration without writing.

        This is deliberately evidence-heavy.  It is not a general farm adoption
        path: the source request and successful receipts must identify the old
        farm, while the target snapshot and a target-world player observation
        must independently prove the same physical farm and land still exist.
        """
        server_key, save_key = str(server_key).strip(), str(save_key).strip()
        source_world_id, target_world_id = str(source_world_id).strip(), str(target_world_id).strip()
        farm_name, discord_id, unique_user_id = (str(value).strip()
                                                  for value in (farm_name, discord_id, unique_user_id))
        if not server_key or not save_key or not source_world_id or not target_world_id:
            raise ValueError("server, save, source world, and target world are required")
        if source_world_id == target_world_id:
            raise ValueError("source and target worlds must differ")
        farm_id = self._parse_positive_int(farm_id, "farm_id")
        farmland_id = self._parse_positive_int(farmland_id, "farmland_id")
        if not farm_name or not discord_id or not unique_user_id:
            raise ValueError("farm name, Discord identity, and stable FS25 identity are required")
        options = self._optional_session(session)
        scope = {"server_key": server_key, "save_key": save_key}

        source_generation = self.db.world_generations.find_one(
            {**scope, "world_id": source_world_id}, **options)
        if not isinstance(source_generation, dict) or source_generation.get("state") != "historical":
            raise ValueError("source world must be an existing historical generation")
        target_generation = self.db.world_generations.find_one(
            {**scope, "world_id": target_world_id, "state": "active"}, **options)
        if not isinstance(target_generation, dict):
            raise ValueError("target world must be the active generation")
        source_evidence = source_generation.get("evidence") or {}
        target_evidence = target_generation.get("evidence") or {}
        if not isinstance(source_evidence, dict) or not isinstance(target_evidence, dict):
            raise ValueError("source and target physical evidence is missing")
        source_slot = source_evidence.get("savegame_index")
        target_slot = target_evidence.get("savegame_index")
        if source_slot is None or target_slot is None or str(source_slot) != str(target_slot):
            raise ValueError("source and target physical save-slot evidence does not match")
        source_map = str(source_evidence.get("map_id") or "")
        target_map = str(target_evidence.get("map_id") or "")
        if not source_map or source_map != target_map:
            raise ValueError("source and target map evidence does not match")

        target_snapshot = self.latest_snapshot(server_key, save_key, target_world_id, session=session)
        if not isinstance(target_snapshot, dict) or target_snapshot.get("source") != "game":
            raise ValueError("current target-world game snapshot is required")
        if (target_snapshot.get("savegame_index") is not None
                and str(target_snapshot.get("savegame_index")) != str(target_slot)):
            raise ValueError("target snapshot physical save-slot evidence does not match")
        target_farms = target_snapshot.get("farms") or {}
        matching_farms = []
        for raw_id, name in target_farms.items():
            try:
                parsed_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if str(name or "") == farm_name:
                matching_farms.append(parsed_id)
        if matching_farms != [farm_id]:
            raise ValueError("target snapshot does not contain exactly the expected farm")
        try:
            target_owner = int((target_snapshot.get("farmlands") or {}).get(str(farmland_id), 0))
        except (TypeError, ValueError):
            raise ValueError("target snapshot has an invalid farmland owner") from None
        if target_owner != farm_id:
            raise ValueError("target snapshot does not prove the expected farmland owner")

        source_mapping = self.db.sin_farms.find_one({
            **scope, "world_id": source_world_id, "farm_type": MEMBER_FARM_TYPE,
            "canonical_name": farm_name, "fs25_farm_id": farm_id,
            "starting_farmland_id": farmland_id,
        }, **options)
        if not isinstance(source_mapping, dict):
            raise ValueError("historical source farm mapping does not match the migration evidence")
        old_owner = source_mapping.get("owner_discord_id")
        if old_owner not in (None, "", discord_id):
            raise ValueError("historical farm mapping belongs to a different Discord identity")

        source_request = self.db.farm_requests.find_one({
            **scope, "world_id": source_world_id, "discord_id": discord_id,
            "farm_name": farm_name, "farm_id": farm_id, "starting_field": farmland_id,
        }, **options)
        if not isinstance(source_request, dict):
            raise ValueError("historical source farm request does not match the migration evidence")
        provision_id = source_request.get("operation_id")
        provision = self.db.farm_operations.find_one({
            **scope, "world_id": source_world_id, "_id": provision_id,
            "operation_type": "provision_farm", "state": "succeeded",
        }, **options) if provision_id else None
        if not isinstance(provision, dict):
            raise ValueError("historical farm provisioning receipt is not successful")
        provision_receipt = provision.get("receipt") or {}
        if provision_receipt.get("status") not in {"applied", "already_applied"} \
                or str(provision_receipt.get("world_id") or "") != source_world_id \
                or str(provision.get("fs25_farm_id")) != str(farm_id):
            raise ValueError("historical farm provisioning receipt does not match the source world")
        land_id = source_request.get("land_operation_id")
        land = self.db.farm_operations.find_one({
            **scope, "world_id": source_world_id, "_id": land_id,
            "operation_type": "assign_farmland", "state": "succeeded",
        }, **options) if land_id else None
        if not isinstance(land, dict):
            raise ValueError("historical farmland receipt is not successful")
        land_receipt = land.get("receipt") or {}
        if land_receipt.get("status") not in {"applied", "already_satisfied"} \
                or str(land_receipt.get("world_id") or "") != source_world_id \
                or str(land_receipt.get("farmland_id")) != str(farmland_id) \
                or str(land_receipt.get("owner_farm_id")) != str(farm_id):
            raise ValueError("historical farmland receipt does not match the source world")

        identity_rows = list(self.db.game_identities.find({
            "server_id": server_key, "save_id": save_key, "discord_id": discord_id,
        }, **options).limit(2))
        if len(identity_rows) != 1:
            raise ValueError("target save identity link is missing or ambiguous")
        identity = identity_rows[0]
        identity_ids = {str(identity.get("fs25_unique_user_id") or ""),
                        str(identity.get("game_player_id") or "")}
        if unique_user_id not in identity_ids:
            raise ValueError("target save identity link does not match the stable FS25 identity")
        application = self.db.community_applications.find_one(
            {"_id": discord_id, "state": "approved"}, **options)
        if not isinstance(application, dict):
            raise ValueError("the Discord identity is not currently approved")

        observation_query = {"server_key": server_key, "save_key": save_key,
                             "world_id": target_world_id,
                             "fs25_unique_user_id": unique_user_id}
        observations = list(self.db.observed_fs25_identities.find(
            observation_query, **options).sort("last_seen_at", -1).limit(2))
        observation_source = "observed_fs25_identities"
        if not observations:
            observations = list(self.db.player_activity_sessions.find(
                observation_query, **options).sort("observed_farm_at", -1).limit(2))
            observation_source = "player_activity_sessions"
        if not observations:
            raise ValueError("target world has no current observation for the stable FS25 identity")
        observation = observations[0]
        try:
            observed_farm_id = int(observation.get("current_farm_id", observation.get("observed_farm_id", 0)) or 0)
        except (TypeError, ValueError):
            observed_farm_id = 0
        if observed_farm_id != farm_id:
            raise ValueError("target-world identity observation is not in the expected farm")

        migration_id = self._continuity_migration_id(
            server_key, save_key, source_world_id, target_world_id,
            farm_id, farmland_id, discord_id, unique_user_id)
        return {
            "migration_id": migration_id,
            "server_key": server_key, "save_key": save_key,
            "source_world_id": source_world_id, "target_world_id": target_world_id,
            "farm_id": farm_id, "farm_name": farm_name, "farmland_id": farmland_id,
            "discord_id": discord_id, "fs25_unique_user_id": unique_user_id,
            "source_mapping_id": source_mapping.get("_id"),
            "source_request_id": source_request.get("_id"),
            "source_provision_operation_id": provision_id,
            "source_land_operation_id": land_id,
            "physical_savegame_index": str(target_slot),
            "physical_map_id": target_map,
            "target_observed_farm_id": farm_id,
            "target_snapshot_received_at": target_snapshot.get("received_at"),
            "target_observation_source": observation_source,
            "target_observation_at": observation.get("last_seen_at") or observation.get("observed_farm_at"),
            "target_observation_session_id": observation.get("session_id"),
        }

    def plan_same_physical_world_migration(self, server_key, save_key, source_world_id,
                                           target_world_id, farm_id, farm_name, farmland_id,
                                           discord_id, unique_user_id):
        """Return a read-only, evidence-backed continuity migration plan."""
        plan = self._continuity_plan(server_key, save_key, source_world_id, target_world_id,
                                     farm_id, farm_name, farmland_id, discord_id, unique_user_id)
        existing = self.db.world_continuity_migrations.find_one({"_id": plan["migration_id"]})
        if isinstance(existing, dict) and existing.get("status") == "applied":
            plan["existing_status"] = "applied"
        return plan

    def migrate_same_physical_world(self, server_key, save_key, source_world_id,
                                    target_world_id, farm_id, farm_name, farmland_id,
                                    discord_id, unique_user_id, operator_id):
        """Apply one explicit, idempotent continuity migration.

        Only a new current-world projection is written.  Historical farms,
        requests, operations, receipts, sessions, and generation rows are
        never rewritten or copied.
        """
        if not str(operator_id or "").strip():
            raise ValueError("an operator identity is required for continuity migration")

        def apply(session=None):
            options = self._optional_session(session)
            plan = self._continuity_plan(server_key, save_key, source_world_id, target_world_id,
                                         farm_id, farm_name, farmland_id, discord_id,
                                         unique_user_id, session=session)
            migration_id = plan["migration_id"]
            existing = self.db.world_continuity_migrations.find_one({"_id": migration_id}, **options)
            if isinstance(existing, dict) and existing.get("status") == "applied":
                return {"status": "already_applied", "migration": existing, "plan": plan}
            scope = {"server_key": plan["server_key"], "save_key": plan["save_key"],
                     "world_id": plan["target_world_id"]}
            # Keep the query Mongo-compatible with the in-process deterministic
            # adapter as well: inspect the small current-world member set and
            # match either stable identity in Python.  This is fail-closed for
            # every unrelated current-world farm.
            current_mappings = [
                row for row in self.db.sin_farms.find({
                    **scope, "farm_type": MEMBER_FARM_TYPE,
                }, **options)
                if (row.get("fs25_farm_id") in (plan["farm_id"], str(plan["farm_id"]))
                    or row.get("canonical_name") == plan["farm_name"])
            ]
            for mapping in current_mappings:
                if (mapping.get("fs25_farm_id") not in (None, plan["farm_id"], str(plan["farm_id"]))
                        or mapping.get("canonical_name") not in (None, plan["farm_name"])
                        or mapping.get("owner_discord_id") not in (None, "", plan["discord_id"])):
                    raise ValueError("target world already contains a conflicting member farm mapping")
            mapping_id = next((row.get("_id") for row in current_mappings if row.get("_id")), None) \
                or _operation_id("continuity-farm", migration_id)
            request_id = _operation_id("continuity-request", migration_id)
            conflicting_requests = list(self.db.farm_requests.find({
                **scope, "discord_id": plan["discord_id"],
                "_id": {"$ne": request_id},
            }, **options).limit(2))
            if conflicting_requests:
                raise ValueError("target world already contains a farm request for this identity")
            now = self._now()
            mapping_values = {
                "_id": mapping_id, **scope, "canonical_name": plan["farm_name"],
                "farm_type": MEMBER_FARM_TYPE, "owner_discord_id": None,
                "source_request_id": None, "starting_farmland_id": plan["farmland_id"],
                "fs25_farm_id": plan["farm_id"], "state": "provisioned",
                "migration_id": migration_id, "migration_source_world_id": plan["source_world_id"],
                "migration_source_request_id": plan["source_request_id"],
                "created_at": now, "updated_at": now,
            }
            mapping_insert = dict(mapping_values)
            mapping_insert.pop("updated_at", None)
            self.db.sin_farms.update_one({"_id": mapping_id}, {
                "$setOnInsert": mapping_insert,
                # Do not repeat insert-only paths in another update operator;
                # Mongo rejects overlapping $setOnInsert/$set paths.  Existing
                # rows have already been checked above for exact compatibility.
                "$set": {"updated_at": now, "migration_last_verified_at": now}},
                upsert=True, **options)

            observation_query = {"server_key": plan["server_key"], "save_key": plan["save_key"],
                                 "world_id": plan["target_world_id"],
                                 "fs25_unique_user_id": plan["fs25_unique_user_id"]}
            observation = self.db.observed_fs25_identities.find_one(observation_query, **options)
            if not isinstance(observation, dict):
                session_query = dict(observation_query)
                session_observation = self.db.player_activity_sessions.find_one(session_query, **options)
                if isinstance(session_observation, dict):
                    observation = session_observation
            if not isinstance(observation, dict):
                raise ValueError("target-world observation disappeared before migration commit")
            try:
                observed_farm_id = int(observation.get("current_farm_id", observation.get("observed_farm_id", 0)) or 0)
            except (TypeError, ValueError):
                observed_farm_id = 0
            if observed_farm_id != plan["farm_id"]:
                raise ValueError("target-world identity observation changed before migration commit")
            self.db.observed_fs25_identities.update_one(observation_query, {
                "$set": {"latest_display_name": observation.get("latest_display_name")
                         or observation.get("observed_display_name") or plan["farm_name"],
                         "current_farm_id": plan["farm_id"],
                         "currently_connected": observation.get("currently_connected", False),
                         "last_seen_at": observation.get("last_seen_at") or now,
                         "migration_id": migration_id,
                         "observation_source": "same_physical_world_migration"},
                "$setOnInsert": {"first_seen_at": observation.get("first_seen_at") or now}},
                upsert=True, **options)

            request_values = {
                "_id": request_id, "discord_id": plan["discord_id"],
                "server_key": plan["server_key"], "save_key": plan["save_key"],
                "world_id": plan["target_world_id"], "farm_name": plan["farm_name"],
                "starting_field": plan["farmland_id"], "starting_field_id": plan["farmland_id"],
                "state": "awaiting_manager", "farm_id": plan["farm_id"],
                "mapping_id": mapping_id, "assigned_farmland_id": plan["farmland_id"],
                # This is a current snapshot observation, not a new land
                # mutation.  Do not invent an operation-style before-owner.
                "land_confirmed": True, "owner_before_farm_id": None,
                "owner_farm_id": plan["farm_id"], "approved_by": str(operator_id),
                "migration_id": migration_id, "migration_source_world_id": plan["source_world_id"],
                "migration_source_request_id": plan["source_request_id"],
                "created_at": now, "updated_at": now,
            }
            request_insert = dict(request_values)
            request_insert.pop("updated_at", None)
            self.db.farm_requests.update_one({"_id": request_id}, {
                "$setOnInsert": request_insert,
                "$set": {"updated_at": now, "migration_last_verified_at": now}},
                upsert=True, **options)
            migration = {
                "_id": migration_id, "server_key": plan["server_key"],
                "save_key": plan["save_key"], "source_world_id": plan["source_world_id"],
                "target_world_id": plan["target_world_id"], "farm_id": plan["farm_id"],
                "farm_name": plan["farm_name"], "farmland_id": plan["farmland_id"],
                "discord_id": plan["discord_id"], "fs25_unique_user_id": plan["fs25_unique_user_id"],
                "source_mapping_id": plan["source_mapping_id"],
                "source_request_id": plan["source_request_id"],
                "source_provision_operation_id": plan["source_provision_operation_id"],
                "source_land_operation_id": plan["source_land_operation_id"],
                "target_mapping_id": mapping_id, "target_request_id": request_id,
                "evidence": {"physical_savegame_index": plan["physical_savegame_index"],
                              "physical_map_id": plan["physical_map_id"],
                              "target_snapshot_received_at": plan["target_snapshot_received_at"],
                              "target_observation_source": plan["target_observation_source"],
                              "target_observation_at": plan["target_observation_at"],
                              "target_observation_session_id": plan["target_observation_session_id"]},
                "operator_id": str(operator_id), "status": "applied",
                "created_at": existing.get("created_at", now) if isinstance(existing, dict) else now,
                "updated_at": now,
            }
            migration_insert = dict(migration)
            migration_insert.pop("updated_at", None)
            self.db.world_continuity_migrations.update_one({"_id": migration_id}, {
                "$setOnInsert": migration_insert,
                "$set": {"updated_at": now, "last_verified_at": now}}, upsert=True, **options)
            return {"status": "applied", "migration": migration, "plan": plan}

        if isinstance(self.database, Database):
            return self.database.atomic(apply)
        return apply(None)

    def require_current_world(self, server_key, save_key, world_id):
        return self.worlds.require_active(server_key, save_key, world_id)

    def _scope(self, server_key, save_key, world_id=None):
        scope = {"server_key": str(server_key), "save_key": str(save_key)}
        active = str(world_id) if world_id else self.current_world_id(server_key, save_key)
        if active:
            scope["world_id"] = active
        return scope

    def latest_snapshot(self, server_key, save_key, world_id=None, session=None):
        kwargs = {"sort": [("received_at", -1)]}
        if session is not None:
            kwargs["session"] = session
        return self.db.server_snapshots.find_one(self._scope(server_key, save_key, world_id), **kwargs)

    def record_snapshot(self, server_key, save_key, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("source") != "game":
            raise ValueError("game snapshot is required")
        world_id = snapshot.get("world_id")

        def persist(session=None):
            options = {"session": session} if session is not None else {}
            if world_id:
                self.worlds.activate(server_key, save_key, world_id, evidence={
                    "map_id": snapshot.get("map_id"), "savegame_index": snapshot.get("savegame_index")},
                    session=session)
            now = self._now()
            record = dict(snapshot)
            scope = self._scope(server_key, save_key, world_id)
            record.update(scope, received_at=now)
            self.db.server_snapshots.update_one(scope, {"$set": record}, upsert=True, **options)
            return record

        # A replacement snapshot changes the active-world boundary and archives
        # every old-world collection. Keep that transition and the authoritative
        # snapshot in one Mongo transaction so a failed write cannot leave the
        # old generation retired with no current generation available.
        active = self.current_world_id(server_key, save_key) if world_id else None
        if world_id and str(active or "") != str(world_id):
            record = self.database.atomic(persist)
        else:
            record = persist()
        if world_id:
            # World activation and snapshot durability are the authoritative
            # boundary. System-farm bootstrap is a retryable follow-up; a
            # malformed historical mapping or transient bootstrap failure must
            # not turn an otherwise valid replacement snapshot into a rejected
            # world (and leave the Agent receiving 404s for the new marker).
            try:
                self.ensure_system_farm(server_key, save_key)
            except Exception:
                LOG.exception("system-farm bootstrap deferred after accepting snapshot server=%s save=%s world=%s",
                              server_key, save_key, world_id)
        return record

    def _operation(self, operation_id):
        return self.db.farm_operations.find_one({"_id": operation_id})

    def _queue_operation(self, operation_id, operation_type, server_key, save_key, payload, request_id=None):
        now = self._now()
        values = dict(_id=operation_id, operation_id=operation_id, operation_type=operation_type,
                      server_key=server_key, save_key=save_key, payload=payload,
                      request_id=request_id, state="pending", attempts=0,
                      created_at=now, updated_at=now)
        world_id = self.current_world_id(server_key, save_key)
        if world_id:
            values["world_id"] = world_id
        insert_values = dict(values)
        insert_values.pop("updated_at", None)
        self.db.farm_operations.update_one(
            {"_id": operation_id},
            {"$setOnInsert": insert_values, "$set": {"updated_at": now}}, upsert=True)
        return self._operation(operation_id)

    def ensure_system_farm(self, server_key, save_key):
        scope = dict(self._scope(server_key, save_key), farm_type=SYSTEM_FARM_TYPE,
                     canonical_name=SYSTEM_FARM_NAME)
        mappings = list(self.db.sin_farms.find(scope))
        if len(mappings) > 1:
            return {"status": "reconciliation_required", "reason": "multiple system farm mappings"}
        confirmed = [row for row in mappings
                     if row.get("fs25_farm_id") is not None and row.get("state") == "active"]
        confirmed_ids = {int(row["fs25_farm_id"]) for row in confirmed}
        if len(confirmed) == 1 and len(confirmed_ids) == 1:
            return {"status": "active", "mapping": confirmed[0]}
        if len(confirmed_ids) > 1 or len(confirmed) > 1:
            return {"status": "reconciliation_required", "reason": "multiple system farm mappings"}

        # A game snapshot is authoritative for recovery after a receipt was
        # applied in FS25 but central persistence failed.  Adopt only one exact
        # name match; never infer a farm ID or choose between duplicates.
        snapshot = self.latest_snapshot(server_key, save_key)
        game_farms = snapshot.get("farms", {}) if snapshot else {}
        matches = []
        for farm_id, name in game_farms.items():
            if str(name) == SYSTEM_FARM_NAME:
                try:
                    parsed_id = int(farm_id)
                except (TypeError, ValueError):
                    continue
                if parsed_id > 0:
                    matches.append(parsed_id)
        if len(matches) > 1:
            return {"status": "reconciliation_required", "reason": "multiple game system farms"}
        if len(matches) == 1:
            known_ids = {int(row["fs25_farm_id"]) for row in mappings
                         if row.get("fs25_farm_id") is not None}
            if known_ids and known_ids != {matches[0]}:
                return {"status": "reconciliation_required", "reason": "central and game farm IDs disagree"}
            world_id = self.current_world_id(server_key, save_key)
            mapping_id = mappings[0].get("_id") if mappings and mappings[0].get("_id") else _operation_id(
                "farm", server_key, save_key, world_id or "legacy", "ensure-system-farm")
            now = self._now()
            operation_id = _operation_id("ensure-system-farm", server_key, save_key, world_id or "legacy")
            mapping_values = {"_id": mapping_id, "server_key": server_key, "save_key": save_key,
                              "canonical_name": SYSTEM_FARM_NAME, "farm_type": SYSTEM_FARM_TYPE,
                              "owner_discord_id": None, "source_request_id": None,
                              "state": "active", "operation_id": operation_id,
                              "fs25_farm_id": matches[0], "updated_at": now}
            if world_id:
                mapping_values["world_id"] = world_id
            self.db.sin_farms.update_one({"_id": mapping_id}, {
                # Keep all fields in one operator to avoid MongoDB path
                # conflicts when this adopts a record left by a failed receipt.
                "$setOnInsert": {"_id": mapping_id},
                "$set": {key: value for key, value in mapping_values.items() if key != "_id"}
            }, upsert=True)
            existing_operation = self._operation(operation_id)
            if existing_operation and existing_operation.get("state") != "succeeded":
                self.db.farm_operations.update_one({"_id": operation_id}, {"$set": {
                    "state": "succeeded", "fs25_farm_id": matches[0],
                    "receipt": {"status": "already_applied", "farm_id": matches[0],
                                 "reason": "adopted_from_game_snapshot"}, "updated_at": now}})
            return {"status": "active", "adopted": True,
                    "mapping": mapping_values}

        world_id = self.current_world_id(server_key, save_key)
        operation_id = _operation_id("ensure-system-farm", server_key, save_key, world_id or "legacy")
        existing_operation = self._operation(operation_id)
        if existing_operation and existing_operation.get("state") in {"succeeded", "reconciliation_required"}:
            return {"status": "reconciliation_required", "operation": existing_operation,
                    "mapping": mappings[0] if len(mappings) == 1 else None}
        operation = self._queue_operation(operation_id, "ensure_farm", server_key, save_key,
                                          {"farm_type": SYSTEM_FARM_TYPE, "canonical_name": SYSTEM_FARM_NAME})
        return {"status": "pending", "operation": operation,
                "mapping": mappings[0] if len(mappings) == 1 else None}

    def ensure_for_server(self, server_key):
        results = []
        for row in self.db.sin_saves.find({"server_key": server_key}):
            if self.current_world_id(server_key, row["save_key"]):
                results.append(self.ensure_system_farm(server_key, row["save_key"]))
            else:
                results.append({"status": "world_generation_required", "save_key": row["save_key"]})
        return results

    def _manager_authorization_error(self, request_id, farm_id, mapping_id, error):
        """Keep a failed manager handoff explicitly recoverable.

        Farm and farmland confirmation is durable independently of the permission
        handoff.  A failed handoff must therefore never look like a request that
        is merely waiting for a game receipt; the next normal operations poll can
        retry it safely.
        """
        message = str(error).strip() or "manager authorization could not be created"
        self.db.farm_requests.update_one({"_id": request_id}, {"$set": {
            "state": "manager_authorization_required", "farm_id": farm_id,
            "mapping_id": mapping_id, "manager_authorization_error": message[:300],
            "updated_at": self._now()}})

    def _ensure_manager_authorization(self, request, server_key, save_key, farm_id, mapping_id, farm_name):
        """Create or repair the owner assignment after game-side provisioning.

        The durable farm lifecycle uses the registered Discord/game identity and
        the explicit farm approval as its authorization boundary.  This is
        intentionally separate from the older operator assignment path, which
        still requires an identity record marked with ``approved_by``.
        """
        request_id = request.get("_id")
        if not request_id:
            return None
        try:
            membership_operation = self.authorization.assign(
                request["discord_id"], server_key, save_key, int(farm_id),
                "farm_manager", {int(farm_id): farm_name or ""},
                request.get("approved_by") or "farm-provisioning", world_id=self.current_world_id(server_key, save_key),
                allow_unapproved_identity=True, idempotent=True)
            membership_query = {
                "server_id": server_key, "save_id": save_key,
                "discord_id": str(request["discord_id"]), "farm_id": int(farm_id),
                "desired_role": "farm_manager"}
            world_id = self.current_world_id(server_key, save_key)
            if world_id:
                membership_query["world_id"] = world_id
            membership = self.db.memberships.find_one(membership_query)
            state = "active" if isinstance(membership, dict) and membership.get("state") == "active" \
                and membership.get("applied_role") == "farm_manager" else "awaiting_manager"
            values = {"state": state, "farm_id": int(farm_id), "mapping_id": mapping_id,
                      "manager_authorization_error": None, "updated_at": self._now()}
            if membership_operation:
                values["permission_operation_id"] = membership_operation
            self.db.farm_requests.update_one({"_id": request_id}, {"$set": values})
            if state == "active":
                self.db.sin_farms.update_one({"_id": mapping_id}, {"$set": {
                    "state": "active", "owner_discord_id": request.get("discord_id"),
                    "updated_at": self._now()}})
            try:
                contractor_operation = self._reconcile_shared_contractor_authorizations(
                    server_key, save_key).get(str(request["discord_id"]))
            except Exception as contractor_error:
                # Shared-farm access is an independent relationship. A
                # temporary contractor failure must not roll back or obscure
                # the player's personal-farm manager authority.
                contractor_operation = None
                self.db.farm_requests.update_one({"_id": request_id}, {"$set": {
                    "contractor_authorization_state": "retry",
                    "contractor_authorization_error": str(contractor_error)[:300],
                    "updated_at": self._now()}})
            if contractor_operation:
                self.db.farm_requests.update_one({"_id": request_id}, {"$set": {
                    "contractor_permission_operation_id": contractor_operation,
                    "contractor_authorization_state": "active" if state == "active" else "pending",
                    "contractor_authorization_error": None,
                    "updated_at": self._now()}})
            return membership_operation
        except Exception as error:
            self._manager_authorization_error(request_id, farm_id, mapping_id, error)
            return None

    def _shared_system_farm(self, server_key, save_key):
        """Return one authoritative SiN Harvest mapping, or fail closed."""
        mappings = list(self.db.sin_farms.find({
            **self._scope(server_key, save_key),
            "farm_type": SYSTEM_FARM_TYPE, "canonical_name": SYSTEM_FARM_NAME,
            "state": "active", "fs25_farm_id": {"$exists": True}}))
        if len(mappings) != 1:
            return None
        mapping = mappings[0]
        try:
            farm_id = int(mapping.get("fs25_farm_id"))
        except (TypeError, ValueError):
            return None
        return mapping if farm_id > 0 else None

    def _current_observed_farm(self, server_key, save_key, identity):
        """Return the current-world source farm observed for one identity.

        Contractor authority is a native FS25 farm-to-farm relationship.  A
        stable identity alone is insufficient: the player must currently have
        a real source farm.  Farm 0/spectator and missing observations are
        deliberately treated as ineligible until the next authoritative
        snapshot/activity observation.
        """
        unique_id = str(identity.get("fs25_unique_user_id") or identity.get("game_player_id") or "").strip()
        if not unique_id:
            return None
        query = {"server_key": server_key, "save_key": save_key,
                 "fs25_unique_user_id": unique_id}
        world_id = self.current_world_id(server_key, save_key)
        if world_id:
            query["world_id"] = str(world_id)
        def latest(collection, sort):
            try:
                return collection.find_one(query, sort=sort)
            except TypeError:
                return collection.find_one(query)

        # Both projections are authoritative observations of the same current
        # world.  A farm-0 projection is not a reason to ignore a newer session
        # observation proving that the player has since joined a personal farm;
        # conversely, a newer farm-0 observation must withdraw eligibility.
        observed_projection = latest(self.db.observed_fs25_identities,
                                     [("last_seen_at", -1), ("observed_farm_at", -1)])
        observed_session = latest(self.db.player_activity_sessions,
                                  [("observed_farm_at", -1), ("last_seen_at", -1),
                                   ("updated_at", -1)])

        def observation_time(row):
            if not isinstance(row, dict):
                return 0.0
            values = []
            for field in ("observed_farm_at", "last_seen_at", "updated_at", "connected_at"):
                value = row.get(field)
                if isinstance(value, datetime):
                    values.append(value.timestamp())
                elif value:
                    try:
                        text = str(value).replace("Z", "+00:00")
                        parsed = datetime.fromisoformat(text)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        values.append(parsed.timestamp())
                    except (TypeError, ValueError, OverflowError):
                        continue
            return max(values, default=0.0)

        candidates = [row for row in (observed_projection, observed_session)
                      if isinstance(row, dict)]
        observed = max(enumerate(candidates),
                       key=lambda item: (observation_time(item[1]), -item[0]),
                       default=(0, None))[1]
        if not isinstance(observed, dict):
            return None
        try:
            farm_id = int(observed.get("current_farm_id", observed.get("observed_farm_id", 0)) or 0)
        except (TypeError, ValueError):
            return None
        LOG.info("[SiN Contractor] current source observation server=%s save=%s unique=%s farm=%s source=%s",
                 server_key, save_key, unique_id[:12], farm_id,
                 "session" if observed is observed_session else "projection")
        return farm_id if farm_id > 0 else None

    def _current_farm_names(self, server_key, save_key):
        snapshot = self.latest_snapshot(server_key, save_key)
        farms = snapshot.get("farms") if isinstance(snapshot, dict) else None
        if not isinstance(farms, dict):
            return {}
        result = {}
        for farm_id, name in farms.items():
            try:
                result[int(farm_id)] = str(name or "")
            except (TypeError, ValueError):
                continue
        return result

    def _quarantine_legacy_contractor(self, relationship, reason):
        """Remove a pre-native contractor row from actionable authority.

        The old implementation had no source-farm evidence and therefore
        cannot safely be treated as a current farm-to-farm grant.  Keep the
        record for audit/recovery, but make it non-actionable until a bounded
        native cleanup or explicit operator reconciliation completes.
        """
        if relationship.get("state") in {"active", "pending", "dispatched"}:
            self.db.memberships.update_one(
                {"_id": relationship.get("_id"),
                 "state": {"$in": ["active", "pending", "dispatched"]}},
                {"$set": {"state": "reconciliation_required",
                          "reconciliation_reason": reason[:300],
                          "updated_at": self._now()}})
        self.db.permission_jobs.update_many(
            {"membership_id": relationship.get("_id"), "role": "contractor",
             "source_farm_id": {"$exists": False},
             "state": {"$in": ["pending", "dispatched"]}},
            {"$set": {"state": "reconciliation_required",
                      "reconciliation_reason": reason[:300],
                      "updated_at": self._now()}})

    def _reconcile_shared_contractor_authorizations(self, server_key, save_key):
        """Derive SiN Harvest access for every currently approved identity.

        A contractor relationship is a second, farm-scoped membership.  It is
        intentionally independent of farm requests and personal manager
        authority, so existing approved/registered members receive repair on
        the normal Agent operations poll after a deploy or restart.
        """
        system = self._shared_system_farm(server_key, save_key)
        if not system:
            LOG.info("[SiN Contractor] reconciliation deferred server=%s save=%s reason=system_farm_unavailable",
                     server_key, save_key)
            return {}
        shared_farm_id = int(system["fs25_farm_id"])
        farm_names = self._current_farm_names(server_key, save_key)
        eligible = {}
        identities = self.db.game_identities.find({"server_id": server_key, "save_id": save_key})
        for identity in identities:
            discord_id = str(identity.get("discord_id") or "")
            if not discord_id or not identity.get("game_player_id"):
                continue
            application = self.db.community_applications.find_one(
                {"_id": discord_id, "state": "approved"})
            source_farm_id = self._current_observed_farm(server_key, save_key, identity) \
                if application else None
            # A player in spectator/farm 0 has no native source farm.  Do not
            # manufacture a relationship; the next current-farm observation
            # will make the normal reconciliation path eligible.
            if application and source_farm_id and source_farm_id != shared_farm_id \
                    and source_farm_id in farm_names:
                eligible[discord_id] = dict(identity,
                                            source_farm_id=source_farm_id,
                                            source_farm_name=farm_names[source_farm_id])

        # A pre-native row has target-farm permissions but no provable source
        # farm.  Clean that residue first; do not create a second native grant
        # in the same poll, because the cleanup must remain receipt-gated.
        membership_scope = {"server_id": server_key, "save_id": save_key, "farm_id": shared_farm_id}
        world_id = self.current_world_id(server_key, save_key)
        if world_id:
            membership_scope["world_id"] = world_id
        existing_relationships = list(self.db.memberships.find({
            **membership_scope, "desired_role": {"$in": ["contractor", "revoked"]}}))
        legacy_users = {str(row.get("discord_id")) for row in existing_relationships
                        if row.get("desired_role") == "contractor" and not row.get("source_farm_id")}
        operations = {}
        for discord_id in sorted(eligible):
            if discord_id in legacy_users:
                continue
            try:
                operation = self.authorization.assign(
                    discord_id, server_key, save_key, shared_farm_id,
                    "contractor", {shared_farm_id: SYSTEM_FARM_NAME,
                                    eligible[discord_id]["source_farm_id"]: eligible[discord_id]["source_farm_name"]},
                    "shared-contractor-policy", world_id=self.current_world_id(server_key, save_key), allow_unapproved_identity=True,
                    idempotent=True, source_farm_id=eligible[discord_id]["source_farm_id"],
                    source_farm_name=eligible[discord_id]["source_farm_name"])
                if operation:
                    operations[discord_id] = operation
            except ValueError as error:
                # Identity and relationship checks are deliberately re-run by
                # AuthorizationManager.  A malformed/ambiguous record must
                # suppress this grant rather than guess an authority target.
                LOG.warning("[SiN Contractor] grant deferred server=%s save=%s discord=%s sourceFarmId=%s targetFarmId=%s reason=%s",
                            server_key, save_key, discord_id,
                            eligible[discord_id].get("source_farm_id"), shared_farm_id,
                            str(error)[:240])
                continue

        relationships = self.db.memberships.find({
            **membership_scope,
            "desired_role": {"$in": ["contractor", "revoked"]}})
        for relationship in relationships:
            discord_id = str(relationship.get("discord_id") or "")
            desired = eligible.get(discord_id)
            relationship_source = relationship.get("source_farm_id")
            try:
                relationship_source = int(relationship_source) if relationship_source is not None else None
            except (TypeError, ValueError):
                relationship_source = None
            if desired and relationship.get("desired_role") == "contractor" \
                    and relationship_source == desired["source_farm_id"] \
                    and relationship.get("state") in {"pending", "active"}:
                continue
            if desired and relationship.get("desired_role") == "contractor" \
                    and relationship_source == desired["source_farm_id"] \
                    and relationship.get("state") == "reconciliation_required":
                try:
                    operation = self.authorization.assign(
                        discord_id, server_key, save_key, shared_farm_id,
                        "contractor", {shared_farm_id: SYSTEM_FARM_NAME,
                                        desired["source_farm_id"]: desired["source_farm_name"]},
                        "shared-contractor-policy", world_id=world_id, allow_unapproved_identity=True,
                        idempotent=True, source_farm_id=desired["source_farm_id"],
                        source_farm_name=desired["source_farm_name"])
                    if operation:
                        operations[discord_id] = operation
                except ValueError:
                    pass
                continue
            if relationship.get("desired_role") == "revoked" \
                    and relationship.get("state") in {"pending", "active", "revoked"}:
                continue
            if relationship.get("desired_role") == "revoked" \
                    and relationship.get("state") == "reconciliation_required" and desired:
                try:
                    operation = self.authorization.assign(
                        discord_id, server_key, save_key, shared_farm_id,
                        "contractor", {shared_farm_id: SYSTEM_FARM_NAME,
                                        desired["source_farm_id"]: desired["source_farm_name"]},
                        "shared-contractor-policy", world_id=world_id, allow_unapproved_identity=True,
                        idempotent=True, source_farm_id=desired["source_farm_id"],
                        source_farm_name=desired["source_farm_name"])
                    if operation:
                        operations[discord_id] = operation
                except ValueError:
                    pass
                continue
            if relationship.get("desired_role") == "revoked" \
                    and relationship.get("state") == "reconciliation_required" and not desired \
                    and relationship_source is not None:
                try:
                    operation = self.authorization.revoke_contractor(
                        discord_id, server_key, save_key, shared_farm_id,
                        "shared-contractor-policy", world_id=world_id, idempotent=True,
                        source_farm_id=relationship_source)
                    if operation:
                        operations[discord_id] = operation
                except ValueError:
                    pass
                continue
            if desired and relationship.get("desired_role") == "revoked" \
                    and relationship.get("state") == "revoked":
                try:
                    operation = self.authorization.assign(
                        discord_id, server_key, save_key, shared_farm_id,
                        "contractor", {shared_farm_id: SYSTEM_FARM_NAME,
                                        desired["source_farm_id"]: desired["source_farm_name"]},
                        "shared-contractor-policy", world_id=world_id, allow_unapproved_identity=True,
                        idempotent=True, source_farm_id=desired["source_farm_id"],
                        source_farm_name=desired["source_farm_name"])
                    if operation:
                        operations[discord_id] = operation
                except ValueError:
                    continue
                continue
            if relationship_source is None:
                if desired:
                    # The known current source is sufficient to perform a
                    # bounded cleanup of the pre-native target-permission
                    # record; it is never exposed as current authority.
                    relationship_source = desired["source_farm_id"]
                else:
                    self._quarantine_legacy_contractor(
                        relationship, "legacy contractor relationship has no provable source farm")
                    continue
            try:
                operation = self.authorization.revoke_contractor(
                    discord_id, server_key, save_key, shared_farm_id,
                    "shared-contractor-policy", world_id=world_id, idempotent=True,
                    source_farm_id=relationship_source)
                if operation:
                    operations[discord_id] = operation
            except ValueError:
                # Keep a malformed relationship visible for diagnosis; do not
                # mutate an unknown farm/user relationship as a side effect.
                continue
        return operations

    def _repair_manager_authorizations(self, server_key, save_key):
        """Retry missing owner assignments during the existing Agent poll."""
        requests = self.db.farm_requests.find({
            **self._scope(server_key, save_key),
            "state": {"$in": ["awaiting_manager", "manager_authorization_required",
                                "financial_capability_required"]},
            "farm_id": {"$exists": True}}).sort("updated_at", 1).limit(50)
        for request in list(requests):
            operation_id = request.get("operation_id")
            if operation_id:
                operation = self.db.farm_operations.find_one({"_id": operation_id,
                    **self._scope(server_key, save_key)})
                if isinstance(operation, dict) and operation.get("state") != "succeeded":
                    continue
            self._ensure_manager_authorization(
                request, server_key, save_key, request.get("farm_id"),
                request.get("mapping_id"), request.get("farm_name"))

    def _repair_contractor_authorizations(self, server_key, save_key):
        self._reconcile_shared_contractor_authorizations(server_key, save_key)

    def available_fields(self, server_key, save_key, *, world_id=None, session=None):
        snapshot = self.latest_snapshot(server_key, save_key, world_id, session=session)
        if not snapshot:
            raise ValueError("No current game snapshot is available")
        fields = snapshot.get("farmlands") or {}
        return {int(field_id): int(owner or 0) for field_id, owner in fields.items()}

    def assert_personal_farm_request_allowed(self, discord_id, server_key, save_key,
                                             *, world_id=None, session=None):
        """Enforce one personal farm/request for this identity in this world.

        Only generation-scoped member-farm mappings count.  The SiN Harvest
        system farm and contractor memberships are deliberately unrelated to a
        player's personal farm entitlement.  Legacy rows without a world ID
        are not allowed to block a current generation.
        """
        selected_world = str(world_id) if world_id is not None else self.current_world_id(server_key, save_key)
        if not selected_world:
            return None
        scope = self._scope(server_key, save_key, selected_world)
        farm_query = {
            **scope,
            "farm_type": MEMBER_FARM_TYPE,
            "owner_discord_id": str(discord_id),
            "state": {"$in": sorted(PERSONAL_FARM_STATES)},
        }
        farms = list(self.db.sin_farms.find(farm_query, **({"session": session} if session is not None else {})))
        if farms:
            if len(farms) > 1:
                raise ValueError("Multiple personal farms are recorded in this current FS25 world; staff reconciliation is required")
            name = str(farms[0].get("canonical_name") or "your personal farm")
            raise ValueError(f"You already have a personal farm ({name}) in this current FS25 world")

        request_query = {
            **scope,
            "discord_id": str(discord_id),
            "state": {"$in": sorted(PERSONAL_FARM_REQUEST_STATES)},
        }
        requests = list(self.db.farm_requests.find(
            request_query, **({"session": session} if session is not None else {})))
        if requests:
            if len(requests) > 1:
                raise ValueError("Multiple farm requests are recorded in this current FS25 world; staff reconciliation is required")
            raise ValueError("You already have a pending farm request in this current FS25 world")
        return None

    def request_farm(self, discord_id, server_key, save_key, starting_field, *, world_id=None, field_id=None):
        application = self.db.community_applications.find_one({"_id": str(discord_id), "state": "approved"})
        if not application or not application.get("farm_name"):
            raise ValueError("You must complete SiN membership approval before requesting a farm")
        try:
            farmland_id = int(str(starting_field).strip())
        except (TypeError, ValueError):
            raise ValueError("Starting field must be a numeric FS25 farmland ID") from None
        if farmland_id <= 0:
            raise ValueError("Starting field must be a positive FS25 farmland ID")

        def reserve(session):
            active_world = self.current_world_id(server_key, save_key)
            selected_world = str(world_id) if world_id is not None else active_world
            if world_id is not None and active_world != str(world_id):
                raise ValueError("This field picker belongs to an older FS25 world generation; start again")
            self.assert_personal_farm_request_allowed(
                discord_id, server_key, save_key, world_id=selected_world, session=session)
            available = self.available_fields(server_key, save_key, world_id=selected_world, session=session)
            owner = available.get(farmland_id)
            if owner is None:
                raise ValueError("That starting field is not present on the current save")
            if owner != 0:
                raise ValueError("That starting field is no longer available")
            if selected_world:
                try:
                    map_model = MapStore(self.database).load_model(
                        server_key, save_key, selected_world, session=session)
                except MapValidationError:
                    raise ValueError("Current-world farmland pricing is unavailable") from None
                price = map_model.farmland_prices.get(farmland_id) if map_model else None
                if price is None:
                    raise ValueError("Current-world farmland pricing is unavailable")
                if price >= FIRST_FIELD_MAX_PRICE:
                    raise ValueError("The first field must cost less than $750,000")
            generation_key = selected_world or "legacy"
            request_id = hashlib.sha256(
                f"{server_key}|{save_key}|{generation_key}|{discord_id}".encode()).hexdigest()
            request_query = {"_id": request_id}
            existing = self.db.farm_requests.find_one(request_query, session=session)
            if isinstance(existing, dict) and existing.get("state") in {"pending", "requested"}:
                old_field = existing.get("starting_field")
                if old_field is not None and int(old_field) != farmland_id:
                    raise ValueError("You already have a pending farm request for this world")
            reservation_query = {"server_key": str(server_key), "save_key": str(save_key),
                                 "world_id": generation_key, "farmland_id": farmland_id}
            reservation = self.db.farm_field_reservations.find_one(reservation_query, session=session)
            if isinstance(reservation, dict) and reservation.get("request_id") != request_id:
                raise ValueError("That starting field was just reserved by another pending request")
            now = self._now()
            reservation_values = dict(**reservation_query, request_id=request_id,
                                      discord_id=str(discord_id), state="pending", created_at=now)
            try:
                self.db.farm_field_reservations.update_one(
                    reservation_query, {"$setOnInsert": reservation_values, "$set": {"updated_at": now}},
                    upsert=True, session=session)
            except DuplicateKeyError:
                raise ValueError("That starting field was just reserved by another pending request") from None
            record = dict(_id=request_id, discord_id=str(discord_id), server_key=server_key, save_key=save_key,
                          farm_name=str(application["farm_name"]).strip(), starting_field=farmland_id,
                          state="pending", created_at=now, updated_at=now)
            if selected_world:
                record["world_id"] = selected_world
            if field_id is not None:
                record["starting_field_id"] = int(field_id)
            if isinstance(existing, dict) and existing.get("state") == "rejected":
                self.db.farm_requests.replace_one(request_query, record, upsert=True, session=session)
            else:
                self.db.farm_requests.update_one(request_query, {"$setOnInsert": record}, upsert=True, session=session)
            return self.db.farm_requests.find_one(request_query, session=session)

        # MongoDB's Database.atomic gives the reservation and request one
        # transaction.  Lightweight deterministic adapters execute the same
        # callback directly; their reservation collection still models the
        # unique-field boundary used by production MongoDB.
        if isinstance(self.database, Database):
            return self.database.atomic(reserve)
        return reserve(None)

    def request_status(self, discord_id):
        return self.db.farm_requests.find_one({"discord_id": str(discord_id)}, sort=[("updated_at", -1), ("created_at", -1)])

    def staff_status(self, server_key, save_key, discord_id):
        """Return one bounded, current-world farm lifecycle view for staff."""
        scope = self._scope(server_key, save_key)
        request_rows = list(self.db.farm_requests.find({**scope, "discord_id": str(discord_id)}))
        requests = sorted(request_rows,
                          key=lambda row: (str(row.get("updated_at") or ""),
                                           str(row.get("created_at") or "")), reverse=True)[:1]
        request = requests[0] if requests else None
        identity = self.db.game_identities.find_one({
            "server_id": str(server_key), "save_id": str(save_key), "discord_id": str(discord_id)})
        application = self.db.community_applications.find_one({"_id": str(discord_id)})
        # Farm lifecycle records use the server_key/save_key vocabulary, while
        # native authority projections deliberately use server_id/save_id.  Do
        # not query memberships with the lifecycle scope: that silently turns
        # every real manager/contractor relationship into "none" in the staff
        # diagnostic.  Authority is also world-scoped, so retain only the
        # active generation's rows.
        authority_scope = {"server_id": str(server_key), "save_id": str(save_key)}
        if scope.get("world_id"):
            authority_scope["world_id"] = scope["world_id"]
        memberships = list(self.db.memberships.find(
            {**authority_scope, "discord_id": str(discord_id)}).limit(20))
        operations = []
        operation_ids = []
        if isinstance(request, dict):
            operation_ids.extend(value for value in (
                request.get("operation_id"), request.get("land_operation_id"),
                request.get("permission_operation_id"), request.get("contractor_permission_operation_id")) if value)
            if request.get("financial_provisioning_id"):
                financial = self.db.farm_financial_provisioning.find_one({"_id": request["financial_provisioning_id"]})
            else:
                financial = None
        else:
            financial = None
        for operation_id in dict.fromkeys(operation_ids):
            operation = self.db.farm_operations.find_one({"_id": operation_id, **scope})
            if isinstance(operation, dict):
                operations.append(operation)
        # Permission jobs are the durable authority handoff records.  They do
        # not live in farm_operations, so include the jobs referenced by the
        # request and by current-world memberships.  This makes pending,
        # applied, failed, and retry states visible without widening the query
        # to historical generations or unrelated players.
        permission_operation_ids = list(dict.fromkeys(
            [row.get("operation_id") for row in memberships if row.get("operation_id")]
            + [request.get(key) for key in (
                "permission_operation_id", "contractor_permission_operation_id")
               if isinstance(request, dict) and request.get(key)]))
        permission_jobs = []
        for operation_id in permission_operation_ids:
            job = self.db.permission_jobs.find_one({"_id": operation_id, **authority_scope})
            if isinstance(job, dict):
                permission_jobs.append(job)
        session = None
        if isinstance(identity, dict):
            stable_id = identity.get("fs25_unique_user_id") or identity.get("game_player_id")
            if stable_id:
                session = self.db.player_activity_sessions.find_one({**scope,
                    "fs25_unique_user_id": str(stable_id)}, sort=[("last_seen_at", -1)])
        return {"server_key": str(server_key), "save_key": str(save_key),
                "world_id": scope.get("world_id"), "request": request,
                "identity": identity, "application": application, "memberships": memberships,
                "operations": operations, "permission_jobs": permission_jobs,
                "financial": financial, "session": session}

    def requests(self, server_key, save_key):
        return list(self.db.farm_requests.find({**self._scope(server_key, save_key),
            "state": {"$in": ["pending", "requested"]}}).sort("created_at", 1).limit(25))

    def reject_request(self, request_id, server_key, save_key, approved_by, reason):
        if not approved_by or not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
            raise ValueError("A staff reviewer and reason (1-300 characters) are required")
        result = self.db.farm_requests.update_one({"_id": request_id, **self._scope(server_key, save_key),
            "state": {"$in": ["pending", "requested"]}}, {"$set": {
                "state": "rejected", "reviewed_by": str(approved_by), "reason": reason.strip(),
                "reviewed_at": self._now(), "updated_at": self._now()}})
        if getattr(result, "modified_count", 1) != 1:
            raise ValueError("Request is unknown or already reviewed")
        self.db.farm_field_reservations.delete_one({"request_id": request_id,
            "server_key": str(server_key), "save_key": str(save_key)})

    def approve_request(self, request_id, server_key, save_key, approved_by):
        if not approved_by:
            raise ValueError("An operator is required")
        request = self.db.farm_requests.find_one({"_id": request_id, **self._scope(server_key, save_key)})
        if not request:
            raise ValueError("Unknown farm request")
        if request.get("state") in {"provisioning", "awaiting_manager", "active"} and request.get("operation_id"):
            return request["operation_id"]
        if request.get("state") == "land_assigning" and request.get("land_operation_id"):
            return request["land_operation_id"]
        if request.get("state") == "land_pending":
            return self._queue_requested_farmland(request, server_key, save_key, approved_by)
        if request.get("state") not in {"pending", "requested"}:
            raise ValueError("Farm request is already reviewed")
        farmland_id = int(request["starting_field"])
        fields = self.available_fields(server_key, save_key)
        if farmland_id not in fields:
            raise ValueError("Requested farmland is not present on the current save")
        if fields[farmland_id] != 0:
            raise ValueError("Requested farmland is already owned")
        operation_id = _operation_id("provision-farm", self.current_world_id(server_key, save_key) or "legacy", request_id)
        self._queue_operation(operation_id, "provision_farm", server_key, save_key,
                              {"farm_type": MEMBER_FARM_TYPE, "canonical_name": request["farm_name"],
                               "request_id": request_id, "farmland_id": farmland_id}, request_id=request_id)
        self.db.farm_requests.update_one({"_id": request_id, "state": {"$in": ["pending", "requested"]}}, {"$set": {
            "state": "provisioning", "operation_id": operation_id, "approved_by": str(approved_by),
            "approved_at": self._now(), "updated_at": self._now()}})
        return operation_id

    def _queue_requested_farmland(self, request, server_key, save_key, approved_by):
        """Queue the farmland selected in the original approved request.

        The current snapshot is only a preflight guard. The FS25 runtime reads
        ownership again immediately before mutation and supplies the decisive
        read-after-write evidence in its durable receipt.
        """
        if not approved_by:
            raise ValueError("An operator is required to assign farmland")
        try:
            farmland_id = int(request.get("starting_field"))
        except (TypeError, ValueError):
            raise ValueError("Farm request has no valid selected farmland ID") from None
        if farmland_id <= 0:
            raise ValueError("Farmland ID must be a positive FS25 farmland ID")
        if request.get("state") == "land_assigning":
            if int(request.get("assigned_farmland_id", 0)) == farmland_id and request.get("land_operation_id"):
                return request["land_operation_id"]
        if request.get("state") != "land_pending":
            raise ValueError("Farm is not awaiting ownership assignment")
        try:
            farm_id = int(request.get("farm_id"))
        except (TypeError, ValueError):
            raise ValueError("Farm request has no authoritative FS25 farm ID") from None
        fields = self.available_fields(server_key, save_key)
        if farmland_id not in fields:
            raise ValueError("Farmland ID is not present on the current save")
        if fields[farmland_id] not in {0, farm_id}:
            raise ValueError("Farmland is already owned; reassignment is denied")
        mapping = self.db.sin_farms.find_one({"_id": request.get("mapping_id"),
            **self._scope(server_key, save_key), "fs25_farm_id": farm_id})
        if not mapping:
            raise ValueError("Destination farm is not confirmed by the authoritative farm receipt")
        attempt = int(request.get("land_attempt", 0)) + 1
        operation_id = _operation_id("assign-farmland", self.current_world_id(server_key, save_key) or "legacy",
                                     request["_id"], farmland_id, attempt)
        self._queue_operation(operation_id, "assign_farmland", server_key, save_key, {
            "farmland_id": farmland_id, "farm_id": farm_id,
            "request_id": str(request["_id"]), "authorized_by": str(approved_by),
        }, request_id=request["_id"])
        result = self.db.farm_requests.update_one({"_id": request["_id"], **self._scope(server_key, save_key),
            "state": "land_pending"}, {"$set": {
                "state": "land_assigning", "land_operation_id": operation_id,
                "assigned_farmland_id": farmland_id, "land_attempt": attempt,
                "land_authorized_by": str(approved_by),
                "land_assignment_requested_at": self._now(), "updated_at": self._now()}})
        if getattr(result, "modified_count", 1) != 1:
            raise ValueError("Farm land state changed; retry after refreshing status")
        return operation_id

    def operations_for(self, server_key, save_key, world_id=None):
        if world_id is not None:
            self.require_current_world(server_key, save_key, world_id)
        # Registration/permission completion is not pushed from Discord to the
        # game server.  Reuse this existing Agent poll as the repair cadence for
        # a provisioned farm whose owner assignment is missing or incomplete.
        self._repair_manager_authorizations(server_key, save_key)
        self._repair_contractor_authorizations(server_key, save_key)
        rows = list(self.db.farm_operations.find({**self._scope(server_key, save_key, world_id),
            "state": {"$in": ["pending", "dispatched"]}}).sort("created_at", 1).limit(50))
        for row in rows:
            self.db.farm_operations.update_one({"_id": row["_id"], "state": {"$in": ["pending", "dispatched"]}},
                {"$set": {"state": "dispatched", "updated_at": self._now()}, "$inc": {"attempts": 1}})
        return rows

    def accept_receipt(self, server_key, save_key, receipt, world_id=None):
        if not isinstance(receipt, dict) or not receipt.get("operation_id"):
            raise ValueError("operation receipt is required")
        expected_world_id = self.current_world_id(server_key, save_key)
        if world_id is not None:
            self.require_current_world(server_key, save_key, world_id)
        if expected_world_id and str(receipt.get("world_id") or "") != expected_world_id:
            raise ValueError("operation receipt world generation does not match current FS25 save")
        operation = self.db.farm_operations.find_one({"_id": receipt["operation_id"],
            **self._scope(server_key, save_key, world_id)})
        if not operation:
            raise ValueError("unknown farm operation")
        if operation.get("operation_type") == "assign_farmland":
            return self._accept_farmland_receipt(operation, server_key, save_key, receipt)
        if operation.get("state") == "succeeded":
            request_id = (operation.get("payload") or {}).get("request_id")
            if request_id and operation.get("operation_type") == "provision_farm":
                request = self.db.farm_requests.find_one({"_id": request_id})
                if isinstance(request, dict) and request.get("land_confirmed") and request.get("farm_id") is not None:
                    self._ensure_manager_authorization(
                        request, server_key, save_key, request["farm_id"],
                        request.get("mapping_id"), (operation.get("payload") or {}).get("canonical_name"))
            return operation
        if receipt.get("status") not in {"applied", "already_applied"}:
            self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
                "state": "reconciliation_required", "receipt": receipt, "updated_at": self._now()}})
            return self._operation(operation["_id"])
        farm_id = receipt.get("farm_id") or receipt.get("result", {}).get("farm_id")
        if farm_id is None:
            raise ValueError("successful farm operation must return farm_id")
        farm_id = int(farm_id)
        payload = operation.get("payload") or {}
        if operation.get("operation_type") == "provision_farm":
            # Provisioning only creates/adopts the named farm. Land is a
            # separate explicit, receipt-gated operation from land_pending.
            pass
        world_id = operation.get("world_id") or self.current_world_id(server_key, save_key)
        mapping_id = _operation_id("farm", server_key, save_key, world_id or "legacy",
                                   payload.get("request_id") or operation["operation_id"])
        if operation.get("operation_type") == "ensure_farm":
            existing_system = list(self.db.sin_farms.find({
                **self._scope(server_key, save_key, world_id),
                "farm_type": SYSTEM_FARM_TYPE, "canonical_name": SYSTEM_FARM_NAME}))
            existing_ids = {int(row["fs25_farm_id"]) for row in existing_system
                            if row.get("fs25_farm_id") is not None}
            if len(existing_ids) > 1 or len(existing_system) > 1 and len(existing_ids) == 1:
                self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
                    "state": "reconciliation_required", "receipt": receipt,
                    "updated_at": self._now()}})
                return self._operation(operation["_id"])
            if existing_ids:
                existing = next(row for row in existing_system if row.get("fs25_farm_id") is not None)
                if int(next(iter(existing_ids))) != farm_id:
                    self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
                        "state": "reconciliation_required", "receipt": receipt,
                        "updated_at": self._now()}})
                    return self._operation(operation["_id"])
                mapping_id = existing["_id"]
        mapping = {"_id": mapping_id, "server_key": server_key, "save_key": save_key,
                   "canonical_name": payload.get("canonical_name"),
                   "farm_type": payload.get("farm_type", MEMBER_FARM_TYPE),
                   "owner_discord_id": None, "source_request_id": payload.get("request_id"),
                   "state": "active" if operation["operation_type"] == "ensure_farm" else "provisioned",
                   "starting_farmland_id": payload.get("farmland_id"), "operation_id": operation["operation_id"],
                   "updated_at": self._now()}
        if world_id:
            mapping["world_id"] = world_id
        insert_mapping = dict(mapping)
        insert_mapping.pop("updated_at", None)
        self.db.sin_farms.update_one({"_id": mapping_id}, {"$setOnInsert": insert_mapping, "$set": {
            "fs25_farm_id": farm_id, "updated_at": self._now()}}, upsert=True)
        self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
            "state": "succeeded", "receipt": receipt, "fs25_farm_id": farm_id, "updated_at": self._now()}})
        request_id = payload.get("request_id")
        if request_id:
            request = self.db.farm_requests.find_one({"_id": request_id})
            if request:
                self.db.farm_requests.update_one({"_id": request_id, "state": "provisioning"}, {"$set": {
                    "state": "land_pending", "farm_id": farm_id, "mapping_id": mapping_id,
                    "land_confirmed": False, "updated_at": self._now()}})
                pending_request = dict(request, state="land_pending", farm_id=farm_id, mapping_id=mapping_id)
                try:
                    self._queue_requested_farmland(pending_request, server_key, save_key,
                                                   request.get("approved_by") or "farm-approval")
                except ValueError as error:
                    # The farm exists, but ownership cannot advance until a
                    # current scoped snapshot makes the selected land safe.
                    self.db.farm_requests.update_one({"_id": request_id, "state": "land_pending"}, {"$set": {
                        "land_failure_reason": str(error)[:300], "updated_at": self._now()}})
        return self._operation(operation["_id"])

    def _accept_farmland_receipt(self, operation, server_key, save_key, receipt):
        """Commit land progression only from a scoped FS25 owner read-back."""
        payload = operation.get("payload") or {}
        success = receipt.get("status") in {"applied", "already_satisfied"}
        try:
            farmland_id = int(receipt.get("farmland_id"))
            farm_id = int(receipt.get("farm_id"))
            owner_before = int(receipt.get("owner_before_farm_id"))
            owner_after = int(receipt.get("owner_farm_id"))
        except (TypeError, ValueError):
            farmland_id = farm_id = owner_before = owner_after = None
        try:
            expected_farmland = int(payload.get("farmland_id"))
            expected_farm = int(payload.get("farm_id"))
        except (TypeError, ValueError):
            expected_farmland = expected_farm = None
        receipt_scope_matches = (
            (receipt.get("server_id") is None or str(receipt.get("server_id")) == str(server_key))
            and (receipt.get("save_id") is None or str(receipt.get("save_id")) == str(save_key))
            and (not operation.get("world_id") or str(receipt.get("world_id") or "") == str(operation["world_id"])))
        valid = success and farmland_id == expected_farmland and farm_id == expected_farm \
            and owner_after == expected_farm and owner_before is not None and receipt_scope_matches
        if not valid:
            state = "failed" if receipt.get("status") in {"rejected", "failed"} else "reconciliation_required"
            self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
                "state": state, "receipt": receipt, "updated_at": self._now()}})
            request_id = payload.get("request_id")
            if request_id:
                self.db.farm_requests.update_one({"_id": request_id, "state": "land_assigning"}, {"$set": {
                    "state": "land_pending", "land_failure_reason": str(receipt.get("receipt") or "ownership was not confirmed")[:300],
                    "updated_at": self._now()}})
            return self._operation(operation["_id"])
        if operation.get("state") == "succeeded":
            return operation
        request = self.db.farm_requests.find_one({"_id": payload.get("request_id"),
            **self._scope(server_key, save_key, operation.get("world_id")), "state": "land_assigning"})
        if not request:
            self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
                "state": "reconciliation_required", "receipt": receipt, "updated_at": self._now()}})
            return self._operation(operation["_id"])
        now = self._now()
        self.db.farm_operations.update_one({"_id": operation["_id"], "state": {"$in": ["pending", "dispatched"]}},
            {"$set": {"state": "succeeded", "receipt": receipt, "farmland_id": farmland_id,
                      "farm_id": farm_id, "owner_before_farm_id": owner_before,
                      "owner_farm_id": owner_after, "mutation_performed": receipt.get("mutation_performed"),
                      "updated_at": now}})
        # Financial provisioning is a separate capability from farm-manager
        # authority.  We must not claim that cash/loan mutations happened
        # before a live-verified adapter exists, but a successful authoritative
        # land read-back is sufficient to queue the owner's native manager
        # relationship.  Keeping these states separate avoids leaving a real
        # FS25 farm owner unable to manage the farm merely because financial
        # provisioning is still unavailable.
        if operation.get("world_id"):
            financial_id = _operation_id("financial-provisioning", server_key, save_key,
                                         operation["world_id"], request["_id"])
            self.db.farm_financial_provisioning.update_one({"_id": financial_id}, {"$setOnInsert": {
                "_id": financial_id, "server_key": server_key, "save_key": save_key,
                "world_id": operation["world_id"], "request_id": request["_id"],
                "farm_id": farm_id, "farmland_id": farmland_id,
                "target_operating_cash": 1_000_000,
                "target_land_loan": "authoritative_farmland_price_required",
                "state": "capability_required",
                "blocked_reason": "FS25 money and loan mutation/read-back adapter is not live-verified",
                "created_at": now}}, upsert=True)
            permission_operation = self._ensure_manager_authorization(
                request, server_key, save_key, farm_id, request.get("mapping_id"), request.get("farm_name"))
            request_state = "awaiting_manager" if permission_operation else "manager_authorization_required"
            current_request = self.db.farm_requests.find_one({"_id": request["_id"]})
            if isinstance(current_request, dict) and current_request.get("state") == "active":
                request_state = "active"
            self.db.farm_requests.update_one({"_id": request["_id"], "state": {"$in": ["land_assigning", "awaiting_manager", "manager_authorization_required"]}}, {"$set": {
                "state": request_state, "land_confirmed": True,
                "land_acknowledged_at": now, "owner_before_farm_id": owner_before,
                "owner_farm_id": owner_after, "financial_provisioning_id": financial_id,
                "financial_capability_state": "capability_required",
                "financial_capability_reason": "FS25 money and loan mutation/read-back adapter is not live-verified",
                "permission_operation_id": permission_operation,
                "updated_at": now}})
            return self._operation(operation["_id"])
        permission_operation = self._ensure_manager_authorization(
            request, server_key, save_key, farm_id, request.get("mapping_id"), request.get("farm_name"))
        self.db.farm_requests.update_one({"_id": request["_id"], "state": "land_assigning"}, {"$set": {
            "state": "awaiting_manager", "land_confirmed": True, "land_acknowledged_at": now,
            "owner_before_farm_id": owner_before, "owner_farm_id": owner_after,
            "permission_operation_id": permission_operation, "updated_at": now}})
        return self._operation(operation["_id"])

    def permission_applied(self, operation_id):
        request = self.db.farm_requests.find_one({"permission_operation_id": operation_id})
        if not request:
            return
        now = self._now()
        self.db.farm_requests.update_one({"_id": request["_id"], "state": "awaiting_manager"},
            {"$set": {"state": "active", "activated_at": now, "updated_at": now}})
        self.db.sin_farms.update_one({"_id": request.get("mapping_id")}, {"$set": {
            "state": "active", "owner_discord_id": request.get("discord_id"), "activated_at": now,
            "updated_at": now}})
