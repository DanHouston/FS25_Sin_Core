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
        request_id = key(server_id, save_id, str(discord_id))
        record = dict(_id=request_id, discord_id=str(discord_id), server_id=server_id, save_id=save_id,
                      farm_name=farm_name.strip(), starting_field=starting_field.strip(), state="requested",
                      created_at=datetime.now(timezone.utc))
        old = self.db.farm_requests.find_one({"_id": request_id})
        if old and old.get("state") == "rejected":
            record["created_at"] = old.get("created_at", record["created_at"])
            self.db.farm_requests.replace_one({"_id": request_id}, record, upsert=True)
        else:
            self.db.farm_requests.update_one({"_id": request_id}, {"$setOnInsert": record}, upsert=True)
        return self.db.farm_requests.find_one({"_id": request_id})

    def observe_players(self, server_id, save_id, snapshot):
        """Persist connected trusted UserManager observations without linking them."""
        now = datetime.now(timezone.utc)
        observed = snapshot.get("observed_users") or [dict(unique_user_id=k, name=v, farm_id=0, connected=True)
                                                       for k, v in snapshot.get("players", {}).items()]
        for item in observed:
            unique_id, player = item.get("unique_user_id"), item
            if isinstance(player, dict):
                display_name, user_id, farm_id, connected = player.get("name", ""), player.get("user_id"), player.get("farm_id", 0), player.get("connected", True)
            else:
                display_name, user_id, farm_id, connected = player, None, 0, True
            self.db.observed_fs25_identities.update_one(
                {"server_id": server_id, "save_id": save_id, "fs25_unique_user_id": unique_id},
                {"$set": {"latest_display_name": display_name, "user_id": user_id, "current_farm_id": farm_id,
                          "currently_connected": connected, "last_seen_at": now},
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

    def registration_request(self, server_id, save_id, unique_user_id, observed_name=None, transient_user_id=None):
        if not isinstance(unique_user_id, str) or not unique_user_id.strip():
            raise ValueError("FS25 uniqueUserId is required")
        rows = list(self.db.game_identities.find({"server_id": server_id, "save_id": save_id,
            "$or": [{"fs25_unique_user_id": unique_user_id}, {"game_player_id": unique_user_id}]}).limit(2))
        if len(rows) > 1:
            raise ValueError("FS25 identity has conflicting links")
        if rows:
            return {"status": "registered", "fs25_unique_user_id": unique_user_id}
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
        rows = list(self.db.game_identities.find({"server_id": server_id, "save_id": save_id,
            "$or": [{"fs25_unique_user_id": unique_user_id}, {"game_player_id": unique_user_id}]}).limit(2))
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
        return {"linked": True, "discord_user_id": discord_id, "application_approved": True,
                "canonical_name": application.get("server_nickname"), "fully_registered": True,
                "reason": "approved", "match_count": 1}

    def requests(self, server_id, save_id):
        return list(self.db.farm_requests.find(dict(server_id=server_id, save_id=save_id,
            state={"$in": ["requested", "pending"]})).sort("created_at", 1).limit(15))

    def pending_request_for_user(self, discord_id, server_id, save_id):
        request = self.db.farm_requests.find_one(dict(
            _id=key(server_id, save_id, str(discord_id)),
            server_id=server_id,
            save_id=save_id,
            state={"$in": ["requested", "pending"]},
        ))
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

        def approve(session):
            request = self.db.farm_requests.find_one(dict(_id=request_id, server_id=server_id, save_id=save_id), session=session)
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
            if existing and (existing["game_player_id"] != player_id or not existing.get("approved_by")):
                raise ValueError("Existing identity requires operator reconciliation")
            claimed = self.db.game_identities.find_one({"server_id": server_id, "save_id": save_id,
                                                        "game_player_id": player_id}, session=session)
            if claimed and claimed.get("discord_id") is not None and claimed.get("discord_id") != request["discord_id"]:
                raise ValueError("That observed player identity is already associated with another user; reconciliation is required")
            if not existing:
                self.db.game_identities.insert_one(dict(**scope, game_player_id=player_id,
                    approved_by=str(approved_by), approved_at=datetime.now(timezone.utc),
                    observation_session=snapshot["session"], observation_sequence=snapshot["sequence"]), session=session)
            operation = request.get("operation_id") or str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            self.db.land_operations.update_one({"_id": operation}, {"$setOnInsert": dict(
                _id=operation, operation_id=operation, request_id=request_id,
                discord_id=request["discord_id"], server_id=server_id, save_id=save_id,
                farm_id=farm_id, farmland_id=farmland_id, state="pending",
                created_at=now, updated_at=now, attempts=0)}, upsert=True, session=session)
            self.db.farm_requests.update_one({"_id": request_id}, {"$set": dict(state="land_pending", farm_id=farm_id,
                game_player_id=player_id, approved_by=str(approved_by), approved_at=datetime.now(timezone.utc),
                operation_id=operation, land_confirmed=False)}, session=session)
            return operation
        return self.database.atomic(approve)

    def acknowledge_land(self, operation_id, server_id, save_id, farmland_id, farm_id, owner_farm_id, success, details):
        if not details or not success or int(owner_farm_id) != int(farm_id):
            raise ValueError("Land acknowledgement does not prove requested ownership")

        def acknowledge(session):
            op = self.db.land_operations.find_one({"_id": operation_id, "server_id": server_id,
                "save_id": save_id}, session=session)
            if not op or int(op["farmland_id"]) != int(farmland_id) or int(op["farm_id"]) != int(farm_id):
                raise ValueError("Unknown land operation or mismatched server/save/farm/field")
            if op["state"] == "succeeded":
                return "succeeded"
            request = self.db.farm_requests.find_one({"_id": op["request_id"], "server_id": server_id,
                "save_id": save_id, "operation_id": operation_id}, session=session)
            if not request or request["state"] != "land_pending":
                raise ValueError("Land operation is not associated with a pending farm request")
            now = datetime.now(timezone.utc)
            self.db.land_operations.update_one({"_id": operation_id, "state": {"$in": ["pending", "dispatched"]}},
                {"$set": {"state": "succeeded", "acknowledgement": details,
                          "owner_farm_id": int(owner_farm_id), "updated_at": now}}, session=session)
            permission_operation = self.assign(request["discord_id"], server_id, save_id, int(farm_id),
                "farm_manager", {int(farm_id): request["farm_name"]}, request.get("approved_by") or "land-ack", session=session)
            self.db.farm_requests.update_one({"_id": request["_id"], "state": "land_pending"},
                {"$set": {"state": "approved", "land_confirmed": True, "land_acknowledged_at": now,
                          "permission_operation_id": permission_operation}}, session=session)
            return "succeeded"
        return self.database.atomic(acknowledge)

    def reject_request(self, request_id, server_id, save_id, approved_by, reason):
        if not approved_by or not reason.strip() or len(reason) > 300:
            raise ValueError("A staff reviewer and reason (1–300 characters) are required")
        result = self.db.farm_requests.update_one(dict(_id=request_id, server_id=server_id, save_id=save_id, state="requested"),
            {"$set": dict(state="rejected", reviewed_by=str(approved_by), reason=reason.strip(), reviewed_at=datetime.now(timezone.utc))})
        if result.modified_count != 1:
            raise ValueError("Request is unknown or already reviewed")

    def assign(self, discord_id, server_id, save_id, farm_id, role, farms, approved_by,
               session=None, allow_unapproved_identity=False, idempotent=False, world_id=None):
        """Called only after the Discord/operator boundary authorizes approved_by."""
        if role not in ROLES or type(farm_id) is not int or farm_id <= 0 or not approved_by:
            raise ValueError("Invalid farm, role, or approver")
        if farm_id not in farms:
            raise ValueError("Farm is absent from the server snapshot")
        user = str(discord_id)
        relationship_id = key(server_id, save_id, world_id or "legacy", user, farm_id)
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
            if world_id:
                record["world_id"] = str(world_id)
            self.db.memberships.replace_one({"_id": membership_id}, record, upsert=True, session=session)
            job = dict(_id=operation_id, membership_id=membership_id,
                server_id=server_id, save_id=save_id, game_player_id=identity["game_player_id"],
                farm_id=farm_id, role=role, revision=revision, state="pending",
                approved_by=str(approved_by), created_at=datetime.now(timezone.utc))
            if world_id:
                job["world_id"] = str(world_id)
            self.db.permission_jobs.insert_one(job, session=session)
            return operation_id
        return assign(session) if session is not None else self.database.atomic(assign)

    def revoke_contractor(self, discord_id, server_id, save_id, farm_id, approved_by,
                          session=None, idempotent=True, world_id=None):
        """Durably remove a previously derived shared-farm contractor grant.

        This is deliberately narrower than a general farm-role editor.  The
        shared-authority policy may revoke only its own contractor relationship;
        it cannot demote a personal farm manager or silently repurpose another
        membership.  The FS25 command remains receipt-gated just like a grant.
        """
        if type(farm_id) is not int or farm_id <= 0 or not approved_by:
            raise ValueError("Invalid farm or revocation approver")
        user = str(discord_id)

        def revoke(session):
            relationship_query = {
                "server_id": server_id, "save_id": save_id,
                "discord_id": user, "farm_id": farm_id}
            if world_id:
                relationship_query["world_id"] = str(world_id)
            relationship = self.db.memberships.find_one(relationship_query, session=session)
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
            if world_id:
                record["world_id"] = str(world_id)
            self.db.memberships.replace_one({"_id": membership_id}, record, upsert=True, session=session)
            job = dict(
                _id=operation_id, membership_id=membership_id, server_id=server_id,
                save_id=save_id, game_player_id=game_player_id, farm_id=farm_id,
                role="revoked", revision=revision, state="pending",
                approved_by=str(approved_by), created_at=datetime.now(timezone.utc))
            if world_id:
                job["world_id"] = str(world_id)
            self.db.permission_jobs.insert_one(job, session=session)
            return operation_id
        return revoke(session) if session is not None else self.database.atomic(revoke)

    def acknowledge(self, operation_id, authenticated_server_id, save_id, revision, receipt, world_id=None):
        """Only after the mod confirms the exact job was applied and persisted."""
        if not receipt:
            raise ValueError("A durable mod receipt is required")

        def acknowledge(session):
            query = {"_id": operation_id, "server_id": authenticated_server_id,
                     "save_id": save_id, "revision": revision}
            if world_id:
                query["world_id"] = str(world_id)
            job = self.db.permission_jobs.find_one(query, session=session)
            if not job:
                raise ValueError("Unknown operation or wrong server/save/revision")
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
        return self.database.atomic(acknowledge)

    def status(self, discord_id, server_id, save_id):
        return self.db.memberships.find_one({"_id": key(server_id, save_id, str(discord_id))})
