"""Reusable, deterministic integration scenarios for the SiN boundaries.

Scenarios in this module are deliberately offline.  They exercise the same
mailbox and XML contracts used by the Agent, while keeping credentials and
machine-specific paths out of their reports.  The registry is intentionally
small and dependency-free so projects embedding the core can add scenarios
without copying the campaign runner.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping
from urllib.parse import urlsplit
from xml.etree import ElementTree

from discord import app_commands

from .agent import PairingAgent
from .local_test import build_mod, simulate
from .protocol_harness import MailboxOperationHarness, FarmlandOwnershipReferenceExecutor
from .farm_lifecycle import FarmLifecycle
from .authorization import AuthorizationManager
from .business_workflows import ContractService
from .admin_manager import AdminManager
from .map_service import MapService
from .bot_frontend import NetworkBot
from .event_processing import (CentralEventProcessor, EventRetryableError,
                               EventValidationError)
from .activity import ActivityPublisher
from pymongo.errors import DuplicateKeyError


class _Response:
    def __init__(self, payload=None, status=200):
        self.status = status
        self.headers = {}
        self.payload = payload or {"status": "accepted"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _CaptureChannel:
    """Small Discord I/O boundary used by the composed scenarios.

    Production publishers still build the message, view, and ``discord.File``;
    this object only captures the final channel.send call deterministically.
    """

    class _Guild:
        me = object()

    class _Permissions:
        view_channel = True
        send_messages = True

    def __init__(self, channel_id=None):
        self.guild = self._Guild()
        self.channel_id = channel_id
        self.messages = []
        self.deliveries = []
        self._next_message_id = 1

    def permissions_for(self, _member):
        return self._Permissions()

    async def send(self, message=None, **kwargs):
        content = message if message is not None else kwargs.get("content")
        self.messages.append(content)
        self.deliveries.append({"content": content, **kwargs})
        result = type("CapturedMessage", (), {})()
        result.id = self._next_message_id
        self._next_message_id += 1
        return result


class _CaptureBot:
    """Minimal bot lookup surface consumed by ActivityPublisher.publish()."""

    user = object()
    guilds = []

    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _channel_id):
        return self.channel


class _CaptureBank:
    """Only the database attribute required by NetworkBot's service graph."""

    def __init__(self, database):
        self.database = database


class _CommandResponse:
    """Deterministic interaction response boundary for a real command callback."""

    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    def is_done(self):
        return bool(self.messages)


class _CommandInteraction:
    """Only the production contract command's interaction fields."""

    def __init__(self, discord_id="contract-user", guild_id=1, channel_id=321):
        self.user = type("ScenarioUser", (), {"id": discord_id})()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = _CommandResponse()


class _FailingMapService(MapService):
    """Real presentation fallback seam; it fails only map rendering."""

    def __init__(self):
        super().__init__()
        self.render_attempts = 0

    def render_contract_map(self, *args, **kwargs):
        self.render_attempts += 1
        raise RuntimeError("deterministic map renderer unavailable")


class _CountingMapService(MapService):
    """Real MapService with observation-only counters for scenario evidence."""

    def __init__(self):
        super().__init__()
        self.payload_loads = 0
        self.render_requests = []

    def register_payload(self, *args, **kwargs):
        result = super().register_payload(*args, **kwargs)
        self.payload_loads += 1
        return result

    def render_map(self, server_key, save_key, **kwargs):
        result = super().render_map(server_key, save_key, **kwargs)
        self.render_requests.append({
            "server_key": str(server_key),
            "save_key": str(save_key),
            "highlight_fields": [int(value) for value in (kwargs.get("highlight_fields") or [])],
            "highlight_farmlands": [int(value) for value in (kwargs.get("highlight_farmlands") or [])],
        })
        return result


