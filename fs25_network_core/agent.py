"""Mongo-free per-server Agent for authenticated mailbox transport."""
import argparse
import errno
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote
from xml.etree import ElementTree

LOG = logging.getLogger(__name__)
DEFAULT_EVENT_BATCH_SIZE = 50


class MailboxWriteError(OSError):
    """A central request succeeded but its local mailbox publication failed."""


def _is_transient_windows_replace_denied(error):
    """Return true only for the Windows file-lock/access-denied race."""
    if getattr(error, "winerror", None) == 5:
        return True
    return os.name == "nt" and getattr(error, "errno", None) in {errno.EACCES, errno.EPERM}


def _atomic_replace(temporary, destination, attempts=8, initial_backoff=0.05, max_backoff=0.5):
    """Atomically publish a mailbox file through a bounded Windows lock retry."""
    delay = initial_backoff
    try:
        for attempt in range(attempts):
            try:
                temporary.replace(destination)
                return
            except PermissionError as error:
                # FS25 can briefly hold a mailbox file while enumerating or
                # opening it.  Retry only the Windows-style access-denied
                # replacement case; all other failures remain immediate.
                if not _is_transient_windows_replace_denied(error):
                    raise
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, max_backoff)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOG.warning("could not remove temporary mailbox file after failed atomic replace")
        raise


