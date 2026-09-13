"""Small central HTTP API for bootstrap server pairing.

This process owns the database connection.  The game-server Agent must never
import this module or connect to MongoDB directly.
"""
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import load_local_environment
from .database import Database
from .server_registry import ServerRegistry
from .event_processing import CentralEventProcessor, EventAuthenticationError, EventScopeError, EventValidationError
from .clock_policy import target_game_minutes

LOG = logging.getLogger(__name__)
PAIR_PATH = "/api/server/pair"
REGISTRATION_PATH = "/api/server/registration/request"


def _json_response(handler, status, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class PairingRequestHandler(BaseHTTPRequestHandler):
    """HTTP boundary for the one-time pairing bootstrap secret."""

    registry = None
    event_processor = None

    def log_message(self, format, *args):
        # Do not put request bodies, pairing codes, or credentials in access logs.
        LOG.info("central API request path=%s status=%s", self.path, args[1] if len(args) > 1 else "unknown")

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        if urlsplit(self.path).path != "/api/server/clock":
            _json_response(self, 404, {"error": "not_found"})
            return
        query = parse_qs(urlsplit(self.path).query)
        fs25_save_id = query.get("fs25_save_id", [None])[0]
        server_key = self.headers.get("X-SiN-Server-Key")
        credential = self.headers.get("Authorization", "")
        if credential.startswith("Bearer "):
            credential = credential[7:]
        if not server_key or not credential or not fs25_save_id:
            _json_response(self, 400, {"error": "missing_clock_scope_or_authentication"})
            return
        try:
            record = self.event_processor.registry.authenticate(server_key, credential)
        except ValueError:
            _json_response(self, 401, {"error": "invalid_server_authentication"})
            return
        try:
            save_key = self.event_processor.registry.resolve_save(record["server_key"], fs25_save_id)
            policy = self.event_processor.registry.clock_policy(record["server_key"], save_key)
            local = datetime.now(timezone.utc).astimezone(ZoneInfo(policy["timezone"]))
            target_local = local + timedelta(minutes=policy["offset_minutes"])
            response = dict(policy, save_key=save_key, target_game_minutes=target_game_minutes(policy),
                            target_local_date=target_local.date().isoformat(), target_local_time=target_local.strftime("%H:%M"),
                            generated_at=datetime.now(timezone.utc).isoformat())
        except ValueError:
            _json_response(self, 404, {"error": "unknown_or_unconfigured_save"})
            return
        _json_response(self, 200, response)

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/api/server/events":
            self.do_events()
            return
        if self.path == REGISTRATION_PATH:
            self.do_registration()
            return
        if self.path != PAIR_PATH:
            _json_response(self, 404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 16_384:
                raise ValueError("invalid request size")
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            pairing_code = payload.get("pairing_code") if isinstance(payload, dict) else None
            if not isinstance(pairing_code, str) or not pairing_code.strip():
                raise ValueError("pairing_code is required")
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError, AttributeError):
            _json_response(self, 400, {"error": "malformed_request"})
            return

        try:
            server_key, credential = self.registry.pair_code(pairing_code)
        except ValueError:
            # Deliberately do not distinguish invalid, expired, and already-used
            # codes: all are bootstrap authentication failures.
            _json_response(self, 401, {"error": "invalid_pairing_code"})
            return
        except Exception:
            LOG.exception("central API pairing failure")
            _json_response(self, 500, {"error": "internal_error"})
            return

        _json_response(self, 200, {"server_key": server_key, "credential": credential})

    def do_registration(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 16_384:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
            save_id = str(payload.get("fs25_save_id", "")).strip()
            unique_id = str(payload.get("fs25_unique_user_id", "")).strip()
            if not save_id or not unique_id:
                raise ValueError("registration scope and identity are required")
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError, AttributeError):
            _json_response(self, 400, {"error": "malformed_registration_request"})
            return
        server_key = self.headers.get("X-SiN-Server-Key")
        credential = self.headers.get("Authorization", "")
        if credential.startswith("Bearer "):
            credential = credential[7:]
        if not server_key or not credential:
            _json_response(self, 400, {"error": "missing_registration_authentication"})
            return
        try:
            record = self.event_processor.registry.authenticate(server_key, credential)
        except ValueError:
            _json_response(self, 401, {"error": "invalid_server_authentication"})
            return
        try:
            save_key = self.event_processor.registry.resolve_save(record["server_key"], save_id)
            result = self.event_processor.authorization.registration_request(
                record["server_key"], save_key, unique_id,
                payload.get("observed_name"), payload.get("transient_user_id"))
        except ValueError as error:
            message = str(error)
            status = 404 if "save" in message.lower() else 400
            _json_response(self, status, {"error": "invalid_registration_request"})
            return
        result["save_key"] = save_key
        _json_response(self, 200, result)

    def do_events(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 1_000_000:
                raise ValueError("invalid request size")
            event = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            _json_response(self, 400, {"error": "malformed_event"})
            return
        # New Agents send the credential through the authenticated header so
        # it is not duplicated in event JSON. Accept the older body field
        # during rollout for already-deployed Agents.
        header_server_key = self.headers.get("X-SiN-Server-Key")
        header_credential = self.headers.get("Authorization", "")
        if header_credential.startswith("Bearer "):
            header_credential = header_credential[7:]
        if header_server_key or header_credential:
            if not isinstance(event, dict) or event.get("server_key") != header_server_key or not header_credential:
                _json_response(self, 401, {"error": "invalid_server_authentication"})
                return
            event["server_credential"] = header_credential
        try:
            result = self.event_processor.process(event)
        except EventAuthenticationError:
            _json_response(self, 401, {"error": "invalid_server_authentication"})
            return
        except EventScopeError:
            _json_response(self, 404, {"error": "unknown_or_unconfigured_save"})
            return
        except EventValidationError:
            _json_response(self, 400, {"error": "invalid_event"})
            return
        except Exception:
            LOG.exception("central API event processing failure")
            _json_response(self, 500, {"error": "internal_error"})
            return
        _json_response(self, 200, result)


def make_server(database, host="127.0.0.1", port=8787):
    PairingRequestHandler.registry = ServerRegistry(database)
    PairingRequestHandler.event_processor = CentralEventProcessor(database)
    return ThreadingHTTPServer((host, int(port)), PairingRequestHandler)


def main():
    logging.basicConfig(level=logging.INFO)
    load_local_environment()
    database = Database()
    database.initialize()
    host = os.environ.get("SIN_API_HOST", "127.0.0.1")
    port = int(os.environ.get("SIN_API_PORT", "8787"))
    server = make_server(database, host, port)
    LOG.info("central pairing API listening host=%s port=%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
