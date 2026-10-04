import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fs25_network_core.activity import ActivityOutbox, ActivityPublisher


class ActivityNotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_staff_notification_is_idempotent_and_destination_scoped(self):
        database = MagicMock()
        outbox = ActivityOutbox(database)
        first = outbox.enqueue_staff("farm-manager-applied:op-1", "server", "Farm complete",
                                     save_key="save", world_id="world")
        second = outbox.enqueue_staff("farm-manager-applied:op-1", "server", "Farm complete",
                                      save_key="save", world_id="world")

        self.assertEqual(first["_id"], second["_id"])
        self.assertEqual(first["destination"], "staff")

    async def test_staff_notification_publishes_to_configured_staff_channel(self):
        database = MagicMock()
        channel = MagicMock()
        channel.guild = SimpleNamespace(me=MagicMock())
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
        channel.send = AsyncMock()
        bot = MagicMock()
        bot.channels = {"staff": 1552115384205713448}
        bot.get_channel.return_value = channel
        publisher = ActivityPublisher(bot, database)
        record = {"_id": "notification", "server_key": "server", "save_key": "save",
                  "world_id": "world", "destination": "staff", "activity_type": "staff_notification",
                  "message": "Farm complete", "status": "pending", "attempts": 0}

        await publisher.publish(record)

        bot.get_channel.assert_called_once_with(1552115384205713448)
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.args[0], "Farm complete")
        update = database.db.activity_outbox.update_one.call_args.args[1]
        self.assertEqual(update["$set"]["status"], "published")

    async def test_native_contract_available_creates_parent_card_and_thread(self):
        database = MagicMock()
        parent = MagicMock(id=101)
        thread = MagicMock(id=202)
        parent.create_thread = AsyncMock(return_value=thread)
        channel = MagicMock(id=303)
        channel.send = AsyncMock(return_value=parent)
        channel.fetch_message = AsyncMock(return_value=None)
        bot = MagicMock()
        publisher = ActivityPublisher(bot, database)
        contract = {"_id": "contract-key", "mission_type": "harvestMission",
                    "field_id": "12", "field_name": "Field 12"}
        database.db.native_contracts.find_one.return_value = contract
        record = {"metadata": {"native_contract_id": "contract-key", "lifecycle": "available"},
                  "message": "📋 Contract Available — Harvest\nField 12"}

        await publisher._publish_native_contract(channel, record)

        channel.send.assert_awaited_once()
        parent.create_thread.assert_awaited_once()
        update = database.db.native_contracts.update_one.call_args.args[1]
        self.assertEqual(update["$set"]["discord_thread_id"], "202")

    async def test_native_contract_updates_are_sent_into_contract_thread(self):
        database = MagicMock()
        bot = MagicMock()
        thread = MagicMock()
        thread.send = AsyncMock()
        bot.get_channel.return_value = thread
        publisher = ActivityPublisher(bot, database)
        database.db.native_contracts.find_one.return_value = {"_id": "contract-key",
                                                               "discord_thread_id": "202"}
        record = {"metadata": {"native_contract_id": "contract-key", "lifecycle": "accepted"},
                  "message": "Claimed by Farm 2"}

        await publisher._publish_native_contract(MagicMock(), record)

        bot.get_channel.assert_called_once_with(202)
        thread.send.assert_awaited_once_with("Claimed by Farm 2", allowed_mentions=unittest.mock.ANY)

    async def test_native_contract_disposition_refreshes_parent_and_posts_in_thread(self):
        database = MagicMock()
        parent = MagicMock(id=101, content="Contract Available - Harvest\nField 12")
        parent.edit = AsyncMock()
        thread = MagicMock(id=202)
        thread.send = AsyncMock()
        channel = MagicMock(id=303)
        channel.fetch_message = AsyncMock(return_value=parent)
        bot = MagicMock()
        bot.get_channel.return_value = thread
        publisher = ActivityPublisher(bot, database)
        database.db.native_contracts.find_one.return_value = {
            "_id": "contract-key", "discord_parent_message_id": "101",
            "discord_parent_base_message": "Contract Available - Harvest\nField 12",
            "discord_thread_id": "202"}

        await publisher._publish_native_contract(channel, {
            "metadata": {"native_contract_id": "contract-key", "lifecycle": "accepted"},
            "message": "Claimed by SiN Harvest"})

        parent.edit.assert_awaited_once_with(
            content="Contract Claimed - Harvest\nField 12\n\n**Current status:** Claimed by SiN Harvest",
            allowed_mentions=unittest.mock.ANY)
        thread.send.assert_awaited_once_with("Claimed by SiN Harvest",
                                             allowed_mentions=unittest.mock.ANY)

    async def test_native_contract_expiration_keeps_thread_and_marks_parent_expired(self):
        database = MagicMock()
        parent = MagicMock(id=101, content="Contract Available - Harvest\nField 12")
        parent.edit = AsyncMock()
        thread = MagicMock(id=202)
        thread.send = AsyncMock()
        channel = MagicMock(id=303)
        channel.fetch_message = AsyncMock(return_value=parent)
        bot = MagicMock()
        bot.get_channel.return_value = thread
        publisher = ActivityPublisher(bot, database)
        database.db.native_contracts.find_one.return_value = {
            "_id": "contract-key", "discord_parent_message_id": "101",
            "discord_parent_base_message": "Contract Available - Harvest\nField 12",
            "discord_thread_id": "202"}
        await publisher._publish_native_contract(channel, {
            "metadata": {"native_contract_id": "contract-key", "lifecycle": "expired"},
            "message": "Expired in FS25"})
        parent.edit.assert_awaited_once_with(
            content="Contract Expired - Harvest\nField 12\n\n**Current status:** Expired in FS25",
            allowed_mentions=unittest.mock.ANY)
        thread.send.assert_awaited_once_with("Expired in FS25", allowed_mentions=unittest.mock.ANY)

    async def test_native_contract_requires_thread_permissions(self):
        database = MagicMock()
        channel = MagicMock()
        channel.guild = SimpleNamespace(me=MagicMock())
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, send_messages=True,
            create_public_threads=False, send_messages_in_threads=False)
        bot = MagicMock()
        bot.get_channel.return_value = channel
        publisher = ActivityPublisher(bot, database)
        publisher._channel_member = AsyncMock(return_value=MagicMock())
        publisher._record_failure = MagicMock()
        record = {"_id": "contract-event", "server_key": "server",
                  "activity_type": "native_contract_available", "status": "pending", "attempts": 0}
        database.db.sin_servers.find_one.return_value = {"discord_activity_channel_id": "303"}

        await publisher.publish(record)

        channel.send.assert_not_called()
        publisher._record_failure.assert_called_once()
        self.assertTrue(publisher._record_failure.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
