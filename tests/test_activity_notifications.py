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


if __name__ == "__main__":
    unittest.main()
