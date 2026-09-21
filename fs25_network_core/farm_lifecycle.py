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
            mapping_id = mappings[0].get("_id") if mappings and mappings[0].get("_id") else _operation_id(
                "farm", server_key, save_key, "ensure-system-farm")
            now = self._now()
            operation_id = _operation_id("ensure-system-farm", server_key, save_key)
            mapping_values = {"_id": mapping_id, "server_key": server_key, "save_key": save_key,
                              "canonical_name": SYSTEM_FARM_NAME, "farm_type": SYSTEM_FARM_TYPE,
                              "owner_discord_id": None, "source_request_id": None,
                              "state": "active", "operation_id": operation_id,
                              "fs25_farm_id": matches[0], "updated_at": now}
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

        operation_id = _operation_id("ensure-system-farm", server_key, save_key)
        existing_operation = self._operation(operation_id)
        if existing_operation and existing_operation.get("state") in {"succeeded", "reconciliation_required"}:
            return {"status": "reconciliation_required", "operation": existing_operation,
                    "mapping": mappings[0] if len(mappings) == 1 else None}
        operation = self._queue_operation(operation_id, "ensure_farm", server_key, save_key,
                                          {"farm_type": SYSTEM_FARM_TYPE, "canonical_name": SYSTEM_FARM_NAME})
        return {"status": "pending", "operation": operation,
                "mapping": mappings[0] if len(mappings) == 1 else None}

    def ensure_for_server(self, server_key):
        return [self.ensure_system_farm(server_key, row["save_key"])
                for row in self.db.sin_saves.find({"server_key": server_key})]

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
                request.get("approved_by") or "farm-provisioning",
                allow_unapproved_identity=True, idempotent=True)
            membership = self.db.memberships.find_one({
                "server_id": server_key, "save_id": save_key,
                "discord_id": str(request["discord_id"]), "farm_id": int(farm_id),
                "desired_role": "farm_manager"})
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
                contractor_operation = self._ensure_contractor_authorization(
                    request, server_key, save_key, int(farm_id), request.get("approved_by") or "farm-provisioning")
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

    def _ensure_contractor_authorization(self, request, server_key, save_key, personal_farm_id, approved_by):
        """Queue shared SiN Harvest access without replacing personal authority."""
        system = self.db.sin_farms.find_one({
            "server_key": server_key, "save_key": save_key,
            "farm_type": SYSTEM_FARM_TYPE, "canonical_name": SYSTEM_FARM_NAME,
            "state": "active", "fs25_farm_id": {"$exists": True}})
        if not isinstance(system, dict) or system.get("fs25_farm_id") is None:
            return None
        shared_farm_id = int(system["fs25_farm_id"])
        if shared_farm_id == int(personal_farm_id):
            return None
        operation = self.authorization.assign(
            request["discord_id"], server_key, save_key, shared_farm_id,
            "contractor", {shared_farm_id: SYSTEM_FARM_NAME}, approved_by,
            allow_unapproved_identity=True, idempotent=True)
        membership = self.db.memberships.find_one({
            "server_id": server_key, "save_id": save_key,
            "discord_id": str(request["discord_id"]), "farm_id": shared_farm_id,
            "desired_role": "contractor"})
        if isinstance(membership, dict) and membership.get("state") == "active" \
                and membership.get("applied_role") == "contractor":
            return operation
        return operation

    def _repair_manager_authorizations(self, server_key, save_key):
        """Retry missing owner assignments during the existing Agent poll."""
        requests = self.db.farm_requests.find({
            "server_key": server_key, "save_key": save_key,
            "state": {"$in": ["awaiting_manager", "manager_authorization_required"]},
            "farm_id": {"$exists": True}}).sort("updated_at", 1).limit(50)
        for request in list(requests):
            operation_id = request.get("operation_id")
            if operation_id:
                operation = self.db.farm_operations.find_one({"_id": operation_id,
                    "server_key": server_key, "save_key": save_key})
                if isinstance(operation, dict) and operation.get("state") != "succeeded":
                    continue
            self._ensure_manager_authorization(
                request, server_key, save_key, request.get("farm_id"),
                request.get("mapping_id"), request.get("farm_name"))

    def _repair_contractor_authorizations(self, server_key, save_key):
        requests = self.db.farm_requests.find({
            "server_key": server_key, "save_key": save_key,
            "state": {"$in": ["active", "awaiting_manager", "manager_authorization_required"]},
            "farm_id": {"$exists": True}}).sort("updated_at", 1).limit(50)
        for request in list(requests):
            try:
                operation = self._ensure_contractor_authorization(
                    request, server_key, save_key, request.get("farm_id"),
                    request.get("approved_by") or "contractor-repair")
                if operation:
                    self.db.farm_requests.update_one({"_id": request.get("_id")}, {"$set": {
                        "contractor_permission_operation_id": operation,
                        "contractor_authorization_state": "pending",
                        "updated_at": self._now()}})
            except Exception:
                # Keep the personal farm request authoritative and retry the
                # independent shared-farm relationship on the next poll.
                continue

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
        operation_id = _operation_id("provision-farm", request_id)
        self._queue_operation(operation_id, "provision_farm", server_key, save_key,
                              {"farm_type": MEMBER_FARM_TYPE, "canonical_name": request["farm_name"],
                               "request_id": request_id}, request_id=request_id)
        self.db.farm_requests.update_one({"_id": request_id, "state": {"$in": ["pending", "requested"]}}, {"$set": {
            "state": "provisioning", "operation_id": operation_id, "approved_by": str(approved_by),
            "approved_at": self._now(), "updated_at": self._now()}})
        return operation_id

    def land_pending_requests(self, server_key, save_key):
        """Return provisioned farms awaiting one explicit staff land decision."""
        return list(self.db.farm_requests.find({"server_key": server_key, "save_key": save_key,
            "state": "land_pending", "farm_id": {"$exists": True}}).sort("updated_at", 1).limit(25))

    def assign_farmland(self, request_id, server_key, save_key, farmland_id, approved_by):
        """Queue one operator-authorized, unowned-farmland assignment.

        The current snapshot is only a preflight guard. The FS25 runtime reads
        ownership again immediately before mutation and supplies the decisive
        read-after-write evidence in its durable receipt.
        """
        if not approved_by:
            raise ValueError("An operator is required to assign farmland")
        try:
            farmland_id = int(farmland_id)
        except (TypeError, ValueError):
            raise ValueError("Farmland ID must be a positive FS25 farmland ID") from None
        if farmland_id <= 0:
            raise ValueError("Farmland ID must be a positive FS25 farmland ID")
        request = self.db.farm_requests.find_one({"_id": request_id, "server_key": server_key,
            "save_key": save_key})
        if not request:
            raise ValueError("Unknown farm request for this server/save")
        if request.get("state") == "land_assigning":
            if int(request.get("assigned_farmland_id", 0)) == farmland_id and request.get("land_operation_id"):
                return request["land_operation_id"]
            raise ValueError("A different farmland assignment is already pending for this farm")
        if request.get("state") != "land_pending":
            raise ValueError("Farm is not awaiting an explicit farmland assignment")
        try:
            farm_id = int(request.get("farm_id"))
        except (TypeError, ValueError):
            raise ValueError("Farm request has no authoritative FS25 farm ID") from None
        fields = self.available_fields(server_key, save_key)
        if farmland_id not in fields:
            raise ValueError("Farmland ID is not present on the current save")
        if fields[farmland_id] != 0:
            raise ValueError("Farmland is already owned; reassignment is denied")
        snapshot = self.latest_snapshot(server_key, save_key) or {}
        farms = snapshot.get("farms") or {}
        if str(farm_id) not in {str(key) for key in farms}:
            raise ValueError("Destination farm is absent from the current authoritative snapshot")
        operation_id = str(uuid.uuid4())
        self._queue_operation(operation_id, "assign_farmland", server_key, save_key, {
            "farmland_id": farmland_id, "farm_id": farm_id,
            "request_id": str(request_id), "authorized_by": str(approved_by),
        }, request_id=request_id)
        result = self.db.farm_requests.update_one({"_id": request_id, "server_key": server_key,
            "save_key": save_key, "state": "land_pending"}, {"$set": {
                "state": "land_assigning", "land_operation_id": operation_id,
                "assigned_farmland_id": farmland_id, "land_authorized_by": str(approved_by),
                "land_assignment_requested_at": self._now(), "updated_at": self._now()}})
        if getattr(result, "modified_count", 1) != 1:
            raise ValueError("Farm land state changed; retry after refreshing status")
        return operation_id

    def operations_for(self, server_key, save_key):
        # Registration/permission completion is not pushed from Discord to the
        # game server.  Reuse this existing Agent poll as the repair cadence for
        # a provisioned farm whose owner assignment is missing or incomplete.
        self._repair_manager_authorizations(server_key, save_key)
        self._repair_contractor_authorizations(server_key, save_key)
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
        if operation.get("operation_type") == "assign_farmland":
            return self._accept_farmland_receipt(operation, server_key, save_key, receipt)
        if operation.get("state") == "succeeded":
            request_id = (operation.get("payload") or {}).get("request_id")
            if request_id and operation.get("operation_type") == "provision_farm":
                request = self.db.farm_requests.find_one({"_id": request_id})
                if isinstance(request, dict) and request.get("farm_id") is not None:
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
        mapping_id = _operation_id("farm", server_key, save_key, payload.get("request_id") or operation["operation_id"])
        if operation.get("operation_type") == "ensure_farm":
            existing_system = list(self.db.sin_farms.find({
                "server_key": server_key, "save_key": save_key,
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
            and (receipt.get("save_id") is None or str(receipt.get("save_id")) == str(save_key)))
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
            "server_key": server_key, "save_key": save_key, "state": "land_assigning"})
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
