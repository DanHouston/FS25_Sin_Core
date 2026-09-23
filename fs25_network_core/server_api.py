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
from .event_processing import (CentralEventProcessor, EventAuthenticationError,
                                EventRetryableError, EventScopeError, EventValidationError)
from .clock_policy import target_game_minutes
from .farm_lifecycle import FarmLifecycle

LOG = logging.getLogger(__name__)
PAIR_PATH = "/api/server/pair"
REGISTRATION_PATH = "/api/server/registration/request"
OPERATIONS_PATH = "/api/server/operations"
RECEIPTS_PATH = "/api/server/operation-receipts"
SNAPSHOT_PATH = "/api/server/snapshot"


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
    farm_lifecycle = None

    def log_message(self, format, *args):
        # Do not put request bodies, pairing codes, or credentials in access logs.
        LOG.info("central API request path=%s status=%s", self.path, args[1] if len(args) > 1 else "unknown")

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlsplit(self.path).path
        if path not in {"/api/server/clock", OPERATIONS_PATH, "/api/server/manager-authority"}:
            _json_response(self, 404, {"error": "not_found"})
            return
        query = parse_qs(urlsplit(self.path).query)
        fs25_save_id = query.get("fs25_save_id", [None])[0]
        world_id = query.get("world_id", [None])[0]
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
            if path == OPERATIONS_PATH:
                self.farm_lifecycle.require_current_world(record["server_key"], save_key, world_id)
                operations = self.farm_lifecycle.operations_for(record["server_key"], save_key, world_id)
                permission_jobs = list(self.event_processor.authorization.db.permission_jobs.find({
                    "server_id": record["server_key"], "save_id": save_key,
                    "world_id": str(world_id),
                    "state": "pending"}).sort("created_at", 1).limit(50))
                for job in permission_jobs:
                    operations.append({"operation_id": job["_id"], "operation_type": "permission",
                        "server_key": record["server_key"], "save_key": save_key, "payload": {
                            "game_player_id": job["game_player_id"], "farm_id": job["farm_id"],
                            "role": job["role"], "revision": job["revision"]}, "state": "pending"})
                safe = [{key: operation.get(key) for key in
                         ("operation_id", "operation_type", "server_key", "save_key", "payload", "state")}
                        for operation in operations]
                _json_response(self, 200, {"operations": safe, "save_key": save_key})
                return
            if path == "/api/server/manager-authority":
                self.farm_lifecycle.require_current_world(record["server_key"], save_key, world_id)
                relationships = self.event_processor.authorization.db.memberships.find({
                    "server_id": record["server_key"], "save_id": save_key,
                    "world_id": str(world_id),
                    # A pending manager assignment is already a persisted SiN
                    # authorization created only after the game confirmed the
                    # exact farm and starting farmland.  The game must receive
                    # that desired authority before it can produce the
                    # permission receipt that transitions the request active.
                    # Filtering only applied memberships creates a deadlock:
                    # empty authority XML prevents the game from applying the
                    # pending job in the first place.
                    "state": {"$in": ["pending", "active"]},
                    "desired_role": {"$in": ["farm_manager", "contractor"]}})
                managers = [row for row in relationships if row.get("desired_role") == "farm_manager"]
                contractors = [row for row in relationships if row.get("desired_role") == "contractor"]
                _json_response(self, 200, {"save_key": save_key, "managers": [
                    {"game_player_id": row["game_player_id"], "farm_id": row["farm_id"]} for row in managers],
                    "contractors": [{"game_player_id": row["game_player_id"], "farm_id": row["farm_id"]}
                                    for row in contractors]})
                return
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
        if self.path == SNAPSHOT_PATH:
            self.do_snapshot()
            return
        if self.path == RECEIPTS_PATH:
            self.do_receipt()
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

        # Pairing establishes trust first.  Resource bootstrap is deliberately
        # a durable follow-up and may remain pending while FS25 is offline.
        try:
            self.farm_lifecycle.ensure_for_server(server_key)
        except Exception:
            # Pairing is a trust/bootstrap boundary.  A resource-queue write
            # failure must not invalidate the already-created credential.
            LOG.exception("server resource bootstrap could not be queued")

        _json_response(self, 200, {"server_key": server_key, "credential": credential})

    def _authenticated_scope(self, payload=None):
        server_key = self.headers.get("X-SiN-Server-Key")
        credential = self.headers.get("Authorization", "")
        if credential.startswith("Bearer "):
            credential = credential[7:]
        if not server_key or not credential:
            raise PermissionError("missing server authentication")
        record = self.event_processor.registry.authenticate(server_key, credential)
        fs25_save_id = (payload or {}).get("fs25_save_id") or parse_qs(urlsplit(self.path).query).get("fs25_save_id", [None])[0]
        if fs25_save_id is None or str(fs25_save_id).strip() == "":
            raise ValueError("FS25 save scope is required")
        save_key = self.event_processor.registry.resolve_save(record["server_key"], fs25_save_id)
        return record, save_key

    def _read_json(self, maximum=1_000_000):
        length = int(self.headers.get("Content-Length", "-1"))
        if length < 0 or length > maximum:
            raise ValueError("invalid request size")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request must be an object")
        return payload

    def do_snapshot(self):
        try:
            payload = self._read_json()
            record, save_key = self._authenticated_scope(payload)
        except PermissionError:
            _json_response(self, 401, {"error": "invalid_server_authentication"})
            return
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            LOG.warning("snapshot malformed server=%s reason=%s",
                        self.headers.get("X-SiN-Server-Key", "<missing>"), str(error))
            _json_response(self, 400, {"error": "malformed_snapshot"})
            return
        try:
            snapshot = payload.get("snapshot")
            if not isinstance(snapshot, dict) or not snapshot.get("world_id"):
                raise ValueError("authoritative world generation is required")
            result = self.farm_lifecycle.record_snapshot(record["server_key"], save_key, snapshot)
        except ValueError as error:
            LOG.warning("snapshot rejected server=%s save=%s reason=%s",
                        record["server_key"], save_key, str(error))
            _json_response(self, 400, {"error": "invalid_snapshot"})
            return
        _json_response(self, 200, {"status": "accepted", "save_key": save_key,
                                   "received_at": result["received_at"].isoformat()})

    def do_receipt(self):
        try:
            payload = self._read_json(128_000)
            record, save_key = self._authenticated_scope(payload)
            receipt = payload.get("receipt")
            if not isinstance(receipt, dict):
                raise ValueError("operation receipt is required")
            world_id = payload.get("world_id")
            self.farm_lifecycle.require_current_world(record["server_key"], save_key, world_id)
            if str(receipt.get("world_id") or "") != str(world_id):
                raise ValueError("operation receipt world generation does not match runtime")
            if receipt.get("operation_type") == "assign_farmland":
                # Farmland assignment is a FarmLifecycle operation.  Its
                # receipt includes the FS25 owner read-back; do not route it
                # through the legacy land_operations compatibility path.
                result = self.farm_lifecycle.accept_receipt(record["server_key"], save_key, receipt, world_id)
            elif receipt.get("operation_type") in {"vehicle_transfer", "product_transfer"}:
                result = self.event_processor.transfers.accept_receipt(
                    receipt["transfer_id"], receipt, record["server_key"], save_key, world_id)
                result = {"operation_id": receipt["operation_id"], "state": result.get("status")}
            elif receipt.get("operation_type") == "chat_message":
                result = self.event_processor.chat.accept_receipt(
                    receipt["operation_id"], receipt.get("status"), receipt.get("receipt"),
                    record["server_key"], save_key, world_id)
                result = {"operation_id": receipt["operation_id"], "state": result}
            elif receipt.get("operation_type") == "withdraw_funds":
                result = self.event_processor.banking.settle_withdrawal(
                    receipt["withdrawal_id"], receipt.get("status"), receipt,
                    record["server_key"], save_key, world_id)
                result = {"operation_id": receipt["operation_id"], "state": result}
            elif receipt.get("operation_type") == "deposit_funds":
                result = self.event_processor.banking.settle_deposit(
                    receipt["deposit_id"], receipt.get("status"), receipt,
                    record["server_key"], save_key, world_id)
                result = {"operation_id": receipt["operation_id"], "state": result}
            elif receipt.get("revision") is not None and receipt.get("operation_type") not in {"ensure_farm", "provision_farm"}:
                result = self.event_processor.authorization.acknowledge(
                    receipt["operation_id"], record["server_key"], save_key,
                    int(receipt["revision"]), receipt.get("receipt"), world_id)
                result = {"operation_id": receipt["operation_id"], "state": result}
            else:
                result = self.farm_lifecycle.accept_receipt(record["server_key"], save_key, receipt, world_id)
        except PermissionError:
            _json_response(self, 401, {"error": "invalid_server_authentication"})
            return
        except (KeyError, TypeError, ValueError) as error:
            status = 404 if "unknown" in str(error).lower() or "save" in str(error).lower() else 400
            _json_response(self, status, {"error": "invalid_operation_receipt"})
            return
        _json_response(self, 200, {"status": "accepted", "operation_id": result["operation_id"],
                                   "operation_state": result.get("state")})

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
        except EventRetryableError:
            _json_response(self, 409, {"error": "event_waiting_for_prior_activity"})
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
    PairingRequestHandler.farm_lifecycle = FarmLifecycle(database, PairingRequestHandler.event_processor.authorization)
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
