import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import discord

from fs25_network_core.integration_campaign import _MemoryDatabase
from fs25_network_core.server_status import ServerStatusProjection, ServerStatusPublisher


class _Message:
    def __init__(self, message_id, content=""):
        self.id = message_id
        self.content = content
        self.edits = 0

    async def edit(self, **kwargs):
        self.content = kwargs["content"]
        self.edits += 1


class _Channel:
    def __init__(self):
        self.messages = {}
        self.sent = []
        self.next_id = 100
        self.deleted = set()

    async def send(self, **kwargs):
        message = _Message(self.next_id, kwargs["content"])
        self.next_id += 1
        self.messages[message.id] = message
        self.sent.append(message)
        return message

    async def fetch_message(self, message_id):
        if message_id in self.deleted or message_id not in self.messages:
            raise discord.NotFound(MagicMock(status=404), "missing")
        return self.messages[message_id]


class _Bot:
    def __init__(self, channel):
        self.channels = {"server_status": 777}
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if int(channel_id) == 777 else None

    async def fetch_channel(self, channel_id):
        return self.channel


def _server(db, key="sin-fs25-01", world="hobo-world", save="sin-fs25-hobo"):
    now = datetime.now(timezone.utc)
    db.db.sin_servers.insert_one({"_id": key, "server_key": key,
                                  "display_name": "SiN Test Server 01", "enabled": True,
                                  "online": True,
                                  "active_runtime": {"save_key": save, "world_id": world,
                                                      "last_seen_at": now}})
    db.db.server_snapshots.insert_one({"server_key": key, "save_key": save,
                                       "world_id": world, "map_id": "Hobo's Hollow",
                                       "current_month": 3, "current_day": 12,
                                       "day_time_minutes": 615, "time_scale": 5,
                                       "players": {"stable": {"name": "Repton", "farm_id": 2}},
                                       "received_at": now})
    # A historical snapshot with the same numeric IDs must never be selected.
    db.db.server_snapshots.insert_one({"server_key": key, "save_key": save,
                                       "world_id": "courtright-world", "map_id": "Courtright",
                                       "players": {"old": {"name": "Old Player"}},
                                       "received_at": now - timedelta(days=1)})


class ServerStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_reads_only_active_world_and_renders_required_state(self):
        database = _MemoryDatabase()
        _server(database)
        row = database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        projection = ServerStatusProjection(database).project(row)
        self.assertEqual(projection["world_id"], "hobo-world")
        self.assertEqual(projection["map_id"], "Hobo's Hollow")
        self.assertEqual(projection["players"], ["Repton"])
        self.assertIn("month 3, day 12, 10:15", projection["game_time"])
        self.assertEqual(projection["time_scale"], "5")
        self.assertNotIn("Courtright", ServerStatusProjection.render(projection))

    async def test_status_lights_use_strict_sixty_second_update_boundary(self):
        database = _MemoryDatabase()
        _server(database)
        now = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
        snapshot = database.db.server_snapshots.find_one({"world_id": "hobo-world"})
        database.db.server_snapshots.update_one(
            {"world_id": "hobo-world"},
            {"$set": {"received_at": now - timedelta(seconds=59)}},
        )
        database.db.sin_servers.update_one(
            {"server_key": "sin-fs25-01"},
            {"$set": {"last_seen_at": now - timedelta(seconds=10)}},
        )
        projection = ServerStatusProjection(database, now=lambda: now).project(
            database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        )
        content = ServerStatusProjection.render(projection)
        self.assertEqual(projection["state"], "online")
        self.assertTrue(projection["last_update_fresh"])
        self.assertIn("State: 🟢", content)
        self.assertIn("Last successful update: 🟢", content)
        self.assertNotIn("<t:", content)

        database.db.server_snapshots.update_one(
            {"world_id": "hobo-world"},
            {"$set": {"received_at": now - timedelta(seconds=60)}},
        )
        projection = ServerStatusProjection(database, now=lambda: now).project(
            database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        )
        content = ServerStatusProjection.render(projection)
        self.assertFalse(projection["last_update_fresh"])
        self.assertIn("State: 🟢", content)
        self.assertIn("Last successful update: 🔴", content)

    async def test_status_lights_show_offline_and_missing_update_as_red(self):
        database = _MemoryDatabase()
        _server(database)
        now = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
        database.db.sin_servers.update_one(
            {"server_key": "sin-fs25-01"},
            {"$set": {"last_seen_at": now - timedelta(seconds=91)}},
        )
        database.db.server_snapshots.update_one(
            {"world_id": "hobo-world"},
            {"$set": {"received_at": None}},
        )
        database.db.sin_servers.update_one(
            {"server_key": "sin-fs25-01"},
            {"$set": {"active_runtime": {
                "save_key": "sin-fs25-hobo", "world_id": "hobo-world",
                "last_seen_at": None,
            }}},
        )
        projection = ServerStatusProjection(database, now=lambda: now).project(
            database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        )
        content = ServerStatusProjection.render(projection)
        self.assertEqual(projection["state"], "offline")
        self.assertFalse(projection["last_update_fresh"])
        self.assertIn("State: 🔴", content)
        self.assertIn("Last successful update: 🔴", content)

    async def test_one_card_is_idempotent_and_edits_in_place(self):
        database = _MemoryDatabase()
        _server(database)
        channel = _Channel()
        bot = _Bot(channel)
        publisher = ServerStatusPublisher(bot, database)
        row = database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        self.assertEqual(await publisher.sync_server(row), "created")
        self.assertEqual(await publisher.sync_server(row), "unchanged")
        self.assertEqual(len(channel.sent), 1)
        message = channel.sent[0]
        snapshot = database.db.server_snapshots.find_one({"world_id": "hobo-world"})
        database.db.server_snapshots.update_one({"world_id": "hobo-world"},
                                                 {"$set": {"players": {}, "received_at": datetime.now(timezone.utc)}})
        self.assertEqual(await publisher.sync_server(row), "edited")
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(message.edits, 1)

    async def test_deleted_card_is_replaced_and_id_persisted(self):
        database = _MemoryDatabase()
        _server(database)
        channel = _Channel()
        bot = _Bot(channel)
        publisher = ServerStatusPublisher(bot, database)
        row = database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        await publisher.sync_server(row)
        old_id = channel.sent[0].id
        channel.deleted.add(old_id)
        database.db.server_snapshots.update_one({"world_id": "hobo-world"},
                                                 {"$set": {"time_scale": 15}})
        self.assertEqual(await publisher.sync_server(row), "created")
        self.assertEqual(len(channel.sent), 2)
        saved = database.db.server_status_cards.find_one({"server_key": "sin-fs25-01"})
        self.assertEqual(saved["message_id"], str(channel.sent[-1].id))
        self.assertNotEqual(saved["message_id"], str(old_id))

    async def test_restart_reuses_persisted_message_without_creating_another(self):
        database = _MemoryDatabase()
        _server(database)
        channel = _Channel()
        first = ServerStatusPublisher(_Bot(channel), database)
        row = database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        await first.sync_server(row)
        second = ServerStatusPublisher(_Bot(channel), database)
        self.assertEqual(await second.sync_server(row), "unchanged")
        self.assertEqual(len(channel.sent), 1)

    async def test_restart_edits_existing_message_when_current_projection_changed(self):
        database = _MemoryDatabase()
        _server(database)
        channel = _Channel()
        first = ServerStatusPublisher(_Bot(channel), database)
        row = database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        await first.publish_once()
        message = channel.sent[0]
        database.db.server_snapshots.update_one({"world_id": "hobo-world"},
                                                 {"$set": {"current_day": 13}})
        second = ServerStatusPublisher(_Bot(channel), database)
        self.assertEqual(await second.publish_once(), [("sin-fs25-01", "edited")])
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(message.edits, 1)

    async def test_startup_recovery_replaces_deleted_card_even_when_content_is_unchanged(self):
        database = _MemoryDatabase()
        _server(database)
        channel = _Channel()
        first = ServerStatusPublisher(_Bot(channel), database)
        row = database.db.sin_servers.find_one({"server_key": "sin-fs25-01"})
        await first.publish_once()
        channel.deleted.add(channel.sent[0].id)
        second = ServerStatusPublisher(_Bot(channel), database)
        result = await second.publish_once()
        self.assertEqual(result, [("sin-fs25-01", "created")])
        self.assertEqual(len(channel.sent), 2)

    async def test_multiple_registered_servers_get_one_card_each(self):
        database = _MemoryDatabase()
        _server(database, key="one", world="one-world", save="one-save")
        _server(database, key="two", world="two-world", save="two-save")
        channel = _Channel()
        publisher = ServerStatusPublisher(_Bot(channel), database)
        result = await publisher.publish_once()
        self.assertEqual({key for key, _ in result}, {"one", "two"})
        self.assertEqual(len(channel.sent), 2)


if __name__ == "__main__":
    unittest.main()
