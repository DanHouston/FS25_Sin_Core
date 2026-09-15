"""Small, auditable central workflows for non-core SiN community features.

These services deliberately stop at the durable central boundary when a
GIANTS runtime mutation is not verified.  They do not grant farm authority;
callers must provide an explicit actor and, where needed, an authorization
record from AuthorizationManager.
"""
from datetime import datetime, timezone
import hashlib
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_TEXT = 1000
MAX_CHAT = 500
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _now():
    return datetime.now(timezone.utc)


def _id(prefix, *parts):
    value = "|".join(str(part) for part in (prefix,) + parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value, field, limit=MAX_TEXT, required=True):
    value = str(value or "").strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} is too long")
    if CONTROL_CHARS.search(value):
        raise ValueError(f"{field} contains control characters")
    return value


def _amount(value):
    if type(value) is not int or not 0 < value <= 1_000_000_000:
        raise ValueError("Amount must be a whole currency unit between 1 and 1,000,000,000")
    return value


def parse_scheduled_start(value, default_timezone=None):
    """Parse an explicit ISO-8601 timestamp and normalize it to UTC.

    A timezone is required so a community event cannot silently move based on
    the machine timezone of the bot process.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _text(value, "Scheduled start", 80)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Scheduled start must be ISO-8601, for example 2026-09-15T20:00-04:00") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not default_timezone:
            raise ValueError("Scheduled start must include a timezone offset, for example -04:00 or Z")
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(str(default_timezone)))
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("The configured community timezone is invalid") from None
    return parsed.astimezone(timezone.utc)


class ChatService:
    """Idempotent chat message persistence and outbound operation creation."""

    def __init__(self, database):
        self.database, self.db = database, database.db

    @staticmethod
    def fs25_injection_supported():
        """The target GIANTS runtime adapter is not source-verified yet."""
        return False

    @staticmethod
    def sanitize(message):
        return _text(message, "Message", MAX_CHAT)

    def ingest_fs25(self, server_key, save_key, event_id, payload):
        message = self.sanitize(payload.get("message"))
        source = str(payload.get("source", "fs25"))
        if source == "discord":
            raise ValueError("Discord-originated messages cannot be mirrored back from FS25")
        message_id = str(payload.get("message_id") or event_id)
        storage_id = _id("fs25-chat", server_key, save_key, message_id)
        record = {"_id": storage_id, "message_id": message_id, "server_key": server_key,
                  "save_key": save_key, "source": "fs25", "message": message,
                  "unique_user_id": str(payload.get("unique_user_id", "")),
                  "created_at": _now(), "state": "received"}
        self.db.chat_messages.update_one({"_id": storage_id}, {"$setOnInsert": record}, upsert=True)
        return self.db.chat_messages.find_one({"_id": storage_id})

    def queue_to_fs25(self, server_key, save_key, actor_id, message, operation_id=None):
        message = self.sanitize(message)
        actor_id = str(actor_id)
        operation_id = operation_id or _id("discord-chat", server_key, save_key, actor_id, message, uuid.uuid4())
        record = {"_id": operation_id, "message_id": operation_id, "server_key": server_key,
                  "save_key": save_key, "source": "discord", "message": message,
                  "actor_discord_id": actor_id, "created_at": _now(), "state": "pending"}
        self.db.chat_messages.update_one({"_id": operation_id}, {"$setOnInsert": record}, upsert=True)
        self.db.farm_operations.update_one(
            {"_id": operation_id},
            {"$setOnInsert": {"_id": operation_id, "operation_id": operation_id,
                               "operation_type": "chat_message", "server_key": server_key,
                               "save_key": save_key, "payload": {"message_id": operation_id,
                               "message": message, "source": "discord"}, "state": "pending",
                               "attempts": 0, "created_at": _now()},
             "$set": {"updated_at": _now()}}, upsert=True)
        return operation_id

    def accept_receipt(self, operation_id, status, receipt, server_key=None, save_key=None):
        if status not in {"applied", "already_applied", "failed", "pending_validation"}:
            raise ValueError("Invalid chat operation outcome")
        operation = self.db.farm_operations.find_one({"_id": str(operation_id),
                                                       "operation_type": "chat_message"})
        if not operation:
            raise ValueError("Unknown chat operation")
        if server_key is not None and (operation.get("server_key") != server_key or operation.get("save_key") != save_key):
            raise ValueError("Chat operation is outside the authenticated server/save scope")
        state = "succeeded" if status in {"applied", "already_applied"} else "reconciliation_required"
        current = operation.get("state")
        if current == "succeeded":
            if state != "succeeded":
                raise ValueError("Chat operation has a conflicting terminal receipt")
            return current
        if current == "reconciliation_required" and state == "reconciliation_required":
            return current
        result = self.db.farm_operations.update_one(
            {"_id": str(operation_id), "operation_type": "chat_message"},
            {"$set": {"state": state, "receipt": receipt, "updated_at": _now()}})
        if result.modified_count != 1:
            raise ValueError("Chat operation changed before its receipt could be committed")
        self.db.chat_messages.update_one(
            {"_id": str(operation_id)},
            {"$set": {"state": "delivered" if state == "succeeded" else "not_delivered",
                      "receipt": receipt, "updated_at": _now()}})
        return state


class ContractService:
    STATUSES = {"open", "accepted", "in_progress", "completed", "cancelled"}
    WORK_TYPES = {
        "harvesting", "planting", "cultivating", "plowing", "fertilizing",
        "spraying", "liming", "rolling", "mowing", "baling", "transport", "forestry",
    }
    COMPENSATION_TYPES = {"fixed", "hourly"}

    def __init__(self, database):
        self.db = database.db

    def create(self, creator_id, title, description, value=0, server_key=None, save_key=None, due_at=None,
               work_type=None, fields=None, compensation_type="fixed", rate=None, server_name=None):
        if work_type is not None:
            work_type = str(work_type).strip().lower()
            if work_type not in self.WORK_TYPES:
                raise ValueError("Unsupported farm-work type")
        else:
            work_type = "general"
        compensation_type = str(compensation_type or "fixed").strip().lower()
        if compensation_type not in self.COMPENSATION_TYPES:
            raise ValueError("Compensation must be fixed or hourly")
        if rate is not None:
            value = rate
        if fields is not None:
            normalized_fields = []
            for item in str(fields).split(","):
                item = item.strip()
                if not item:
                    continue
                try:
                    field_id = int(item)
                except ValueError:
                    raise ValueError("Fields must be comma-separated positive numbers") from None
                if field_id <= 0:
                    raise ValueError("Fields must be comma-separated positive numbers")
                normalized_fields.append(str(field_id))
            if not normalized_fields:
                raise ValueError("At least one field is required")
            fields = ", ".join(dict.fromkeys(normalized_fields))
        title = _text(title or f"{work_type.title()} — Fields {fields or 'unspecified'}", "Contract title", 120)
        description = _text(description, "Contract description", MAX_TEXT)
        if type(value) is not int or value < 0 or value > 1_000_000_000:
            raise ValueError("Contract value must be a whole currency unit between 0 and 1,000,000,000")
        contract_id = str(uuid.uuid4())
        record = {"contract_id": contract_id, "creator_discord_id": str(creator_id),
                  "acceptor_discord_id": None, "creator_farm_id": None, "acceptor_farm_id": None,
                  "server_key": server_key, "save_key": save_key, "title": title,
                  "server_name": server_name,
                  "description": description, "value": value, "work_type": work_type,
                  "fields": fields, "compensation_type": compensation_type, "rate": value,
                  "status": "open",
                  "created_at": _now(), "due_at": due_at, "accepted_at": None,
                  "completed_at": None, "cancellation_reason": None, "completion_note": None}
        self.db.contracts.insert_one(record)
        return record

    def get(self, contract_id):
        return self.db.contracts.find_one({"contract_id": str(contract_id)})

    def open(self, server_key=None, save_key=None):
        query = {"status": "open"}
        if server_key is not None: query["server_key"] = server_key
        if save_key is not None: query["save_key"] = save_key
        return list(self.db.contracts.find(query).sort("created_at", 1).limit(50))

    def set_marketplace_message(self, contract_id, channel_id, message_id):
        """Record the public card location without changing contract state."""
        self.db.contracts.update_one(
            {"contract_id": str(contract_id)},
            {"$set": {"marketplace_channel_id": str(channel_id),
                       "marketplace_message_id": str(message_id),
                       "marketplace_updated_at": _now()}})

    def accept(self, contract_id, actor_id, farm_id=None):
        existing = self.get(contract_id)
        if existing and existing.get("creator_discord_id") == str(actor_id):
            raise ValueError("You can't accept a contract posted by your own farm")
        result = self.db.contracts.update_one(
            {"contract_id": str(contract_id), "status": "open",
             "creator_discord_id": {"$ne": str(actor_id)}},
            {"$set": {"status": "accepted", "acceptor_discord_id": str(actor_id),
                      "acceptor_farm_id": farm_id, "accepted_at": _now()}})
        if result.modified_count != 1:
            raise ValueError("Contract is unavailable or cannot be accepted by this actor")
        return self.get(contract_id)

    def cancel(self, contract_id, actor_id, reason, staff=False):
        reason = _text(reason, "Cancellation reason", 300)
        record = self.get(contract_id)
        if not record or (not staff and record.get("creator_discord_id") != str(actor_id)):
            raise ValueError("Only the contract creator or staff can cancel this contract")
        if record.get("status") in {"completed", "cancelled"}:
            raise ValueError("Contract is already terminal")
        result = self.db.contracts.update_one(
            {"contract_id": str(contract_id), "status": {"$nin": ["completed", "cancelled"]}},
            {"$set": {"status": "cancelled", "cancellation_reason": reason,
                      "cancelled_at": _now(), "cancelled_by": str(actor_id)}})
        if result.modified_count != 1:
            raise ValueError("Contract changed before it could be cancelled")
        return self.get(contract_id)

    def complete(self, contract_id, actor_id, note=""):
        note = _text(note, "Completion note", 500, required=False)
        record = self.get(contract_id)
        if not record or str(actor_id) not in {record.get("creator_discord_id"), record.get("acceptor_discord_id")}:
            raise ValueError("Only a contract participant can complete this contract")
        if record.get("status") not in {"accepted", "in_progress"}:
            raise ValueError("Contract is not ready for completion")
        result = self.db.contracts.update_one(
            {"contract_id": str(contract_id), "status": {"$in": ["accepted", "in_progress"]}},
            {"$set": {"status": "completed", "completed_at": _now(),
                      "completion_note": note, "completed_by": str(actor_id)}})
        if result.modified_count != 1:
            raise ValueError("Contract changed before it could be completed")
        return self.get(contract_id)


class InvoiceService:
    STATUSES = {"issued", "paid", "cancelled"}

    def __init__(self, database, banking):
        self.db, self.banking = database.db, banking

    def create(self, issuer_id, recipient_id, amount, description, due_at=None):
        if str(issuer_id) == str(recipient_id):
            raise ValueError("You cannot invoice yourself")
        amount = _amount(amount)
        description = _text(description, "Invoice description", MAX_TEXT)
        invoice_id = str(uuid.uuid4())
        record = {"invoice_id": invoice_id, "issuer_discord_id": str(issuer_id),
                  "recipient_discord_id": str(recipient_id), "amount": amount,
                  "description": description, "status": "issued", "created_at": _now(),
                  "issued_at": _now(), "due_at": due_at, "paid_at": None, "cancelled_at": None}
        self.db.invoices.insert_one(record)
        return record

    def get(self, invoice_id):
        return self.db.invoices.find_one({"invoice_id": str(invoice_id)})

    def list_for(self, discord_id):
        return list(self.db.invoices.find({"$or": [{"issuer_discord_id": str(discord_id)},
                                                     {"recipient_discord_id": str(discord_id)}]}).sort("created_at", -1).limit(50))

    def pay(self, invoice_id, payer_id):
        def pay_in_transaction(session):
            invoice = self.db.invoices.find_one({"invoice_id": str(invoice_id)}, session=session)
            if not invoice or invoice.get("recipient_discord_id") != str(payer_id):
                raise ValueError("Invoice is not payable by this actor")
            if invoice.get("status") == "paid":
                return invoice
            if invoice.get("status") != "issued":
                raise ValueError("Invoice is not payable")
            transaction_id = f"invoice:{invoice['invoice_id']}"
            self.banking.transfer_wallet(transaction_id, payer_id, invoice["issuer_discord_id"],
                                         invoice["amount"], "invoice payment", session=session)
            result = self.db.invoices.update_one(
                {"invoice_id": invoice["invoice_id"], "status": "issued"},
                {"$set": {"status": "paid", "paid_at": _now(),
                          "paid_by": str(payer_id), "transaction_id": transaction_id}}, session=session)
            if result.modified_count != 1:
                raise ValueError("Invoice changed while payment was being committed")
            return self.db.invoices.find_one({"invoice_id": invoice["invoice_id"]}, session=session)
        return self.banking.database.atomic(pay_in_transaction)

    def cancel(self, invoice_id, actor_id, staff=False):
        invoice = self.get(invoice_id)
        if not invoice or (not staff and invoice.get("issuer_discord_id") != str(actor_id)):
            raise ValueError("Only the issuer or staff can cancel this invoice")
        if invoice.get("status") != "issued":
            raise ValueError("Invoice is not cancellable")
        result = self.db.invoices.update_one(
            {"invoice_id": str(invoice_id), "status": "issued"},
            {"$set": {"status": "cancelled", "cancelled_at": _now(),
                      "cancelled_by": str(actor_id)}})
        if result.modified_count != 1:
            raise ValueError("Invoice changed before it could be cancelled")
        return self.get(invoice_id)


class CommunityEventService:
    STATUSES = {"scheduled", "active", "completed", "cancelled"}

    def __init__(self, database): self.db = database.db

    def create(self, organizer_id, name, description, scheduled_start, server_key=None, save_key=None,
               max_participants=None, default_timezone=None):
        name = _text(name, "Event name", 120)
        description = _text(description, "Event description", MAX_TEXT)
        scheduled_start = parse_scheduled_start(scheduled_start, default_timezone)
        if max_participants is not None and (type(max_participants) is not int or not 1 <= max_participants <= 500):
            raise ValueError("Maximum participants must be between 1 and 500")
        event_id = str(uuid.uuid4())
        record = {"event_id": event_id, "name": name, "description": description,
                  "organizer_discord_id": str(organizer_id), "server_key": server_key,
                  "save_key": save_key, "scheduled_start": scheduled_start,
                  "scheduled_end": None, "status": "scheduled", "participants": [],
                  "max_participants": max_participants, "created_at": _now()}
        self.db.community_events.insert_one(record)
        return record

    def get(self, event_id): return self.db.community_events.find_one({"event_id": str(event_id)})
    def list(self, status=None):
        return list(self.db.community_events.find({} if status is None else {"status": status}).sort("scheduled_start", 1).limit(50))

    def set_board_message(self, event_id, channel_id, message_id):
        self.db.community_events.update_one(
            {"event_id": str(event_id)},
            {"$set": {"board_channel_id": str(channel_id),
                       "board_message_id": str(message_id),
                       "board_updated_at": _now()}})

    def join(self, event_id, discord_id):
        event = self.get(event_id)
        if not event or event.get("status") not in {"scheduled", "active"}:
            raise ValueError("Event is not open")
        if str(discord_id) in event.get("participants", []): return event
        maximum = event.get("max_participants")
        query = {"event_id": str(event_id), "status": {"$in": ["scheduled", "active"]},
                 "participants": {"$ne": str(discord_id)}}
        if maximum is not None:
            # The capacity check must be part of the update predicate; a
            # read-then-write check allows two simultaneous joins to consume
            # the final slot.
            query["$expr"] = {"$lt": [{"$size": {"$ifNull": ["$participants", []]}}, maximum]}
        result = self.db.community_events.update_one(query,
                                                     {"$addToSet": {"participants": str(discord_id)}})
        if result.modified_count != 1:
            current = self.get(event_id)
            if current and str(discord_id) in current.get("participants", []):
                return current
            if current and current.get("status") in {"scheduled", "active"} and maximum is not None \
                    and len(current.get("participants", [])) >= maximum:
                raise ValueError("Event is full")
            raise ValueError("Event is no longer open")
        return self.get(event_id)

    def leave(self, event_id, discord_id):
        self.db.community_events.update_one({"event_id": str(event_id)}, {"$pull": {"participants": str(discord_id)}})
        return self.get(event_id)

    def finish(self, event_id, actor_id, status="completed", staff=False):
        if status not in {"completed", "cancelled"}:
            raise ValueError("Invalid terminal event status")
        event = self.get(event_id)
        if not event or (not staff and event.get("organizer_discord_id") != str(actor_id)):
            raise ValueError("Only the organizer can finish this event")
        result = self.db.community_events.update_one(
            {"event_id": str(event_id), "status": {"$nin": ["completed", "cancelled"]}},
            {"$set": {"status": status, "completed_at": _now(), "completed_by": str(actor_id)}})
        if result.modified_count != 1:
            raise ValueError("Event changed before it could be finished")
        return self.get(event_id)


class TransferService:
    """Auditable vehicle/product transfer state; game mutation is receipt-gated."""

    def __init__(self, database, authorization=None):
        self.db, self.authorization = database.db, authorization

    def create(self, kind, requester_id, server_key, save_key, source_farm_id, destination_farm_id,
               item, quantity=None, source_location=None):
        if kind not in {"vehicle", "product"}:
            raise ValueError("Transfer kind must be vehicle or product")
        if int(source_farm_id) <= 0 or int(destination_farm_id) <= 0 or int(source_farm_id) == int(destination_farm_id):
            raise ValueError("Source and destination farms must be different positive IDs")
        item = _text(item, "Transfer item", 120)
        if kind == "product" and (type(quantity) not in (int, float) or quantity <= 0):
            raise ValueError("Product quantity must be positive")
        if self.authorization is not None:
            manager = self.authorization.db.memberships.find_one({
                "discord_id": str(requester_id), "server_id": server_key, "save_id": save_key,
                "farm_id": int(source_farm_id), "desired_role": "farm_manager",
                # A queued/pending permission job is not yet game authority.
                # Value-affecting transfers require the mod-confirmed role.
                "state": "active", "applied_role": "farm_manager"})
            if not manager:
                raise ValueError("Source-farm manager authorization is required for a transfer")
        transfer_id = str(uuid.uuid4())
        record = {"transfer_id": transfer_id, "kind": kind, "requester_discord_id": str(requester_id),
                  "server_key": server_key, "save_key": save_key, "source_farm_id": int(source_farm_id),
                  "destination_farm_id": int(destination_farm_id), "item": item,
                  "quantity": quantity, "source_location": source_location, "status": "requested",
                  "created_at": _now(), "accepted_at": None, "operation_id": None,
                  "receipt": None}
        self.db.transfers.insert_one(record)
        return record

    def get(self, transfer_id): return self.db.transfers.find_one({"transfer_id": str(transfer_id)})

    def accept(self, transfer_id, actor_id):
        record = self.get(transfer_id)
        if not record or record.get("status") != "requested":
            raise ValueError("Transfer is not awaiting acceptance")
        if record.get("requester_discord_id") == str(actor_id):
            raise ValueError("Requester cannot accept their own transfer")
        result = self.db.transfers.update_one(
            {"transfer_id": str(transfer_id), "status": "requested"},
            {"$set": {"status": "accepted", "accepted_by": str(actor_id), "accepted_at": _now()}})
        if result.modified_count != 1:
            raise ValueError("Transfer changed before it could be accepted")
        return self.get(transfer_id)

    def queue_game_operation(self, transfer_id, actor_id):
        record = self.get(transfer_id)
        if not record or record.get("status") not in {"accepted", "pending_game"}:
            raise ValueError("Transfer must be accepted before game delivery")
        operation_id = record.get("operation_id") or _id("transfer", transfer_id)
        payload = {key: record.get(key) for key in ("transfer_id", "kind", "source_farm_id",
                                                     "destination_farm_id", "item", "quantity", "source_location")}
        self.db.farm_operations.update_one({"_id": operation_id},
            {"$setOnInsert": {"_id": operation_id, "operation_id": operation_id,
                               "operation_type": record["kind"] + "_transfer", "server_key": record["server_key"],
                               "save_key": record["save_key"], "payload": payload, "state": "pending",
                               "attempts": 0, "created_at": _now()}, "$set": {"updated_at": _now()}}, upsert=True)
        self.db.transfers.update_one({"transfer_id": str(transfer_id)},
                                     {"$set": {"status": "pending_game", "operation_id": operation_id,
                                               "queued_by": str(actor_id), "updated_at": _now()}})
        return operation_id

    def accept_receipt(self, transfer_id, receipt, server_key=None, save_key=None):
        if not isinstance(receipt, dict):
            raise ValueError("Transfer receipt must be an object")
        record = self.get(transfer_id)
        if not record:
            raise ValueError("Unknown transfer")
        if server_key is not None and (record.get("server_key") != server_key or record.get("save_key") != save_key):
            raise ValueError("Transfer is outside the authenticated server/save scope")
        if not receipt.get("operation_id") or record.get("operation_id") != receipt.get("operation_id"):
            raise ValueError("Transfer receipt does not match the queued operation")
        outcome = receipt.get("status")
        if outcome not in {"applied", "already_applied", "failed", "pending_validation", "definitively_not_applied"}:
            raise ValueError("Invalid transfer receipt status")
        target_status = "completed" if outcome in {"applied", "already_applied"} else "reconciliation_required"
        current_status = record.get("status")
        if current_status == "completed":
            if target_status != "completed":
                raise ValueError("Transfer has a conflicting terminal receipt")
            return record
        if current_status == "reconciliation_required" and target_status == "reconciliation_required":
            return record
        if current_status not in {"pending_game", "reconciliation_required"}:
            raise ValueError("Transfer is not awaiting a game receipt")
        result = self.db.transfers.update_one(
            {"transfer_id": str(transfer_id), "status": current_status},
            {"$set": {"status": target_status, "receipt": receipt, "updated_at": _now(),
                      "completed_at": _now() if target_status == "completed" else None}})
        if result.modified_count != 1:
            raise ValueError("Transfer changed before its receipt could be committed")
        operation_state = "succeeded" if target_status == "completed" else "reconciliation_required"
        operation_result = self.db.farm_operations.update_one(
            {"_id": record.get("operation_id"), "operation_type": record.get("kind") + "_transfer"},
            {"$set": {"state": operation_state, "receipt": receipt, "updated_at": _now()}})
        if operation_result.modified_count != 1:
            raise ValueError("Transfer operation changed before its receipt could be committed")
        return self.get(transfer_id)
