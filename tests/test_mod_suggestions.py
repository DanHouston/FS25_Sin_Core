import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import discord

from fs25_network_core.bot_frontend import NetworkBot
from fs25_network_core.mod_suggestions import (
    GREEN_VOTE, LEGACY_NO_VOTE, RED_VOTE, ModSuggestionModal,
    build_suggestion_post, member_vote_counts, prepare_suggestion, update_vote_title,
)


def suggestion_fixture(*, tag_exists=True, bot_reactions=(), applied_tags=()):
    review = SimpleNamespace(id=101, name="Under Review")
    forum = MagicMock(spec=discord.ForumChannel)
    forum.id = 123
    forum.available_tags = [review] if tag_exists else []
    forum.create_tag = AsyncMock(return_value=review)

    message = SimpleNamespace(
        reactions=[SimpleNamespace(emoji=emoji, me=True, count=1) for emoji in bot_reactions],
        add_reaction=AsyncMock(), remove_reaction=AsyncMock(),
    )

    async def add_reaction(emoji):
        message.reactions.append(SimpleNamespace(emoji=emoji, me=True, count=1))

    message.add_reaction.side_effect = add_reaction

    async def remove_reaction(emoji, user):
        message.reactions = [reaction for reaction in message.reactions
                             if not (reaction.emoji == emoji and reaction.me)]

    message.remove_reaction.side_effect = remove_reaction
    thread = SimpleNamespace(id=456, parent_id=123, name="Park it Under a Roof",
                             applied_tags=list(applied_tags),
                             fetch_message=AsyncMock(return_value=message), edit=AsyncMock())

    async def edit(*, applied_tags=None, name=None, reason):
        if applied_tags is not None:
            thread.applied_tags = list(applied_tags)
        if name is not None:
            thread.name = name
        return thread

    thread.edit.side_effect = edit
    return thread, forum, message, review


