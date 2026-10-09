import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fs25_network_core.market_finance import (
    DailyMarketFinance, DailyMarketFinancePublisher, report_window,
)


class DailyMarketFinanceTests(unittest.TestCase):
    def test_eastern_day_window_waits_until_ten_minutes_after_midnight(self):
        self.assertIsNone(report_window(datetime(2026, 10, 9, 4, 9, tzinfo=timezone.utc)))
        day, start, end = report_window(datetime(2026, 10, 9, 4, 10, tzinfo=timezone.utc))
        self.assertEqual(day, "2026-10-08")
        self.assertEqual(start, datetime(2026, 10, 8, 4, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 9, 4, tzinfo=timezone.utc))

    def test_dst_day_is_not_assumed_to_have_24_hours(self):
        day, start, end = report_window(datetime(2026, 11, 2, 5, 11, tzinfo=timezone.utc))
        self.assertEqual(day, "2026-11-01")
        self.assertEqual((end - start).total_seconds(), 25 * 3600)

    def test_server_total_uses_only_active_world_and_all_farms(self):
        database = MagicMock()
        service = DailyMarketFinance(database)
        service.registry.eligible_servers = MagicMock(return_value=[{
            "server_key": "server-1", "display_name": "Server One",
            "active_save_key": "save-1", "saves": [{"save_key": "save-1"}]}])
        service.worlds.active_id = MagicMock(return_value="world-1")
        database.db.farm_finance_changes.aggregate.return_value = [{
            "income": 900, "expense": 350, "count": 4}]

        result = service.summarize(datetime(2026, 10, 9, 4, 11, tzinfo=timezone.utc))

        match = database.db.farm_finance_changes.aggregate.call_args.args[0][0]["$match"]
        self.assertEqual({key: match[key] for key in ("server_key", "save_key", "world_id")},
                         {"server_key": "server-1", "save_key": "save-1", "world_id": "world-1"})
        self.assertEqual(match["farm_id"], {"$gte": 1, "$lte": 254})
        self.assertEqual(match["observed_at"], {"$gte": result["start"], "$lt": result["end"]})
        self.assertEqual(result["totals"], {"income": 900, "expense": 350, "count": 4})
        rendered = service.render(result)
        self.assertIn("revenue / native inflows: **$900**", rendered)
        self.assertIn("expenses / native outflows: **$350**", rendered)
        self.assertIn("Net observed cash change: **$550**", rendered)
        self.assertIn("not profit", rendered)

    def test_no_active_world_never_queries_historical_finance(self):
        database = MagicMock()
        service = DailyMarketFinance(database)
        service.registry.eligible_servers = MagicMock(return_value=[{
            "server_key": "server-1", "active_save_key": "save-1"}])
        service.worlds.active_id = MagicMock(return_value=None)
        self.assertIsNone(service.summarize(datetime(2026, 10, 9, 4, 11, tzinfo=timezone.utc)))
        database.db.farm_finance_changes.aggregate.assert_not_called()


class FakeMessage:
    def __init__(self, message_id, content, author):
        self.id, self.content, self.author = message_id, content, author
        self.edits = 0

    async def edit(self, **kwargs):
        self.content = kwargs["content"]
        self.edits += 1


class FakeChannel:
    def __init__(self, author):
        self.author = author
        self.messages = {}
        self.sends = 0

    async def send(self, **kwargs):
        self.sends += 1
        message = FakeMessage(100 + self.sends, kwargs["content"], self.author)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        return self.messages[message_id]

    async def history(self, **kwargs):
        for message in self.messages.values():
            yield message


class DailyMarketPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_restarts_reuse_card_and_late_data_edits_in_place(self):
        database = MagicMock()
        author = object()
        channel = FakeChannel(author)
        bot = SimpleNamespace(guild=SimpleNamespace(id=1), channels={"market": 777}, user=author,
                              get_channel=lambda channel_id: channel)
        publisher = DailyMarketFinancePublisher(bot, database)
        now = datetime(2026, 10, 9, 4, 11, tzinfo=timezone.utc)
        day, start, end = report_window(now)
        summary = {"day": day, "start": start, "end": end,
                   "totals": {"income": 100, "expense": 20, "count": 2},
                   "servers": [{"name": "Server One", "income": 100, "expense": 20, "count": 2}]}
        publisher.projection.summarize = MagicMock(return_value=summary)
        stored = {}
        database.db.market_daily_finance.find_one.side_effect = lambda query: stored.get(query["_id"])
        def save(query, update, **kwargs):
            stored[query["_id"]] = {"_id": query["_id"], **update["$set"]}
        database.db.market_daily_finance.update_one.side_effect = save

        self.assertEqual(await publisher.publish_once(now), "created")
        self.assertEqual(channel.sends, 1)
        self.assertEqual(await publisher.publish_once(now), "unchanged")
        summary["totals"]["income"] = 125
        self.assertEqual(await publisher.publish_once(now), "updated")
        self.assertEqual(channel.sends, 1)
        self.assertEqual(channel.messages[101].edits, 1)

        stored.clear()  # Simulate send succeeded just before Mongo persistence.
        self.assertEqual(await publisher.publish_once(now), "updated")
        self.assertEqual(channel.sends, 1)
        self.assertIn("1:2026-10-08", stored)
