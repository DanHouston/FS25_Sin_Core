"""Operator-approved links; a farm ID is meaningful only within one save."""


class AdminManager:
    def __init__(self, database):
        self.db = database.db

    def link(self, discord_id, server_id, save_id, farm_id, farm_name, farms, approved_by):
        if not approved_by:
            raise ValueError("An operator must approve the in-game identity and manager assignment")
        if farms.get(farm_id) != farm_name:
            raise ValueError("Farm ID and name do not match the inspected save")
        record = dict(discord_id=str(discord_id), server_id=server_id, save_id=save_id,
                      farm_id=farm_id, farm_name=farm_name, permissions="farm_manager",
                      approved_by=str(approved_by))
        self.db.farm_links.insert_one(record)
        return record

    def lookup(self, discord_id, server_id, save_id, session=None):
        link = self.db.memberships.find_one(dict(discord_id=str(discord_id), server_id=server_id, save_id=save_id,
                                                state="active", desired_role="farm_manager", applied_role="farm_manager"), session=session)
        if not link:
            raise ValueError("No active, mod-confirmed farm manager mapping for this user and save")
        return link
