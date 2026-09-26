"""Mongo-backed SiN server records and one-time pairing material."""
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from .clock_policy import validate_policy
from .world_generation import WorldGenerationRegistry


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ServerRegistry:
    def __init__(self, database):
        self.db = database.db
        self.worlds = WorldGenerationRegistry(database)

    def register(self, server_key, display_name, guild_id, channel_id):
        if not server_key or not server_key.replace("-", "").replace("_", "").isalnum() or len(server_key) > 64:
            raise ValueError("Server key must contain only letters, numbers, '-' or '_'")
        if not display_name.strip():
            raise ValueError("Server display name is required")
        now = datetime.now(timezone.utc)
        old = self.db.sin_servers.find_one({"server_key": server_key})
        if old and old.get("credential_hash"):
            self.db.sin_servers.update_one({"_id": old["_id"]}, {"$set": {"display_name": display_name.strip(),
                "discord_guild_id": str(guild_id), "discord_activity_channel_id": str(channel_id),
                "discord_chat_channel_id": str(channel_id), "updated_at": now}})
            return self.db.sin_servers.find_one({"_id": old["_id"]}), None
        pairing = secrets.token_urlsafe(8).replace("-", "").replace("_", "").upper()
        record = {"_id": server_key, "server_key": server_key, "display_name": display_name.strip(),
                  "discord_guild_id": str(guild_id), "discord_activity_channel_id": str(channel_id),
                  "discord_chat_channel_id": str(channel_id),
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

    def configure_money_bridge(self, server_key, enabled):
        """Explicitly enable/disable the verified FS25 wallet bridge.

        The flag is intentionally separate from farm provisioning and from the
        legacy ``withdrawals_enabled`` setting.  Operators must enable it only
        after the deployed FS25 adapter has passed live mutation/readback
        validation.
        """
        record = self.info(server_key)
        if not record or not record.get("enabled"):
            raise ValueError("Unknown or disabled SiN server")
        result = self.db.sin_servers.update_one(
            {"server_key": str(server_key), "enabled": True},
            {"$set": {"fs25_money_bridge_enabled": bool(enabled),
                      "updated_at": datetime.now(timezone.utc)}})
        if getattr(result, "modified_count", 1) != 1 and record.get("fs25_money_bridge_enabled") != bool(enabled):
            raise ValueError("FS25 money bridge setting was not changed")
        return self.info(server_key)

    @staticmethod
    def _runtime_evidence(snapshot):
        """Normalize the small runtime proof carried by a game snapshot."""
        if not isinstance(snapshot, dict):
            raise ValueError("game snapshot is required")
        try:
            runtime_generation = int(snapshot.get("runtime_generation") or 0)
            sequence = int(snapshot.get("sequence") or 0)
        except (TypeError, ValueError):
            raise ValueError("invalid FS25 runtime evidence") from None
        if runtime_generation < 0 or sequence < 0:
            raise ValueError("invalid FS25 runtime evidence")
        return {
            "runtime_generation": runtime_generation,
            "session": str(snapshot.get("session") or ""),
            "sequence": sequence,
            "world_id": str(snapshot.get("world_id") or ""),
            "fs25_save_id": str(snapshot.get("savegame_index") or ""),
        }

    def active_runtime(self, server_key):
        record = self.db.sin_servers.find_one({"server_key": server_key})
        runtime = record.get("active_runtime") if isinstance(record, dict) else None
        return dict(runtime) if isinstance(runtime, dict) else None

    def validate_runtime_snapshot(self, server_key, save_key, snapshot):
        """Reject delayed runtime traffic before it can enter world state.

        ``runtime_generation`` is persisted by the mod mailbox and increases on
        every FS25 runtime load.  ``session``/``sequence`` remain useful for
        compatibility and same-runtime ordering, but are not sufficient by
        themselves because the session label is wall-clock-second based.
        """
        incoming = self._runtime_evidence(snapshot)
        active = self.active_runtime(server_key)
        if not active:
            return incoming
        try:
            active_generation = int(active.get("runtime_generation") or 0)
            active_sequence = int(active.get("sequence") or 0)
        except (TypeError, ValueError):
            raise ValueError("active FS25 runtime evidence is invalid") from None
        active_save = str(active.get("save_key") or "")
        active_session = str(active.get("session") or "")
        incoming_generation = incoming["runtime_generation"]

        if incoming_generation and active_generation:
            if incoming_generation < active_generation:
                raise ValueError("stale FS25 runtime snapshot")
            if incoming_generation == active_generation:
                if str(incoming["session"]) != active_session or str(save_key) != active_save:
                    raise ValueError("conflicting FS25 runtime snapshot")
                if incoming["sequence"] < active_sequence:
                    raise ValueError("stale FS25 runtime snapshot sequence")
            return incoming

        # A new-format runtime can safely supersede a legacy active pointer.
        if incoming_generation:
            return incoming

        # Legacy snapshots may continue refreshing the already-active save, but
        # cannot switch a server between saves without the durable runtime token.
        if str(save_key) != active_save:
            raise ValueError("runtime generation evidence is required to switch active saves")
        if incoming["session"] < active_session or (
                incoming["session"] == active_session and incoming["sequence"] < active_sequence):
            raise ValueError("stale FS25 runtime snapshot")
        return incoming

    def activate_runtime(self, server_key, save_key, snapshot):
        evidence = self.validate_runtime_snapshot(server_key, save_key, snapshot)
        runtime = dict(evidence, save_key=str(save_key), last_seen_at=datetime.now(timezone.utc))
        self.db.sin_servers.update_one({"server_key": server_key}, {"$set": {"active_runtime": runtime}})
        return runtime

    @staticmethod
    def _available_snapshot_fields(snapshot):
        """Return valid, unowned numeric farmland IDs from a game snapshot."""
        if not isinstance(snapshot, dict):
            return []
        fields = snapshot.get("farmlands") or {}
        available = []
        if not isinstance(fields, dict):
            return available
        for field_id, owner in fields.items():
            try:
                parsed_id = int(field_id)
                parsed_owner = int(owner or 0)
            except (TypeError, ValueError):
                continue
            if parsed_id > 0 and parsed_owner == 0:
                available.append(parsed_id)
        return sorted(set(available))

    def eligible_servers(self, purpose="reconcile"):
        """Return dynamic Discord server choices from the central registry.

        The legacy ``servers.json`` roster is deliberately not consulted here.
        It remains an explicit local-development adapter, while production
        Discord choices are derived from paired central records and their save
        state.

        ``info`` includes enabled records for staff inspection, ``reconcile``
        requires a paired server with at least one configured save, and
        ``farm_request`` additionally requires a current snapshot containing at
        least one available numeric farmland.
        """
        if purpose not in {"info", "reconcile", "farm_request"}:
            raise ValueError("Unknown server discovery purpose")

        records = []
        for server in self.db.sin_servers.find({"enabled": True}):
            if not isinstance(server, dict) or not server.get("enabled"):
                continue
            paired = bool(server.get("credential_hash"))
            if purpose != "info" and not paired:
                continue

            active_runtime = server.get("active_runtime") if isinstance(server.get("active_runtime"), dict) else None
            active_save_key = str(active_runtime.get("save_key")) if active_runtime and active_runtime.get("save_key") else None
            saves = []
            for save in self.db.sin_saves.find({"server_key": server.get("server_key")}):
                if not isinstance(save, dict) or not save.get("save_key"):
                    continue
                if purpose != "info" and active_save_key and str(save["save_key"]) != active_save_key:
                    continue
                if save.get("fs25_save_id") is None or str(save.get("fs25_save_id")).strip() == "":
                    continue
                save_choice = {"save_key": save["save_key"], "fs25_save_id": str(save["fs25_save_id"])}
                if purpose == "farm_request":
                    snapshot_query = {"server_key": server["server_key"], "save_key": save["save_key"]}
                    active_world = self.worlds.active_id(server["server_key"], save["save_key"])
                    if active_world:
                        snapshot_query["world_id"] = active_world
                    snapshot = self.db.server_snapshots.find_one(snapshot_query,
                        sort=[("received_at", -1)])
                    available = self._available_snapshot_fields(snapshot)
                    if not available:
                        continue
                    save_choice["available_fields"] = available
                saves.append(save_choice)

            if purpose == "reconcile" and not saves:
                continue
            if purpose == "farm_request" and not saves:
                continue
            result = {
                "server_key": server["server_key"],
                "display_name": server.get("display_name") or server["server_key"],
                "enabled": True,
                "paired": paired,
                "saves": saves,
            }
            for channel_field in ("discord_activity_channel_id", "discord_chat_channel_id"):
                if server.get(channel_field) is not None:
                    result[channel_field] = str(server[channel_field])
            if active_save_key:
                result["active_save_key"] = active_save_key
                result["active_world_id"] = active_runtime.get("world_id")
            records.append(result)
        return sorted(records, key=lambda record: (record["display_name"].lower(), record["server_key"]))

    def eligible_server(self, server_key, purpose="reconcile"):
        """Resolve one server choice and fail closed for stale/manual values."""
        matches = [record for record in self.eligible_servers(purpose)
                   if record.get("server_key") == server_key]
        if not matches:
            raise ValueError("Unknown or ineligible game server")
        return matches[0]

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

    def configure_save(self, server_key, save_key, fs25_save_id, expected_fs25_save_id=None):
        if not server_key or not save_key or fs25_save_id is None or str(fs25_save_id).strip() == "":
            raise ValueError("Server save mapping requires server_key, save_key, and fs25_save_id")
        now = datetime.now(timezone.utc)
        document_id = f"{server_key}:{save_key}"
        query = {"_id": document_id}
        if expected_fs25_save_id is not None:
            query["fs25_save_id"] = str(expected_fs25_save_id)
        result = self.db.sin_saves.update_one(query,
            {"$set": {"server_key": server_key, "save_key": save_key,
                       "fs25_save_id": str(fs25_save_id), "updated_at": now},
             "$setOnInsert": {"_id": document_id, "created_at": now}},
            upsert=expected_fs25_save_id is None)
        if expected_fs25_save_id is not None and getattr(
                result, "matched_count", getattr(result, "modified_count", 0)) != 1:
            raise ValueError("Current FS25 save mapping did not match the expected value")
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
