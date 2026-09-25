"""Staff-approved identity associations and desired/applied farm membership.

Mutating approval methods require a trusted staff caller. Acknowledge requires
an authenticated game adapter; neither boundary is exposed as a public API.
Player self-linking is disabled, including previously issued codes.
"""
import hashlib
import hmac
import logging
import os
import re
import uuid
import secrets
from datetime import datetime, timezone, timedelta
from .world_generation import WorldGenerationRegistry
from pymongo.errors import DuplicateKeyError


ROLES = {"farm_manager", "contractor", "worker", "visitor", "revoked"}


def key(*parts):
    import json
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def require_operator(guild_id, expected_guild_id, role_ids, operator_role_ids):
    if guild_id != expected_guild_id or not set(role_ids).intersection(operator_role_ids):
        raise ValueError("A configured Network Admin role in this Discord server is required")


class AuthorizationManager:
    def __init__(self, database):
        self.database, self.db = database, database.db
        self.community_db = database.db
        self.worlds = WorldGenerationRegistry(database)

    def issue_code(self, discord_id, server_id, save_id):
        raise ValueError("Player self-linking is disabled; submit a farm request for staff approval")

    def verify_code(self, code, authenticated_server_id, save_id, game_player_id):
        raise ValueError("Player self-linking is disabled; old link codes cannot establish identity")

    def request_farm(self, discord_id, server_id, save_id, starting_field):
        application = self.community_db.community_applications.find_one({"_id": str(discord_id)})
        if application is None:
            logging.info("Community membership lookup: no application for Discord member")
        elif application.get("state") == "approved" and application.get("farm_name"):
            logging.info("Community membership lookup: approved application found")
        elif application.get("state") in ("pending", "denied"):
            logging.info("Community membership lookup: application exists with state=%s", application["state"])
        else:
            logging.warning("Community membership lookup: legacy/incompatible application record detected")
        if not application or application.get("state") != "approved" or not application.get("farm_name"):
            application = None
        if not application:
            raise ValueError("You must complete SiN membership approval before requesting a farm")
        if not starting_field.strip() or len(starting_field) > 80:
            raise ValueError("Starting field must contain 1–80 characters")
        farm_name = application["farm_name"]
        world_id = self.worlds.active_id(server_id, save_id)
        request_id = key(server_id, save_id, world_id, str(discord_id)) if world_id else key(server_id, save_id, str(discord_id))
        record = dict(_id=request_id, discord_id=str(discord_id), server_id=server_id, save_id=save_id,
                      farm_name=farm_name.strip(), starting_field=starting_field.strip(), state="requested",
                      created_at=datetime.now(timezone.utc))
        if world_id:
            record["world_id"] = world_id
        old = self.db.farm_requests.find_one({"_id": request_id})
        if old and old.get("state") == "rejected":
            record["created_at"] = old.get("created_at", record["created_at"])
            self.db.farm_requests.replace_one({"_id": request_id}, record, upsert=True)
        else:
            self.db.farm_requests.update_one({"_id": request_id}, {"$setOnInsert": record}, upsert=True)
        return self.db.farm_requests.find_one({"_id": request_id})

    def observe_players(self, server_id, save_id, snapshot):
        """Persist connected trusted UserManager observations without linking them."""
        world_id = snapshot.get("world_id") if isinstance(snapshot, dict) else None
        active = self.worlds.active_id(server_id, save_id)
        if active:
            world_id = self.worlds.require_active(server_id, save_id, world_id)
        now = datetime.now(timezone.utc)
        observed = snapshot.get("observed_users") or [dict(unique_user_id=k, name=v, farm_id=0, connected=True)
                                                       for k, v in snapshot.get("players", {}).items()]
        for item in observed:
            unique_id, player = item.get("unique_user_id"), item
            if isinstance(player, dict):
                display_name, user_id, farm_id, connected = player.get("name", ""), player.get("user_id"), player.get("farm_id", 0), player.get("connected", True)
            else:
                display_name, user_id, farm_id, connected = player, None, 0, True
            query = {"server_id": server_id, "save_id": save_id, "fs25_unique_user_id": unique_id}
            if world_id:
                query["world_id"] = str(world_id)
            values = {"latest_display_name": display_name, "user_id": user_id, "current_farm_id": farm_id,
                      "currently_connected": connected, "last_seen_at": now}
            if world_id:
                values["world_id"] = str(world_id)
            self.db.observed_fs25_identities.update_one(
                query,
                {"$set": values,
                 "$setOnInsert": {"first_seen_at": now}}, upsert=True)

    def _registration_code(self, server_id, save_id, unique_user_id, issued_at):
        secret = os.environ.get("SIN_REGISTRATION_CODE_SECRET") or os.environ.get("MONGODB_URI") or "local-registration-secret"
        material = f"{server_id}|{save_id}|{unique_user_id}|{issued_at}".encode()
        digest = hmac.new(secret.encode(), material, hashlib.sha256).digest()
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(alphabet[value % len(alphabet)] for value in digest[:8])

    def create_registration_code(self, server_id, save_id, unique_user_id, ttl_seconds=900):
        now = datetime.now(timezone.utc)
        scope = {"server_id": server_id, "save_id": save_id,
                 "fs25_unique_user_id": unique_user_id, "state": "pending"}
        active = self.db.registration_codes.find_one(dict(scope, expires_at={"$gt": now}))
        if active and active.get("issued_at") is not None:
            token = self._registration_code(server_id, save_id, unique_user_id, active["issued_at"])
            return token, active["expires_at"]
        issued_at = int(now.timestamp())
        token = self._registration_code(server_id, save_id, unique_user_id, issued_at)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        values = {"token_hash": token_hash, "expires_at": now + timedelta(seconds=ttl_seconds), "updated_at": now}
        pending = self.db.registration_codes.find_one(scope)
        if pending and pending.get("expires_at") is not None and pending["expires_at"] <= now:
            # This is a replacement issuance, not an update to an active
            # pending record. Refresh issued_at here so future status polls
            # derive the same token hash that was stored for the new window.
            values["issued_at"] = issued_at
            self.db.registration_codes.update_one(
                {"_id": pending["_id"], "state": "pending", "expires_at": pending["expires_at"]},
                {"$set": values})
        else:
            self.db.registration_codes.update_one(
                scope,
                {"$set": values,
                 "$setOnInsert": {"_id": key(server_id, save_id, unique_user_id), "issued_at": issued_at}}, upsert=True)
        return token, now + timedelta(seconds=ttl_seconds)

    def _matching_game_identities(self, server_id, save_id, unique_user_id, session=None):
        """Return one de-duplicated set of identity links for a stable game ID.

        Older approved links used ``game_player_id`` while newer records also
        carry ``fs25_unique_user_id``.  Query both fields explicitly so the
        cross-save enrollment path remains compatible with either record shape.
        """
        base = {"server_id": server_id}
        if save_id is not None:
            base["save_id"] = save_id
        matches = []
        seen = set()
        for field in ("fs25_unique_user_id", "game_player_id"):
            query = dict(base, **{field: unique_user_id})
            cursor = self.db.game_identities.find(query, session=session)
            for row in cursor.limit(50):
                if not isinstance(row, dict):
                    continue
                fingerprint = row.get("_id")
                if fingerprint is None:
                    fingerprint = tuple((key, row.get(key)) for key in (
                        "server_id", "save_id", "discord_id", "fs25_unique_user_id", "game_player_id"))
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                matches.append(row)
        return matches

    def _auto_enroll_registration(self, server_id, save_id, unique_user_id):
        """Resolve or create a save-local link from one approved server identity.

        This deliberately creates only a ``game_identities`` record.  Farm,
        membership, authority, land, contract, session, and FS25 economy state
        remain untouched and continue through their existing save/world flows.
        """
        now = datetime.now(timezone.utc)

        def enroll(session):
            target_rows = self._matching_game_identities(
                server_id, save_id, unique_user_id, session=session)
            if len(target_rows) > 1:
                raise ValueError("FS25 identity has conflicting links")
            if target_rows:
                return {"status": "registered", "fs25_unique_user_id": unique_user_id}

            source_rows = self._matching_game_identities(
                server_id, None, unique_user_id, session=session)
            if not source_rows:
                return None

            # Multiple save rows for the same Discord member are one
            # unambiguous identity.  Different members, or an unowned legacy
            # row, must never be guessed through automatic enrollment.
            discord_ids = {str(row.get("discord_id", "")).strip()
                           for row in source_rows}
            if not discord_ids or "" in discord_ids or len(discord_ids) != 1:
                raise ValueError(
                    "FS25 identity has ambiguous existing links; explicit resolution is required")
            discord_id = next(iter(discord_ids))
            application = self.community_db.community_applications.find_one(
                {"_id": discord_id, "state": "approved"}, session=session)
            if not application:
                return None

            # A target-save row for this Discord member with a different game
            # identity is a real conflict, even though it did not match the
            # stable ID query above.
            target_owner_rows = list(self.db.game_identities.find(
                {"server_id": server_id, "save_id": save_id,
                 "discord_id": discord_id}, session=session).limit(2))
            if len(target_owner_rows) > 1:
                raise ValueError("Target save has conflicting identity links")
            if target_owner_rows:
                owner = target_owner_rows[0]
                owner_ids = {owner.get("fs25_unique_user_id"), owner.get("game_player_id")}
                if unique_user_id not in owner_ids:
                    raise ValueError(
                        "Target save already links this Discord member to another FS25 identity")
                return {"status": "registered", "fs25_unique_user_id": unique_user_id}

            identity = {
                "server_id": server_id,
                "save_id": save_id,
                "discord_id": discord_id,
                "fs25_unique_user_id": unique_user_id,
                "game_player_id": unique_user_id,
                "registered_at": now,
                # This is linkage provenance only.  It is intentionally not
                # an approval/authority field and carries no world state.
                "registration_source": "approved_cross_save_auto_enrollment",
            }
            self.db.game_identities.insert_one(identity, session=session)
            return {"status": "registered", "fs25_unique_user_id": unique_user_id}

        try:
            return self.database.atomic(enroll)
        except DuplicateKeyError:
            # A concurrent request may have won the unique target-save index.
            # Re-read after the failed transaction and treat the durable row as
            # the idempotent result; never create a second identity or code.
            target_rows = self._matching_game_identities(server_id, save_id, unique_user_id)
            if len(target_rows) == 1:
                return {"status": "registered", "fs25_unique_user_id": unique_user_id}
            raise

    def registration_request(self, server_id, save_id, unique_user_id, observed_name=None, transient_user_id=None):
        if not isinstance(unique_user_id, str) or not unique_user_id.strip():
            raise ValueError("FS25 uniqueUserId is required")
        linked = self._auto_enroll_registration(server_id, save_id, unique_user_id)
        if linked is not None:
            return linked
        token, expires_at = self.create_registration_code(server_id, save_id, unique_user_id)
        self.db.observed_fs25_identities.update_one(
            {"server_id": server_id, "save_id": save_id, "fs25_unique_user_id": unique_user_id},
            {"$set": {"latest_display_name": str(observed_name or ""),
                      "latest_user_id": str(transient_user_id or ""), "last_seen_at": datetime.now(timezone.utc)}}, upsert=True)
        return {"status": "registration_required", "fs25_unique_user_id": unique_user_id,
                "code": token, "expires_at": expires_at.isoformat()}

    def register_identity(self, discord_id, code, server_id=None, save_id=None):
        normalized = code.strip().upper() if isinstance(code, str) else ""
        if not re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", normalized):
            raise ValueError("That registration code is invalid, expired, or already used")
        token_hash = hashlib.sha256(normalized.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        def complete(session=None):
            query = {"token_hash": token_hash, "state": "pending", "expires_at": {"$gt": now}}
            if server_id is not None: query["server_id"] = server_id
            if save_id is not None: query["save_id"] = save_id
            rows = list(self.db.registration_codes.find(query, session=session).limit(2))
            if len(rows) != 1:
                raise ValueError("That registration code is invalid, expired, or already used")
            registration = rows[0]
            unique_id = registration["fs25_unique_user_id"]
            scoped_server, scoped_save = registration["server_id"], registration["save_id"]
            existing_user = self.db.game_identities.find_one(
                {"server_id": scoped_server, "save_id": scoped_save, "discord_id": str(discord_id)}, session=session)
            existing_id = self.db.game_identities.find_one(
                {"server_id": scoped_server, "save_id": scoped_save, "fs25_unique_user_id": unique_id}, session=session)
            if existing_user and existing_user.get("fs25_unique_user_id") != unique_id:
                raise ValueError("This Discord user already has a different FS25 identity")
            if existing_id and existing_id.get("discord_id") != str(discord_id):
                raise ValueError("This FS25 identity is already linked to another Discord user")
            self.db.game_identities.update_one(
                {"server_id": scoped_server, "save_id": scoped_save, "discord_id": str(discord_id)},
                {"$set": {"fs25_unique_user_id": unique_id, "game_player_id": unique_id, "registered_at": now}},
                upsert=True, session=session)
            result = self.db.registration_codes.update_one({"_id": registration["_id"], "state": "pending"},
                {"$set": {"state": "used", "used_by": str(discord_id), "used_at": now}}, session=session)
            if getattr(result, "modified_count", 1) != 1:
                raise ValueError("That registration code was already used")
            return unique_id
        return self.database.atomic(complete)

    def resolve_player_identity(self, server_id, save_id, unique_user_id):
        """Resolve one trusted game identity and its approved SiN membership."""
        rows = self._matching_game_identities(server_id, save_id, unique_user_id)
        if len(rows) != 1:
            return {"linked": False, "discord_user_id": None, "application_approved": False,
                    "canonical_name": None, "fully_registered": False,
                    "reason": "multiple_game_identity_matches" if len(rows) > 1 else "no_matching_game_identity",
                    "match_count": len(rows)}
        discord_id = str(rows[0].get("discord_id", ""))
        application = self.community_db.community_applications.find_one({"_id": discord_id, "state": "approved"})
        if not application:
            return {"linked": True, "discord_user_id": discord_id, "application_approved": False,
                    "canonical_name": None, "fully_registered": False, "reason": "no_approved_application", "match_count": 1}
        canonical_name = str(application.get("server_nickname") or "").strip()
        if not canonical_name:
            nickname = str(application.get("nickname") or "").strip()
            farm_name = str(application.get("farm_name") or "").strip()
            if nickname and farm_name:
                canonical_name = f"{nickname} | {farm_name}"
            else:
                canonical_name = nickname or farm_name or discord_id
        return {"linked": True, "discord_user_id": discord_id, "application_approved": True,
                "canonical_name": canonical_name, "fully_registered": True,
                "reason": "approved", "match_count": 1}

    def requests(self, server_id, save_id):
        query = dict(server_id=server_id, save_id=save_id, state={"$in": ["requested", "pending"]})
        active = self.worlds.active_id(server_id, save_id)
        if active:
            query["world_id"] = active
        return list(self.db.farm_requests.find(query).sort("created_at", 1).limit(15))

    def pending_request_for_user(self, discord_id, server_id, save_id, world_id=None):
        world_id = world_id or self.worlds.active_id(server_id, save_id)
        query = dict(
            _id=(key(server_id, save_id, world_id, str(discord_id)) if world_id
                 else key(server_id, save_id, str(discord_id))),
            server_id=server_id,
            save_id=save_id,
            state={"$in": ["requested", "pending"]},
        )
        if world_id:
            query["world_id"] = world_id
        request = self.db.farm_requests.find_one(query)
        if not request:
            raise ValueError("That member has no pending farm request for this server")
        return request

    def approve_request(self, request_id, server_id, save_id, farm_id, player_id, snapshot, approved_by, confirmed):
        """Trusted staff boundary: snapshot comes from configured server transport, not Discord input.

        Staff confirms identity in game. Land assignment is a separate durable operation.
        """
        if confirmed is not True or not approved_by:
            raise ValueError("Staff must confirm the player's identity and starting land in game")
        if snapshot.get("source") != "game" or player_id not in snapshot.get("players", {}):
            raise ValueError("Select a player identity from the fresh game roster")
        if not snapshot.get("farms", {}).get(farm_id, "").strip():
            raise ValueError("Select a named farm from the fresh game snapshot")
        active_world = self.worlds.active_id(server_id, save_id)
        world_id = snapshot.get("world_id")
        if active_world:
            world_id = self.worlds.require_active(server_id, save_id, world_id)
        elif world_id:
            raise ValueError("FS25 world generation is not current for this server/save")

        def approve(session):
            request_query = dict(_id=request_id, server_id=server_id, save_id=save_id)
            if world_id:
                request_query["world_id"] = world_id
            request = self.db.farm_requests.find_one(request_query, session=session)
            if not request:
                raise ValueError("Unknown request for this server/save")
            try:
                farmland_id = int(str(request["starting_field"]).strip())
            except (KeyError, TypeError, ValueError):
                raise ValueError("Starting field must be a numeric FS25 farmland ID") from None
            if request["state"] != "requested":
                if request["state"] == "land_pending" and request.get("operation_id"):
                    return request["operation_id"]
                raise ValueError("Request is already reviewed; use farm_status or staff records")
            if request["farm_name"] != snapshot["farms"][farm_id]:
                raise ValueError("Created farm name must match the requested farm name")
            scope = dict(server_id=server_id, save_id=save_id, discord_id=request["discord_id"])
            existing = self.db.game_identities.find_one(scope, session=session)
            if existing and existing.get("game_player_id") != player_id:
                raise ValueError("Existing identity requires operator reconciliation")
            if existing and not existing.get("approved_by") \
                    and existing.get("registration_source") != "approved_cross_save_auto_enrollment":
                raise ValueError("Existing identity requires operator reconciliation")
            claimed = self.db.game_identities.find_one({"server_id": server_id, "save_id": save_id,
                                                        "game_player_id": player_id}, session=session)
            if claimed and claimed.get("discord_id") is not None and claimed.get("discord_id") != request["discord_id"]:
                raise ValueError("That observed player identity is already associated with another user; reconciliation is required")
            if not existing:
                self.db.game_identities.insert_one(dict(**scope, game_player_id=player_id,
                    approved_by=str(approved_by), approved_at=datetime.now(timezone.utc),
                    observation_session=snapshot["session"], observation_sequence=snapshot["sequence"]), session=session)
            elif not existing.get("approved_by"):
                # Cross-save auto-enrollment proves only the member's stable
                # identity linkage.  Staff farm approval remains the separate
                # authority boundary and adds the normal approval evidence.
                self.db.game_identities.update_one(
                    scope,
                    {"$set": {"approved_by": str(approved_by),
                              "approved_at": datetime.now(timezone.utc),
                              "observation_session": snapshot["session"],
                              "observation_sequence": snapshot["sequence"]}},
                    session=session)
            operation = request.get("operation_id") or str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            land_values = dict(
                _id=operation, operation_id=operation, request_id=request_id,
                discord_id=request["discord_id"], server_id=server_id, save_id=save_id,
                farm_id=farm_id, farmland_id=farmland_id, state="pending",
                created_at=now, updated_at=now, attempts=0)
            if world_id:
                land_values["world_id"] = world_id
            self.db.land_operations.update_one({"_id": operation}, {"$setOnInsert": land_values}, upsert=True, session=session)
            self.db.farm_requests.update_one({"_id": request_id}, {"$set": dict(state="land_pending", farm_id=farm_id,
                game_player_id=player_id, approved_by=str(approved_by), approved_at=datetime.now(timezone.utc),
                operation_id=operation, land_confirmed=False)}, session=session)
            return operation
        return self.database.atomic(approve)

    def acknowledge_land(self, operation_id, server_id, save_id, farmland_id, farm_id, owner_farm_id, success, details,
                         world_id=None):
        if not details or not success or int(owner_farm_id) != int(farm_id):
            raise ValueError("Land acknowledgement does not prove requested ownership")

        active_world = self.worlds.active_id(server_id, save_id)
        if active_world:
            world_id = self.worlds.require_active(server_id, save_id, world_id)
        elif world_id:
            raise ValueError("FS25 world generation is not current for this server/save")

        def acknowledge(session):
            op_query = {"_id": operation_id, "server_id": server_id, "save_id": save_id}
            if world_id:
                op_query["world_id"] = world_id
            op = self.db.land_operations.find_one(op_query, session=session)
            if not op or int(op["farmland_id"]) != int(farmland_id) or int(op["farm_id"]) != int(farm_id):
                raise ValueError("Unknown land operation or mismatched server/save/farm/field")
            if op["state"] == "succeeded":
                return "succeeded"
            request_query = {"_id": op["request_id"], "server_id": server_id,
                             "save_id": save_id, "operation_id": operation_id}
            if world_id:
                request_query["world_id"] = world_id
            request = self.db.farm_requests.find_one(request_query, session=session)
            if not request or request["state"] != "land_pending":
                raise ValueError("Land operation is not associated with a pending farm request")
            now = datetime.now(timezone.utc)
            self.db.land_operations.update_one({"_id": operation_id, "state": {"$in": ["pending", "dispatched"]}},
                {"$set": {"state": "succeeded", "acknowledgement": details,
                          "owner_farm_id": int(owner_farm_id), "updated_at": now}}, session=session)
            permission_operation = self.assign(request["discord_id"], server_id, save_id, int(farm_id),
                "farm_manager", {int(farm_id): request["farm_name"]}, request.get("approved_by") or "land-ack",
                session=session, world_id=world_id)
            self.db.farm_requests.update_one({"_id": request["_id"], "state": "land_pending"},
                {"$set": {"state": "approved", "land_confirmed": True, "land_acknowledged_at": now,
                          "permission_operation_id": permission_operation}}, session=session)
            return "succeeded"
        return self.database.atomic(acknowledge)

    def reject_request(self, request_id, server_id, save_id, approved_by, reason):
        if not approved_by or not reason.strip() or len(reason) > 300:
            raise ValueError("A staff reviewer and reason (1–300 characters) are required")
        query = dict(_id=request_id, server_id=server_id, save_id=save_id, state="requested")
        active_world = self.worlds.active_id(server_id, save_id)
        if active_world:
            query["world_id"] = active_world
        result = self.db.farm_requests.update_one(query,
            {"$set": dict(state="rejected", reviewed_by=str(approved_by), reason=reason.strip(), reviewed_at=datetime.now(timezone.utc))})
        if result.modified_count != 1:
            raise ValueError("Request is unknown or already reviewed")

    def assign(self, discord_id, server_id, save_id, farm_id, role, farms, approved_by,
               session=None, allow_unapproved_identity=False, idempotent=False, world_id=None,
               source_farm_id=None, source_farm_name=None):
        """Called only after the Discord/operator boundary authorizes approved_by."""
        if role not in ROLES or type(farm_id) is not int or farm_id <= 0 or not approved_by:
            raise ValueError("Invalid farm, role, or approver")
        if farm_id not in farms:
            raise ValueError("Farm is absent from the server snapshot")
        if role == "contractor":
            if type(source_farm_id) is not int or source_farm_id <= 0 or source_farm_id == farm_id:
                raise ValueError("A contractor relationship requires a distinct positive source farm")
            if source_farm_id not in farms:
                raise ValueError("Source farm is absent from the server snapshot")
        user = str(discord_id)
        relationship_id = key(server_id, save_id, world_id or "legacy", user,
                               source_farm_id if role == "contractor" else "manager", farm_id)
        operation_id = str(uuid.uuid4())

        def assign(session):
            identity = self.db.game_identities.find_one(dict(server_id=server_id, save_id=save_id, discord_id=user), session=session)
            if not identity:
                raise ValueError("A registered game identity is required before manager authority can be assigned")
            if not allow_unapproved_identity and not identity.get("approved_by"):
                raise ValueError("Staff must approve the player's game identity through a farm request first")
            if allow_unapproved_identity and not (identity.get("fs25_unique_user_id") or identity.get("game_player_id")):
                raise ValueError("A registered game identity is required before manager authority can be assigned")
            relationship_query = {"server_id": server_id, "save_id": save_id,
                                  "discord_id": user, "farm_id": farm_id}
            if role == "contractor":
                relationship_query["source_farm_id"] = source_farm_id
            if world_id:
                relationship_query["world_id"] = str(world_id)
            old = self.db.memberships.find_one(relationship_query, session=session)
            # Read the pre-v0.1.25 personal-membership key during migration so
            # a manager relationship is repaired in place instead of being
            # duplicated. New relationships are keyed by farm as well as user,
            # allowing personal and shared-farm authority to coexist.
            if old is None:
                legacy = self.db.memberships.find_one(
                    {"_id": key(server_id, save_id, user)}, session=session)
                if isinstance(legacy, dict) and int(legacy.get("farm_id", 0)) == farm_id:
                    old = legacy
            membership_id = old.get("_id", relationship_id) if isinstance(old, dict) else relationship_id
            if idempotent and old and old.get("farm_id") == farm_id and old.get("desired_role") == role:
                if old.get("state") == "pending":
                    existing_operation_id = old.get("operation_id")
                    if not existing_operation_id:
                        raise ValueError("Existing manager membership requires reconciliation")
                    existing_job = self.db.permission_jobs.find_one(
                        {"_id": existing_operation_id, "membership_id": membership_id}, session=session)
                    if not existing_job:
                        self.db.permission_jobs.update_one(
                            {"_id": existing_operation_id},
                            {"$setOnInsert": dict(
                                _id=existing_operation_id, membership_id=membership_id,
                                server_id=server_id, save_id=save_id,
                                game_player_id=identity["game_player_id"], farm_id=farm_id,
                                role=role, revision=old["revision"], state="pending",
                                approved_by=str(approved_by), created_at=datetime.now(timezone.utc),
                                **({"world_id": str(world_id)} if world_id else {}))},
                            upsert=True, session=session)
                    return existing_operation_id
                if old.get("state") == "active" and old.get("applied_role") == role:
                    return old.get("operation_id")
            if old and old["state"] == "pending":
                raise ValueError("Reconcile the pending permission operation first")
            if old and old["farm_id"] != farm_id:
                raise ValueError("Farm migration requires operator reconciliation; it is not yet supported")
            revision = old["revision"] + 1 if old else 1
            record = dict(_id=membership_id, discord_id=user, server_id=server_id, save_id=save_id,
                          game_player_id=identity["game_player_id"], farm_id=farm_id,
                          farm_name=farms[farm_id], desired_role=role, applied_role=old.get("applied_role") if old else None,
                          revision=revision, state="pending", operation_id=operation_id, approved_by=str(approved_by))
            if role == "contractor":
                record["source_farm_id"] = source_farm_id
                record["source_farm_name"] = source_farm_name or farms.get(source_farm_id, "")
            if world_id:
                record["world_id"] = str(world_id)
            self.db.memberships.replace_one({"_id": membership_id}, record, upsert=True, session=session)
            job = dict(_id=operation_id, membership_id=membership_id,
                server_id=server_id, save_id=save_id, game_player_id=identity["game_player_id"],
                farm_id=farm_id, role=role, revision=revision, state="pending",
                approved_by=str(approved_by), created_at=datetime.now(timezone.utc))
            if role == "contractor":
                job["source_farm_id"] = source_farm_id
            if world_id:
                job["world_id"] = str(world_id)
            self.db.permission_jobs.insert_one(job, session=session)
            return operation_id
        return assign(session) if session is not None else self.database.atomic(assign)

    def revoke_contractor(self, discord_id, server_id, save_id, farm_id, approved_by,
                          session=None, idempotent=True, world_id=None, source_farm_id=None):
        """Durably remove a previously derived shared-farm contractor grant.

        This is deliberately narrower than a general farm-role editor.  The
        shared-authority policy may revoke only its own contractor relationship;
        it cannot demote a personal farm manager or silently repurpose another
        membership.  The FS25 command remains receipt-gated just like a grant.
        """
        if type(farm_id) is not int or farm_id <= 0 or not approved_by:
            raise ValueError("Invalid farm or revocation approver")
        if source_farm_id is not None and (type(source_farm_id) is not int or source_farm_id <= 0
                                           or source_farm_id == farm_id):
            raise ValueError("Invalid contractor source farm")
        user = str(discord_id)

        def revoke(session):
            relationship_query = {
                "server_id": server_id, "save_id": save_id,
                "discord_id": user, "farm_id": farm_id}
            if source_farm_id is not None:
                relationship_query["source_farm_id"] = source_farm_id
            if world_id:
                relationship_query["world_id"] = str(world_id)
            relationship = self.db.memberships.find_one(relationship_query, session=session)
            legacy_cleanup = False
            if relationship is None and source_farm_id is not None:
                # Pre-native-contractor records carried only the target farm.
                # They are eligible for one bounded cleanup only when the
                # current authoritative observation supplies the source farm.
                legacy_query = {"server_id": server_id, "save_id": save_id,
                                "discord_id": user, "farm_id": farm_id,
                                "source_farm_id": {"$exists": False}}
                if world_id:
                    legacy_query["world_id"] = str(world_id)
                legacy_rows = list(self.db.memberships.find(legacy_query, session=session).limit(2))
                if len(legacy_rows) > 1:
                    raise ValueError("Multiple legacy contractor relationships require reconciliation")
                if legacy_rows:
                    relationship = legacy_rows[0]
                    legacy_cleanup = True
            if not relationship:
                return None
            if relationship.get("desired_role") not in {"contractor", "revoked"} \
                    or relationship.get("applied_role") not in {"contractor", "revoked", None}:
                raise ValueError("Only a shared contractor relationship can be revoked")
            membership_id = relationship.get("_id") or key(server_id, save_id, user, farm_id)
            if relationship.get("state") == "pending":
                # A durable grant may already be in the Agent mailbox.  Do not
                # overwrite its revision: let its exact receipt settle, then
                # the next reconciliation poll will issue the revocation.
                return relationship.get("operation_id") if idempotent else None
            if relationship.get("state") == "revoked" and relationship.get("desired_role") == "revoked":
                return relationship.get("operation_id") if idempotent else None
            game_player_id = relationship.get("game_player_id")
            identity = self.db.game_identities.find_one(
                {"server_id": server_id, "save_id": save_id, "discord_id": user}, session=session)
            if identity and identity.get("game_player_id"):
                game_player_id = identity["game_player_id"]
            if not game_player_id:
                raise ValueError("A registered game identity is required before contractor authority can be revoked")
            operation_id = str(uuid.uuid4())
            revision = int(relationship.get("revision", 0)) + 1
            record = dict(relationship, _id=membership_id, discord_id=user,
                          server_id=server_id, save_id=save_id, game_player_id=game_player_id,
                          farm_id=farm_id, desired_role="revoked", revision=revision,
                          state="pending", operation_id=operation_id,
                          approved_by=str(approved_by))
            if source_farm_id is not None:
                record["source_farm_id"] = source_farm_id
            if legacy_cleanup:
                self.db.permission_jobs.update_many(
                    {"membership_id": membership_id, "role": "contractor",
                     "source_farm_id": {"$exists": False},
                     "state": {"$in": ["pending", "dispatched"]}},
                    {"$set": {"state": "reconciliation_required",
                              "reconciliation_reason": "legacy contractor job has no source farm",
                              "updated_at": datetime.now(timezone.utc)}}, session=session)
                record["legacy_cleanup"] = True
            if world_id:
                record["world_id"] = str(world_id)
            self.db.memberships.replace_one({"_id": membership_id}, record, upsert=True, session=session)
            job = dict(
                _id=operation_id, membership_id=membership_id, server_id=server_id,
                save_id=save_id, game_player_id=game_player_id, farm_id=farm_id,
                role="revoked", revision=revision, state="pending",
                approved_by=str(approved_by), created_at=datetime.now(timezone.utc))
            if source_farm_id is not None:
                job["source_farm_id"] = source_farm_id
            if legacy_cleanup:
                job["legacy_cleanup"] = True
            if world_id:
                job["world_id"] = str(world_id)
            self.db.permission_jobs.insert_one(job, session=session)
            return operation_id
        return revoke(session) if session is not None else self.database.atomic(revoke)

    @staticmethod
    def _receipt_bool(receipt, key):
        value = receipt.get(key)
        return value is True or str(value).strip().lower() in {"true", "1", "yes"}

    def _validate_permission_receipt(self, job, receipt):
        """Validate structured authoritative FS25 read-back before applying a job."""
        if not isinstance(receipt, dict):
            raise ValueError("A structured permission receipt is required")
        if receipt.get("operation_id") != job.get("_id"):
            raise ValueError("Permission receipt does not match the queued operation")
        if receipt.get("status") not in {"applied", "already_applied"}:
            raise ValueError("Permission receipt is not an authoritative success")
        if not str(receipt.get("receipt") or "").strip():
            raise ValueError("Permission receipt explanation is required")
        if job.get("role") == "contractor":
            try:
                source = int(receipt.get("source_farm_id"))
                target = int(receipt.get("target_farm_id"))
                expected_source = int(job.get("source_farm_id"))
                expected_target = int(job.get("farm_id"))
            except (TypeError, ValueError):
                raise ValueError("Contractor receipt must include source and target farm read-back") from None
            if (source, target) != (expected_source, expected_target):
                raise ValueError("Contractor receipt farm relationship does not match the queued operation")
            contracting = self._receipt_bool(receipt, "contracting_for")
            expected = job.get("role") == "contractor"
            if contracting != expected or not self._receipt_bool(receipt, "authoritative_readback"):
                raise ValueError("Contractor receipt lacks authoritative relationship success evidence")
        elif job.get("role") == "revoked":
            try:
                source = int(receipt.get("source_farm_id"))
                target = int(receipt.get("target_farm_id"))
                expected_source = int(job.get("source_farm_id"))
                expected_target = int(job.get("farm_id"))
            except (TypeError, ValueError):
                raise ValueError("Contractor revocation receipt must include source and target farm read-back") from None
            if (source, target) != (expected_source, expected_target) \
                    or self._receipt_bool(receipt, "contracting_for") \
                    or not self._receipt_bool(receipt, "authoritative_readback"):
                raise ValueError("Contractor revocation lacks authoritative relationship success evidence")
        elif job.get("role") == "farm_manager":
            try:
                target = int(receipt.get("farm_id"))
                current = int(receipt.get("current_farm_id"))
            except (TypeError, ValueError):
                raise ValueError("Manager receipt must include current farm read-back") from None
            if target != int(job.get("farm_id")) or current != target \
                    or not self._receipt_bool(receipt, "manager") \
                    or not self._receipt_bool(receipt, "authoritative_readback"):
                raise ValueError("Manager receipt lacks authoritative success evidence")
        else:
            # Legacy worker/visitor roles have no native adapter in this
            # runtime.  Keep the generic boundary receipt-gated and require a
            # role-specific authoritative read-back before they can ever be
            # committed; a manager receipt must never satisfy another role.
            if receipt.get("role") != job.get("role") \
                    or not self._receipt_bool(receipt, "permission_applied") \
                    or not self._receipt_bool(receipt, "authoritative_readback"):
                raise ValueError("Permission receipt lacks role-specific authoritative success evidence")

    def _notify_manager_applied(self, operation_id):
        """Create one durable staff notification after manager read-back."""
        job = self.db.permission_jobs.find_one({"_id": operation_id})
        if not isinstance(job, dict) or job.get("role") != "farm_manager":
            return
        request = self.db.farm_requests.find_one({"permission_operation_id": operation_id})
        if not isinstance(request, dict):
            return
        server_key = str(job.get("server_id") or request.get("server_key") or "")
        save_key = str(job.get("save_id") or request.get("save_key") or "")
        world_id = str(job.get("world_id") or request.get("world_id") or "") or None
        farm_id = job.get("farm_id") or request.get("farm_id")
        farm_name = request.get("farm_name") or "Unnamed farm"
        message = (f"✅ Farm setup completed: {farm_name} (FS25 Farm {farm_id}) "
                   f"now has manager authority for member {request.get('discord_id')}. "
                   f"Server {server_key}, save {save_key}. "
                   "Financial provisioning remains pending until the FS25 money/loan capability is live-verified.")
        try:
            from .activity import ActivityOutbox
            ActivityOutbox(self.database).enqueue_staff(
                f"farm-manager-applied:{operation_id}", server_key, message,
                save_key=save_key, world_id=world_id)
        except Exception:
            # Notification failure must never roll back an authoritative
            # permission receipt. The durable farm/request state is primary.
            logging.exception("staff farm completion notification could not be queued operation=%s", operation_id)

    def acknowledge(self, operation_id, authenticated_server_id, save_id, revision, receipt, world_id=None):
        """Only after the mod confirms the exact job was applied and persisted."""

        def acknowledge(session):
            query = {"_id": operation_id, "server_id": authenticated_server_id,
                     "save_id": save_id, "revision": revision}
            if world_id:
                query["world_id"] = str(world_id)
            job = self.db.permission_jobs.find_one(query, session=session)
            if not job:
                raise ValueError("Unknown operation or wrong server/save/revision")
            try:
                self._validate_permission_receipt(job, receipt)
            except ValueError:
                if job.get("state") in {"pending", "dispatched"}:
                    now = datetime.now(timezone.utc)
                    self.db.permission_jobs.update_one({"_id": operation_id,
                        "state": {"$in": ["pending", "dispatched"]}},
                        {"$set": {"state": "reconciliation_required", "receipt": receipt,
                                  "updated_at": now}}, session=session)
                    self.db.memberships.update_one({"_id": job["membership_id"],
                        "operation_id": operation_id, "revision": revision,
                        "state": {"$in": ["pending", "active"]}},
                        {"$set": {"state": "reconciliation_required", "receipt": receipt,
                                  "updated_at": now}}, session=session)
                raise
            if job["state"] == "applied":
                return "applied"
            result = self.db.memberships.update_one({"_id": job["membership_id"], "operation_id": operation_id,
                "revision": revision, "state": "pending"}, {"$set": {"applied_role": job["role"],
                "state": "revoked" if job["role"] == "revoked" else "active"}}, session=session)
            if result.modified_count != 1:
                raise ValueError("Stale permission operation")
            self.db.permission_jobs.update_one({"_id": operation_id}, {"$set": {"state": "applied", "receipt": receipt}}, session=session)
            # FarmLifecycle owns the request transition after the persisted
            # manager permission receipt.  Keep this hook optional so the
            # authorization manager remains usable by existing callers.
            request = self.db.farm_requests.find_one({"permission_operation_id": operation_id}, session=session)
            if request and request.get("state") == "awaiting_manager":
                now = datetime.now(timezone.utc)
                self.db.farm_requests.update_one({"_id": request["_id"], "state": "awaiting_manager"},
                    {"$set": {"state": "active", "activated_at": now, "updated_at": now}}, session=session)
                if request.get("mapping_id"):
                    self.db.sin_farms.update_one({"_id": request["mapping_id"]}, {"$set": {
                        "state": "active", "owner_discord_id": request.get("discord_id"),
                        "activated_at": now, "updated_at": now}}, session=session)
            return "applied"
        result = self.database.atomic(acknowledge)
        if result == "applied":
            self._notify_manager_applied(operation_id)
        return result

    def status(self, discord_id, server_id, save_id, world_id=None):
        active = self.worlds.active_id(server_id, save_id)
        query = {"discord_id": str(discord_id), "server_id": server_id, "save_id": save_id}
        if active:
            if not world_id:
                return None
            query["world_id"] = self.worlds.require_active(server_id, save_id, world_id)
        elif world_id:
            raise ValueError("FS25 world generation is not current for this server/save")
        return self.db.memberships.find_one(query)
