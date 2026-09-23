"""Durable local mailbox transport for development permission jobs.

Writing a command never changes its MongoDB state.  Only a matching success
receipt is passed to AuthorizationManager.acknowledge.
"""
import json
import argparse
import hashlib
import time
import logging
from pathlib import Path
from xml.etree import ElementTree as ET

LOG = logging.getLogger(__name__)


class LocalPermissionBridge:
    def __init__(self, authorization, directory, server_id, save_id):
        self.authorization = authorization
        self.directory = Path(directory)
        self.server_id, self.save_id = server_id, save_id
        self.commands = self.directory / "permission-commands"
        self.receipts = self.directory / "permission-receipts"
        self.events = self.directory / "events"

    def deliver(self):
        self.commands.mkdir(parents=True, exist_ok=True)
        pairing_requests = self.commands.glob("pairing-request-*.xml")
        if pairing_requests:
            from .server_registry import ServerRegistry
            registry = ServerRegistry(self.authorization.database)
            for request_path in pairing_requests:
                request = ET.parse(request_path).getroot().attrib
                try:
                    server_key, credential = registry.pair_code(request["code"])
                    response = ET.Element("serverPairingResponse", serverKey=server_key, credential=credential)
                    ET.ElementTree(response).write(self.commands / "server-pairing-response.xml", encoding="utf-8", xml_declaration=True)
                except (KeyError, ValueError):
                    pass
                request_path.unlink()
        jobs = self.authorization.db.permission_jobs.find({"server_id": self.server_id,
            "save_id": self.save_id, "state": "pending"})
        delivered = []
        for job in jobs:
            if job.get("role") == "contractor" and not job.get("source_farm_id"):
                # Pre-native target-only jobs are historical residue, not a
                # safe command. Central normally quarantines them during its
                # operations poll; keep this local bridge fail-closed too.
                continue
            payload = {key: job[key] for key in ("_id", "server_id", "save_id", "game_player_id", "farm_id", "role", "revision")}
            for key in ("source_farm_id", "legacy_cleanup"):
                if key in job:
                    payload[key] = job[key]
            destination = self.commands / (job["_id"] + ".xml")
            if not destination.exists():
                temporary = destination.with_suffix(".tmp")
                root = ET.Element("permissionCommand", schemaVersion="1", **{key: str(value) for key, value in payload.items()})
                ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
                temporary.replace(destination)
            delivered.append(job["_id"])
        # Deliberately do not deliver legacy land_operations.  Authoritative
        # farmland mutations use FarmLifecycle → Agent → authenticated Central
        # receipt so owner read-back and scope cannot be bypassed by this
        # development-only Mongo bridge.
        identities = self.authorization.db.game_identities.find({"server_id": self.server_id, "save_id": self.save_id})
        for identity in identities:
            application = self.authorization.db.community_applications.find_one(
                {"_id": identity["discord_id"], "state": "approved"})
            if not application:
                continue
            operation_id = "name-" + hashlib.sha256(identity["game_player_id"].encode()).hexdigest()
            payload = {"operation_id": operation_id, "operation_type": "align_name",
                       "server_id": self.server_id, "save_id": self.save_id,
                       "unique_user_id": identity["game_player_id"],
                       "canonical_name": application["server_nickname"]}
            destination = self.commands / (operation_id + ".xml")
            if not destination.exists():
                temporary = destination.with_suffix(".tmp")
                root = ET.Element("networkLocalCommand", schemaVersion="1",
                                  **{key: str(value) for key, value in payload.items()})
                ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
                temporary.replace(destination)
            delivered.append(operation_id)
        manifest = ET.Element("permissionCommands", schemaVersion="1")
        for operation_id in delivered:
            ET.SubElement(manifest, "command", operationId=operation_id)
        temporary = self.commands / "manifest.tmp"
        ET.ElementTree(manifest).write(temporary, encoding="utf-8", xml_declaration=True)
        temporary.replace(self.commands / "manifest.xml")
        relationships = self.authorization.db.memberships.find({"server_id": self.server_id,
            "save_id": self.save_id, "state": {"$in": ["pending", "active"]},
            "desired_role": {"$in": ["farm_manager", "contractor"]}})
        authority = ET.Element("managerAuthority", schemaVersion="1")
        for manager in relationships:
            if manager.get("desired_role") != "farm_manager":
                continue
            ET.SubElement(authority, "manager", gamePlayerId=manager["game_player_id"], farmId=str(manager["farm_id"]))
        for contractor in relationships:
            if contractor.get("desired_role") != "contractor" or not contractor.get("source_farm_id"):
                continue
            ET.SubElement(authority, "contractor", gamePlayerId=contractor["game_player_id"],
                          farmId=str(contractor["farm_id"]),
                          sourceFarmId=str(contractor.get("source_farm_id", 0)))
        temporary = self.directory / "manager-authority.tmp"
        ET.ElementTree(authority).write(temporary, encoding="utf-8", xml_declaration=True)
        temporary.replace(self.directory / "manager-authority.xml")
        return delivered

    def consume_receipts(self):
        if not self.receipts.exists():
            return []
        applied = []
        for path in self.receipts.glob("*.xml"):
            try:
                receipt = ET.parse(path).getroot().attrib
                required = {"operation_id", "server_id", "save_id", "revision", "status", "receipt"}
                if not required.issubset(receipt) or receipt["status"] not in {"applied", "already_applied"}:
                    raise ValueError("receipt is not a definitive success")
                if receipt["server_id"] != self.server_id or receipt["save_id"] != self.save_id:
                    raise ValueError("receipt scope does not match this bridge")
                self.authorization.acknowledge(receipt["operation_id"], self.server_id, self.save_id,
                                               int(receipt["revision"]), receipt, receipt.get("world_id"))
                applied.append(receipt["operation_id"])
            except (ET.ParseError, KeyError, TypeError, ValueError, OSError):
                failed = path.with_suffix(path.suffix + ".failed")
                try:
                    path.replace(failed)
                except OSError:
                    LOG.warning("could not quarantine invalid permission receipt path=%s", path)
        return applied

    def process_events(self, publisher=None):
        """Authenticate and consume Lua-originated events exactly once.

        ``publisher`` is deliberately injectable: the Discord bot supplies its
        channel-aware publisher, while the bridge remains usable headlessly.
        """
        if not self.events.exists():
            return []
        from .server_registry import ServerRegistry
        registry = ServerRegistry(self.authorization.database)
        processed = []
        for path in sorted(list(self.events.glob("*.json")) + list(self.events.glob("*.xml"))):
            try:
                if path.suffix == ".xml":
                    event = ET.parse(path).getroot().attrib
                    event["payload"] = {k: v for k, v in event.items() if k not in {"event_id", "event_type", "server_key", "server_credential", "created_at"}}
                else:
                    event = json.loads(path.read_text(encoding="utf-8"))
                event_id = str(event["event_id"])
                event_type = event["event_type"]
                record = registry.authenticate(event["server_key"], event["server_credential"])
                raw_save_id = event.get("save_id")
                resolved_save_key = registry.resolve_save(record["server_key"], raw_save_id)
                if resolved_save_key != self.save_id:
                    raise ValueError("event save does not match bridge canonical save")
                if event_type not in {"heartbeat", "player_connected", "player_disconnected"}:
                    raise ValueError("unsupported server event")
                duplicate = self.authorization.db.processed_server_events.find_one({"_id": event_id})
                if duplicate:
                    path.unlink(); continue
                now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                if event_type == "heartbeat":
                    was_online = bool(record.get("online"))
                    self.authorization.database.db.sin_servers.update_one({"_id": record["_id"]}, {"$set": {"online": True, "last_seen_at": now}})
                    if not was_online:
                        from .activity import ActivityOutbox
                        name = record.get("display_name") or record["server_key"]
                        ActivityOutbox(self.authorization.database).enqueue(
                            event_id, record["server_key"], "server_online",
                            f"🟢 Server Online\n{name} is connected to SiN JiN.", save_key=self.save_id)
                elif event_type in {"player_connected", "player_disconnected"}:
                    details = dict(event.get("payload") or {}, event_type=event_type)
                    from .activity import ActivityOutbox
                    ActivityOutbox(self.authorization.database).enqueue(
                        event_id, record["server_key"], event_type,
                        self.activity_message(record, details), save_key=self.save_id)
                LOG.info("[SiN Bridge] processed type=%s serverKey=%s", event_type, record["server_key"])
                self.authorization.db.processed_server_events.insert_one({"_id": event_id, "server_key": record["server_key"], "processed_at": now})
                path.unlink(); processed.append(event_id)
            except Exception as error:
                # Quarantine malformed/failed input so one file cannot stall the queue.
                failed = path.with_suffix(path.suffix + ".failed")
                try: path.replace(failed)
                except OSError: pass
                print(f"bridge: event rejected ({type(error).__name__})")
        return processed

    def activity_message(self, server, payload):
        """Return safe Discord presentation text; never expose game IDs."""
        unique_id = payload.get("unique_user_id")
        resolved = self.authorization.resolve_player_identity(server["server_key"], self.save_id, unique_id) if unique_id else {"fully_registered": False}
        LOG.info("[SiN Identity Resolve] server=%s save=%s player=%s uniqueUserId=%s linkFound=%s appApproved=%s fullyRegistered=%s reason=%s matches=%s",
                 server["server_key"], self.save_id, payload.get("display_name", ""), unique_id,
                 resolved.get("linked", False), resolved.get("application_approved", False),
                 resolved.get("fully_registered", False), resolved.get("reason", "no_unique_user_id"),
                 resolved.get("match_count", 0))
        registered = resolved.get("fully_registered", False)
        name = resolved.get("canonical_name") if registered else payload.get("display_name", "Player")
        verb = "joined" if payload.get("event_type") == "player_connected" else "left"
        icon = "\U0001f7e2" if verb == "joined" else "\U0001f534"
        suffix = "" if registered else " (SiN Registration: Required)"
        title = {"player_connected": "🟢 Player Connected", "player_disconnected": "🔴 Player Disconnected"}.get(payload.get("event_type"), "")
        return f"{icon} {name} has {verb} the server.{suffix}"

    def watch(self, interval=2.0, publisher=None, stop=None):
        """Run the existing bridge continuously until stop() returns true."""
        try:
            while not stop or not stop():
                self.deliver(); self.consume_receipts(); self.process_events(publisher)
                time.sleep(max(0.1, interval))
        except KeyboardInterrupt:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="FS25 modSettings/FS25_SiN_Server directory")
    parser.add_argument("--server", default="local-dev")
    parser.add_argument("--save", default="local-dev-save-001")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    from .database import Database
    from .authorization import AuthorizationManager
    database = Database(name="fs25_network_local_test")
    database.initialize()
    bridge = LocalPermissionBridge(AuthorizationManager(database), args.directory, args.server, args.save)
    if args.watch:
        bridge.watch(args.interval)
    else:
        print(json.dumps({"delivered": bridge.deliver(), "acknowledged": bridge.consume_receipts(), "events": bridge.process_events()}))


if __name__ == "__main__":
    main()
