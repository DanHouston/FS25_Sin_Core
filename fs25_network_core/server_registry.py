"""Mongo-backed SiN server records and one-time pairing material."""
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from .clock_policy import validate_policy


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ServerRegistry:
    def __init__(self, database):
        self.db = database.db

    def register(self, server_key, display_name, guild_id, channel_id):
        if not server_key or not server_key.replace("-", "").replace("_", "").isalnum() or len(server_key) > 64:
            raise ValueError("Server key must contain only letters, numbers, '-' or '_'")
        if not display_name.strip():
            raise ValueError("Server display name is required")
        now = datetime.now(timezone.utc)
        old = self.db.sin_servers.find_one({"server_key": server_key})
        if old and old.get("credential_hash"):
            self.db.sin_servers.update_one({"_id": old["_id"]}, {"$set": {"display_name": display_name.strip(),
                "discord_guild_id": str(guild_id), "discord_activity_channel_id": str(channel_id), "updated_at": now}})
            return self.db.sin_servers.find_one({"_id": old["_id"]}), None
        pairing = secrets.token_urlsafe(8).replace("-", "").replace("_", "").upper()
        record = {"_id": server_key, "server_key": server_key, "display_name": display_name.strip(),
                  "discord_guild_id": str(guild_id), "discord_activity_channel_id": str(channel_id),
                  "enabled": True, "credential_hash": None, "paired_at": None,
                  "pairing_code_hash": _hash(pairing), "pairing_expires_at": now + timedelta(minutes=30),
                  "created_at": now, "updated_at": now}
        if old:
            self.db.sin_servers.replace_one({"_id": old["_id"]}, record)
        else:
            self.db.sin_servers.insert_one(record)
        return record, pairing

    def info(self, server_key):
        return self.db.sin_servers.find_one({"server_key": server_key})

    def pair(self, server_key, pairing_code):
        now = datetime.now(timezone.utc)
        record = self.db.sin_servers.find_one({"server_key": server_key, "enabled": True,
            "credential_hash": None, "pairing_code_hash": _hash(pairing_code.strip().upper()),
            "pairing_expires_at": {"$gt": now}})
        if not record:
            raise ValueError("Invalid, expired, or already-used server pairing code")
        credential = secrets.token_urlsafe(32)
        result = self.db.sin_servers.update_one({"_id": record["_id"], "credential_hash": None,
            "pairing_code_hash": record["pairing_code_hash"]}, {"$set": {"credential_hash": _hash(credential),
            "paired_at": now, "updated_at": now}, "$unset": {"pairing_code_hash": "", "pairing_expires_at": ""}})
        if result.modified_count != 1:
            raise ValueError("Server pairing was already claimed")
        return credential

    def pair_code(self, pairing_code):
        now = datetime.now(timezone.utc)
        record = self.db.sin_servers.find_one({"enabled": True, "credential_hash": None,
            "pairing_code_hash": _hash(pairing_code.strip().upper()), "pairing_expires_at": {"$gt": now}})
        if not record:
            raise ValueError("Invalid, expired, or already-used server pairing code")
        return record["server_key"], self.pair(record["server_key"], pairing_code)

    def authenticate(self, server_key, credential):
        record = self.db.sin_servers.find_one({"server_key": server_key, "enabled": True,
                                               "credential_hash": _hash(credential)})
        if not record:
            raise ValueError("Server authentication failed")
        return record

    def configure_save(self, server_key, save_key, fs25_save_id):
        if not server_key or not save_key or fs25_save_id is None or str(fs25_save_id).strip() == "":
            raise ValueError("Server save mapping requires server_key, save_key, and fs25_save_id")
        now = datetime.now(timezone.utc)
        document_id = f"{server_key}:{save_key}"
        self.db.sin_saves.update_one({"_id": document_id},
            {"$set": {"server_key": server_key, "save_key": save_key,
                      "fs25_save_id": str(fs25_save_id), "updated_at": now},
             "$setOnInsert": {"_id": document_id, "created_at": now}}, upsert=True)
        return self.db.sin_saves.find_one({"_id": document_id})

    def resolve_save(self, server_key, fs25_save_id):
        rows = list(self.db.sin_saves.find({"server_key": server_key,
            "fs25_save_id": str(fs25_save_id)}).limit(2))
        if len(rows) != 1:
            raise ValueError("Unknown or ambiguous FS25 save mapping")
        return rows[0]["save_key"]

    def configure_clock_policy(self, server_key, save_key, policy):
        value = validate_policy(policy)
        result = self.db.sin_saves.update_one(
            {"server_key": server_key, "save_key": save_key},
            {"$set": {"clock_policy": value, "updated_at": datetime.now(timezone.utc)}})
        if result.matched_count != 1:
            raise ValueError("Unknown server/save mapping; configure the FS25 save first")
        return self.db.sin_saves.find_one({"server_key": server_key, "save_key": save_key})

    def clock_policy(self, server_key, save_key):
        record = self.db.sin_saves.find_one({"server_key": server_key, "save_key": save_key})
        if not record:
            raise ValueError("Unknown server/save mapping")
        return validate_policy(record.get("clock_policy"))

    def heartbeat(self, server_key, credential):
        record = self.authenticate(server_key, credential)
        now = datetime.now(timezone.utc)
        was_online = bool(record.get("online"))
        self.db.sin_servers.update_one({"_id": record["_id"]}, {"$set": {"last_seen_at": now, "online": True}})
        return record, not was_online
