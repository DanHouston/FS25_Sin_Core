"""Staff-approved identity associations and desired/applied farm membership.

Mutating approval methods require a trusted staff caller. Acknowledge requires
an authenticated game adapter; neither boundary is exposed as a public API.
Player self-linking is disabled, including previously issued codes.
"""
import hashlib
import uuid
from datetime import datetime, timezone


ROLES = {"farm_manager", "worker", "visitor", "revoked"}


def key(*parts):
    import json
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def require_operator(guild_id, expected_guild_id, role_ids, operator_role_ids):
    if guild_id != expected_guild_id or not set(role_ids).intersection(operator_role_ids):
        raise ValueError("A configured Network Admin role in this Discord server is required")


class AuthorizationManager:
    def __init__(self, database):
        self.database, self.db = database, database.db

    def issue_code(self, discord_id, server_id, save_id):
        raise ValueError("Player self-linking is disabled; submit a farm request for staff approval")

    def verify_code(self, code, authenticated_server_id, save_id, game_player_id):
        raise ValueError("Player self-linking is disabled; old link codes cannot establish identity")

    def request_farm(self, discord_id, server_id, save_id, farm_name, starting_field):
        if not farm_name.strip() or len(farm_name) > 80 or not starting_field.strip() or len(starting_field) > 80:
            raise ValueError("Farm name and starting field must each contain 1–80 characters")
        request_id = key(server_id, save_id, str(discord_id))
        record = dict(_id=request_id, discord_id=str(discord_id), server_id=server_id, save_id=save_id,
                      farm_name=farm_name.strip(), starting_field=starting_field.strip(), state="requested",
                      created_at=datetime.now(timezone.utc))
        self.db.farm_requests.update_one({"_id": request_id}, {"$setOnInsert": record}, upsert=True)
        return self.db.farm_requests.find_one({"_id": request_id})

    def requests(self, server_id, save_id):
        return list(self.db.farm_requests.find(dict(server_id=server_id, save_id=save_id, state="requested")).sort("created_at", 1).limit(15))

    def pending_request_for_user(self, discord_id, server_id, save_id):
        request = self.db.farm_requests.find_one(dict(
            _id=key(server_id, save_id, str(discord_id)),
            server_id=server_id,
            save_id=save_id,
            state="requested",
        ))
        if not request:
            raise ValueError("That member has no pending farm request for this server")
        return request

    def approve_request(self, request_id, server_id, save_id, farm_id, player_id, snapshot, approved_by, confirmed):
        """Trusted staff boundary: snapshot comes from configured server transport, not Discord input.

        Staff confirms identity and starting land in game. This does not allocate land.
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
            if request["state"] != "requested":
                raise ValueError("Request is already reviewed; use farm_status or staff records")
            if request["farm_name"] != snapshot["farms"][farm_id]:
                raise ValueError("Created farm name must match the requested farm name")
            scope = dict(server_id=server_id, save_id=save_id, discord_id=request["discord_id"])
            existing = self.db.game_identities.find_one(scope, session=session)
            if existing and (existing["game_player_id"] != player_id or not existing.get("approved_by")):
                raise ValueError("Existing identity requires operator reconciliation")
            if not existing:
                self.db.game_identities.insert_one(dict(**scope, game_player_id=player_id,
                    approved_by=str(approved_by), approved_at=datetime.now(timezone.utc),
                    observation_session=snapshot["session"], observation_sequence=snapshot["sequence"]), session=session)
            operation = self.assign(request["discord_id"], server_id, save_id, farm_id, "farm_manager",
                                    snapshot["farms"], approved_by, session=session)
            self.db.farm_requests.update_one({"_id": request_id}, {"$set": dict(state="approved", farm_id=farm_id,
                game_player_id=player_id, approved_by=str(approved_by), approved_at=datetime.now(timezone.utc),
                operation_id=operation, land_confirmed=True)}, session=session)
            return operation
        return self.database.atomic(approve)

    def reject_request(self, request_id, server_id, save_id, approved_by, reason):
        if not approved_by or not reason.strip() or len(reason) > 300:
            raise ValueError("A staff reviewer and reason (1–300 characters) are required")
        result = self.db.farm_requests.update_one(dict(_id=request_id, server_id=server_id, save_id=save_id, state="requested"),
            {"$set": dict(state="rejected", reviewed_by=str(approved_by), reason=reason.strip(), reviewed_at=datetime.now(timezone.utc))})
        if result.modified_count != 1:
            raise ValueError("Request is unknown or already reviewed")

    def assign(self, discord_id, server_id, save_id, farm_id, role, farms, approved_by, session=None):
        """Called only after the Discord/operator boundary authorizes approved_by."""
        if role not in ROLES or type(farm_id) is not int or farm_id <= 0 or not approved_by:
            raise ValueError("Invalid farm, role, or approver")
        if farm_id not in farms:
            raise ValueError("Farm is absent from the server snapshot")
        user = str(discord_id)
        membership_id = key(server_id, save_id, user)
        operation_id = str(uuid.uuid4())

        def assign(session):
            identity = self.db.game_identities.find_one(dict(server_id=server_id, save_id=save_id, discord_id=user), session=session)
            if not identity or not identity.get("approved_by"):
                raise ValueError("Staff must approve the player's game identity through a farm request first")
            old = self.db.memberships.find_one({"_id": membership_id}, session=session)
            if old and old["state"] == "pending":
                raise ValueError("Reconcile the pending permission operation first")
            if old and old["farm_id"] != farm_id:
                raise ValueError("Farm migration requires operator reconciliation; it is not yet supported")
            revision = old["revision"] + 1 if old else 1
            record = dict(_id=membership_id, discord_id=user, server_id=server_id, save_id=save_id,
                          game_player_id=identity["game_player_id"], farm_id=farm_id,
                          farm_name=farms[farm_id], desired_role=role, applied_role=old.get("applied_role") if old else None,
                          revision=revision, state="pending", operation_id=operation_id, approved_by=str(approved_by))
            self.db.memberships.replace_one({"_id": membership_id}, record, upsert=True, session=session)
            self.db.permission_jobs.insert_one(dict(_id=operation_id, membership_id=membership_id,
                server_id=server_id, save_id=save_id, game_player_id=identity["game_player_id"],
                farm_id=farm_id, role=role, revision=revision, state="pending",
                approved_by=str(approved_by), created_at=datetime.now(timezone.utc)), session=session)
            return operation_id
        return assign(session) if session is not None else self.database.atomic(assign)

    def acknowledge(self, operation_id, authenticated_server_id, save_id, revision, receipt):
        """Only after the mod confirms the exact job was applied and persisted."""
        if not receipt:
            raise ValueError("A durable mod receipt is required")

        def acknowledge(session):
            job = self.db.permission_jobs.find_one({"_id": operation_id, "server_id": authenticated_server_id,
                                                   "save_id": save_id, "revision": revision}, session=session)
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
            return "applied"
        return self.database.atomic(acknowledge)

    def status(self, discord_id, server_id, save_id):
        return self.db.memberships.find_one({"_id": key(server_id, save_id, str(discord_id))})
