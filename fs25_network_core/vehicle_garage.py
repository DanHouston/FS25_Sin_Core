"""Account-owned native vehicle archives. Game mutation is receipt/checkpoint gated."""
import base64
import csv
import hashlib
import io
import re
import uuid
from datetime import datetime, timezone

from defusedxml import ElementTree

from .activity import ActivityOutbox
from .vehicle_inventory import validate_vehicle_inventory
from .world_generation import WorldGenerationRegistry

MAX_ARCHIVE_BYTES = 2_000_000
GARAGE_TYPES = {"garage_prepare", "garage_store", "garage_retrieve"}


def now():
    return datetime.now(timezone.utc)


def garage_receipt_reason(value, limit=200):
    """Keep the useful native reason ahead of any display length limit.

    Historical receipts retain their raw Lua locations for audit, but those
    paths must not consume the space available for the farmer's explanation.
    """
    text = re.sub(r"^(?:.*?\.lua:\d+:\s*)+", "", str(value or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit-1] + "…"


def garage_csv(records, owner_id, owner_name):
    """Export the complete current garage snapshot supplied by list_for."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)

    def write(*values):
        safe = []
        for value in values:
            if isinstance(value, datetime):
                value = value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
            text = "" if value is None else str(value)
            if not isinstance(value, int) and text.lstrip().startswith(("=", "+", "-", "@", "\t")):
                text = "'" + text
            safe.append(text)
        writer.writerow(safe)

    write("Personal vehicle garage", owner_name, "Owner Discord ID", owner_id)
    write("Exported at UTC", now(), "Records", len(records))
    write("Scope", "Retained vehicles and pending/review operations; failed stores and completed retrievals excluded")
    write("Stored vehicles are retained assets and earn no farm sale credit.")
    write()
    write("Vehicle code", "Vehicle", "State", "Server", "Save", "World", "Source farm ID",
          "Destination farm ID", "Created at UTC", "Updated at UTC", "Store reason", "Retrieve reason",
          "Last receipt status", "Last receipt", "Garage ID", "Native vehicle ID", "Vehicle filename")
    for item in records:
        receipt = item.get("last_receipt") or {}
        write(item.get("vehicle_code"), item.get("name"), item.get("state"), item.get("server_key"),
              item.get("save_key"), item.get("world_id"), item.get("source_farm_id"),
              item.get("destination_farm_id"), item.get("created_at"), item.get("updated_at"),
              item.get("reason"), item.get("retrieve_reason"), receipt.get("status"), receipt.get("receipt"),
              item.get("_id"), item.get("native_unique_id"), item.get("source_filename"))
    return stream.getvalue().encode("utf-8-sig")


class VehicleGarageService:
    def __init__(self, database):
        self.database, self.db = database, database.db
        self.worlds = WorldGenerationRegistry(database)

    def _snapshot(self, scope):
        self.worlds.require_active(scope["server_key"], scope["save_key"], scope["world_id"])
        snapshot = self.db.server_snapshots.find_one({**scope, "source": "game"})
        observed = (snapshot or {}).get("received_at")
        if not isinstance(observed, datetime):
            raise ValueError("A fresh complete vehicle snapshot is required")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if not -30 <= (now() - observed).total_seconds() <= 120:
            raise ValueError("Vehicle snapshot is stale")
        rows = validate_vehicle_inventory(snapshot)
        if rows is None:
            raise ValueError("Vehicle inventory is incomplete")
        return snapshot, rows

    def _owner(self, scope, farm_id, session=None):
        options = {"session": session} if session is not None else {}
        matches = [row for row in self.db.sin_farms.find({**scope, "state": "active"}, **options)
                   if str(row.get("fs25_farm_id")) == str(farm_id)]
        if (len(matches) != 1 or matches[0].get("farm_type") != "member"
                or not str(matches[0].get("owner_discord_id") or "").isdigit()):
            raise ValueError("The farm must have one canonical member-account owner")
        return str(matches[0]["owner_discord_id"])

    def _code(self, vehicle_code):
        code = str(vehicle_code or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{5,12}", code):
            raise ValueError("Choose the vehicle's SiN code")
        row = self.db.vehicle_codes.find_one({"code": code})
        if not row:
            raise ValueError("Unknown vehicle code")
        return row

    def _operation(self, item, operation_id, operation_type, payload, session):
        self.db.farm_operations.update_one({"_id": operation_id}, {"$setOnInsert": {
            "_id": operation_id, "operation_id": operation_id,
            **{key: item[key] for key in ("server_key", "save_key", "world_id")},
            "operation_type": operation_type, "payload": {"garage_id": item["_id"],
                "vehicle_unique_id": item["native_unique_id"], "vehicle_code": item["vehicle_code"],
                "source_farm_id": item["source_farm_id"], **payload},
            "state": "pending", "created_at": now(), "updated_at": now()}}, upsert=True, session=session)

    def store(self, request_id, actor_id, server_key, vehicle_code, reason):
        reason = self._reason(reason)
        code = self._code(vehicle_code)
        scope = {key: code[key] for key in ("server_key", "save_key", "world_id")}
        if server_key != scope["server_key"]:
            raise ValueError("Choose the vehicle's current server")
        garage_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "sin-garage:" + str(request_id)))
        previous = self.db.vehicle_garage.find_one({"_id": garage_id})
        if previous:
            if previous["vehicle_code"] != code["code"] or previous["actor_id"] != str(actor_id):
                raise ValueError("Garage request ID was reused")
            return previous
        _, rows = self._snapshot(scope)
        matches = [row for row in rows if row["unique_id"] == code["native_unique_id"]]
        if len(matches) != 1:
            raise ValueError("Vehicle is not in the live inventory")
        vehicle = matches[0]

        def apply(session):
            existing = self.db.vehicle_garage.find_one({"_id": garage_id}, session=session)
            if existing:
                if existing["vehicle_code"] != code["code"] or existing["actor_id"] != str(actor_id):
                    raise ValueError("Garage request ID was reused")
                return existing
            owner = self._owner(scope, vehicle["farm_id"], session)
            # Unique custody prevents a second store/retrieve while this one is
            # unresolved. This survives farm decommission and bot restarts.
            lock = self.db.vehicle_codes.update_one({"code": code["code"],
                "garage_lock_id": {"$in": [None, ""]}}, {"$set": {"garage_lock_id": garage_id}}, session=session)
            if lock.modified_count != 1:
                raise ValueError("Vehicle already has an active garage operation or stored archive")
            for operation in self.db.farm_operations.find({**scope,
                    "operation_type": "vehicle_transfer", "state": {"$nin": ["succeeded", "failed", "world_superseded"]}}, session=session):
                if (operation.get("payload") or {}).get("vehicle_unique_id") == code["native_unique_id"]:
                    raise ValueError("Resolve the pending vehicle transfer before garage storage")
            item = {"_id": garage_id, **scope, "owner_discord_id": owner,
                    "vehicle_code": code["code"], "native_unique_id": code["native_unique_id"],
                    "source_farm_id": vehicle["farm_id"], "name": vehicle.get("name") or vehicle["filename"],
                    "source_filename": vehicle["filename"], "state": "preparing", "actor_id": str(actor_id),
                    "reason": reason, "created_at": now(), "updated_at": now(),
                    "request_id": str(request_id), "prepare_operation_id": garage_id + "-prepare"}
            self.db.vehicle_garage.insert_one(item, session=session)
            self._operation(item, item["prepare_operation_id"], "garage_prepare", {}, session)
            return item
        return self.database.atomic(apply)

    @staticmethod
    def _reason(reason):
        value = str(reason or "").strip()
        if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
            raise ValueError("An audit reason of 1-200 characters is required")
        return value

    def _scoped_item(self, garage_id, scope, session=None):
        options = {"session": session} if session is not None else {}
        self.worlds.require_active(scope["server_key"], scope["save_key"], scope["world_id"])
        item = self.db.vehicle_garage.find_one({"_id": str(garage_id), **scope}, **options)
        if not item:
            raise ValueError("Unknown garage archive in this server/save/world")
        return item

    def capture(self, scope, payload):
        item = self._scoped_item(payload.get("garage_id"), scope)
        raw = base64.b64decode(str(payload.get("archive_base64") or ""), validate=True)
        digest = hashlib.sha256(raw).hexdigest()
        if not 1 <= len(raw) <= MAX_ARCHIVE_BYTES or digest != payload.get("archive_sha256"):
            raise ValueError("Invalid garage archive bytes or checksum")
        root = ElementTree.fromstring(raw)
        vehicles = root.findall("vehicle") if root.tag == "vehicles" else []
        if (len(vehicles) != 1 or vehicles[0].get("uniqueId") != item["native_unique_id"]
                or vehicles[0].get("farmId") != str(item["source_farm_id"])
                or not vehicles[0].get("filename")):
            raise ValueError("Native garage archive does not match the queued vehicle and owner")

        def apply(session):
            current = self._scoped_item(item["_id"], scope, session)
            if current.get("archive_sha256"):
                if current["archive_sha256"] != digest:
                    raise ValueError("An immutable garage archive cannot be replaced")
                return current
            if current["state"] != "preparing":
                raise ValueError("Garage is not awaiting capture")
            # Independent durable copy and deletion command commit together.
            self.db.vehicle_garage_archives.insert_one({"_id": item["_id"], **scope,
                "archive_sha256": digest, "native_xml": raw, "created_at": now()}, session=session)
            operation_id = item["_id"] + "-store"
            self.db.vehicle_garage.update_one({"_id": item["_id"], "state": "preparing"}, {"$set": {
                "archive_sha256": digest, "store_operation_id": operation_id,
                "state": "removing", "updated_at": now()}}, session=session)
            self.db.farm_operations.update_one({"_id": item["prepare_operation_id"]},
                {"$set": {"state": "succeeded", "updated_at": now()}}, session=session)
            self._operation(item, operation_id, "garage_store", {"archive_sha256": digest}, session)
            return self.db.vehicle_garage.find_one({"_id": item["_id"]}, session=session)
        return self.database.atomic(apply)

    def retrieve(self, request_id, actor_id, vehicle_code, destination_server, destination_farm, reason):
        reason = self._reason(reason)
        code = self._code(vehicle_code)
        item = self.db.vehicle_garage.find_one({"_id": code.get("garage_lock_id")})
        if not item:
            raise ValueError("Vehicle has no active garage archive")
        scope = {key: item[key] for key in ("server_key", "save_key", "world_id")}
        if destination_server != scope["server_key"]:
            raise ValueError("First garage release restores only to the original server and world")
        destination_farm = int(destination_farm)
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "sin-garage-retrieve:" + str(request_id)))
        if item.get("retrieve_operation_id") == operation_id:
            if item.get("retrieve_actor_id") != str(actor_id) or item.get("destination_farm_id") != destination_farm:
                raise ValueError("Garage retrieval request ID was reused")
            return item
        _, rows = self._snapshot(scope)
        if any(row["unique_id"] == item["native_unique_id"] for row in rows):
            raise ValueError("Vehicle is already live; garage retrieval is blocked")
        if self._owner(scope, destination_farm) != item["owner_discord_id"]:
            raise ValueError("Retrieve into a farm owned by this garage's account holder")

        def apply(session):
            current = self._scoped_item(item["_id"], scope, session)
            if current.get("retrieve_operation_id") == operation_id:
                return current
            if current["state"] != "stored":
                raise ValueError("Vehicle is not durably stored; inspect the garage status first")
            self.db.vehicle_garage.update_one({"_id": item["_id"], "state": "stored"}, {"$set": {
                "state": "retrieving", "retrieve_operation_id": operation_id,
                "destination_farm_id": destination_farm, "retrieve_actor_id": str(actor_id),
                "retrieve_reason": reason, "updated_at": now()}}, session=session)
            self._operation(item, operation_id, "garage_retrieve", {
                "archive_sha256": current["archive_sha256"], "destination_farm_id": destination_farm}, session)
            return self.db.vehicle_garage.find_one({"_id": item["_id"]}, session=session)
        return self.database.atomic(apply)

    def archive_for_agent(self, scope, garage_id):
        item = self._scoped_item(garage_id, scope)
        archive = self.db.vehicle_garage_archives.find_one({"_id": item["_id"], **scope})
        if not archive or item["state"] not in {"removing", "retrieving", "awaiting_store_save", "awaiting_retrieve_save"}:
            raise ValueError("Garage archive is not available for an active operation")
        return {"archive_base64": base64.b64encode(archive["native_xml"]).decode("ascii"),
                "archive_sha256": archive["archive_sha256"]}

    def accept_receipt(self, scope, receipt):
        item = self._scoped_item(receipt.get("garage_id"), scope)
        kind = receipt.get("operation_type")
        operation = self.db.farm_operations.find_one({"_id": receipt.get("operation_id"), **scope})
        if (kind not in GARAGE_TYPES or not operation or operation.get("operation_type") != kind
                or (operation.get("payload") or {}).get("garage_id") != item["_id"]
                or receipt.get("vehicle_unique_id") != item["native_unique_id"]):
            raise ValueError("Garage receipt does not match the authorized operation")
        if kind == "garage_prepare" and receipt.get("status") == "prepared":
            return {"operation_id": operation["_id"], "state": "awaiting_archive"}
        status = receipt.get("status")
        verified = receipt.get("authoritative_readback") in (True, "true")
        if status == "applied" and verified and kind != "garage_prepare":
            expected = "awaiting_store_save" if kind == "garage_store" else "awaiting_retrieve_save"
            if receipt.get("archive_sha256") != item.get("archive_sha256"):
                raise ValueError("Garage receipt checksum does not match the durable archive")
            if kind == "garage_store" and receipt.get("vehicle_absent") not in (True, "true"):
                raise ValueError("Garage storage requires native absence readback")
            if kind == "garage_retrieve" and (str(receipt.get("owner_farm_id")) != str(item.get("destination_farm_id"))
                    or receipt.get("shop_position_readback") not in (True, "true")):
                raise ValueError("Garage retrieval requires shop and owner readback")
            if operation.get("state") == "succeeded":
                return {"operation_id": operation["_id"], "state": item["state"]}
            allowed = {"removing", expected} if kind == "garage_store" else {"retrieving", expected}
            if item["state"] not in allowed:
                raise ValueError("Garage receipt is out of sequence")
            self.db.vehicle_garage.update_one({"_id": item["_id"], "state": item["state"]},
                {"$set": {"state": expected, "last_receipt": receipt, "updated_at": now()}})
            self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
                "state": "awaiting_save", "receipt": receipt, "updated_at": now()}})
            return {"operation_id": operation["_id"], "state": expected}
        if operation.get("state") in {"succeeded", "awaiting_save"}:
            raise ValueError("Garage operation has a conflicting receipt")
        if status == "definitively_not_applied" and verified and receipt.get("mutation_started") in (False, "false"):
            source_present = (receipt.get("vehicle_absent") in (False, "false")
                              and str(receipt.get("owner_farm_id")) == str(item["source_farm_id"]))
            absent = receipt.get("vehicle_absent") in (True, "true")
            if (kind in {"garage_prepare", "garage_store"} and source_present
                    or kind == "garage_retrieve" and absent):
                target = "stored" if kind == "garage_retrieve" else "failed"
                def reject(session):
                    self.db.vehicle_garage.update_one({"_id": item["_id"], "state": item["state"]}, {"$set": {
                        "state": target, "last_receipt": receipt, "updated_at": now()}}, session=session)
                    self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
                        "state": "failed", "receipt": receipt, "updated_at": now()}}, session=session)
                    if target == "failed":
                        self.db.vehicle_codes.update_one({"code": item["vehicle_code"], "garage_lock_id": item["_id"]},
                            {"$unset": {"garage_lock_id": ""}}, session=session)
                    self._notice(item, "failed: " + garage_receipt_reason(receipt.get("receipt") or "native preflight failed"), session)
                self.database.atomic(reject)
                return {"operation_id": operation["_id"], "state": target}
        # Once a mutation was authorized, a failure/timeout never releases its
        # custody lock or makes the archive retrievable a second time.
        self.db.vehicle_garage.update_one({"_id": item["_id"]}, {"$set": {
            "state": "review_required", "last_receipt": receipt, "updated_at": now()}})
        self.db.farm_operations.update_one({"_id": operation["_id"]}, {"$set": {
            "state": "reconciliation_required", "receipt": receipt, "updated_at": now()}})
        self._notice(item, "needs review: " + garage_receipt_reason(receipt.get("receipt") or "game operation unconfirmed"))
        return {"operation_id": operation["_id"], "state": "review_required"}

    def checkpoint(self, scope, payload):
        item = self._scoped_item(payload.get("garage_id"), scope)
        state = item["state"]
        if state in {"stored", "retrieved"}:
            return {"state": state}
        if state not in {"awaiting_store_save", "awaiting_retrieve_save"}:
            raise ValueError("Garage operation is not awaiting a save checkpoint")
        if payload.get("archive_sha256") != item.get("archive_sha256"):
            raise ValueError("Garage checkpoint checksum mismatch")
        _, rows = self._snapshot(scope)
        live = [row for row in rows if row["unique_id"] == item["native_unique_id"]]
        target = "stored" if state == "awaiting_store_save" else "retrieved"
        if target == "stored":
            if live or payload.get("disk_vehicle_present") is not False:
                raise ValueError("The vehicle is not absent from both live inventory and the saved game")
        elif (len(live) != 1 or live[0]["farm_id"] != item["destination_farm_id"]
              or payload.get("disk_vehicle_present") is not True
              or str(payload.get("disk_owner_farm_id")) != str(item["destination_farm_id"])):
            raise ValueError("The restored vehicle is not confirmed in both live inventory and the saved game")

        def apply(session):
            changed = self.db.vehicle_garage.update_one({"_id": item["_id"], "state": state}, {"$set": {
                "state": target, "checkpoint": payload, "updated_at": now()}}, session=session)
            if changed.modified_count != 1:
                return {"state": self.db.vehicle_garage.find_one({"_id": item["_id"]}, session=session)["state"]}
            operation_id = item["store_operation_id"] if target == "stored" else item["retrieve_operation_id"]
            self.db.farm_operations.update_one({"_id": operation_id}, {"$set": {
                "state": "succeeded", "updated_at": now()}}, session=session)
            if target == "retrieved":
                self.db.vehicle_codes.update_one({"code": item["vehicle_code"], "garage_lock_id": item["_id"]},
                    {"$unset": {"garage_lock_id": ""}}, session=session)
            self._notice(item, "stored in the owner's garage." if target == "stored" else "retrieved at the shop.", session)
            return {"state": target}
        result = self.database.atomic(apply)
        return result

    def list_for(self, owner):
        # The garage is retained inventory and unresolved custody, not an
        # attempt history. Failed stores remain available in the audit data.
        return list(self.db.vehicle_garage.find({"owner_discord_id": str(owner),
                    "state": {"$nin": ["failed", "retrieved"]}}).sort("created_at", -1))

    def _notice(self, item, text, session=None):
        ActivityOutbox(self.database).enqueue("garage:" + item["_id"] + ":" + text,
            item["server_key"], "vehicle_transfer_receipt", "Vehicle garage for " + item["vehicle_code"] + " " + text,
            save_key=item["save_key"], world_id=item["world_id"], destination="interaction",
            metadata={"transfer_id": "garage:" + item["_id"], "discord_user_id": item.get("retrieve_actor_id") or item["actor_id"]},
            session=session)
