"""Forum-post setup for community mod suggestions.

The bot's two reactions are neutral seed votes. Discord still includes them in
the visible counts; a member-only tally must subtract one from each emoji.
"""

import logging

GREEN_VOTE = "\U0001f7e2"
RED_VOTE = "\U0001f534"
REVIEW_TAG = "Under Review"


async def prepare_suggestion(thread, forum, bot_user):
    """Idempotently tag a new forum post and seed its starter message.

    The caller supplies a freshly fetched thread. Reading the starter message
    makes this safe when gateway events arrive in either order or are replayed.
    """
    current = thread
    if current.parent_id != forum.id:
        raise ValueError("suggestion thread no longer belongs to the configured forum")

    tag = next((item for item in forum.available_tags
                if item.name.casefold() == REVIEW_TAG.casefold()), None)
    if tag is None:
        tag = await forum.create_tag(name=REVIEW_TAG, moderated=True,
                                     reason="SiN mod suggestion review status")

    changed = False
    applied = list(current.applied_tags)
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