class PairingAgent:
    DEFAULT_EVENT_BATCH_SIZE = DEFAULT_EVENT_BATCH_SIZE
    MAX_EVENT_BATCH_SIZE = 500
    MAX_EVENT_LOOKAHEAD = 1000

    def __init__(self, mailbox_dir, backend_url, opener=None, event_batch_size=None):
        self.directory = Path(mailbox_dir)
        self.commands = self.directory / "permission-commands"
        self.registration_requests = self.directory / "registration-requests"
        self.registration_responses = self.directory / "registration-responses"
        self.backend_root = backend_url.rstrip("/")
        self.backend_url = self.backend_root + "/api/server/pair"
        self.operations_url = self.backend_root + "/api/server/operations"
        self.receipts_url = self.backend_root + "/api/server/operation-receipts"
        self.snapshot_url = self.backend_root + "/api/server/snapshot"
        self.manager_authority_url = self.backend_root + "/api/server/manager-authority"
        self.opener = opener or urlopen
        self._event_cache = {}
        batch_size = self.DEFAULT_EVENT_BATCH_SIZE if event_batch_size is None else int(event_batch_size)
        if batch_size < 1 or batch_size > self.MAX_EVENT_BATCH_SIZE:
            raise ValueError(f"event batch size must be between 1 and {self.MAX_EVENT_BATCH_SIZE}")
        self.event_batch_size = batch_size

    @staticmethod
    def _quarantine(path):
        """Move a poison mailbox item aside without blocking later items."""
        destination = path.with_suffix(path.suffix + ".failed")
        suffix = 1
        while destination.exists():
            destination = path.with_suffix(path.suffix + f".failed.{suffix}")
            suffix += 1
        try:
            path.replace(destination)
            return True
        except OSError:
            return False

    def _pair(self, pairing_code):
        body = json.dumps({"pairing_code": pairing_code}).encode("utf-8")
        request = Request(self.backend_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with self.opener(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("pairing API rejected request")
            payload = json.loads(response.read().decode("utf-8"))
        server_key = payload.get("server_key")
        credential = payload.get("credential")
        if not isinstance(server_key, str) or not server_key or not isinstance(credential, str) or not credential:
            raise RuntimeError("pairing API returned an invalid response")
        return server_key, credential

    def _post_event(self, event):
        transport_event = dict(event)
        server_key = transport_event.get("server_key")
        credential = transport_event.pop("server_credential", None)
        headers = {"Content-Type": "application/json"}
        if server_key and credential:
            headers["X-SiN-Server-Key"] = str(server_key)
            headers["Authorization"] = "Bearer " + str(credential)
        body = json.dumps(transport_event).encode("utf-8")
        request = Request(self.backend_url.rsplit("/api/server/pair", 1)[0] + "/api/server/events",
                          data=body, headers=headers, method="POST")
        with self.opener(request, timeout=10) as response:
            if response.status != 200:
                raise HTTPError(request.full_url, response.status, "event API rejected request",
                                response.headers, None)
            return json.loads(response.read().decode("utf-8"))

    def _post_registration(self, server_key, credential, request):
        body = json.dumps(request).encode("utf-8")
        request_obj = Request(self.backend_root + "/api/server/registration/request", data=body,
                              headers={"Content-Type": "application/json", "X-SiN-Server-Key": server_key,
                                       "Authorization": "Bearer " + credential}, method="POST")
        with self.opener(request_obj, timeout=10) as response:
            if response.status != 200:
                raise HTTPError(request_obj.full_url, response.status, "registration API rejected request", response.headers, None)
            return json.loads(response.read().decode("utf-8"))

    def _write_registration_response(self, result):
        self.registration_responses.mkdir(parents=True, exist_ok=True)
        request_id = str(result["request_id"])
        destination = self.registration_responses / (request_id + ".xml")
        temporary = destination.with_suffix(".tmp")
        root = ElementTree.Element("registrationResponse", **{
            key: str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in result.items() if value is not None})
        ElementTree.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
        _atomic_replace(temporary, destination)

    def _binding(self):
        root = ElementTree.parse(self.directory / "serverBinding.xml").getroot()
        if root.tag != "serverBinding" or not root.get("serverKey") or not root.get("credential"):
            raise ValueError("invalid server binding")
        return root.get("serverKey"), root.get("credential")

    def _runtime_save_id(self):
        root = ElementTree.parse(self.directory / "snapshot.xml").getroot()
        save_id = root.get("savegameIndex") if root.tag == "networkLocal" else None
        if not save_id:
            raise ValueError("runtime FS25 save ID is unavailable")
        return save_id

    def _runtime_world_id(self):
        root = ElementTree.parse(self.directory / "snapshot.xml").getroot()
        world_id = root.get("worldId") if root.tag == "networkLocal" else None
        if not world_id:
            raise ValueError("authoritative FS25 world generation is unavailable")
        return world_id

    def _get_clock_policy(self, server_key, credential, fs25_save_id):
        url = self.backend_root + "/api/server/clock?fs25_save_id=" + quote(str(fs25_save_id), safe="")
        request = Request(url, headers={"X-SiN-Server-Key": server_key,
                                        "Authorization": "Bearer " + credential}, method="GET")
        with self.opener(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("clock API rejected request")
            return json.loads(response.read().decode("utf-8"))

    def _get_operations(self, server_key, credential, fs25_save_id, world_id):
        url = (self.operations_url + "?fs25_save_id=" + quote(str(fs25_save_id), safe="")
               + "&world_id=" + quote(str(world_id), safe=""))
        request = Request(url, headers={"X-SiN-Server-Key": server_key,
                                        "Authorization": "Bearer " + credential}, method="GET")
        with self.opener(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("operation API rejected request")
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict) or not isinstance(result.get("operations"), list):
            raise ValueError("operation API returned an invalid response")
        return result["operations"]

    def _post_receipt(self, server_key, credential, fs25_save_id, world_id, receipt):
        body = json.dumps({"fs25_save_id": str(fs25_save_id), "world_id": str(world_id), "receipt": receipt}).encode("utf-8")
        request = Request(self.receipts_url, data=body,
                          headers={"Content-Type": "application/json", "X-SiN-Server-Key": server_key,
                                   "Authorization": "Bearer " + credential}, method="POST")
        with self.opener(request, timeout=10) as response:
            if response.status != 200:
                raise HTTPError(request.full_url, response.status, "receipt API rejected request", response.headers, None)
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _safe_http_error_detail(raw_body):
        """Return bounded, non-secret response details for operational logs."""
        if isinstance(raw_body, bytes):
            text = raw_body.decode("utf-8", errors="replace")
        else:
            text = str(raw_body or "")
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"body": text[:240]}
        if isinstance(payload, dict):
            safe = {}
            for key in ("error", "reason", "message", "status"):
                if key in payload and payload[key] is not None:
                    safe[key] = str(payload[key])[:240]
            return safe or {"body": text[:240]}
        return {"body": text[:240]}

    def _snapshot_payload(self):
        root = ElementTree.parse(self.directory / "snapshot.xml").getroot()
        if root.tag != "networkLocal" or root.get("source") != "game":
            raise ValueError("invalid game snapshot")
        farms = {}
        for node in root.findall("./farms/farm"):
            if node.get("farmId") is not None:
                farms[node.get("farmId")] = node.get("name", "")
        players = {}
        for node in root.findall("./players/player"):
            unique = node.get("uniqueId")
            if unique:
                players[unique] = {"name": node.get("name", ""), "user_id": node.get("userId"),
                                   "farm_id": int(node.get("farmId", "0")), "connected": True}
        farmlands = {}
        for node in root.findall("./farmlands/farmland"):
            if node.get("id") is not None:
                farmlands[node.get("id")] = int(node.get("farmId", "0"))
        return {"source": "game", "session": root.get("session", ""),
                "sequence": int(root.get("sequence", "0")),
                "savegame_index": int(root.get("savegameIndex", "0")),
                "world_id": root.get("worldId", ""), "map_id": root.get("mapId", ""),
                "farms": farms, "players": players, "farmlands": farmlands}

    def process_operations_once(self):
        """Materialize central durable operations into the existing Lua mailbox."""
        self.commands.mkdir(parents=True, exist_ok=True)
        try:
            server_key, credential = self._binding()
            fs25_save_id = self._runtime_save_id()
            world_id = self._runtime_world_id()
            operations = self._get_operations(server_key, credential, fs25_save_id, world_id)
        except (ElementTree.ParseError, ValueError, OSError, HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError):
            return []
        delivered = []
        for operation in operations:
            operation_id = str(operation.get("operation_id", ""))
            payload = operation.get("payload") or {}
            if not operation_id or not isinstance(payload, dict):
                continue
            destination = self.commands / (operation_id + ".xml")
            # A durable receipt is authoritative evidence that this operation
            # was already consumed by the game. Do not recreate or redispatch
            # its command during the fetch-before-ack window.
            if (self.directory / "permission-receipts" / (operation_id + ".xml")).exists():
                continue
            if not destination.exists():
                values = {"operation_id": operation_id, "operation_type": operation.get("operation_type", ""),
                          "server_id": server_key, "save_id": operation.get("save_key", ""),
                          "world_id": world_id,
                          **{str(key): value for key, value in payload.items()}}
                root_name = "permissionCommand" if operation.get("operation_type") == "permission" else "networkLocalCommand"
                root = ElementTree.Element(root_name, schemaVersion="1",
                                           **{key: str(value) for key, value in values.items() if value is not None})
                temporary = destination.with_suffix(".tmp")
                ElementTree.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
                _atomic_replace(temporary, destination)
            delivered.append(operation_id)
        manifest = ElementTree.Element("permissionCommands", schemaVersion="1")
        for operation_id in delivered:
            ElementTree.SubElement(manifest, "command", operationId=operation_id)
        temporary = self.commands / "manifest.tmp"
        ElementTree.ElementTree(manifest).write(temporary, encoding="utf-8", xml_declaration=True)
        try:
            _atomic_replace(temporary, self.commands / "manifest.xml")
        except OSError:
            # The command XMLs remain durable and will be included again on
            # the next pass.  Make the failure explicit without mislabeling
            # it as a pairing/API failure.
            LOG.warning("permission-command manifest write failed; retaining commands for retry")
            raise
        return delivered

    def process_receipts_once(self):
        try:
            server_key, credential = self._binding()
            fs25_save_id = self._runtime_save_id()
            world_id = self._runtime_world_id()
        except (ElementTree.ParseError, ValueError, OSError):
            return []
        receipts = []
        receipt_directory = self.directory / "permission-receipts"
        if not receipt_directory.exists():
            return receipts
        for path in sorted(receipt_directory.glob("*.xml")):
            try:
                root = ElementTree.parse(path).getroot()
                if root.tag not in {"networkLocalReceipt", "permissionReceipt"} or not root.get("operation_id"):
                    continue
                receipt = dict(root.attrib)
                receipt["result"] = {key: value for key, value in receipt.items()
                                      if key not in {"operation_id", "operation_type", "server_id", "save_id", "status", "receipt"}}
                self._post_receipt(server_key, credential, fs25_save_id, world_id, receipt)
                path.unlink()
                receipts.append(path.name)
            except (ElementTree.ParseError, ValueError):
                self._quarantine(path)
            except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as error:
                if getattr(error, "code", None) in {400, 401, 403, 404, 422}:
                    # Failed/rejected receipts remain durable audit evidence;
                    # Central reconciliation may need them after a policy or
                    # scope correction. Never bulk-delete authoritative acks.
                    LOG.warning("receipt rejected; retaining for audit path=%s", path.name)
        return receipts

    def process_manager_authority_once(self):
        try:
            server_key, credential = self._binding()
            fs25_save_id = self._runtime_save_id()
            world_id = self._runtime_world_id()
            url = (self.manager_authority_url + "?fs25_save_id=" + quote(str(fs25_save_id), safe="")
                   + "&world_id=" + quote(str(world_id), safe=""))
            request = Request(url, headers={"X-SiN-Server-Key": server_key,
                                            "Authorization": "Bearer " + credential}, method="GET")
            with self.opener(request, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError("manager authority API rejected request")
                payload = json.loads(response.read().decode("utf-8"))
            managers = payload.get("managers") if isinstance(payload, dict) else None
            if not isinstance(managers, list):
                raise ValueError("manager authority API returned an invalid response")
            contractors = payload.get("contractors", []) if isinstance(payload, dict) else []
            if not isinstance(contractors, list):
                raise ValueError("contractor authority API returned an invalid response")
            root = ElementTree.Element("managerAuthority", schemaVersion="1", worldId=str(world_id))
            for manager in managers:
                if isinstance(manager, dict) and manager.get("game_player_id") is not None:
                    ElementTree.SubElement(root, "manager", gamePlayerId=str(manager["game_player_id"]),
                                            farmId=str(manager.get("farm_id", 0)))
            for contractor in contractors:
                if isinstance(contractor, dict) and contractor.get("game_player_id") is not None:
                    ElementTree.SubElement(root, "contractor", gamePlayerId=str(contractor["game_player_id"]),
                                            farmId=str(contractor.get("farm_id", 0)))
            destination = self.directory / "manager-authority.xml"
            temporary = destination.with_suffix(".tmp")
            ElementTree.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
            _atomic_replace(temporary, destination)
            return True
        except (ElementTree.ParseError, ValueError, OSError, HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError):
            return False

    def process_snapshot_once(self):
        try:
            server_key, credential = self._binding()
            fs25_save_id = self._runtime_save_id()
            snapshot = self._snapshot_payload()
            body = json.dumps({"fs25_save_id": fs25_save_id, "snapshot": snapshot}).encode("utf-8")
            request = Request(self.snapshot_url, data=body,
                              headers={"Content-Type": "application/json", "X-SiN-Server-Key": server_key,
                                       "Authorization": "Bearer " + credential}, method="POST")
            with self.opener(request, timeout=10) as response:
                if response.status != 200:
                    try:
                        raw_body = response.read(4096)
                    except TypeError:
                        raw_body = response.read()
                    detail = self._safe_http_error_detail(raw_body)
                    LOG.warning("snapshot submission rejected status=%s detail=%s",
                                response.status, detail)
                    raise RuntimeError("snapshot API rejected request")
                response.read()
            return True
        except HTTPError as error:
            try:
                raw_body = error.read(4096)
            except (OSError, TypeError):
                raw_body = b""
            detail = self._safe_http_error_detail(raw_body)
            LOG.warning("snapshot submission rejected status=%s detail=%s",
                        getattr(error, "code", "unknown"), detail)
            return False
        except (ElementTree.ParseError, ValueError, OSError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError):
            return False

    def _write_clock_policy(self, policy):
        destination = self.directory / "clock-policy.xml"
        temporary = destination.with_suffix(".tmp")
        root = ElementTree.Element("clockPolicy", **{key: str(value).lower() if isinstance(value, bool) else str(value)
                                                       for key, value in policy.items()})
        ElementTree.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
        _atomic_replace(temporary, destination)

    def process_clock_once(self):
        try:
            server_key, credential = self._binding()
            policy = self._get_clock_policy(server_key, credential, self._runtime_save_id())
            required = {"enabled", "timezone", "target_game_minutes", "normal_time_scale", "ahead_time_scale",
                        "fast_catchup_threshold_minutes", "fast_catchup_time_scale",
                        "catchup_time_scale", "tolerance_minutes", "hard_resync_threshold_minutes",
                        "hard_resync_enabled", "check_interval_seconds", "save_key", "generated_at"}
            if not required.issubset(policy):
                raise ValueError("clock API returned an invalid policy")
            self._write_clock_policy(policy)
            LOG.info("clock policy refreshed for save=%s", policy["save_key"])
            return int(policy["check_interval_seconds"])
        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, ValueError, json.JSONDecodeError):
            LOG.warning("clock policy unavailable; retaining last policy")
            return 60

    def _write_response(self, server_key, credential):
        self.commands.mkdir(parents=True, exist_ok=True)
        destination = self.commands / "server-pairing-response.xml"
        temporary = destination.with_suffix(".tmp")
        root = ElementTree.Element("serverPairingResponse", serverKey=server_key, credential=credential)
        ElementTree.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
        _atomic_replace(temporary, destination)

    def pair_once(self, pairing_code):
        """Pair directly from the CLI and write the response consumed by Lua."""
        if not isinstance(pairing_code, str) or not pairing_code.strip():
            raise ValueError("pairing code is required")
        server_key, credential = self._pair(pairing_code.strip().upper())
        try:
            self._write_response(server_key, credential)
        except OSError as error:
            raise MailboxWriteError("pairing response mailbox write failed") from error
        return server_key

    def process_once(self):
        self.commands.mkdir(parents=True, exist_ok=True)
        processed = []
        for path in sorted(self.commands.glob("pairing-request-*.xml")):
            try:
                root = ElementTree.parse(path).getroot()
                pairing_code = root.get("code") if root.tag == "serverPairingRequest" else None
                if not isinstance(pairing_code, str) or not pairing_code.strip():
                    raise ValueError("missing pairing code")
                try:
                    server_key, credential = self._pair(pairing_code.strip().upper())
                except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError):
                    LOG.warning("pairing API unavailable or rejected request; will retry")
                    continue
                try:
                    self._write_response(server_key, credential)
                except OSError:
                    LOG.warning("pairing response mailbox write failed; will retry")
                    continue
                try:
                    path.unlink()
                except OSError:
                    LOG.warning("pairing request cleanup failed; will retry")
                    continue
                processed.append(path.name)
                LOG.info("pairing request completed; server binding response queued")
            except (ElementTree.ParseError, ValueError):
                self._quarantine(path)
                LOG.warning("malformed pairing request quarantined")
        return processed

    @staticmethod
    def _short_event_id(event_id):
        """Return a non-sensitive stable diagnostic label for an event ID."""
        digest = hashlib.sha256(str(event_id).encode("utf-8")).hexdigest()
        return digest[:12]

    @staticmethod
    def _event_sort_key(item):
        """Keep lifecycle order deterministic without relying on cwd or mtime.

        NetworkLocal emits minute observations before the matching disconnect,
        but filenames from different event families do not share a common
        lexical prefix.  Grouping by session and ordering connect -> minute ->
        disconnect keeps a disconnect summary from being finalized ahead of
        the minute files already present in the mailbox.
        """
        path, event = item
        payload = event.get("payload") or {}
        scope = (str(event.get("server_key", "")), str(event.get("save_id", "")),
                 str(payload.get("unique_user_id", "")), str(payload.get("session_id", "")))
        event_type = event.get("event_type")
        priority = {"player_connected": 0, "player_activity_minute": 1,
                    "player_disconnected": 2}.get(event_type, 1)
        try:
            minute = int(payload.get("minute_sequence", 0))
        except (TypeError, ValueError):
            minute = 0
        return (scope, priority, minute, path.name)

    @staticmethod
    def _event_minute_sequence(event):
        """Return a valid activity minute, or None for malformed metadata."""
        try:
            minute = int((event.get("payload") or {}).get("minute_sequence"))
        except (TypeError, ValueError):
            return None
        return minute if minute >= 1 else None

    @classmethod
    def _parse_event_file(cls, path):
        root = ElementTree.parse(path).getroot()
        required = {"event_id", "event_type", "server_key", "server_credential", "save_id"}
        if root.tag != "serverEvent" or not required.issubset(root.attrib):
            raise ValueError("invalid event XML")
        event = dict(root.attrib)
        event["payload"] = {key: value for key, value in event.items() if key not in required}
        if event["event_type"] not in {"heartbeat", "player_connected", "player_disconnected",
                                        "player_activity_minute", "chat_message", "map_geometry"}:
            raise ValueError("unsupported event type")
        if event["event_type"] == "map_geometry":
            event["payload"] = cls._parse_map_geometry(root)
        return event

    def _cached_event(self, path):
        stat = path.stat()
        cache_key = (stat.st_mtime_ns, stat.st_size)
        cached = self._event_cache.get(path)
        if cached and cached[0] == cache_key:
            return cached[1]
        event = self._parse_event_file(path)
        self._event_cache[path] = (cache_key, event)
        return event

    @staticmethod
    def _parse_map_geometry(root):
        """Convert the bounded nested XML map export into the API payload."""
        fields = {}
        for field in root.findall("./fields/field"):
            field_id = field.get("field_id")
            if field_id is None:
                raise ValueError("map field ID is required")
            points = []
            for point in field.findall("./points/point"):
                points.append([point.get("x"), point.get("z")])
            fields[str(field_id)] = {
                "field_id": field_id,
                "farmland_id": field.get("farmland_id"),
                "area_ha": field.get("area_ha"),
                "rings": [points],
            }
        farmlands = {}
        farmland_ids = []
        for farmland in root.findall("./farmlands/farmland"):
            farmland_id = farmland.get("farmland_id")
            if farmland_id is None:
                raise ValueError("map farmland ID is required")
            farmland_ids.append(farmland_id)
            points = [[point.get("x"), point.get("z")]
                      for point in farmland.findall("./points/point")]
            # FS25 exposes farmland ownership through its density map, not a
            # vector polygon API.  Preserve a polygon only when a trusted
            # exporter provides one; otherwise retain the farmland identity
            # and let consumers report geometry as unavailable.
            if len(points) >= 3:
                farmlands[str(farmland_id)] = {
                    "farmland_id": farmland_id,
                    "area_ha": farmland.get("area_ha"),
                    "rings": [points],
                }
        payload = {key: root.get(key) for key in (
            "map_id", "map_title", "world_width", "world_depth", "image_width",
            "image_height", "overview_asset_identity", "version", "image_y_inverted",
            "coordinate_system")}
        payload["schema_version"] = root.get("schema_version", "1")
        payload["image_y_inverted"] = str(payload.get("image_y_inverted", "false")).lower() == "true"
        payload["farmland_ids"] = farmland_ids
        payload["fields"] = fields
        payload["farmlands"] = farmlands
        return {"map": payload, "source_generation": root.get("source_generation")}

    def process_events_once(self, max_events=None):
        events = self.directory / "events"
        events.mkdir(parents=True, exist_ok=True)
        processed = []
        paths = sorted(events.glob("*.xml"))
        bounded = max_events is not None
        limit = len(paths) if max_events is None else max(0, int(max_events))
        # Parse only the work budget initially.  If that window contains a
        # watermarked disconnect, discover just enough of the mailbox to find
        # its session minutes. Cached headers keep retries from becoming a
        # repeated full XML parse; the hard cap prevents a hostile mailbox
        # from turning one watch pass into an unbounded parse.
        scan_paths = paths if not bounded else paths[:limit]
        pending = []
        for path in scan_paths:
            try:
                pending.append((path, self._cached_event(path)))
            except (ElementTree.ParseError, ValueError, OSError):
                self._quarantine(path)
                LOG.warning("malformed event quarantined")

        disconnects = [event for _, event in pending
                       if event.get("event_type") == "player_disconnected"
                       and (event.get("payload") or {}).get("final_minute_sequence") is not None]
        discovered = 0
        targets = set()
        for event in disconnects:
            payload = event.get("payload") or {}
            try:
                watermark = int(payload.get("final_minute_sequence"))
            except (TypeError, ValueError):
                continue
            targets.add((str(event.get("server_key", "")), str(event.get("save_id", "")),
                         str(payload.get("unique_user_id", "")), str(payload.get("session_id", "")),
                         watermark))
        if bounded and targets:
            for path in paths[limit:limit + self.MAX_EVENT_LOOKAHEAD]:
                try:
                    event = self._cached_event(path)
                except (ElementTree.ParseError, ValueError, OSError):
                    self._quarantine(path)
                    continue
                payload = event.get("payload") or {}
                candidate_key = (str(event.get("server_key", "")), str(event.get("save_id", "")),
                                 str(payload.get("unique_user_id", "")), str(payload.get("session_id", "")))
                minute = self._event_minute_sequence(event)
                if event.get("event_type") == "player_activity_minute" and minute is not None and any(
                        candidate_key == target[:4] and minute <= target[4] for target in targets):
                    pending.append((path, event))
                    discovered += 1

        pending.sort(key=self._event_sort_key)
        if max_events is None:
            max_events = len(pending)
        else:
            max_events = max(0, int(max_events))
        attempted = 0
        effective_max = len(pending) if max_events is None else max(0, int(max_events))
        selected = pending
        blocked_sessions = set()
        for path, event in selected:
            if attempted >= effective_max:
                break
            payload = event.get("payload") or {}
            ordering_key = (str(event.get("server_key", "")), str(event.get("save_id", "")),
                            str(payload.get("unique_user_id", "")), str(payload.get("session_id", "")))
            if ordering_key in blocked_sessions:
                continue
            attempted += 1
            try:
                self._post_event(event)
                path.unlink()
                processed.append(path.name)
            except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError, OSError) as error:
                status = getattr(error, "code", None)
                if status in {400, 401, 403, 404, 422}:
                    self._quarantine(path)
                    LOG.warning("event permanently rejected and quarantined type=%s id=%s",
                                event.get("event_type"), self._short_event_id(event.get("event_id")))
                else:
                    LOG.warning("event API unavailable; event retained for retry type=%s id=%s",
                                event.get("event_type"), self._short_event_id(event.get("event_id")))
                    # Preserve per-session ordering without starving other
                    # sessions whose events are eligible in this pass.
                    blocked_sessions.add(ordering_key)
                    continue
        remaining = len(list(events.glob("*.xml")))
        if attempted:
            types = sorted({event.get("event_type", "unknown") for _, event in pending})
            LOG.info("event batch complete count=%s removed=%s remaining=%s catch_up=%s types=%s",
                     attempted, len(processed), remaining, remaining > 0, ",".join(types))
        return processed

    def process_registration_once(self):
        self.registration_requests.mkdir(parents=True, exist_ok=True)
        processed = []
        try:
            server_key, credential = self._binding()
        except (ElementTree.ParseError, ValueError, OSError):
            return processed
        for path in sorted(self.registration_requests.glob("*.xml")):
            try:
                root = ElementTree.parse(path).getroot()
                required = {"request_id", "fs25_save_id", "fs25_unique_user_id"}
                if root.tag != "registrationRequest" or not required.issubset(root.attrib):
                    raise ValueError("invalid registration request XML")
                if any(not root.get(key, "").strip() for key in required) or any(char in root.get("request_id", "") for char in "/\\"):
                    raise ValueError("invalid registration request XML")
                request = {"fs25_save_id": root.get("fs25_save_id"),
                           "fs25_unique_user_id": root.get("fs25_unique_user_id"),
                           "observed_name": root.get("observed_name", ""),
                           "transient_user_id": root.get("transient_user_id", "")}
                result = self._post_registration(server_key, credential, request)
                if not isinstance(result, dict) or result.get("status") not in {"registered", "registration_required"}:
                    raise ValueError("registration API returned an invalid response")
                result["request_id"] = root.get("request_id")
                result.setdefault("fs25_unique_user_id", root.get("fs25_unique_user_id"))
                self._write_registration_response(result)
                path.unlink()
                processed.append(path.name)
                LOG.info("registration state delivered; local request removed")
            except (ElementTree.ParseError, ValueError):
                self._quarantine(path)
                LOG.warning("malformed registration request quarantined")
            except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as error:
                if getattr(error, "code", None) in {400, 401, 403, 404, 422}:
                    self._quarantine(path)
                    LOG.warning("registration request permanently rejected and quarantined")
                else:
                    LOG.warning("registration API unavailable; request retained for retry")
        return processed

    def watch(self, interval=2.0, stop=None):
        next_clock_refresh = 0
        next_snapshot = 0
        next_authority = 0
        while not stop or not stop():
            # Control-plane work is deliberately ahead of the event batch.
            # A historical event backlog must not delay pairing, registration,
            # operations, receipts, or current policy refreshes.
            self.process_once()
            self.process_registration_once()
            self.process_receipts_once()
            # Receipt polling precedes operation materialization: a command
            # acknowledged by the game must never be recreated in the window
            # between those two control-plane calls.
            self.process_operations_once()
            if time.monotonic() >= next_authority:
                self.process_manager_authority_once()
                next_authority = time.monotonic() + 20
            if time.monotonic() >= next_snapshot:
                self.process_snapshot_once()
                next_snapshot = time.monotonic() + 20
            if time.monotonic() >= next_clock_refresh:
                refresh_seconds = self.process_clock_once()
                next_clock_refresh = time.monotonic() + max(5, refresh_seconds)
            self.process_events_once(self.event_batch_size)
            time.sleep(max(0.1, interval))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pair", metavar="CODE", help="pair once using a one-time server pairing code")
    mode.add_argument("--watch", action="store_true", help="watch the mailbox for pairing requests")
    parser.add_argument("--backend-url", default=os.environ.get("SIN_BACKEND_URL"))
    parser.add_argument("--mailbox-dir", default=os.environ.get("SIN_MAILBOX_DIR"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("SIN_POLL_INTERVAL", "2")))
    parser.add_argument("--event-batch-size", type=int,
                        default=int(os.environ.get("SIN_EVENT_BATCH_SIZE", str(DEFAULT_EVENT_BATCH_SIZE))))
    args = parser.parse_args()
    if not args.backend_url or not args.mailbox_dir:
        parser.error("SIN_BACKEND_URL and SIN_MAILBOX_DIR are required")
    logging.basicConfig(level=logging.INFO)
    agent = PairingAgent(args.mailbox_dir, args.backend_url, event_batch_size=args.event_batch_size)
    try:
        if args.pair is not None:
            server_key = agent.pair_once(args.pair)
            print(f"Pairing succeeded for server_key={server_key}; response queued for FS25_SiN_Server")
            return 0
        if args.watch:
            agent.watch(args.interval)
        else:
            print(json.dumps({"processed": agent.process_once()}))
        return 0
    except (HTTPError, URLError, TimeoutError):
        if args.pair is not None:
            print("Pairing failed: central API is unreachable or rejected the request", file=__import__("sys").stderr)
        else:
            print("Agent mailbox processing failed: central API is unreachable or rejected the request", file=__import__("sys").stderr)
        return 1
    except MailboxWriteError as error:
        print(f"Agent mailbox write failed: {error}", file=__import__("sys").stderr)
        return 1
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
        prefix = "Pairing failed" if args.pair is not None else "Agent mailbox processing failed"
        print(f"{prefix}: {error}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
