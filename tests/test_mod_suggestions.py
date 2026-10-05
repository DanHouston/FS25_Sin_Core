import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import discord

from fs25_network_core.bot_frontend import NetworkBot
from fs25_network_core.mod_suggestions import GREEN_VOTE, RED_VOTE, prepare_suggestion


def suggestion_fixture(*, tag_exists=True, bot_reactions=(), applied_tags=()):
    review = SimpleNamespace(id=101, name="Under Review")
    forum = MagicMock(spec=discord.ForumChannel)
    forum.id = 123
    forum.available_tags = [review] if tag_exists else []
    forum.create_tag = AsyncMock(return_value=review)

    message = SimpleNamespace(
        reactions=[SimpleNamespace(emoji=emoji, me=True) for emoji in bot_reactions],
        add_reaction=AsyncMock(), remove_reaction=AsyncMock(),
    )

    async def add_reaction(emoji):
        message.reactions.append(SimpleNamespace(emoji=emoji, me=True))

    message.add_reaction.side_effect = add_reaction

    async def remove_reaction(emoji, user):
        message.reactions = [reaction for reaction in message.reactions
                             if not (reaction.emoji == emoji and reaction.me)]

    message.remove_reaction.side_effect = remove_reaction
    thread = SimpleNamespace(id=456, parent_id=123, applied_tags=list(applied_tags),
                             fetch_message=AsyncMock(return_value=message), edit=AsyncMock())

    async def edit(*, applied_tags, reason):
        thread.applied_tags = list(applied_tags)
        return thread

    thread.edit.side_effect = edit
    return thread, forum, message, review


class ModSuggestionPolicyTests(unittest.IsolatedAsyncioTestCase):
    bot_user = SimpleNamespace(id=999)

    async def test_new_post_gets_tag_and_balanced_reactions_once(self):
        thread, forum, message, review = suggestion_fixture()
        self.assertEqual(await prepare_suggestion(thread, forum, self.bot_user), (456, True))
        self.assertEqual(thread.applied_tags, [review])
        self.assertEqual([call.args[0] for call in message.add_reaction.call_args_list],
                         [GREEN_VOTE, RED_VOTE])
        self.assertEqual(await prepare_suggestion(thread, forum, self.bot_user), (456, False))
        thread.edit.assert_awaited_once()
        self.assertEqual(message.add_reaction.await_count, 2)
        forum.create_tag.assert_not_awaited()

    async def test_existing_tag_and_green_reaction_only_adds_red(self):
        thread, forum, message, review = suggestion_fixture(
            bot_reactions=(GREEN_VOTE,), applied_tags=(SimpleNamespace(id=202, name="Equipment"),))
        await prepare_suggestion(thread, forum, self.bot_user)
        self.assertEqual([tag.id for tag in thread.applied_tags], [202, review.id])
        message.add_reaction.assert_awaited_once_with(RED_VOTE)

    async def test_missing_review_tag_is_created_moderated(self):
        thread, forum, _, review = suggestion_fixture(tag_exists=False)
        await prepare_suggestion(thread, forum, self.bot_user)
        forum.create_tag.assert_awaited_once_with(
            name="Under Review", moderated=True, reason="SiN mod suggestion review status")
        self.assertEqual(thread.applied_tags, [review])

    async def test_full_tag_limit_fails_before_reactions(self):
        tags = [SimpleNamespace(id=number, name=str(number)) for number in range(5)]
        thread, forum, message, _ = suggestion_fixture(applied_tags=tags)
        with self.assertRaisesRegex(ValueError, "maximum five tags"):
            await prepare_suggestion(thread, forum, self.bot_user)
        thread.edit.assert_not_awaited()
        message.add_reaction.assert_not_awaited()

    async def test_wrong_forum_is_not_modified(self):
        thread, forum, message, _ = suggestion_fixture()
        thread.parent_id = 999
        with self.assertRaisesRegex(ValueError, "configured forum"):
            await prepare_suggestion(thread, forum, self.bot_user)
        thread.edit.assert_not_awaited()
        message.add_reaction.assert_not_awaited()

    async def test_second_reaction_failure_retracts_bots_green_seed(self):
        thread, forum, message, _ = suggestion_fixture()

        async def fail_red(emoji):
            if emoji == RED_VOTE:
                raise RuntimeError("red reaction rejected")
            message.reactions.append(SimpleNamespace(emoji=emoji, me=True))

        message.add_reaction.side_effect = fail_red
        with self.assertRaisesRegex(RuntimeError, "red reaction rejected"):
            await prepare_suggestion(thread, forum, self.bot_user)
        message.remove_reaction.assert_awaited_once_with(GREEN_VOTE, self.bot_user)
        self.assertEqual(message.reactions, [])


class ModSuggestionBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {"DISCORD_OPERATOR_ROLE_IDS": "42"}):
            self.bot = NetworkBot(MagicMock(), {}, 1,
                                  channels={"mod_suggestions": 123, "sin_apply": 10})

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_thread_creation_only_processes_configured_forum(self):
        thread, forum, message, _ = suggestion_fixture()
        thread.guild = SimpleNamespace(id=1)
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id:
                                           forum if channel_id == forum.id else thread)
        await self.bot.on_thread_create(thread)
        self.bot.fetch_channel.assert_has_awaits([call(123), call(456)])
        self.assertEqual(message.add_reaction.await_count, 2)
        thread.parent_id = 999
        await self.bot.on_thread_create(thread)
        self.assertEqual(message.add_reaction.await_count, 2)

    async def test_concurrent_thread_and_starter_events_do_not_duplicate_setup(self):
        thread, forum, message, _ = suggestion_fixture()
        thread.guild = SimpleNamespace(id=1)
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id:
                                           forum if channel_id == forum.id else thread)
        starter = SimpleNamespace(
            id=thread.id, channel=thread, guild=thread.guild,
            author=SimpleNamespace(bot=False, system=False))
        await asyncio.gather(self.bot.on_thread_create(thread), self.bot.on_message(starter))
        thread.edit.assert_awaited_once()
        self.assertEqual(message.add_reaction.await_count, 2)

    async def test_starter_message_retries_when_thread_event_is_missed(self):
        thread, forum, message, _ = suggestion_fixture()
        thread.guild = SimpleNamespace(id=1)
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id:
                                           forum if channel_id == forum.id else thread)
        self.bot.server_registry.eligible_servers = MagicMock()
        await self.bot.on_message(SimpleNamespace(
            id=thread.id, channel=thread, guild=thread.guild,
            author=SimpleNamespace(bot=False, system=False)))
        self.assertEqual(message.add_reaction.await_count, 2)
        self.bot.server_registry.eligible_servers.assert_not_called()

    async def test_gateway_thread_is_refetched_before_mutation(self):
        current, forum, message, review = suggestion_fixture(
            bot_reactions=(GREEN_VOTE, RED_VOTE),
            applied_tags=(SimpleNamespace(id=101, name="Under Review"),))
        stale = SimpleNamespace(id=current.id, parent_id=forum.id,
                                guild=SimpleNamespace(id=1), applied_tags=[])
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id:
                                           forum if channel_id == forum.id else current)
        await self.bot.on_thread_create(stale)
        current.edit.assert_not_awaited()
        message.add_reaction.assert_not_awaited()
        self.bot.fetch_channel.assert_has_awaits([call(forum.id), call(current.id)])


if __name__ == "__main__":
    unittest.main()
