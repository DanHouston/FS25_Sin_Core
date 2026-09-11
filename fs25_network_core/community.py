"""Discord community membership applications; independent of FS25 authorization."""
from datetime import datetime, timezone

NICKNAME_LIMIT = 32


def requested_server_nickname(nickname, farm_name):
    nickname, farm_name = nickname.strip(), farm_name.strip()
    if not nickname or not farm_name:
        raise ValueError("Nickname and farm name are required")
    if "|" in nickname or "|" in farm_name:
        raise ValueError("Nickname and farm name cannot contain |")
    value = f"{nickname} | {farm_name}"
    if len(value) > NICKNAME_LIMIT:
        raise ValueError("That SiN display name is too long; shorten the nickname or farm name")
    return nickname, farm_name, value


class CommunityApplications:
    def __init__(self, database): self.database, self.db = database, database.db

    def apply(self, discord_id, nickname, farm_name):
        nickname, farm_name, server_nickname = requested_server_nickname(nickname, farm_name)
        old = self.db.community_applications.find_one({"_id": str(discord_id)})
        if old and old["state"] == "pending":
            if (old["nickname"], old["farm_name"]) != (nickname, farm_name):
                raise ValueError("You already have a different pending community application")
            return old
        if old and old["state"] == "approved": return old
        record = {"_id": str(discord_id), "discord_id": str(discord_id), "nickname": nickname,
                  "farm_name": farm_name, "server_nickname": server_nickname, "state": "pending",
                  "submitted_at": datetime.now(timezone.utc), "reviewed_by": None, "reviewed_at": None}
        self.db.community_applications.replace_one({"_id": str(discord_id)}, record, upsert=True)
        return record

    def pending(self): return list(self.db.community_applications.find({"state": "pending"}).sort("submitted_at", 1).limit(25))
    def pending_for(self, discord_id):
        record = self.db.community_applications.find_one({"_id": str(discord_id), "state": "pending"})
        if not record: raise ValueError("That member has no pending community application")
        return record
    def approve(self, discord_id, reviewer):
        result = self.db.community_applications.update_one({"_id": str(discord_id), "state": "pending"}, {"$set": {"state": "approved", "reviewed_by": str(reviewer), "reviewed_at": datetime.now(timezone.utc)}})
        if result.modified_count != 1: raise ValueError("Application is no longer pending")
    def deny(self, discord_id, reviewer, reason):
        if not reason.strip(): raise ValueError("A denial reason is required")
        result = self.db.community_applications.update_one({"_id": str(discord_id), "state": "pending"}, {"$set": {"state": "denied", "reviewed_by": str(reviewer), "reviewed_at": datetime.now(timezone.utc), "denial_reason": reason.strip()}})
        if result.modified_count != 1: raise ValueError("Application is no longer pending")