class ModSuggestionPolicyTests(unittest.IsolatedAsyncioTestCase):
    bot_user = SimpleNamespace(id=999)

    def test_form_uses_four_field_template_and_submitter_credit(self):
        title, content = build_suggestion_post(
            mod_name="Fermenting Silo Pack", link="https://example.org/mod?id=123",
            adds="Makes silage", reason="Separates production silos", submitter_id=77)
        self.assertEqual(title, "Fermenting Silo Pack")
        self.assertIn("**Mod Name:**\nFermenting Silo Pack", content)
        self.assertIn("**Link:**\nhttps://example.org/mod?id=123", content)
        self.assertIn("**What does it add?**\nMakes silage", content)
        self.assertIn("**Why should SiN add it?**\nSeparates production silos", content)
        self.assertNotIn("How would you use it?", content)
        self.assertIn("**Submitted by:** <@77>", content)

    def test_form_rejects_missing_fields_and_non_web_links(self):
        values = dict(mod_name="Mod", link="https://example.org/mod", adds="Adds a silo",
                      reason="Useful", submitter_id=77)
        with self.assertRaisesRegex(ValueError, "Complete every field"):
            build_suggestion_post(**{**values, "reason": " "})
        with self.assertRaisesRegex(ValueError, "http:// or https://"):
            build_suggestion_post(**{**values, "link": "javascript:bad"})

    async def test_modal_passes_four_values_to_submitter(self):
        submit = AsyncMock()
        modal = ModSuggestionModal(submit)
        for field, value in ((modal.mod_name, "A mod"), (modal.link, "https://example.org"),
                             (modal.adds, "Adds equipment"), (modal.reason, "It helps")):
            field._value = value
        interaction = SimpleNamespace(user=SimpleNamespace(id=77))
        await modal.on_submit(interaction)
        submit.assert_awaited_once_with(
            interaction, mod_name="A mod", link="https://example.org",
            adds="Adds equipment", reason="It helps")

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
            message.reactions.append(SimpleNamespace(emoji=emoji, me=True, count=1))

        message.add_reaction.side_effect = fail_red
        with self.assertRaisesRegex(RuntimeError, "red reaction rejected"):
            await prepare_suggestion(thread, forum, self.bot_user)
        message.remove_reaction.assert_awaited_once_with(GREEN_VOTE, self.bot_user)
        self.assertEqual(message.reactions, [])

    async def test_vote_title_counts_members_not_bot_and_legacy_no(self):
        thread, _, message, _ = suggestion_fixture(bot_reactions=(GREEN_VOTE, RED_VOTE))
        message.reactions[0].count = 3
        message.reactions[1].count = 2
        message.reactions.append(SimpleNamespace(emoji=LEGACY_NO_VOTE, me=False, count=1))
        self.assertEqual(member_vote_counts(message.reactions), (2, 2))
        self.assertTrue(await update_vote_title(thread, message))
        self.assertEqual(thread.name, "🟢2 🔴2 · Park it Under a Roof")
        self.assertFalse(await update_vote_title(thread, message))
        self.assertEqual(thread.edit.await_count, 1)

    async def test_decision_tag_replaces_vote_title_without_changing_tags(self):
        for decision in ("Accepted", "Rejected"):
            with self.subTest(decision=decision):
                final_tag = SimpleNamespace(id=203, name=decision)
                review_tag = SimpleNamespace(id=101, name="Under Review")
                thread, _, message, _ = suggestion_fixture(
                    bot_reactions=(GREEN_VOTE, RED_VOTE),
                    applied_tags=(review_tag, final_tag))
                thread.name = f"{GREEN_VOTE}2 {RED_VOTE}1 \u00b7 Park it Under a Roof"
                self.assertTrue(await update_vote_title(thread, message))
                self.assertEqual(thread.name, f"{decision} \u00b7 Park it Under a Roof")
                self.assertEqual(thread.applied_tags, [review_tag, final_tag])
                self.assertFalse(await update_vote_title(thread, message))

    async def test_changing_decision_replaces_old_title_label(self):
        accepted = SimpleNamespace(id=203, name="Accepted")
        rejected = SimpleNamespace(id=204, name="Rejected")
        thread, _, message, _ = suggestion_fixture(applied_tags=(accepted,))
        thread.name = "Accepted \u00b7 Park it Under a Roof"
        thread.applied_tags.append(rejected)
        self.assertTrue(await update_vote_title(thread, message))
        self.assertEqual(thread.name, "Rejected \u00b7 Park it Under a Roof")

    async def test_decision_title_at_limit_removes_bubbles_without_truncation(self):
        accepted = SimpleNamespace(id=203, name="Accepted")
        thread, _, message, _ = suggestion_fixture(applied_tags=(accepted,))
        original = "A" * 92
        thread.name = f"{GREEN_VOTE}0 {RED_VOTE}0 \u00b7 {original}"
        self.assertTrue(await update_vote_title(thread, message))
        self.assertEqual(thread.name, original)

    async def test_decided_post_does_not_regain_review_tag_on_setup_retry(self):
        final_tag = SimpleNamespace(id=203, name="Accepted")
        thread, forum, message, _ = suggestion_fixture(
            tag_exists=False, bot_reactions=(GREEN_VOTE, RED_VOTE),
            applied_tags=(final_tag,))
        self.assertEqual(await prepare_suggestion(thread, forum, self.bot_user), (456, False))
        self.assertEqual(thread.applied_tags, [final_tag])
        thread.edit.assert_not_awaited()
        message.add_reaction.assert_not_awaited()
        forum.create_tag.assert_not_awaited()

    async def test_reopened_post_shows_vote_title_again(self):
        final_tag = SimpleNamespace(id=203, name="Rejected")
        thread, _, message, _ = suggestion_fixture(
            bot_reactions=(GREEN_VOTE, RED_VOTE), applied_tags=(final_tag,))
        self.assertTrue(await update_vote_title(thread, message))
        self.assertEqual(thread.name, "Rejected \u00b7 Park it Under a Roof")
        thread.applied_tags = [SimpleNamespace(id=101, name="Under Review")]
        self.assertTrue(await update_vote_title(thread, message))
        self.assertEqual(thread.name, f"{GREEN_VOTE}0 {RED_VOTE}0 \u00b7 Park it Under a Roof")

    async def test_long_title_is_not_truncated(self):
        thread, _, message, _ = suggestion_fixture()
        thread.name = "A" * 100
        self.assertFalse(await update_vote_title(thread, message))
        self.assertEqual(thread.name, "A" * 100)
        thread.edit.assert_not_awaited()


class ModSuggestionBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {"DISCORD_OPERATOR_ROLE_IDS": "42"}):
            self.bot = NetworkBot(MagicMock(), {}, 1,
                                  channels={"mod_suggestions": 123, "sin_apply": 10})

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_suggest_mod_command_opens_modal(self):
        interaction = SimpleNamespace(response=SimpleNamespace(
            send_modal=AsyncMock(), send_message=AsyncMock()))
        command = self.bot.tree.get_command("suggest_mod")
        self.assertIsNotNone(command)
        await command.callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, ModSuggestionModal)
        self.assertEqual(len(modal.children), 4)

    async def test_form_submission_creates_tagged_forum_post_and_confirms_link(self):
        thread, forum, _, review = suggestion_fixture()
        thread.jump_url = "https://discord.com/channels/1/123/456"
        forum.create_thread = AsyncMock(return_value=SimpleNamespace(thread=thread))
        self.bot.fetch_channel = AsyncMock(return_value=forum)
        self.bot._handle_mod_suggestion = AsyncMock()
        interaction = SimpleNamespace(
            guild_id=1, user=SimpleNamespace(id=77),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))
        await self.bot._submit_mod_suggestion(
            interaction, mod_name="Fermenting Silo Pack",
            link="https://example.org/mod", adds="Makes silage",
            reason="Useful for SiN")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        forum.create_thread.assert_awaited_once()
        kwargs = forum.create_thread.await_args.kwargs
        self.assertEqual(kwargs["name"], "Fermenting Silo Pack")
        self.assertEqual(kwargs["applied_tags"], [review])
        self.assertIn("<@77>", kwargs["content"])
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        interaction.followup.send.assert_awaited_once_with(
            "Suggestion posted in #mod-suggestions: " + thread.jump_url,
            ephemeral=True)
        self.bot._handle_mod_suggestion.assert_awaited_once_with(thread)

    async def test_form_submission_fails_closed_if_forum_unavailable(self):
        self.bot.fetch_channel = AsyncMock(return_value=SimpleNamespace(id=123))
        interaction = SimpleNamespace(
            guild_id=1, user=SimpleNamespace(id=77),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))
        await self.bot._submit_mod_suggestion(
            interaction, mod_name="Mod", link="https://example.org/mod",
            adds="Adds a silo", reason="Useful")
        self.assertIn("couldn't confirm", interaction.followup.send.await_args.args[0])

    async def test_form_validation_does_not_create_post(self):
        self.bot.fetch_channel = AsyncMock()
        interaction = SimpleNamespace(
            guild_id=1, user=SimpleNamespace(id=77),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))
        await self.bot._submit_mod_suggestion(
            interaction, mod_name="Mod", link="not a link",
            adds="Adds a silo", reason="Useful")
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()

    async def test_thread_creation_only_processes_configured_forum(self):
        thread, forum, message, _ = suggestion_fixture()
        thread.guild = SimpleNamespace(id=1)
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id:
                                           forum if channel_id == forum.id else thread)
        await self.bot.on_thread_create(thread)
        self.bot.fetch_channel.assert_has_awaits([call(123), call(456)])
        self.assertEqual(message.add_reaction.await_count, 2)
        self.assertEqual(thread.name, "🟢0 🔴0 · Park it Under a Roof")
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
        self.assertEqual(thread.edit.await_count, 2)
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
        self.assertEqual(current.name, "🟢0 🔴0 · Park it Under a Roof")
        message.add_reaction.assert_not_awaited()
        self.bot.fetch_channel.assert_has_awaits([call(forum.id), call(current.id)])

    async def test_reaction_event_refreshes_existing_forum_card(self):
        thread, _, message, _ = suggestion_fixture(bot_reactions=(GREEN_VOTE, RED_VOTE))
        message.reactions[0].count = 2
        message.reactions[1].count = 2
        message.reactions.append(SimpleNamespace(emoji=LEGACY_NO_VOTE, me=False, count=1))
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        payload = SimpleNamespace(guild_id=1, channel_id=456, message_id=456)
        await self.bot.on_raw_reaction_add(payload)
        self.assertEqual(thread.name, "🟢1 🔴2 · Park it Under a Roof")
        message.reactions[1].count = 1
        await self.bot.on_raw_reaction_remove(payload)
        self.assertEqual(thread.name, "🟢1 🔴1 · Park it Under a Roof")
        payload.message_id = 999
        await self.bot.on_raw_reaction_add(payload)
        self.assertEqual(self.bot.fetch_channel.await_count, 2)

    async def test_reaction_event_ignores_other_forum(self):
        thread, _, _, _ = suggestion_fixture()
        thread.parent_id = 999
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        await self.bot.on_raw_reaction_add(
            SimpleNamespace(guild_id=1, channel_id=456, message_id=456))
        thread.fetch_message.assert_not_awaited()

    async def test_decision_tag_update_removes_vote_bubbles_immediately(self):
        thread, _, message, review = suggestion_fixture(
            bot_reactions=(GREEN_VOTE, RED_VOTE), applied_tags=())
        thread.name = f"{GREEN_VOTE}2 {RED_VOTE}1 \u00b7 Park it Under a Roof"
        accepted = SimpleNamespace(id=203, name="Accepted")
        thread.applied_tags = [accepted]
        thread.guild = SimpleNamespace(id=1)
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        before = SimpleNamespace(applied_tags=[review])
        await self.bot.on_thread_update(before, thread)
        self.assertEqual(thread.name, "Accepted \u00b7 Park it Under a Roof")
        self.assertEqual(thread.applied_tags, [accepted])
        self.bot.fetch_channel.assert_awaited_once_with(thread.id)
        thread.fetch_message.assert_awaited_once_with(thread.id)
        message.reactions[0].count = 3
        await self.bot.on_raw_reaction_add(SimpleNamespace(
            guild_id=1, channel_id=thread.id, message_id=thread.id))
        self.assertEqual(thread.name, "Accepted \u00b7 Park it Under a Roof")

    async def test_unrelated_thread_update_does_not_refresh_votes(self):
        thread, _, _, _ = suggestion_fixture()
        thread.guild = SimpleNamespace(id=1)
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        before = SimpleNamespace(applied_tags=[])
        await self.bot.on_thread_update(before, thread)
        thread.parent_id = 999
        thread.applied_tags = [SimpleNamespace(id=203, name="Rejected")]
        await self.bot.on_thread_update(before, thread)
        self.bot.fetch_channel.assert_not_awaited()

    async def test_startup_refreshes_active_posts(self):
        thread, _, message, _ = suggestion_fixture(bot_reactions=(GREEN_VOTE, RED_VOTE))
        message.reactions[0].count = 3
        self.bot._connection.user = SimpleNamespace(id=999)
        self.bot.get_guild = MagicMock(return_value=SimpleNamespace(
            id=1, threads=[thread, SimpleNamespace(id=999, parent_id=999)]))
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        await self.bot.on_ready()
        self.assertEqual(thread.name, "🟢2 🔴0 · Park it Under a Roof")
        self.bot.fetch_channel.assert_awaited_once_with(456)


if __name__ == "__main__":
    unittest.main()
