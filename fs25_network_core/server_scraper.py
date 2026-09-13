"""Read exported save snapshots. Does not assume the panel exposes live transfers."""
from pathlib import Path
import os

from defusedxml import ElementTree


def read_farms(path):
    root = ElementTree.parse(Path(path)).getroot()
    farms = {}
    for farm in root.findall(".//farm"):
        farm_id = int(farm.attrib["farmId"])
        if farm_id in farms:
            raise ValueError("Duplicate farm ID in snapshot")
        farms[farm_id] = farm.attrib["name"]
    return farms


class ServerScraper:
    def __init__(self, servers):
        self.servers = servers

    def farms(self, server_id):
        if self.servers[server_id].get("snapshot_transport") == "local-file":
            return self.snapshot(server_id)["farms"]
        return read_farms(self.servers[server_id]["farms_xml"])

    def snapshot(self, server_id):
        from .local_test import read_snapshot
        config = self.servers[server_id]
        if not config.get("development") or config.get("snapshot_transport") != "local-file":
            raise ValueError("Only the operator-controlled local development snapshot adapter is available")
        path = os.environ.get("FS25_LOCAL_SNAPSHOT") or config.get("snapshot_xml")
        if not path:
            # Resolve redirected Documents (including OneDrive) on Windows.
            if os.name == "nt":
                import ctypes
                buffer = ctypes.create_unicode_buffer(260)
                result = ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer)
                if result != 0:
                    raise ValueError("Set FS25_LOCAL_SNAPSHOT to the mod's snapshot.xml path")
                documents = Path(buffer.value)
            else:
                documents = Path.home() / "Documents"
            path = documents / "My Games/FarmingSimulator2025/modSettings/FS25_SiN_NetworkLocal/snapshot.xml"
        try:
            snapshot = read_snapshot(path)
        except (OSError, ValueError, KeyError) as error:
            raise ValueError("Cannot read a fresh local snapshot. Load the SiN test mod, keep the game running, and check FS25_LOCAL_SNAPSHOT.") from error
        if snapshot["source"] != "game" or snapshot["savegame_index"] != config["savegame_index"]:
            raise ValueError("Snapshot must come from the configured game save slot, not the simulator")
        return snapshot