def _inspect_png(data):
    """Validate the generated attachment's PNG framing without PIL."""
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        return None
    offset = len(signature)
    dimensions = None
    seen_idat = False
    seen_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            return None
        chunk_type = data[offset + 4:offset + 8]
        chunk_data = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length:end])[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xffffffff
        if expected_crc != actual_crc:
            return None
        if chunk_type == b"IHDR":
            if length != 13:
                return None
            dimensions = list(struct.unpack(">II", chunk_data[:8]))
        elif chunk_type == b"IDAT":
            seen_idat = True
        elif chunk_type == b"IEND":
            if length != 0:
                return None
            seen_iend = True
            return {"dimensions": dimensions, "has_idat": seen_idat,
                    "has_iend": True, "valid": dimensions is not None and seen_idat}
        offset = end
    return None


class OfflineCentral:
    """Minimal deterministic central API transport used by built-in scenarios."""

    def __init__(self):
        self.receipts = []
        self.events = []

    def __call__(self, request, timeout=10):
        path = urlsplit(request.full_url).path
        if path.endswith("/api/server/operations"):
            return _Response({"operations": [{
                "operation_id": "campaign-operation-1",
                "operation_type": "assign_farmland",
                "payload": {"farmland_id": 22, "farm_id": 2, "owner_farm_id": 2},
                "state": "pending",
            }]})
        if path.endswith("/api/server/operation-receipts"):
            self.receipts.append(json.loads(request.data.decode("utf-8")))
            return _Response()
        if path.endswith("/api/server/events"):
            self.events.append(json.loads(request.data.decode("utf-8")))
            return _Response()
        raise AssertionError(f"unexpected offline endpoint: {path}")


class _FarmlandLifecycleCentral:
    """Authenticated HTTP-shaped boundary backed by the real lifecycle.

    The paired reference executor below is deterministic transport evidence,
    not evidence of a GIANTS API invocation.
    """

    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.receipt_calls = 0

    def __call__(self, request, timeout=10):
        del timeout
        if request.headers.get("X-sin-server-key") != "sin-campaign" \
                or request.headers.get("Authorization") != "Bearer campaign-secret":
            raise AssertionError("farmland scenario request was not authenticated")
        path = urlsplit(request.full_url).path
        if path.endswith("/api/server/operations"):
            operations = self.lifecycle.operations_for("sin-campaign", "campaign-save")
            return _Response({"operations": [{key: operation.get(key) for key in (
                "operation_id", "operation_type", "server_key", "save_key", "payload", "state")}
                for operation in operations]})
        if path.endswith("/api/server/operation-receipts"):
            payload = json.loads(request.data.decode("utf-8"))
            self.receipt_calls += 1
            operation = self.lifecycle.accept_receipt("sin-campaign", "campaign-save", payload["receipt"])
            return _Response({"status": "accepted", "operation_id": operation["operation_id"],
                              "state": operation["state"]})
        raise AssertionError(f"unexpected farmland scenario endpoint: {path}")


class BoundaryCentral(OfflineCentral):
    """Shared recording transport for scenario adapters.

    It implements the HTTP boundary only; scenarios can inspect normalized
    requests without ever exposing the credential in a report.
    """

    def __init__(self, registration=False, credentials=None):
        super().__init__()
        self.registration = registration
        self.registrations = []
        self.calls = []
        self.credentials = dict(credentials or {"sin-campaign": "campaign-secret"})

    def __call__(self, request, timeout=10):
        path = urlsplit(request.full_url).path
        if not path.endswith("/api/server/pair"):
            server_key = request.headers.get("X-sin-server-key")
            if self.credentials.get(server_key) != request.headers.get("Authorization", "").removeprefix("Bearer "):
                raise AssertionError("boundary request was not authenticated")
            payload = json.loads(request.data.decode("utf-8")) if request.data else {}
            self.calls.append({"path": path, "payload": payload})
        if path.endswith("/api/server/pair"):
            self.calls.append({"path": path, "payload": json.loads(request.data.decode("utf-8"))})
            return _Response({"server_key": "semantic-server", "credential": "semantic-secret"})
        if path.endswith("/api/server/registration/request"):
            payload = json.loads(request.data.decode("utf-8"))
            self.registrations.append(payload)
            return _Response({"status": "registration_required", "code": "ABCD1234",
                              "expires_at": "2030-01-01T00:00:00+00:00",
                              "fs25_unique_user_id": payload["fs25_unique_user_id"]})
        if path.endswith("/api/server/manager-authority"):
            return _Response({"managers": [{"game_player_id": "stable-player", "farm_id": 2}]})
        if path.endswith("/api/server/snapshot"):
            return _Response()
        if path.endswith("/api/server/clock"):
            return _Response({"enabled": False, "timezone": "UTC", "target_game_minutes": 0,
                "normal_time_scale": 1, "ahead_time_scale": 1,
                "fast_catchup_threshold_minutes": 0, "fast_catchup_time_scale": 1,
                "catchup_time_scale": 1, "tolerance_minutes": 1,
                "hard_resync_threshold_minutes": 5, "hard_resync_enabled": False,
                "check_interval_seconds": 60, "save_key": "campaign-save",
                "generated_at": "2030-01-01T00:00:00+00:00"})
        return super().__call__(request, timeout)


class ProcessorCentral(BoundaryCentral):
    """HTTP boundary that hands authenticated event bodies to central code."""

    def __init__(self, processor):
        super().__init__()
        self.processor = processor
        self.results = []
        self.transport_outcomes = []

    def __call__(self, request, timeout=10):
        path = urlsplit(request.full_url).path
        if path.endswith("/api/server/events"):
            server_key = request.headers.get("X-sin-server-key")
            credential = self.credentials.get(server_key)
            if credential is None or request.headers.get("Authorization") != "Bearer " + credential:
                raise AssertionError("central event boundary received an unauthenticated request")
            event = json.loads(request.data.decode("utf-8"))
            event["server_credential"] = credential
            self.calls.append({"path": path, "payload": event})
            self.events.append(event)
            try:
                result = self.processor.process(event)
            except EventRetryableError as error:
                # The real Agent treats 409 as retryable and keeps the
                # mailbox entry.  This is the transport boundary, not a
                # shortcut around Central's watermark policy.
                outcome = {"status": "retryable", "error": str(error)}
                self.transport_outcomes.append(outcome)
                return _Response(outcome, status=409)
            except EventValidationError as error:
                # The real HTTP API turns central validation failures into a
                # permanent 422 response.  Returning that status here keeps
                # the Agent's real quarantine path in the composed scenario.
                outcome = {"status": "rejected", "error": str(error)}
                self.transport_outcomes.append(outcome)
                return _Response(outcome, status=422)
            self.results.append(result)
            self.transport_outcomes.append(result)
            return _Response(result)
        return super().__call__(request, timeout)


# Public adapter contract used by the scenario registry and release manifest.
# Keeping these identifiers stable prevents a report from claiming a scenario
# passed while silently swapping its boundary implementation.
ADAPTER_CONTRACTS = (
    {"id": "mailbox_xml", "component": "PairingAgent", "boundary": "FS25 mailbox XML"},
    {"id": "central_http", "component": "BoundaryCentral", "boundary": "authenticated HTTP transport"},
    {"id": "central_processor", "component": "CentralEventProcessor", "boundary": "central event acceptance"},
    {"id": "memory_persistence", "component": "_MemoryDatabase", "boundary": "durable service state"},
    {"id": "activity_publisher", "component": "ActivityPublisher", "boundary": "durable Discord outbox publisher"},
    {"id": "map_service", "component": "MapService", "boundary": "validated map/render contract"},
    {"id": "networkbot_presentation", "component": "NetworkBot", "boundary": "contract card and Discord I/O"},
)

AUTHORITATIVE_BOUNDARIES = {
    "registration": ("mailbox_xml", "central_http"),
    "control_plane_backlog": ("mailbox_xml", "central_http"),
    "activity_disconnect": ("mailbox_xml", "central_http", "central_processor", "memory_persistence", "activity_publisher"),
    "map_contract": ("mailbox_xml", "central_http", "central_processor", "memory_persistence",
                     "map_service", "networkbot_presentation"),
    "contract_scope": ("memory_persistence", "networkbot_presentation"),
    "authority_regression": ("memory_persistence",),
}


class MailboxBoundaryAdapter:
    """Create deterministic binding, snapshot, event, and command fixtures."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def bind(self, server_key="sin-campaign", credential="campaign-secret", save_id="1"):
        _write_binding_and_snapshot(self.root, server_key, credential, save_id)

    def registration_request(self):
        directory = self.root / "registration-requests"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "registration-1.xml").write_text(
            '<registrationRequest request_id="registration-1" fs25_save_id="1" '
            'fs25_unique_user_id="stable-player" observed_name="Campaign Player" '
            'transient_user_id="7"/>', encoding="utf-8")

    def map_event(self, server_key="sin-campaign", credential="campaign-secret", save_id="1"):
        directory = self.root / "events"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "map.xml").write_text(
            '<serverEvent event_id="map-1" event_type="map_geometry" '
            f'server_key="{server_key}" server_credential="{credential}" save_id="{save_id}" '
            'map_id="campaign-map" map_title="Campaign Map" world_width="2048" '
            'world_depth="2048" image_width="256" image_height="256" '
            'overview_asset_identity="campaign-overview" version="1" '
            'image_y_inverted="false"><fields><field field_id="22" farmland_id="22" '
            'area_ha="12.5"><points><point x="0" z="0"/><point x="10" z="0"/>'
            '<point x="10" z="10"/></points></field></fields></serverEvent>',
            encoding="utf-8")

    def map_contract_event(self, event_id="map-contract-1", changed=False, invalid=False,
                           server_key="sin-campaign", credential="campaign-secret", save_id="1"):
        """Write a NetworkLocal-compatible two-field geometry fixture.

        Field 22 is deliberately simple enough to make the requested-field
        overlay deterministic.  Field 47 is a concave, irregular polygon so
        the scenario proves that downstream loading is not accidentally only
        exercising a triangle.  ``changed`` moves one point while preserving
        valid geometry; ``invalid`` provides a genuinely malformed field for
        the central validation/quarantine path.
        """
        directory = self.root / "events"
        directory.mkdir(parents=True, exist_ok=True)
        field_22 = [("-700", "-650"), ("-400", "-650"), ("-350", "-350"),
                    ("-600", "-300"), ("-760", "-470")]
        field_47 = [("250", "-650"), ("620", "-650"), ("760", "-470"),
                    ("610", "-240"), ("430", "-320"), ("300", "-180"),
                    ("180", "-420")]
        if server_key != "sin-campaign":
            # Keep IDs overlapping while making the independent server
            # projection observably different.
            field_22 = [(str(int(x) + 100), z) for x, z in field_22]
            field_47 = [(str(int(x) + 100), z) for x, z in field_47]
        if changed:
            field_47[-2] = ("300", "-130")
        field_22_points = "".join(f'<point x="{x}" z="{z}"/>' for x, z in field_22)
        field_47_points = "".join(f'<point x="{x}" z="{z}"/>' for x, z in field_47)
        if invalid:
            field_47_points = '<point x="250" z="-650"/><point x="620" z="-650"/>'
        path = directory / f"{event_id}.xml"
        farmland_22 = '<farmland farmland_id="22" area_ha="40"><points>' \
                      '<point x="-800" z="-750"/><point x="-300" z="-750"/>' \
                      '<point x="-300" z="-200"/><point x="-800" z="-200"/>' \
                      '</points></farmland>'
        farmland_47 = '<farmland farmland_id="47" area_ha="55"><points>' \
                      '<point x="100" z="-750"/><point x="850" z="-750"/>' \
                      '<point x="850" z="-100"/><point x="100" z="-100"/>' \
                      '</points></farmland>'
        farmland_xml = farmland_22 + farmland_47
        source_generation = 2 if changed else 1
        path.write_text(
            f'<serverEvent event_id="{event_id}" event_type="map_geometry" '
            f'server_key="{server_key}" server_credential="{credential}" save_id="{save_id}" '
            f'source_generation="{source_generation}" '
            'map_id="campaign-map" map_title="Campaign Map" world_width="2048" '
            'world_depth="2048" image_width="128" image_height="128" '
            'overview_asset_identity="campaign-overview" version="1" '
            'image_y_inverted="false" coordinate_system="giants-centered-xz"><fields>'
            f'<field field_id="22" farmland_id="22" area_ha="12.5"><points>{field_22_points}</points></field>'
            f'<field field_id="47" farmland_id="47" area_ha="18.0"><points>{field_47_points}</points></field>'
            f'</fields><farmlands>{farmland_xml}</farmlands></serverEvent>', encoding="utf-8")
        return path

    def activity_connect(self, event_id="connect-1"):
        directory = self.root / "events"
        directory.mkdir(parents=True, exist_ok=True)
        common = ('server_key="sin-campaign" server_credential="campaign-secret" '
                  'save_id="1" unique_user_id="stable-player" user_id="7" farm_id="2" '
                  'display_name="Campaign Player" session_id="session-1"')
        (directory / f"{event_id}.xml").write_text(
            f'<serverEvent event_id="{event_id}" event_type="player_connected" {common}/>',
            encoding="utf-8")

    def activity_minute(self, sequence, bucket, inactive_minutes, event_id=None):
        directory = self.root / "events"
        directory.mkdir(parents=True, exist_ok=True)
        event_id = event_id or f"minute-{int(sequence)}"
        common = ('server_key="sin-campaign" server_credential="campaign-secret" '
                  'save_id="1" unique_user_id="stable-player" user_id="7" farm_id="2" '
                  'display_name="Campaign Player" session_id="session-1"')
        (directory / f"{event_id}.xml").write_text(
            f'<serverEvent event_id="{event_id}" event_type="player_activity_minute" {common} '
            f'minute_sequence="{int(sequence)}" activity_bucket="{bucket}" '
            f'inactive_minutes="{int(inactive_minutes)}"/>', encoding="utf-8")

    def activity_disconnect(self, watermark=1, event_id="disconnect-1", filename=None):
        directory = self.root / "events"
        directory.mkdir(parents=True, exist_ok=True)
        common = ('server_key="sin-campaign" server_credential="campaign-secret" '
                  'save_id="1" unique_user_id="stable-player" user_id="7" farm_id="2" '
                  'display_name="Campaign Player" session_id="session-1"')
        filename = filename or ("disconnect.xml" if event_id == "disconnect-1" else f"{event_id}.xml")
        (directory / filename).write_text(
            f'<serverEvent event_id="{event_id}" event_type="player_disconnected" {common} '
            f'final_minute_sequence="{int(watermark)}"/>', encoding="utf-8")

    def heartbeat_event(self):
        directory = self.root / "events"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "backlog.xml").write_text(
            '<serverEvent event_id="backlog-1" event_type="heartbeat" '
            'server_key="sin-campaign" server_credential="campaign-secret" save_id="1"/>',
            encoding="utf-8")

    def heartbeat_backlog(self, count):
        directory = self.root / "events"
        directory.mkdir(parents=True, exist_ok=True)
        template = ('<serverEvent event_id="backlog-{0:05d}" event_type="heartbeat" '
                    'server_key="sin-campaign" server_credential="campaign-secret" save_id="1"/>')
        fixtures = []
        for group in range(max(1, (int(count) + 999) // 1000)):
            fixture = self.root / f"heartbeat-fixture-{group:02d}.xml"
            fixture.write_text(template.format(group + 1), encoding="utf-8")
            fixtures.append(fixture)
        for index in range(1, int(count) + 1):
            # Hard links make a large durable mailbox fixture practical on
            # Windows CI while preserving one independent mailbox entry per
            # event.  The transport still parses and forwards every entry.
            (directory / f"backlog-{index:05d}.xml").hardlink_to(fixtures[(index - 1) // 1000])

    def pairing_request(self):
        directory = self.root / "permission-commands"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "pairing-request-1.xml").write_text(
            '<serverPairingRequest code="PAIR1234"/>', encoding="utf-8")


class _MemoryResult:
    def __init__(self, modified_count=1):
        self.modified_count = modified_count


class _MemoryCursor(list):
    def limit(self, count):
        return _MemoryCursor(self[:int(count)])

    def sort(self, key, direction=1):
        return _MemoryCursor(sorted(self, key=lambda row: row.get(key), reverse=direction < 0))


class _MemoryCollection:
    """Tiny deterministic persistence boundary for service compositions."""

    def __init__(self):
        self.rows = []

    def insert_one(self, document, **_kwargs):
        if "_id" in document and any(row.get("_id") == document["_id"] for row in self.rows):
            raise DuplicateKeyError("duplicate _id")
        self.rows.append(dict(document))

    def delete_one(self, query, **_kwargs):
        index = next((index for index, row in enumerate(self.rows)
                      if self._matches(row, query)), None)
        if index is None:
            return _MemoryResult(0)
        self.rows.pop(index)
        return _MemoryResult(1)

    @staticmethod
    def _matches(row, query):
        for key, expected in query.items():
            actual = row.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$exists" in expected and ((key in row) != bool(expected["$exists"])):
                    return False
                if "$gte" in expected and (actual is None or actual < expected["$gte"]):
                    return False
                if "$lte" in expected and (actual is None or actual > expected["$lte"]):
                    return False
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
            elif actual != expected:
                return False
        return True

    def find_one(self, query, **_kwargs):
        return next((dict(row) for row in self.rows if self._matches(row, query)), None)

    def find(self, query=None, **_kwargs):
        query = query or {}
        return _MemoryCursor(dict(row) for row in self.rows if self._matches(row, query))

    def replace_one(self, query, replacement, **_kwargs):
        index = next((index for index, row in enumerate(self.rows)
                      if self._matches(row, query)), None)
        if index is None:
            if not _kwargs.get("upsert"):
                return _MemoryResult(0)
            self.rows.append(dict(replacement))
            return _MemoryResult(1)
        self.rows[index] = dict(replacement)
        return _MemoryResult(1)

    def update_one(self, query, update, **_kwargs):
        index = next((index for index, row in enumerate(self.rows)
                      if self._matches(row, query)), None)
        inserted = index is None
        if inserted and not _kwargs.get("upsert"):
            return _MemoryResult(0)
        if inserted:
            index = len(self.rows)
            self.rows.append({key: value for key, value in query.items() if not isinstance(value, dict)})
        target = self.rows[index]
        target.update(update.get("$setOnInsert", {}) if inserted else {})
        target.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            target[key] = target.get(key, 0) + value
        return _MemoryResult(1)

    def update_many(self, query, update, **_kwargs):
        matched = 0
        for row in self.rows:
            if self._matches(row, query):
                row.update(update.get("$set", {}))
                for key, value in update.get("$inc", {}).items():
                    row[key] = row.get(key, 0) + value
                matched += 1
        return _MemoryResult(matched)


class _MemoryDatabase:
    def __init__(self):
        self.db = _MemoryDatabaseCollections()

    def atomic(self, callback):
        return callback("memory-session")


class _MemoryDatabaseCollections:
    def __init__(self):
        self._collections = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._collections.setdefault(name, _MemoryCollection())


def _write_binding_and_snapshot(root: Path, server_key="sin-campaign",
                                credential="campaign-secret", save_id="1") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "serverBinding.xml").write_text(
        f'<serverBinding serverKey="{server_key}" credential="{credential}"/>',
        encoding="utf-8")
    (root / "snapshot.xml").write_text(
        f'<networkLocal source="game" session="campaign-session" sequence="1" '
        f'savegameIndex="{save_id}" worldId="campaign-world-a"><farms><farm farmId="2" name="Campaign Farm"/>'
        '</farms><players><player uniqueId="stable-player" userId="7" '
        'name="Campaign Player" farmId="2" connected="true"/></players></networkLocal>',
        encoding="utf-8")


@dataclass(frozen=True)
class NamedScenario:
    """A named campaign scenario and its deterministic executor."""

    name: str
    description: str
    execute: Callable[[Path], Mapping[str, object]]


class ScenarioRegistry:
    """Ordered registry for named scenarios.

    Registration rejects replacement and invalid names so a campaign cannot
    silently change meaning due to import order.
    """

    def __init__(self):
        self._scenarios: Dict[str, NamedScenario] = {}

    def register(self, scenario: NamedScenario) -> NamedScenario:
        if not scenario.name or scenario.name.strip() != scenario.name:
            raise ValueError("scenario name must be non-empty and trimmed")
        if scenario.name in self._scenarios:
            raise ValueError(f"scenario already registered: {scenario.name}")
        self._scenarios[scenario.name] = scenario
        return scenario

    def get(self, name: str) -> NamedScenario:
        try:
            return self._scenarios[name]
        except KeyError:
            raise ValueError(f"unknown scenario: {name}") from None

    def names(self):
        return tuple(self._scenarios)

    def describe(self):
        return {name: scenario.description for name, scenario in self._scenarios.items()}


def _full_scenario(root: Path) -> Mapping[str, object]:
    artifact = root / "FS25_SiN_Server.zip"
    build_mod(artifact)
    snapshot_result = simulate(root / "simulated-snapshot.xml")
    _write_binding_and_snapshot(root)
    central = OfflineCentral()
    agent = PairingAgent(root, "http://offline", central)
    delivered = agent.process_operations_once()
    executed = MailboxOperationHarness(root).consume_once()
    acknowledged = agent.process_receipts_once()

    events = root / "events"
    events.mkdir()
    common = ('server_key="sin-campaign" server_credential="campaign-secret" '
              'save_id="1" unique_user_id="stable-player" session_id="session-1"')
    (events / "disconnect.xml").write_text(
        f'<serverEvent event_id="disconnect-1" event_type="player_disconnected" {common} '
        'final_minute_sequence="1"/>', encoding="utf-8")
    (events / "minute.xml").write_text(
        f'<serverEvent event_id="minute-1" event_type="player_activity_minute" {common} '
        'minute_sequence="1" activity_bucket="active" inactive_minutes="0"/>',
        encoding="utf-8")
    event_order = agent.process_events_once()
    report = {
        "report_schema": "sin.integration/1",
        "campaign": "sin-offline-integration",
        "status": "passed",
        "checks": [
            {"id": "artifact", "status": "passed", "boundary": "mod-package"},
            {"id": "snapshot", "status": "passed", "boundary": "snapshot-xml"},
            {"id": "mailbox", "status": "passed", "boundary": "agent-mailbox"},
            {"id": "event-order", "status": "passed", "boundary": "event-mailbox"},
        ],
        "artifact": {"name": artifact.name,
                      "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()},
        "snapshot": {"source": snapshot_result["source"],
                      "farms": {str(key): value for key, value in snapshot_result["farms"].items()}},
        "mailbox": {"operations_delivered": delivered,
                     "operations_executed": executed,
                     "receipts_acknowledged": acknowledged,
                     "central_receipts": len(central.receipts)},
        "events": {"processed_in_order": event_order,
                   "central_event_types": [event["event_type"] for event in central.events]},
    }
    if report["mailbox"]["operations_delivered"] != ["campaign-operation-1"]:
        raise AssertionError("operation was not delivered")
    if report["mailbox"]["operations_executed"] != ["campaign-operation-1"]:
        raise AssertionError("operation was not executed")
    if report["mailbox"]["receipts_acknowledged"] != ["campaign-operation-1.xml"]:
        raise AssertionError("receipt was not acknowledged")
    if report["events"]["central_event_types"] != ["player_activity_minute", "player_disconnected"]:
        raise AssertionError("activity events were not ordered before disconnect")
    return report


def _registration_scenario(root: Path) -> Mapping[str, object]:
    mailbox = MailboxBoundaryAdapter(root)
    mailbox.bind()
    mailbox.registration_request()
    central = BoundaryCentral(registration=True)
    agent = PairingAgent(root, "http://offline", central)
    processed = agent.process_registration_once()
    response = ElementTree.parse(root / "registration-responses" / "registration-1.xml").getroot()
    replay = agent.process_registration_once()
    if processed != ["registration-1.xml"] or replay != [] or response.get("status") != "registration_required":
        raise AssertionError("registration request was not delivered")
    return {"report_schema": "sin.integration/1", "campaign": "sin-offline-integration",
            "status": "passed", "checks": [{"id": "registration", "status": "passed",
            "boundary": "registration-mailbox"}], "registration": {
                "requests_forwarded": len(central.registrations),
                "responses_written": processed,
                "replay_is_idempotent": replay == [],
                "authenticated_requests": len(central.calls),
                "stable_identity": response.get("fs25_unique_user_id"),
                "result": response.get("status")}}


def _map_scenario(root: Path) -> Mapping[str, object]:
    mailbox = MailboxBoundaryAdapter(root)
    mailbox.bind()
    mailbox.map_event()
    central = BoundaryCentral()
    agent = PairingAgent(root, "http://offline", central)
    processed = agent.process_events_once()
    if processed != ["map.xml"] or [item["event_type"] for item in central.events] != ["map_geometry"]:
        raise AssertionError("map geometry event was not forwarded")
    return {"report_schema": "sin.integration/1", "campaign": "sin-offline-integration",
            "status": "passed", "checks": [{"id": "map-geometry", "status": "passed",
            "boundary": "map-event-mailbox"}], "map": {
                "events_forwarded": len(central.events), "field_ids": ["22"],
                "processed_files": processed}}


def _semantic_pairing(root: Path) -> Mapping[str, object]:
    central = BoundaryCentral()
    agent = PairingAgent(root, "http://offline", central)
    server_key = agent.pair_once("PAIR1234")
    response = ElementTree.parse(root / "permission-commands" / "server-pairing-response.xml").getroot()
    if server_key != "semantic-server" or response.get("serverKey") != server_key:
        raise AssertionError("pairing response did not cross the mailbox boundary")
    return {"report_schema": "sin.integration/1", "campaign": "sin-offline-integration",
            "status": "passed", "checks": [{"id": "server-pairing", "status": "passed",
            "boundary": "pairing-mailbox"}], "pairing": {"server_key": server_key,
            "response_written": True}}


def _semantic_farm_lifecycle(root: Path) -> Mapping[str, object]:
    report = _full_scenario(root)
    return {"report_schema": "sin.integration/1", "campaign": report["campaign"],
            "status": "passed", "checks": [{"id": "farm-lifecycle", "status": "passed",
            "boundary": "operation-receipt-mailbox"}], "farm_lifecycle": {
                "operation_id": report["mailbox"]["operations_delivered"][0],
                "receipt_status": "acknowledged", "farmland_id": 22}}


def _semantic_farmland_ownership(root: Path) -> Mapping[str, object]:
    """Exercise central → Agent XML → reference executor → receipt → central.

    The reference executor deliberately has its own mutable owner map, so this
    proves receipt-gated reconciliation rather than merely echoing the request.
    It does not claim to prove a live GIANTS runtime invocation.
    """
    class ScenarioAuthorization:
        def __init__(self):
            self.assignments = []

        def assign(self, *args, **kwargs):
            self.assignments.append((args, kwargs))
            return "scenario-manager-permission"

    database = _MemoryDatabase()
    authorization = ScenarioAuthorization()
    lifecycle = FarmLifecycle(database, authorization)
    database.db.server_snapshots.insert_one({"server_key": "sin-campaign", "save_key": "campaign-save",
        "source": "game", "farmlands": {"22": 0}, "farms": {"2": "Campaign Farm"}})
    database.db.farm_requests.insert_one({"_id": "campaign-land-request", "discord_id": "campaign-player",
        "server_key": "sin-campaign", "save_key": "campaign-save", "farm_name": "Campaign Farm",
        "starting_field": 22, "state": "pending"})
    provision_id = lifecycle.approve_request("campaign-land-request", "sin-campaign", "campaign-save", "staff")
    lifecycle.accept_receipt("sin-campaign", "campaign-save", {"operation_id": provision_id,
        "operation_type": "provision_farm", "status": "applied", "farm_id": 2,
        "receipt": "farm_exists_or_created"})
    pending_request = database.db.farm_requests.find_one({"_id": "campaign-land-request"})
    operation_id = pending_request["land_operation_id"]
    duplicate_operation_id = lifecycle.approve_request(
        "campaign-land-request", "sin-campaign", "campaign-save", "staff")
    _write_binding_and_snapshot(root)
    central = _FarmlandLifecycleCentral(lifecycle)
    agent = PairingAgent(root, "http://offline", central)
    delivered = agent.process_operations_once()
    executor = FarmlandOwnershipReferenceExecutor(root, owners={22: 0}, valid_farms={2})
    executed = executor.consume_once()
    acknowledged = agent.process_receipts_once()
    operation = database.db.farm_operations.find_one({"_id": operation_id})
    request = database.db.farm_requests.find_one({"_id": "campaign-land-request"})
    duplicate_receipt = lifecycle.accept_receipt("sin-campaign", "campaign-save", operation["receipt"])
    if not (operation_id == duplicate_operation_id and delivered == [operation_id] and executed == [operation_id]
            and acknowledged == [operation_id + ".xml"] and executor.owners[22] == 2
            and operation.get("state") == "succeeded" and request.get("state") == "awaiting_manager"
            and duplicate_receipt.get("state") == "succeeded" and len(authorization.assignments) == 1):
        raise AssertionError("farmland ownership command/receipt reconciliation did not complete")
    return {"report_schema": "sin.integration/1", "campaign": "sin-authoritative-integration", "status": "passed",
            "checks": [{"id": "farmland-ownership", "status": "passed",
                        "boundary": "central+agent+xml+reference-executor+receipt"}],
            "farmland_ownership": {"operation_id": operation_id, "server_key": "sin-campaign",
                "save_key": "campaign-save", "farmland_id": 22, "target_farm_id": 2,
                "owner_before_farm_id": operation.get("owner_before_farm_id"),
                "owner_after_farm_id": operation.get("owner_farm_id"),
                "duplicate_command_idempotent": True, "duplicate_receipt_idempotent": True,
                "executor": "deterministic reference only; live GIANTS validation required"}}


def _semantic_shared_contractor_authority(root: Path) -> Mapping[str, object]:
    """Exercise shared contractor policy through the production-shaped mailbox.

    The executor only proves Central/Agent/XML/receipt idempotency.  It is not
    evidence that a real GIANTS permission table was mutated.
    """
    class SharedAuthorityCentral:
        def __init__(self, lifecycle, authorization):
            self.lifecycle = lifecycle
            self.authorization = authorization
            self.receipt_calls = 0

        def __call__(self, request, timeout=10):
            del timeout
            if request.headers.get("X-sin-server-key") != "sin-campaign" \
                    or request.headers.get("Authorization") != "Bearer campaign-secret":
                raise AssertionError("shared authority scenario request was not authenticated")
            path = urlsplit(request.full_url).path
            if path.endswith("/api/server/operations"):
                self.lifecycle.operations_for("sin-campaign", "campaign-save")
                jobs = list(self.authorization.db.permission_jobs.find({
                    "server_id": "sin-campaign", "save_id": "campaign-save", "state": "pending"}))
                return _Response({"operations": [{"operation_id": job["_id"], "operation_type": "permission",
                    "server_key": "sin-campaign", "save_key": "campaign-save", "state": "pending",
                    "payload": {**{key: job[key] for key in ("game_player_id", "farm_id", "role", "revision")},
                                **({"source_farm_id": job["source_farm_id"]}
                                   if job.get("source_farm_id") is not None else {})}}
                    for job in jobs]})
            if path.endswith("/api/server/operation-receipts"):
                payload = json.loads(request.data.decode("utf-8"))
                receipt = payload["receipt"]
                self.receipt_calls += 1
                state = self.authorization.acknowledge(receipt["operation_id"], "sin-campaign",
                    "campaign-save", int(receipt["revision"]), receipt, receipt.get("world_id"))
                return _Response({"status": "accepted", "operation_id": receipt["operation_id"], "state": state})
            raise AssertionError(f"unexpected shared authority endpoint: {path}")

    database = _MemoryDatabase()
    authorization = AuthorizationManager(database)
    lifecycle = FarmLifecycle(database, authorization)
    database.db.sin_farms.insert_one({"_id": "system", "server_key": "sin-campaign",
        "save_key": "campaign-save", "farm_type": "system", "canonical_name": "SiN Harvest",
        "state": "active", "fs25_farm_id": 99})
    database.db.game_identities.insert_one({"server_id": "sin-campaign", "save_id": "campaign-save",
        "discord_id": "repton", "game_player_id": "stable-repton", "fs25_unique_user_id": "stable-repton"})
    database.db.community_applications.insert_one({"_id": "repton", "state": "approved", "farm_name": "Repton Does"})
    database.db.memberships.insert_one({"_id": "personal", "server_id": "sin-campaign", "save_id": "campaign-save",
        "discord_id": "repton", "game_player_id": "stable-repton", "farm_id": 2,
        "desired_role": "farm_manager", "applied_role": "farm_manager", "state": "active",
        "operation_id": "personal-op", "revision": 1})
    lifecycle.record_snapshot("sin-campaign", "campaign-save", {
        "source": "game", "world_id": "campaign-world-a",
        "farms": {"2": "Campaign Farm", "99": "SiN Harvest"},
        "farmlands": {"22": 0}, "players": {}})
    database.db.observed_fs25_identities.insert_one({"server_key": "sin-campaign", "save_key": "campaign-save",
        "world_id": "campaign-world-a", "fs25_unique_user_id": "stable-repton", "current_farm_id": 2})
    _write_binding_and_snapshot(root)
    central = SharedAuthorityCentral(lifecycle, authorization)
    agent = PairingAgent(root, "http://offline", central)
    delivered = agent.process_operations_once()
    executed = MailboxOperationHarness(root).consume_once()
    acknowledged = agent.process_receipts_once()
    relationships = sorted(database.db.memberships.find({"discord_id": "repton"}), key=lambda row: row["farm_id"])
    replay = agent.process_operations_once()
    if not (len(delivered) == 1 and delivered == executed and acknowledged == [delivered[0] + ".xml"]
            and replay == [] and central.receipt_calls == 1
            and [(row["farm_id"], row["desired_role"], row["applied_role"], row["state"])
                for row in relationships] == [
                    (2, "farm_manager", "farm_manager", "active"),
                    (99, "contractor", "contractor", "active")]):
        raise AssertionError("shared contractor mailbox reconciliation did not preserve simultaneous authority")
    return {"report_schema": "sin.integration/1", "campaign": "sin-authoritative-integration", "status": "passed",
            "checks": [{"id": "shared-contractor-authority", "status": "passed",
                        "boundary": "central+agent+xml+reference-executor+receipt"}],
            "shared_contractor_authority": {"personal_farm_id": 2, "shared_farm_id": 99,
                "duplicate_delivery_idempotent": True,
                "executor": "deterministic reference only; live GIANTS contractor validation required"}}


def _semantic_release_evidence(root: Path) -> Mapping[str, object]:
    artifact = root / "FS25_SiN_Server.zip"
    build_mod(artifact)
    return {"report_schema": "sin.integration/1", "campaign": "sin-offline-integration",
            "status": "passed", "checks": [{"id": "release-evidence", "status": "passed",
            "boundary": "release-package"}], "release": {
                "artifact": artifact.name,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}}


def _authoritative_control_plane_backlog(root: Path) -> Mapping[str, object]:
    """Measure the real control plane against a 10,000-event backlog."""
    backlog_size = 10_000
    # Use the Agent's production maximum: bounded enough to exercise the
    # backlog contract while keeping the deterministic campaign practical on
    # filesystem-bound CI runners.
    batch_size = PairingAgent.MAX_EVENT_BATCH_SIZE
    mailbox = MailboxBoundaryAdapter(root)
    mailbox.bind()
    mailbox.pairing_request()
    mailbox.heartbeat_backlog(backlog_size)
    central = BoundaryCentral()
    agent = PairingAgent(root, "http://offline", central, event_batch_size=batch_size)
    started = time.perf_counter()
    # Run one real watch cycle first.  The bounded event batch is then drained
    # with the same production method so the measurement covers both control
    # plane precedence and the complete backlog.
    cycles = {"count": 0}
    def stop_after_one_cycle():
        should_stop = cycles["count"] > 0
        cycles["count"] += 1
        return should_stop
    agent.watch(interval=0.1, stop=stop_after_one_cycle)
    first_batch = len(central.events)
    processed = agent.process_events_once(backlog_size)
    processed_count = first_batch + len(processed)
    batch_count = 2
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    paths = [call["path"] for call in central.calls]
    event_index = paths.index("/api/server/events")
    control_paths = ["/api/server/pair", "/api/server/operations",
                     "/api/server/manager-authority", "/api/server/snapshot",
                     "/api/server/clock"]
    remaining = list((root / "events").glob("*.xml"))
    if any(paths.index(path) > event_index for path in control_paths) or processed_count != backlog_size or len(central.events) != backlog_size or remaining:
        raise AssertionError("control-plane work was delayed behind the event backlog")
    return {"report_schema": "sin.integration/1", "campaign": "sin-authoritative-integration",
            "status": "passed", "checks": [{"id": "control_plane_backlog", "status": "passed",
            "boundary": "agent-watch"}], "control_plane_backlog": {
                "backlog_size": backlog_size, "processed_events": processed_count,
                "batch_size": batch_size, "watch_batch": first_batch,
                "batches": batch_count,
                "elapsed_ms": elapsed_ms, "authenticated_requests": len(central.calls),
                "control_paths_before_events": control_paths}}


def _authoritative_registration(root: Path) -> Mapping[str, object]:
    """Compose XML request, authenticated transport, response, and replay."""
    mailbox = MailboxBoundaryAdapter(root)
    mailbox.bind()
    mailbox.registration_request()
    central = BoundaryCentral(registration=True)
    agent = PairingAgent(root, "http://offline", central)
    forwarded = agent.process_registration_once()
    replay = agent.process_registration_once()
    response_path = root / "registration-responses" / "registration-1.xml"
    response = ElementTree.parse(response_path).getroot()
    request_payload = central.registrations[0] if central.registrations else {}
    if forwarded != ["registration-1.xml"] or replay or response.get("status") != "registration_required" \
            or request_payload.get("fs25_unique_user_id") != "stable-player":
        raise AssertionError("registration composition did not complete at the mailbox boundary")
    return {"report_schema": "sin.integration/1", "campaign": "sin-authoritative-integration",
            "status": "passed", "checks": [{"id": "registration", "status": "passed",
            "boundary": "mailbox_xml+central_http"}], "registration": {
                "request": "registration-1.xml", "response": response_path.name,
                "stable_identity": response.get("fs25_unique_user_id"),
                "forwarded": len(central.registrations), "replay_is_idempotent": replay == [],
                "authenticated_requests": len(central.calls)}}


def _authoritative_activity_disconnect(root: Path) -> Mapping[str, object]:
    """Run a complete NetworkLocal activity lifecycle through Agent and Central."""
    expected_totals = {
        "connected_minutes": 12,
        "active_minutes": 1,
        "idle_minutes": 10,
        "afk_minutes": 1,
    }
    mailbox = MailboxBoundaryAdapter(root)
    mailbox.bind()
    mailbox.activity_connect()
    mailbox.activity_minute(1, "active", 0)
    mailbox.activity_disconnect(12)
    database = _MemoryDatabase()
    processor = CentralEventProcessor(database)
    processor.registry.authenticate = lambda server_key, credential: {
        "server_key": "sin-campaign", "display_name": "Campaign", "online": True}
    processor.registry.resolve_save = lambda server_key, save_id: "campaign-save"
    central = ProcessorCentral(processor)
    agent = PairingAgent(root, "http://offline", central)
    early_processed = agent.process_events_once()
    if early_processed != ["connect-1.xml", "minute-1.xml"]:
        raise AssertionError("early activity pass did not retain the watermarked disconnect")
    if not (root / "events" / "disconnect.xml").exists() \
            or not any(outcome.get("status") == "retryable" for outcome in central.transport_outcomes):
        raise AssertionError("early disconnect was not handled as retryable")
    early_disconnect_outbox = [row for row in database.db.activity_outbox.rows
                               if row.get("activity_type") == "player_disconnected"]
    if early_disconnect_outbox:
        raise AssertionError("incomplete disconnect was published before its watermark was satisfied")

    for sequence in range(2, 12):
        mailbox.activity_minute(sequence, "idle", sequence - 1)
    mailbox.activity_minute(12, "afk", 11)
    later_processed = agent.process_events_once()
    if later_processed != [f"minute-{sequence}.xml" for sequence in range(2, 13)] + ["disconnect.xml"]:
        raise AssertionError("later activity minutes or disconnect retry were not persisted")

    session = database.db.player_activity_sessions.find_one({"session_id": "session-1"}) or {}
    minutes = list(database.db.player_activity_minutes.find({"session_id": "session-1"}))
    aggregate = database.db.player_activity_aggregates.find_one({"fs25_unique_user_id": "stable-player"}) or {}
    observed_totals = {key: session.get(key) for key in expected_totals}
    if session.get("state") != "completed" or len(minutes) != expected_totals["connected_minutes"] \
            or observed_totals != expected_totals:
        raise AssertionError("completed activity session counters were not durable")
    if {"active", "idle", "afk"} != {row.get("activity_bucket") for row in minutes}:
        raise AssertionError("completed session did not contain all activity classifications")

    disconnect_outbox = [row for row in database.db.activity_outbox.rows
                         if row.get("activity_type") == "player_disconnected"]
    if len(disconnect_outbox) != 1 or disconnect_outbox[0].get("status") != "pending":
        raise AssertionError("disconnect completion did not create one activity outbox record")
    completed_message = disconnect_outbox[0].get("message", "")
    if not all(f"{label}: {value} min" in completed_message for label, value in (
            ("Session", 12), ("Active", 1), ("Idle", 10), ("AFK", 1))):
        raise AssertionError("completed outbox message did not match persisted totals")

    # Simulate the actual recoverable interruption window: the session is
    # durably complete, but its first outbox insert is lost before publishing.
    # Replaying the same logical disconnect must repair that missing outbox
    # record through CentralEventProcessor and ActivityOutbox.
    deleted_outbox = database.db.activity_outbox.delete_one({"_id": disconnect_outbox[0]["_id"]})
    if deleted_outbox.modified_count != 1:
        raise AssertionError("the simulated interruption did not remove the first outbox row")
    mailbox.activity_disconnect(12, event_id="disconnect-1", filename="disconnect-repair.xml")
    repaired = agent.process_events_once()
    repaired_outbox = [row for row in database.db.activity_outbox.rows
                       if row.get("activity_type") == "player_disconnected"]
    if repaired != ["disconnect-repair.xml"] or len(repaired_outbox) != 1:
        raise AssertionError("duplicate disconnect did not repair the durable outbox")

    mailbox.activity_disconnect(12, event_id="disconnect-1", filename="disconnect-duplicate.xml")
    duplicate = agent.process_events_once()
    final_outbox = [row for row in database.db.activity_outbox.rows
                    if row.get("activity_type") == "player_disconnected"]
    if duplicate != ["disconnect-duplicate.xml"] or len(final_outbox) != 1:
        raise AssertionError("duplicate disconnect was not idempotent")
    duplicate_session = database.db.player_activity_sessions.find_one({"session_id": "session-1"}) or {}
    if duplicate_session.get("state") != "completed" \
            or {key: duplicate_session.get(key) for key in expected_totals} != expected_totals:
        raise AssertionError("duplicate disconnect changed completed session totals")
    message = final_outbox[0].get("message", "")
    if not all(label in message for label in ("Duration:", "Session:", "Active:", "Idle:", "AFK:")):
        raise AssertionError("completed session capture was not NetworkBot-compatible")

    # Use the real durable publisher with a narrow deterministic Discord
    # capture boundary.  This proves the final outbox record is publishable,
    # rather than merely proving that a formatted string exists in memory.
    database.db.sin_servers.insert_one({"server_key": "sin-campaign",
                                        "discord_activity_channel_id": 999})
    channel = _CaptureChannel()
    publisher = ActivityPublisher(_CaptureBot(channel), database)
    asyncio.run(publisher.publish(final_outbox[0]))
    published = database.db.activity_outbox.find_one({"_id": final_outbox[0]["_id"]}) or {}
    if published.get("status") != "published" or channel.messages != [message]:
        raise AssertionError("completed session outbox was not delivered through ActivityPublisher")

    forwarded = [event["event_type"] for event in central.events]
    if forwarded.count("player_connected") != 1 or forwarded.count("player_activity_minute") != 12 \
            or forwarded.count("player_disconnected") != 4:
        raise AssertionError("composed activity lifecycle did not reach Central")
    return {"report_schema": "sin.integration/1", "campaign": "sin-authoritative-integration",
            "status": "passed", "checks": [{"id": "activity_disconnect", "status": "passed",
            "boundary": "event-mailbox+central_processor+activity_outbox"}], "activity_disconnect": {
                "early_processed_files": early_processed,
                "later_processed_files": later_processed,
                "repair_processed_files": repaired,
                "duplicate_processed_files": duplicate,
                "forwarded_event_types": forwarded,
                "expected_totals": expected_totals,
                "observed_totals": observed_totals,
                "lifecycle": {"connected": True, "session_id": "session-1",
                               "active_sequences": [1], "idle_sequences": list(range(2, 12)),
                               "afk_sequences": [12], "disconnect_watermark": 12},
                "final_minute_sequence": 12,
                "processed_files": later_processed,
                "replay_is_idempotent": len(final_outbox) == 1 and duplicate == ["disconnect-duplicate.xml"],
                "retryable_early_disconnect": True,
                "early_disconnect_outbox_count": len(early_disconnect_outbox),
                "persistence": {"minute_count": len(minutes), "session_state": session.get("state"),
                                "connected_minutes": observed_totals["connected_minutes"],
                                "active_minutes": observed_totals["active_minutes"],
                                "idle_minutes": observed_totals["idle_minutes"],
                                "afk_minutes": observed_totals["afk_minutes"],
                                "aggregate_connected_minutes": aggregate.get("connected_minutes")},
                "outbox_before_repair": {"count": 1, "status": "pending",
                                         "message_matches_totals": True},
                "outbox_repaired": len(repaired_outbox) == 1,
                "duplicate_disconnect_idempotent": len(final_outbox) == 1,
                "duplicate_session_totals_preserved": True,
                "crash_repair": {"completed_session_preserved": True,
                                 "lost_outbox_recreated": True,
                                 "duplicate_notification_prevented": True},
                "outbox_published": {"count": 1, "status": published.get("status"),
                                     "captured_messages": len(channel.messages)},
                "networkbot_completed_session": {"activity_type": final_outbox[0]["activity_type"],
                                                   "source_event_id": final_outbox[0]["source_event_id"],
                                                   "message": message},
                "authenticated_requests": len(central.calls),
                "central_results": central.results}}


def _authoritative_map_contract(root: Path) -> Mapping[str, object]:
    """Compose the real map -> contract -> NetworkBot presentation path."""
    mailbox = MailboxBoundaryAdapter(root)
    mailbox.bind()
    mailbox.map_contract_event()
    database = _MemoryDatabase()
    processor = CentralEventProcessor(database)
    credentials = {"sin-campaign": "campaign-secret", "sin-other": "other-secret"}
    saves = {"sin-campaign": "campaign-save", "sin-other": "other-save"}

    def authenticate(server_key, credential):
        if credentials.get(server_key) != credential:
            raise ValueError("invalid campaign credential")
        return {"server_key": server_key, "display_name": server_key, "online": True}

    processor.registry.authenticate = authenticate
    processor.registry.resolve_save = lambda server_key, save_id: saves[server_key]
    central = ProcessorCentral(processor)
    central.credentials = credentials
    agent = PairingAgent(root, "http://offline", central)
    processed_initial = agent.process_events_once()
    persisted_initial = database.db.sin_maps.find_one({
        "server_key": "sin-campaign", "save_key": "campaign-save"})
    mailbox.map_contract_event("map-contract-1")
    replay = agent.process_events_once()
    replay_result = central.results[-1] if central.results else {}
    if (processed_initial != ["map-contract-1.xml"] or replay != ["map-contract-1.xml"]
            or not replay_result.get("duplicate") or not persisted_initial):
        raise AssertionError("map geometry was not persisted through the Agent/Central boundary")
    geometry_initial = persisted_initial.get("map_payload", {})
    fields_initial = geometry_initial.get("fields", {})
    if (set(fields_initial) != {"22", "47"} or fields_initial["22"].get("farmland_id") != 22
            or len(fields_initial["47"].get("rings", [[]])[0]) != 7
            or geometry_initial.get("farmland_ids") != [22, 47]
            or set(geometry_initial.get("farmlands", {})) != {"22", "47"}):
        raise AssertionError("persisted map did not retain both required geometries")

    # A second authenticated Agent/mailbox proves that the same field and
    # farmland IDs are scoped by server/save rather than accidentally shared.
    other_root = root.parent / "map-server-other"
    other_mailbox = MailboxBoundaryAdapter(other_root)
    other_mailbox.bind("sin-other", "other-secret", "1")
    other_mailbox.map_contract_event("map-other", server_key="sin-other",
                                     credential="other-secret", save_id="1")
    other_agent = PairingAgent(other_root, "http://offline", central)
    other_processed = other_agent.process_events_once()
    other_record = database.db.sin_maps.find_one({
        "server_key": "sin-other", "save_key": "other-save"})
    if not other_record or other_processed != ["map-other.xml"]:
        raise AssertionError("second server map was not independently persisted")

    map_service = _CountingMapService()
    bot = NetworkBot(_CaptureBank(database), {}, 1, channels={"jobs": 999}, map_service=map_service)
    channel = _CaptureChannel()
    bot.get_channel = lambda channel_id: channel if int(channel_id) == 999 else None
    contracts = ContractService(database)
    contract = contracts.create(
        "campaign-creator", "", "Harvest Field 22", value=1200,
        server_key="sin-campaign", save_key="campaign-save", fields="22",
        work_type="harvesting", server_name="Campaign")
    persisted_contract = database.db.contracts.find_one({"contract_id": contract["contract_id"]})

    async def compose_presentation():
        try:
            initial_published = await bot.publish_contract_card(persisted_contract)
            initial_loads = map_service.payload_loads
            initial_render = dict(map_service.render_requests[-1]) if map_service.render_requests else None
            farmland_loaded = bot.ensure_registered_map("sin-campaign", "campaign-save")
            farmland_rendered = map_service.render_map(
                "sin-campaign", "campaign-save", highlight_farmlands=[22],
                ownership={22: {"farm_id": 2, "farm_name": "Campaign Farm"}})
            other_loaded = bot.ensure_registered_map("sin-other", "other-save")
            other_rendered = map_service.render_map("sin-other", "other-save",
                                                    highlight_fields=[22])

            # A second event with equivalent content must not cause JiN's
            # persisted-map loader to rebuild its MapService projection.
            mailbox.map_contract_event("map-contract-stable")
            stable_processed = agent.process_events_once()
            stable_record = database.db.sin_maps.find_one({
                "server_key": "sin-campaign", "save_key": "campaign-save"})
            stable_before = map_service.payload_loads
            stable_loaded = bot.ensure_registered_map("sin-campaign", "campaign-save")
            stable_after = map_service.payload_loads

            # A meaningful geometry change crosses the same Agent/Central
            # boundary, then forces NetworkBot's persisted-map reload.
            mailbox.map_contract_event("map-contract-changed", changed=True)
            changed_processed = agent.process_events_once()
            changed_record = database.db.sin_maps.find_one({
                "server_key": "sin-campaign", "save_key": "campaign-save"})
            changed_before = map_service.payload_loads
            changed_loaded = bot.ensure_registered_map("sin-campaign", "campaign-save")
            changed_after = map_service.payload_loads
            changed_rendered = map_service.render_contract_map("sin-campaign", "campaign-save", "22")

            # A delayed event from the previous runtime must not roll back a
            # newer source generation, even though it has a distinct event ID.
            mailbox.map_contract_event("map-contract-stale")
            stale_processed = agent.process_events_once()
            stale_result = central.results[-1] if central.results else {}

            # Keep the real presentation fallback covered without replacing
            # the successful attachment path above.
            fallback_contract = contracts.create(
                "campaign-creator", "", "Unavailable field", value=10,
                server_key="sin-campaign", save_key="campaign-save", fields="999",
                work_type="harvesting", server_name="Campaign")
            fallback_published = await bot.publish_contract_card(fallback_contract)
            return {
                "initial_published": initial_published,
                "initial_loads": initial_loads,
                "initial_render": initial_render,
                "farmland_loaded": farmland_loaded,
                "farmland_rendered": farmland_rendered,
                "other_loaded": other_loaded,
                "other_rendered": other_rendered,
                "stable_processed": stable_processed,
                "stable_record": stable_record,
                "stable_loaded": stable_loaded,
                "stable_loads_before": stable_before,
                "stable_loads_after": stable_after,
                "changed_processed": changed_processed,
                "changed_record": changed_record,
                "changed_loaded": changed_loaded,
                "changed_loads_before": changed_before,
                "changed_loads_after": changed_after,
                "changed_rendered": changed_rendered,
                "stale_processed": stale_processed,
                "stale_result": stale_result,
                "fallback_published": fallback_published,
            }
        finally:
            await bot.close()

    presentation = asyncio.run(compose_presentation())
    delivery = channel.deliveries[0] if channel.deliveries else {}
    attachment = delivery.get("file")
    attachment_bytes = b""
    if attachment is not None and getattr(attachment, "fp", None) is not None:
        attachment.fp.seek(0)
        attachment_bytes = attachment.fp.read()
    png_evidence = _inspect_png(attachment_bytes)
    png_signature = bool(png_evidence)
    png_dimensions = png_evidence.get("dimensions") if png_evidence else None
    farmland_png = _inspect_png(presentation["farmland_rendered"])
    other_png = _inspect_png(presentation["other_rendered"])
    final_record = database.db.sin_maps.find_one({
        "server_key": "sin-campaign", "save_key": "campaign-save"})
    final_model = map_service.model("sin-campaign", "campaign-save")
    final_field = final_model.fields[22]
    irregular_field = final_model.fields[47]
    unsafe_identity_rejected = False
    try:
        unsafe_payload = dict(final_record["map_payload"])
        unsafe_payload["map_id"] = "../arbitrary-local-path"
        map_service.register_payload("sin-campaign", "campaign-save", unsafe_payload)
    except Exception as error:
        unsafe_identity_rejected = type(error).__name__ == "MapValidationError"
    arbitrary_path_lookup = bot.ensure_registered_map("../arbitrary-local-path", "campaign-save")
    invalid_path = mailbox.map_contract_event("map-contract-invalid", invalid=True)
    invalid_processed = agent.process_events_once()
    invalid_quarantine = invalid_path.with_suffix(invalid_path.suffix + ".failed")
    if not (presentation["initial_published"] and presentation["initial_loads"] == 1
            and presentation["stable_loaded"] and presentation["stable_loads_after"] == presentation["stable_loads_before"]
            and presentation["changed_loaded"] and presentation["changed_loads_after"] == presentation["changed_loads_before"] + 1
            and presentation["initial_render"] and presentation["initial_render"]["highlight_fields"] == [22]
            and final_model.fields.keys() == {22, 47}
            and presentation["farmland_loaded"] and farmland_png and farmland_png["valid"]
            and presentation["other_loaded"] and other_png and other_png["valid"]
            and other_record["map_payload"]["fields"]["22"] != final_record["map_payload"]["fields"]["22"]
            and presentation["stale_processed"] == ["map-contract-stale.xml"]
            and presentation["stale_result"].get("map_persistence") == "stale"
            and invalid_processed == [] and invalid_quarantine.exists()
            and final_record.get("map_revision") == presentation["changed_record"].get("map_revision")):
        # The explicit checks below keep the failure messages local and avoid
        # relying on truthiness of a large report object.
        raise AssertionError("map contract composition did not complete")
    if not presentation["fallback_published"]:
        raise AssertionError("text-only contract fallback was not delivered")
    if not unsafe_identity_rejected or arbitrary_path_lookup:
        raise AssertionError("unsafe map identity was not rejected safely")
    if not png_evidence or not png_evidence["valid"] or not attachment_bytes or png_dimensions != [128, 128]:
        raise AssertionError("NetworkBot did not capture a valid generated PNG attachment")
    if final_model.fields[22].farmland_id != 22 or len(irregular_field.rings[0]) != 7:
        raise AssertionError("final map projection lost field/farmland geometry")
    if presentation["changed_record"]["map_revision"] == persisted_initial["map_revision"]:
        raise AssertionError("changed geometry did not produce a new persisted revision")
    if final_record["map_revision"] != presentation["changed_record"]["map_revision"]:
        raise AssertionError("invalid geometry replaced the last valid map")
    highlighted = map_service.render_requests[-1]
    if highlighted["highlight_fields"] != [22] or 47 in highlighted["highlight_fields"]:
        raise AssertionError(f"contract rendering selected the wrong field: {highlighted!r}")
    if len(channel.deliveries) != 2 or "file" in channel.deliveries[1]:
        raise AssertionError("render fallback did not remain text-only")
    persisted_contract = database.db.contracts.find_one({"contract_id": contract["contract_id"]})
    if (persisted_contract.get("server_key"), persisted_contract.get("save_key"),
            persisted_contract.get("fields")) != ("sin-campaign", "campaign-save", "22"):
        raise AssertionError("contract did not persist its server/save/field scope")
    return {"report_schema": "sin.integration/1", "campaign": "sin-authoritative-integration",
            "status": "passed", "checks": [{"id": "map_contract", "status": "passed",
            "boundary": "agent+central+jin+networkbot+discord-capture"}], "map_contract": {
                "input": {"map_id": "campaign-map", "server_key": "sin-campaign", "save_id": "1",
                          "field_ids": [22, 47], "field_22_points": 5, "irregular_field_points": 7},
                "agent": {"accepted": True, "processed_files": processed_initial,
                "replay_is_idempotent": bool(replay_result.get("duplicate")),
                "forwarded_field_ids": sorted(fields_initial),
                "forwarded_server_key": central.events[0].get("server_key"),
                "forwarded_save_id": central.events[0].get("save_id"),
                "forwarded_revision": central.results[0].get("map_revision")},
                "central": {"persisted": True, "map_id": persisted_initial["map_payload"]["map_id"],
                            "save_key": persisted_initial["save_key"], "revision": persisted_initial["map_revision"],
                            "fields": sorted(persisted_initial["map_payload"]["fields"]),
                            "farmland_ids": persisted_initial["map_payload"]["farmland_ids"],
                            "farmland_geometry": sorted(persisted_initial["map_payload"]["farmlands"])},
                "central_result": central.results[0],
                "downstream_load": {"source": "persisted_sin_maps", "initial_payload_loads": presentation["initial_loads"],
                                    "stable_revision_unchanged": presentation["stable_record"]["map_revision"] == persisted_initial["map_revision"],
                                    "stable_refresh_avoided": presentation["stable_loads_after"] == presentation["stable_loads_before"],
                                    "changed_revision_observed": presentation["changed_record"]["map_revision"] != persisted_initial["map_revision"],
                                    "changed_refresh_performed": presentation["changed_loads_after"] == presentation["changed_loads_before"] + 1,
                                    "final_fields": sorted(final_model.fields)},
                "contract": {"contract_id": contract["contract_id"], "persisted": True,
                             "scope": persisted_contract["scope"], "server_key": persisted_contract["server_key"],
                             "save_key": persisted_contract["save_key"], "requested_field": "22"},
                "render": {"requested_field": 22, "highlighted_field": highlighted["highlight_fields"],
                           "available_fields": sorted(final_model.fields), "farmland_id": final_field.farmland_id,
                           "farmland_ids": list(final_model.farmland_ids),
                           "farmland_overlay_rendered": bool(farmland_png and farmland_png["valid"]),
                           "irregular_field_47_present": 47 in final_model.fields,
                           "irregular_field_47_not_highlighted": 47 not in highlighted["highlight_fields"],
                           "png_generated": png_signature, "png_valid": bool(png_evidence and png_evidence["valid"]),
                           "png_dimensions": png_dimensions},
                "presentation": {"path": "NetworkBot.publish_contract_card", "captured_messages": len(channel.deliveries),
                                 "attachment_filename": getattr(attachment, "filename", None),
                                 "attachment_content_type": getattr(attachment, "content_type", None),
                                 "attachment_bytes": len(attachment_bytes), "png_signature": png_signature,
                                 "png_valid": bool(png_evidence and png_evidence["valid"]),
                                 "text_only_fallback": "file" not in channel.deliveries[1]},
                "revision": {"stable_processed": presentation["stable_processed"],
                             "changed_processed": presentation["changed_processed"],
                             "stale_processed": presentation["stale_processed"],
                             "stale_rejected": presentation["stale_result"].get("map_persistence") == "stale",
                             "initial_revision": persisted_initial["map_revision"],
                             "changed_revision": presentation["changed_record"]["map_revision"],
                             "payload_loads": map_service.payload_loads},
                "invalid_geometry": {"processed_files": invalid_processed,
                                     "rejected_and_quarantined": invalid_quarantine.exists(),
                                     "prior_valid_state_preserved": final_record["map_revision"] == presentation["changed_record"]["map_revision"],
                                     "presentation_unchanged": len(channel.deliveries) == 2},
                "filesystem_safety": {"unsafe_identity_rejected": unsafe_identity_rejected,
                                      "arbitrary_path_lookup_rejected": not arbitrary_path_lookup,
                                      "path_input_to_renderer": False},
                "multi_server": {"other_server": "sin-other", "other_save": "other-save",
                                 "overlapping_field_ids": sorted(other_record["map_payload"]["fields"]),
                                 "independent_persisted": bool(other_record),
                                 "independent_load": presentation["other_loaded"],
                                 "independent_render": bool(other_png and other_png["valid"]),
                                 "no_cross_server_geometry_leak": other_record["map_payload"]["fields"]["22"] != final_record["map_payload"]["fields"]["22"]},
                "replay_is_idempotent": bool(replay_result.get("duplicate")),
                "central_results": central.results,
                "authenticated_requests": len(central.calls)}}


def _authoritative_contract_scope(root: Path) -> Mapping[str, object]:
    """Compose context selection through the real NetworkBot command path."""
    database = _MemoryDatabase()
    for server_key, display_name, save_key in (
            ("server-a", "Campaign Alpha", "save-a"),
            ("server-b", "Campaign Bravo", "save-b")):
        database.db.sin_servers.insert_one({
            "_id": server_key, "server_key": server_key, "display_name": display_name,
            "enabled": True, "credential_hash": "hashed-credential"})
        database.db.sin_saves.insert_one({
            "_id": save_key, "server_key": server_key, "save_key": save_key,
            "fs25_save_id": "1"})

    # Identity rows are the real input to NetworkBot.resolve_identity_context;
    # the scenario does not hand a pre-resolved context to ContractService.
    database.db.game_identities.insert_one({
        "_id": "identity-a", "discord_id": "contract-user", "server_id": "server-a",
        "save_id": "save-a", "fs25_unique_user_id": "stable-a"})

    map_service = _FailingMapService()
    bot = NetworkBot(_CaptureBank(database), {}, 1, channels={"jobs": 704}, map_service=map_service)
    channel = _CaptureChannel(channel_id=704)
    bot.get_channel = lambda channel_id: channel if int(channel_id) == 704 else None
    command = bot.tree.get_command("contract_create")
    if command is None:
        raise AssertionError("production contract_create command was not registered")

    async def invoke(discord_id="contract-user", server=""):
        interaction = _CommandInteraction(discord_id=discord_id)
        try:
            await command.callback(
                interaction,
                app_commands.Choice(name="Harvesting", value="harvesting"),
                "22, 22",
                app_commands.Choice(name="Fixed price", value="fixed"),
                1200,
                "Deterministic contract-scope fixture",
                server,
            )
        except ValueError as error:
            return {"interaction": interaction, "error": str(error), "contract": None}
        created = interaction.response.messages[-1] if interaction.response.messages else {}
        record = database.db.contracts.rows[-1] if database.db.contracts.rows else None
        return {"interaction": interaction, "error": None, "contract": dict(record) if record else None,
                "response": created}

    async def compose():
        # Case A: one identity context invokes the automatic production path.
        single = await invoke()
        single_record = bot.contracts.get(single["contract"]["contract_id"]) if single["contract"] else None
        single_evidence = {
            "eligible_context_count": 1,
            "selection_mode": "automatic",
            "selected_server": single_record.get("server_key") if single_record else None,
            "selected_save": single_record.get("save_key") if single_record else None,
            "contract_id": single_record.get("contract_id") if single_record else None,
            "persisted_scope": {key: single_record.get(key) for key in ("scope", "server_key", "save_key")}
            if single_record else None,
            "response_sent": bool(single["interaction"].response.messages),
        }
        if (not single_record or single_record.get("server_key") != "server-a"
                or single_record.get("save_key") != "save-a" or single_record.get("scope") != "server"):
            raise AssertionError("single eligible context was not automatically scoped")

        # Case B: adding a second identity context must fail closed.  The
        # command callback raises before ContractService or presentation.
        database.db.game_identities.insert_one({
            "_id": "identity-b", "discord_id": "contract-user", "server_id": "server-b",
            "save_id": "save-b", "fs25_unique_user_id": "stable-b"})
        count_before_ambiguous = len(database.db.contracts.rows)
        deliveries_before_ambiguous = len(channel.deliveries)
        ambiguous = await invoke()
        multiple_evidence = {
            "eligible_context_count": 2,
            "result": "selection_required",
            "error": ambiguous["error"],
            "contract_created": ambiguous["contract"] is not None,
            "persisted_contract_count_change": len(database.db.contracts.rows) - count_before_ambiguous,
            "presentation_count_change": len(channel.deliveries) - deliveries_before_ambiguous,
        }
        if (ambiguous["contract"] is not None
                or "select an eligible server" not in (ambiguous["error"] or "").lower()
                or len(database.db.contracts.rows) != count_before_ambiguous
                or len(channel.deliveries) != deliveries_before_ambiguous):
            raise AssertionError("ambiguous context did not fail closed before persistence/presentation")

        # Case C: the explicit selector is still resolved through the
        # creator's durable identity contexts, then normalized by ContractService.
        explicit = await invoke(server="server-b")
        explicit_record = bot.contracts.get(explicit["contract"]["contract_id"]) if explicit["contract"] else None
        explicit_evidence = {
            "selector": "server-b",
            "selected_server": explicit_record.get("server_key") if explicit_record else None,
            "selected_save": explicit_record.get("save_key") if explicit_record else None,
            "contract_id": explicit_record.get("contract_id") if explicit_record else None,
            "persisted_scope": {key: explicit_record.get(key) for key in ("scope", "server_key", "save_key")}
            if explicit_record else None,
            "other_context_selected": bool(explicit_record and explicit_record.get("server_key") == "server-a"),
        }
        if (not explicit_record or explicit_record.get("server_key") != "server-b"
                or explicit_record.get("save_key") != "save-b"
                or explicit_record.get("scope") != "server"):
            raise AssertionError("explicit selector did not resolve the selected identity context")
        if explicit_evidence["other_context_selected"]:
            raise AssertionError("explicit selector selected the wrong context")

        # Case E/F/G: successful contracts are durable and the real NetworkBot
        # presentation path uses the configured jobs channel even when map
        # rendering fails.  The renderer is the only deterministic seam here;
        # card construction, destination lookup, persistence, and fallback are
        # all production NetworkBot code.
        scoped_reloaded = bot.contracts.get(explicit_record["contract_id"])
        if not scoped_reloaded or scoped_reloaded.get("scope") != "server":
            raise AssertionError("explicitly scoped contract did not survive service reload")

        # Case D: an invalid or missing context cannot reach ContractService.
        invalid_count = len(database.db.contracts.rows)
        invalid = await invoke(server="server-not-eligible")
        invalid_evidence = {
            "selector": "server-not-eligible",
            "result": "rejected",
            "error": invalid["error"],
            "contract_created": invalid["contract"] is not None,
        }
        if invalid["contract"] is not None or len(database.db.contracts.rows) != invalid_count:
            raise AssertionError("invalid explicit selector created a contract")
        missing_context = await invoke(discord_id="unregistered-user")
        missing_context_evidence = {
            "result": "rejected",
            "error": missing_context["error"],
            "contract_created": missing_context["contract"] is not None,
        }
        if missing_context["contract"] is not None:
            raise AssertionError("missing server/save context created a contract")
        return {
            "single": single_evidence,
            "multiple": multiple_evidence,
            "explicit_selector": explicit_evidence,
            "invalid_selector": invalid_evidence,
            "network_wide": {"available": False},
            "missing_context": missing_context_evidence,
            "jobs_channel": {
                "configured_destination": 704,
                "captured_destination": channel.channel_id,
                "matched": channel.channel_id == 704,
            },
            "persistence": {
                "scoped_reload": {key: scoped_reloaded.get(key) for key in
                                  ("contract_id", "scope", "server_key", "save_key", "fields")},
                "total_contracts": len(database.db.contracts.rows),
            },
            "presentation": {
                "path": "NetworkBot.publish_contract_card",
                "captured_messages": len(channel.deliveries),
                "all_to_configured_jobs": all(channel.channel_id == 704 for _ in channel.deliveries),
                "response_count": len(single["interaction"].response.messages)
                + len(explicit["interaction"].response.messages),
            },
            "fallback": {
                "render_failure": map_service.render_attempts > 0,
                "render_attempts": map_service.render_attempts,
                "contracts_persisted": len(database.db.contracts.rows),
                "scope_preserved": all(record.get("scope") == "server"
                                        for record in database.db.contracts.rows),
                "text_published": len(channel.deliveries) == 2,
                "attachment_count": sum("file" in delivery for delivery in channel.deliveries),
                "destination": channel.channel_id,
            },
        }

    try:
        evidence = asyncio.run(compose())
    finally:
        asyncio.run(bot.close())
    if (not evidence["jobs_channel"]["matched"] or not evidence["presentation"]["all_to_configured_jobs"]
            or evidence["fallback"]["attachment_count"] != 0
            or not evidence["fallback"]["text_published"]):
        raise AssertionError("contract scope presentation/fallback evidence is incomplete")
    return {"report_schema": "sin.integration/1", "campaign": "sin-authoritative-integration",
            "status": "passed", "checks": [{"id": "contract_scope", "status": "passed",
            "boundary": "networkbot+identity-context+contract-service+discord-capture"}],
            "contract_scope": evidence}


def _authoritative_authority_regression(root: Path) -> Mapping[str, object]:
    database = _MemoryDatabase()
    database.db.memberships.insert_one({"discord_id": "discord", "server_id": "server",
        "save_id": "save", "farm_id": 2, "state": "active",
        "desired_role": "farm_manager", "applied_role": "farm_manager"})
    manager = AdminManager(database)
    active = manager.lookup("discord", "server", "save")
    database.db.memberships.rows[0]["state"] = "pending"
    try:
        manager.lookup("discord", "server", "save")
    except ValueError:
        fail_closed = True
    else:
        fail_closed = False
    if not active or not fail_closed:
        raise AssertionError("pending authority was treated as active")
    return {"report_schema": "sin.integration/1", "campaign": "sin-authoritative-integration",
            "status": "passed", "checks": [{"id": "authority_regression", "status": "passed",
            "boundary": "manager-authority"}], "authority_regression": {
                "active_mapping_found": True, "pending_mapping_rejected": fail_closed}}


SCENARIOS = ScenarioRegistry()
SCENARIOS.register(NamedScenario(
    "offline-full", "Build, snapshot, mailbox, receipt, and event ordering proof.", _full_scenario))
SCENARIOS.register(NamedScenario(
    "artifact-and-snapshot", "Deterministic server package and game snapshot proof.",
    lambda root: {key: value for key, value in _full_scenario(root).items() if key in {"report_schema", "campaign", "status", "checks", "artifact", "snapshot"}}))
SCENARIOS.register(NamedScenario(
    "mailbox-roundtrip", "Central operation delivery, execution, and receipt proof.",
    lambda root: {key: value for key, value in _full_scenario(root).items() if key in {"report_schema", "campaign", "status", "checks", "mailbox"}}))
SCENARIOS.register(NamedScenario(
    "event-ordering", "Minute activity is forwarded before its disconnect watermark.",
    lambda root: {key: value for key, value in _full_scenario(root).items() if key in {"report_schema", "campaign", "status", "checks", "events"}}))
SCENARIOS.register(NamedScenario(
    "registration-roundtrip", "Stable player registration crosses the authenticated mailbox.",
    _registration_scenario))
SCENARIOS.register(NamedScenario(
    "map-geometry-boundary", "Bounded map geometry crosses the authenticated event boundary.",
    _map_scenario))

# Semantic names are the stable operator-facing contract.  The boundary names
# above remain registered for backwards compatibility with earlier reports.
SEMANTIC_SCENARIOS = {
    "server-pairing": NamedScenario("server-pairing", "Pair a server and publish its binding response.", _semantic_pairing),
    "farm-lifecycle": NamedScenario("farm-lifecycle", "Deliver and acknowledge a farm/land lifecycle operation.", _semantic_farm_lifecycle),
    "farmland-ownership": NamedScenario("farmland-ownership", "Reconcile an explicit owner read-back through the farmland command mailbox.", _semantic_farmland_ownership),
    "shared-contractor-authority": NamedScenario("shared-contractor-authority", "Reconcile simultaneous personal-manager and shared-contractor authority.", _semantic_shared_contractor_authority),
    "player-registration": NamedScenario("player-registration", "Route a stable player registration request and response.", _registration_scenario),
    "activity-telemetry": NamedScenario("activity-telemetry", "Forward activity minutes before a disconnect watermark.", lambda root: {**{key: value for key, value in _full_scenario(root).items() if key in {"report_schema", "campaign", "status", "checks", "events"}}, "checks": [{"id": "activity-telemetry", "status": "passed", "boundary": "event-mailbox"}]}),
    "map-discovery": NamedScenario("map-discovery", "Forward bounded map geometry for map discovery.", _map_scenario),
    "release-evidence": NamedScenario("release-evidence", "Build a deterministic package suitable for release evidence.", _semantic_release_evidence),
}

# These are the authoritative acceptance scenarios.  Their names are stable
# and intentionally describe behavior, not implementation details.
AUTHORITATIVE_SCENARIOS = ScenarioRegistry()
AUTHORITATIVE_SCENARIOS.register(NamedScenario(
    "registration", "Registration request/response crosses the Agent mailbox.", _authoritative_registration))
AUTHORITATIVE_SCENARIOS.register(NamedScenario(
    "control_plane_backlog", "Control-plane work precedes a bounded event backlog.", _authoritative_control_plane_backlog))
AUTHORITATIVE_SCENARIOS.register(NamedScenario(
    "activity_disconnect", "Activity minutes are committed before disconnect closure.",
    _authoritative_activity_disconnect))
AUTHORITATIVE_SCENARIOS.register(NamedScenario(
    "map_contract", "Map geometry satisfies the authenticated event contract.", _authoritative_map_contract))
AUTHORITATIVE_SCENARIOS.register(NamedScenario(
    "contract_scope", "Contract context selection, scope, persistence, and presentation.",
    _authoritative_contract_scope))
AUTHORITATIVE_SCENARIOS.register(NamedScenario(
    "authority_regression", "Pending authority never passes the active lookup boundary.",
    _authoritative_authority_regression))


def authoritative_manifest():
    """Return the machine-readable contract for the authoritative campaign."""
    return {
        "manifest_schema": "sin.integration/1",
        "campaign": "sin-authoritative-integration",
        "required_scenarios": list(AUTHORITATIVE_SCENARIOS.names()),
        "adapters": [dict(adapter) for adapter in ADAPTER_CONTRACTS],
        "scenario_boundaries": {name: list(AUTHORITATIVE_BOUNDARIES[name])
                                for name in AUTHORITATIVE_SCENARIOS.names()},
    }


def run_named_scenario(name: str = "offline-full", output=None):
    """Execute *name* and optionally write its secret-free JSON report."""
    scenario = (AUTHORITATIVE_SCENARIOS.get(name) if name in AUTHORITATIVE_SCENARIOS.names()
                else SEMANTIC_SCENARIOS.get(name) or SCENARIOS.get(name))
    with tempfile.TemporaryDirectory(prefix="sin-scenario-") as directory:
        report = dict(scenario.execute(Path(directory)))
    report["scenario"] = name
    if name in AUTHORITATIVE_BOUNDARIES:
        report["adapters"] = list(AUTHORITATIVE_BOUNDARIES[name])
    if output is not None:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def run_campaign(output=None):
    """Run the complete semantic campaign and write one aggregate report."""
    report = run_named_scenario("offline-full")
    report["semantic_scenarios"] = []
    for name in SEMANTIC_SCENARIOS:
        scenario_report = run_named_scenario(name)
        report["semantic_scenarios"].append({
            "name": name, "status": scenario_report["status"],
            "checks": scenario_report.get("checks", []),
        })
    report["authoritative_scenarios"] = []
    for name in AUTHORITATIVE_SCENARIOS.names():
        scenario_report = run_named_scenario(name)
        report["authoritative_scenarios"].append({
            "name": name, "status": scenario_report["status"],
            "checks": scenario_report.get("checks", []),
        })
    if output is not None:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
