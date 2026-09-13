"""Mongo-free per-server Agent for authenticated mailbox transport."""
import argparse
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


class PairingAgent:
    def __init__(self, mailbox_dir, backend_url, opener=None):
        self.directory = Path(mailbox_dir)
        self.commands = self.directory / "permission-commands"
        self.registration_requests = self.directory / "registration-requests"
        self.registration_responses = self.directory / "registration-responses"
        self.backend_root = backend_url.rstrip("/")
        self.backend_url = self.backend_root + "/api/server/pair"
        self.opener = opener or urlopen

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
                raise RuntimeError("event API rejected request")
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
        temporary.replace(destination)

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

    def _get_clock_policy(self, server_key, credential, fs25_save_id):
        url = self.backend_root + "/api/server/clock?fs25_save_id=" + quote(str(fs25_save_id), safe="")
        request = Request(url, headers={"X-SiN-Server-Key": server_key,
                                        "Authorization": "Bearer " + credential}, method="GET")
        with self.opener(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("clock API rejected request")
            return json.loads(response.read().decode("utf-8"))

    def _write_clock_policy(self, policy):
        destination = self.directory / "clock-policy.xml"
        temporary = destination.with_suffix(".tmp")
        root = ElementTree.Element("clockPolicy", **{key: str(value).lower() if isinstance(value, bool) else str(value)
                                                       for key, value in policy.items()})
        ElementTree.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
        temporary.replace(destination)

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
        temporary.replace(destination)

    def pair_once(self, pairing_code):
        """Pair directly from the CLI and write the response consumed by Lua."""
        if not isinstance(pairing_code, str) or not pairing_code.strip():
            raise ValueError("pairing code is required")
        server_key, credential = self._pair(pairing_code.strip().upper())
        self._write_response(server_key, credential)
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
                server_key, credential = self._pair(pairing_code.strip().upper())
                self._write_response(server_key, credential)
                path.unlink()
                processed.append(path.name)
                LOG.info("pairing request completed; server binding response queued")
            except (ElementTree.ParseError, ValueError):
                self._quarantine(path)
                LOG.warning("malformed pairing request quarantined")
            except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError, OSError):
                # Leave the request in place so a transient API/network failure
                # can be retried on the next poll.
                LOG.warning("pairing API unavailable or rejected request; will retry")
        return processed

    def process_events_once(self):
        events = self.directory / "events"
        events.mkdir(parents=True, exist_ok=True)
        processed = []
        for path in sorted(events.glob("*.xml")):
            try:
                root = ElementTree.parse(path).getroot()
                required = {"event_id", "event_type", "server_key", "server_credential", "save_id"}
                if root.tag != "serverEvent" or not required.issubset(root.attrib):
                    raise ValueError("invalid event XML")
                event = dict(root.attrib)
                event["payload"] = {key: value for key, value in event.items() if key not in required}
                if event["event_type"] not in {"heartbeat", "player_connected", "player_disconnected"}:
                    raise ValueError("unsupported event type")
                self._post_event(event)
                path.unlink()
                processed.append(path.name)
                LOG.info("event delivered; local event removed")
            except (ElementTree.ParseError, ValueError):
                self._quarantine(path)
                LOG.warning("malformed event quarantined")
            except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError, OSError) as error:
                status = getattr(error, "code", None)
                if status in {400, 401, 403, 404, 422}:
                    self._quarantine(path)
                    LOG.warning("event permanently rejected and quarantined")
                else:
                    LOG.warning("event API unavailable; event retained for retry")
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
        while not stop or not stop():
            self.process_once()
            self.process_events_once()
            self.process_registration_once()
            if time.monotonic() >= next_clock_refresh:
                refresh_seconds = self.process_clock_once()
                next_clock_refresh = time.monotonic() + max(5, refresh_seconds)
            time.sleep(max(0.1, interval))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pair", metavar="CODE", help="pair once using a one-time server pairing code")
    mode.add_argument("--watch", action="store_true", help="watch the mailbox for pairing requests")
    parser.add_argument("--backend-url", default=os.environ.get("SIN_BACKEND_URL"))
    parser.add_argument("--mailbox-dir", default=os.environ.get("SIN_MAILBOX_DIR"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("SIN_POLL_INTERVAL", "2")))
    args = parser.parse_args()
    if not args.backend_url or not args.mailbox_dir:
        parser.error("SIN_BACKEND_URL and SIN_MAILBOX_DIR are required")
    logging.basicConfig(level=logging.INFO)
    agent = PairingAgent(args.mailbox_dir, args.backend_url)
    try:
        if args.pair is not None:
            server_key = agent.pair_once(args.pair)
            print(f"Pairing succeeded for server_key={server_key}; response queued for NetworkLocal")
            return 0
        if args.watch:
            agent.watch(args.interval)
        else:
            print(json.dumps({"processed": agent.process_once()}))
        return 0
    except (HTTPError, URLError, TimeoutError):
        print("Pairing failed: central API is unreachable or rejected the request", file=__import__("sys").stderr)
        return 1
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"Pairing failed: {error}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
