"""Durable local mailbox transport for development permission jobs.

Writing a command never changes its MongoDB state.  Only a matching success
receipt is passed to AuthorizationManager.acknowledge.
"""
import json
import argparse
from pathlib import Path
from xml.etree import ElementTree as ET


class LocalPermissionBridge:
    def __init__(self, authorization, directory, server_id, save_id):
        self.authorization = authorization
        self.directory = Path(directory)
        self.server_id, self.save_id = server_id, save_id
        self.commands = self.directory / "permission-commands"
        self.receipts = self.directory / "permission-receipts"

    def deliver(self):
        self.commands.mkdir(parents=True, exist_ok=True)
        jobs = self.authorization.db.permission_jobs.find({"server_id": self.server_id,
            "save_id": self.save_id, "state": "pending"})
        delivered = []
        for job in jobs:
            payload = {key: job[key] for key in ("_id", "server_id", "save_id", "game_player_id", "farm_id", "role", "revision")}
            destination = self.commands / (job["_id"] + ".xml")
            if not destination.exists():
                temporary = destination.with_suffix(".tmp")
                root = ET.Element("permissionCommand", schemaVersion="1", **{key: str(value) for key, value in payload.items()})
                ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
                temporary.replace(destination)
            delivered.append(job["_id"])
        manifest = ET.Element("permissionCommands", schemaVersion="1")
        for operation_id in delivered:
            ET.SubElement(manifest, "command", operationId=operation_id)
        temporary = self.commands / "manifest.tmp"
        ET.ElementTree(manifest).write(temporary, encoding="utf-8", xml_declaration=True)
        temporary.replace(self.commands / "manifest.xml")
        return delivered

    def consume_receipts(self):
        if not self.receipts.exists():
            return []
        applied = []
        for path in self.receipts.glob("*.xml"):
            receipt = ET.parse(path).getroot().attrib
            required = {"operation_id", "server_id", "save_id", "revision", "status", "receipt"}
            if not required.issubset(receipt) or receipt["status"] != "applied":
                continue
            if receipt["server_id"] != self.server_id or receipt["save_id"] != self.save_id:
                continue
            self.authorization.acknowledge(receipt["operation_id"], self.server_id, self.save_id,
                                           int(receipt["revision"]), receipt["receipt"])
            applied.append(receipt["operation_id"])
        return applied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="FS25 modSettings/FS25SiNNetworkLocal directory")
    parser.add_argument("--server", default="local-dev")
    parser.add_argument("--save", default="local-dev-save-001")
    args = parser.parse_args()
    from .database import Database
    from .authorization import AuthorizationManager
    database = Database(name="fs25_network_local_test")
    database.initialize()
    bridge = LocalPermissionBridge(AuthorizationManager(database), args.directory, args.server, args.save)
    print(json.dumps({"delivered": bridge.deliver(), "acknowledged": bridge.consume_receipts()}))


if __name__ == "__main__":
    main()
