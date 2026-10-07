"""Forum-post setup for community mod suggestions.

The bot's two reactions are neutral seed votes. Discord still includes them in
the visible counts; a member-only tally must subtract one from each emoji.
"""

import logging
import re
from urllib.parse import urlsplit

import discord

GREEN_VOTE = "\U0001f7e2"
RED_VOTE = "\U0001f534"
LEGACY_NO_VOTE = "\u274e"
REVIEW_TAG = "Under Review"
DECISION_TAGS = {"accepted": "Accepted", "rejected": "Rejected"}
VOTE_TITLE_PREFIX = re.compile(r"^\U0001f7e2\d+ \U0001f534\d+ \u00b7 ")
DECISION_TITLE_PREFIX = re.compile(r"^(?:Accepted|Rejected) \u00b7 ", re.IGNORECASE)


def build_suggestion_post(*, mod_name, link, adds, reason, submitter_id):
    """Render the four-field template for a JiN-created post."""
    fields = {
        "mod_name": str(mod_name or "").strip(),
        "link": str(link or "").strip(),
        "adds": str(adds or "").strip(),
        "reason": str(reason or "").strip(),
    }
    if not all(fields.values()):
        raise ValueError("Complete every field before submitting")
    if len(fields["mod_name"]) > 80 or "\n" in fields["mod_name"]:
        raise ValueError("Mod name must be a single line of at most 80 characters")
    parsed = urlsplit(fields["link"])
    if (parsed.scheme.lower() not in {"https", "http"}
            or not parsed.netloc or any(char.isspace() for char in fields["link"])):
        raise ValueError("Link must be a complete http:// or https:// URL")
    safe_name = discord.utils.escape_mentions(discord.utils.escape_markdown(fields["mod_name"]))
    safe_adds = discord.utils.escape_mentions(discord.utils.escape_markdown(fields["adds"]))
    safe_reason = discord.utils.escape_mentions(discord.utils.escape_markdown(fields["reason"]))
    content = (f"**Mod Name:**\n{safe_name}\n\n"
               f"**Link:**\n{fields['link']}\n\n"
               f"**What does it add?**\n{safe_adds}\n\n"
               f"**Why should SiN add it?**\n{safe_reason}\n\n"
               f"**Submitted by:** <@{int(submitter_id)}>")
    if len(content) > 2000:
        raise ValueError("Suggestion is too long for a Discord forum post")
    return fields["mod_name"], content


class ModSuggestionModal(discord.ui.Modal, title="Suggest a mod"):
    mod_name = discord.ui.TextInput(label="Mod Name", max_length=80,
                                    placeholder="Name of the mod")
    link = discord.ui.TextInput(label="Link", max_length=300,
                                placeholder="https://www.farming-simulator.com/mod.php?...")
    adds = discord.ui.TextInput(label="What does it add?", style=discord.TextStyle.paragraph,
                                max_length=350)
    reason = discord.ui.TextInput(label="Why should SiN add it?", style=discord.TextStyle.paragraph,
                                  max_length=350)

    def __init__(self, submit):
        super().__init__()
        self._submit = submit

    async def on_submit(self, interaction):
        await self._submit(interaction, mod_name=self.mod_name.value,
                           link=self.link.value, adds=self.adds.value,
                           reason=self.reason.value)

    async def on_error(self, interaction, error):
        logging.error("Mod suggestion form failed user=%s", interaction.user.id,
                      exc_info=(type(error), error, error.__traceback__))
        message = "Suggestion could not be confirmed. Check #mod-suggestions before retrying."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def ensure_review_tag(forum):
    tag = next((item for item in forum.available_tags
                if item.name.casefold() == REVIEW_TAG.casefold()), None)
    if tag is None:
        tag = await forum.create_tag(name=REVIEW_TAG, moderated=True,
                                     reason="SiN mod suggestion review status")
    return tag


def decision_label(thread):
    """Use the last applied decision tag if moderators applied both."""
    for tag in reversed(thread.applied_tags):
        label = DECISION_TAGS.get(tag.name.casefold())
        if label is not None:
            return label
    return None


def has_decision_tag(thread):
    return decision_label(thread) is not None


def original_title(title):
    """Remove JiN's vote or decision prefix before changing status."""
    while True:
        cleaned = DECISION_TITLE_PREFIX.sub("", VOTE_TITLE_PREFIX.sub("", title, count=1), count=1)
        if cleaned == title:
            return title
        title = cleaned


def member_vote_counts(reactions):
    """Count starter-message votes, excluding JiN's neutral seed reactions."""
    counts = {GREEN_VOTE: 0, RED_VOTE: 0, LEGACY_NO_VOTE: 0}
    for reaction in reactions:
        emoji = str(reaction.emoji)
        if emoji in counts:
            counts[emoji] += max(0, reaction.count - int(reaction.me))
    return counts[GREEN_VOTE], counts[RED_VOTE] + counts[LEGACY_NO_VOTE]


async def update_vote_title(thread, message):
    """Show active votes or the final Accepted/Rejected label in the title."""
    original = original_title(thread.name)
    label = decision_label(thread)
    if label is not None:
        desired = f"{label} \u00b7 {original}"
        if len(desired) > 100:
            # Preserve the full user title; the native decision tag is still
            # visible even when Discord's title limit bars a second label.
            logging.warning("Mod suggestion decision title exceeds Discord limit thread=%s", thread.id)
            desired = original
    else:
        yes, no = member_vote_counts(message.reactions)
        desired = f"{GREEN_VOTE}{yes} {RED_VOTE}{no} \u00b7 {original}"
    if desired == thread.name:
        return False
    if len(desired) > 100:
        logging.warning("Mod suggestion vote title exceeds Discord limit thread=%s", thread.id)
        return False
    await thread.edit(name=desired, reason="Reflect mod-suggestion voting status in forum list")
    return True


async def prepare_suggestion(thread, forum, bot_user):
    """Idempotently tag a new forum post and seed its starter message.

    The caller supplies a freshly fetched thread. Reading the starter message
    makes this safe when gateway events arrive in either order or are replayed.
    """
    current = thread
    if current.parent_id != forum.id:
        raise ValueError("suggestion thread no longer belongs to the configured forum")

    changed = False
    applied = list(current.applied_tags)
    if not has_decision_tag(current):
        tag = await ensure_review_tag(forum)
        if not any(item.id == tag.id for item in applied):
            if len(applied) >= 5:
                raise ValueError("suggestion already has Discord's maximum five tags")
            await current.edit(applied_tags=[*applied, tag],
                               reason="Mark new SiN mod suggestion Under Review")
            changed = True

    # Forum starter messages have the same ID as their thread. A supplied
    # gateway message can have stale reaction data, so always read back from
    # Discord before deciding which of JiN's reactions are missing.
    message = await current.fetch_message(current.id)
    seeded = {str(reaction.emoji) for reaction in message.reactions
              if reaction.me}
    try:
        for emoji in (GREEN_VOTE, RED_VOTE):
            if emoji not in seeded:
                await message.add_reaction(emoji)
                seeded.add(emoji)
                changed = True
    except Exception:
        # Discord has no atomic multi-reaction call. If either seed fails,
        # retract JiN's counterpart so the bot cannot leave a one-sided vote.
        for emoji in seeded:
            try:
                await message.remove_reaction(emoji, bot_user)
            except Exception as error:
                logging.error("Could not retract unbalanced mod-suggestion seed thread=%s emoji=%s: %s",
                              current.id, emoji, error)
        raise
    return current.id, changed
