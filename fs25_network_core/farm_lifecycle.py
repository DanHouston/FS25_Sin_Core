"""Central farm lifecycle state and durable game-operation coordination.

This module owns the SiN side of farm bootstrap/provisioning.  It never assumes
an FS25 numeric farm ID; IDs are learned from authenticated game receipts.
"""
from datetime import datetime, timezone
import hashlib
import uuid


SYSTEM_FARM_NAME = "SiN Harvest"
SYSTEM_FARM_TYPE = "system"
MEMBER_FARM_TYPE = "member"
OPERATION_STATES = {"pending", "dispatched", "succeeded", "failed", "reconciliation_required"}


def _operation_id(*parts):
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


class FarmLifecycle:
    """Idempotent central coordinator for system and member farms."""

    def __init__(self, database, authorization=None):
        self.database = database
        self.db = database.db
        if authorization is None:
            from .authorization import AuthorizationManager
            authorization = AuthorizationManager(database)
        self.authorization = authorization

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _scope(self, server_key, save_key):
        return {"server_key": str(server_key), "save_key": str(save_key)}

    def latest_snapshot(self, server_key, save_key):
        return self.db.server_snapshots.find_one(self._scope(server_key, save_key), sort=[("received_at", -1)])

    def record_snapshot(self, server_key, save_key, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("source") != "game":
            raise ValueError("game snapshot is required")
        now = self._now()
        record = dict(snapshot)
        record.update(self._scope(server_key, save_key), received_at=now)
        self.db.server_snapshots.update_one(self._scope(server_key, save_key), {"$set": record}, upsert=True)
        return record

    def _operation(self, operation_id):
        return self.db.farm_operations.find_one({"_id": operation_id})

    def _queue_operation(self, operation_id, operation_type, server_key, save_key, payload, request_id=None):
        now = self._now()
        values = dict(_id=operation_id, operation_id=operation_id, operation_type=operation_type,
                      server_key=server_key, save_key=save_key, payload=payload,
                      request_id=request_id, state="pending", attempts=0,
                      created_at=now, updated_at=now)
        insert_values = dict(values)
        insert_values.pop("updated_at", None)
        self.db.farm_operations.update_one(
            {"_id": operation_id},
            {"$setOnInsert": insert_values, "$set": {"updated_at": now}}, upsert=True)
        return self._operation(operation_id)

    def ensure_system_farm(self, server_key, save_key):
        scope = dict(self._scope(server_key, save_key), farm_type=SYSTEM_FARM_TYPE,
                     canonical_name=SYSTEM_FARM_NAME)
        mapping = self.db.sin_farms.find_one(scope)
        if mapping and mapping.get("fs25_farm_id") is not None and mapping.get("state") == "active":
            return {"status": "active", "mapping": mapping}
        operation_id = _operation_id("ensure-system-farm", server_key, save_key)
        operation = self._queue_operation(operation_id, "ensure_farm", server_key, save_key,
                                          {"farm_type": SYSTEM_FARM_TYPE, "canonical_name": SYSTEM_FARM_NAME})
        return {"status": "pending", "operation": operation, "mapping": mapping}

    def ensure_for_server(self, server_key):
        return [self.ensure_system_farm(server_key, row["save_key"])
                for row in self.db.sin_saves.find({"server_key": server_key})]

    def available_fields(self, server_key, save_key):
        snapshot = self.latest_snapshot(server_key, save_key)
        if not snapshot:
            raise ValueError("No current game snapshot is available")
        fields = snapshot.get("farmlands") or {}
        return {int(field_id): int(owner or 0) for field_id, owner in fields.items()}

    def request_farm(self, discord_id, server_key, save_key, starting_field):
        application = self.db.community_applications.find_one({"_id": str(discord_id), "state": "approved"})
        if not application or not application.get("farm_name"):
            raise ValueError("You must complete SiN membership approval before requesting a farm")
        try:
            field_id = int(str(starting_field).strip())
        except (TypeError, ValueError):
            raise ValueError("Starting field must be a numeric FS25 farmland ID") from None
        if field_id <= 0:
            raise ValueError("Starting field must be a positive FS25 farmland ID")
        owner = self.available_fields(server_key, save_key).get(field_id)
        if owner is None:
            raise ValueError("That starting field is not present on the current save")
        if owner != 0:
            raise ValueError("That starting field is no longer available")
        request_id = hashlib.sha256(f"{server_key}|{save_key}|{discord_id}".encode()).hexdigest()
        now = self._now()
        record = dict(_id=request_id, discord_id=str(discord_id), server_key=server_key, save_key=save_key,
                      farm_name=str(application["farm_name"]).strip(), starting_field=field_id,
                      state="pending", created_at=now, updated_at=now)
        self.db.farm_requests.update_one({"_id": request_id}, {"$setOnInsert": record}, upsert=True)
        return self.db.farm_requests.find_one({"_id": request_id})

    def request_status(self, discord_id):
        return self.db.farm_requests.find_one({"discord_id": str(discord_id)}, sort=[("updated_at", -1), ("created_at", -1)])

    def requests(self, server_key, save_key):
        return list(self.db.farm_requests.find({"server_key": server_key, "save_key": save_key,
            "state": {"$in": ["pending", "requested"]}}).sort("created_at", 1).limit(25))

    def reject_request(self, request_id, server_key, save_key, approved_by, reason):
        if not approved_by or not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
            raise ValueError("A staff reviewer and reason (1-300 characters) are required")
        result = self.db.farm_requests.update_one({"_id": request_id, "server_key": server_key,
            "save_key": save_key, "state": {"$in": ["pending", "requested"]}}, {"$set": {
                "state": "rejected", "reviewed_by": str(approved_by), "reason": reason.strip(),
                "reviewed_at": self._now(), "updated_at": self._now()}})
        if getattr(result, "modified_count", 1) != 1:
            raise ValueError("Request is unknown or already reviewed")

    def approve_request(self, request_id, server_key, save_key, approved_by):
        if not approved_by:
            raise ValueError("An operator is required")
        request = self.db.farm_requests.find_one({"_id": request_id, "server_key": server_key, "save_key": save_key})
        if not request:
            raise ValueError("Unknown farm request")
        if request.get("state") in {"provisioning", "awaiting_manager", "active"} and request.get("operation_id"):
            return request["operation_id"]
        if request.get("state") not in {"pending", "requested"}:
            raise ValueError("Farm request is already reviewed")
        field_id = int(request["starting_field"])
        fields = self.available_fields(server_key, save_key)
        if field_id not in fields:
            raise ValueError("Requested starting field is not present on the current save")
        if fields[field_id] != 0:
            raise ValueError("Requested starting field is already owned")
        operation_id = _operation_id("provision-farm", request_id)
        self._queue_operation(operation_id, "provision_farm", server_key, save_key,
                              {"farm_type": MEMBER_FARM_TYPE, "canonical_name": request["farm_name"],
                               "farmland_id": field_id, "request_id": request_id}, request_id=request_id)
        self.db.farm_requests.update_one({"_id": request_id, "state": {"$in": ["pending", "requested"]}}, {"$set": {
            "state": "provisioning", "operation_id": operation_id, "approved_by": str(approved_by),
            "approved_at": self._now(), "updated_at": self._now()}})
        return operation_id

    def operations_for(self, server_key, save_key):
        rows = list(self.db.farm_operations.find({"server_key": server_key, "save_key": save_key,
            "state": {"$in": ["pending", "dispatched"]}}).sort("created_at", 1).limit(50))
        for row in rows:
            self.db.farm_operations.update_one({"_id": row["_id"], "state": {"$in": ["pending", "dispatched"]}},
                {"$set": {"state": "dispatched", "updated_at": self._now()}, "$inc": {"attempts": 1}})
        return rows

    def accept_receipt(self, server_key, save_key, receipt):
        if not isinstance(receipt, dict) or not receipt.get("operation_id"):
            raise ValueError("operation receipt is required")
        operation = self.db.farm_operations.find_one({"_id": receipt["operation_id"], "server_key": server_key, "save_key": save_key})
        if not operation:
            raise ValueError("unknown farm operation")
        if operation.get("state") == "succeeded":
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
            owner_farm_id = receipt.get("owner_farm_id") or receipt.get("result", {}).get("owner_farm_id")
            receipt_field_id = receipt.get("farmland_id") or receipt.get("result", {}).get("farmland_id")
            if owner_farm_id is None or int(owner_farm_id) != farm_id \
                    or receipt_field_id is None or int(receipt_field_id) != int(payload.get("farmland_id")):
                self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
                    "state": "reconciliation_required", "receipt": receipt, "updated_at": self._now()}})
                return self._operation(operation["_id"])
        mapping_id = _operation_id("farm", server_key, save_key, payload.get("request_id") or operation["operation_id"])
        mapping = {"_id": mapping_id, "server_key": server_key, "save_key": save_key,
                   "fs25_farm_id": farm_id, "canonical_name": payload.get("canonical_name"),
                   "farm_type": payload.get("farm_type", MEMBER_FARM_TYPE),
                   "owner_discord_id": None, "source_request_id": payload.get("request_id"),
                   "state": "active" if operation["operation_type"] == "ensure_farm" else "provisioned",
                   "starting_farmland_id": payload.get("farmland_id"), "operation_id": operation["operation_id"],
                   "updated_at": self._now()}
        insert_mapping = dict(mapping)
        insert_mapping.pop("updated_at", None)
        self.db.sin_farms.update_one({"_id": mapping_id}, {"$setOnInsert": insert_mapping, "$set": {
            "fs25_farm_id": farm_id, "updated_at": self._now()}}, upsert=True)
        self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
            "state": "succeeded", "receipt": receipt, "fs25_farm_id": farm_id, "updated_at": self._now()}})
        request_id = payload.get("request_id")
        if request_id:
            request = self.db.farm_requests.find_one({"_id": request_id})
            self.db.farm_requests.update_one({"_id": request_id}, {"$set": {
                "state": "awaiting_manager", "farm_id": farm_id, "mapping_id": mapping_id, "updated_at": self._now()}})
            if request:
                identity = self.db.game_identities.find_one({"server_id": server_key, "save_id": save_key,
                    "discord_id": request["discord_id"]})
                if identity:
                    membership_operation = self.authorization.assign(request["discord_id"], server_key, save_key,
                        farm_id, "farm_manager", {farm_id: payload.get("canonical_name") or ""},
                        request.get("approved_by") or "farm-provisioning")
                    self.db.farm_requests.update_one({"_id": request_id}, {"$set": {
                        "permission_operation_id": membership_operation, "updated_at": self._now()}})
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
