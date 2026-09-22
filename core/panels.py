from __future__ import annotations

import logging

import discord

log = logging.getLogger("manager.panels")

SCAN_LIMIT = 60


async def clear_previous(
    channel: discord.abc.Messageable,
    bot_user_id: int,
    *,
    keep: int | None = None,
) -> int:
    removed = 0
    try:
        async for message in channel.history(limit=SCAN_LIMIT):
            if message.author.id != bot_user_id:
                continue
            if keep is not None and message.id == keep:
                continue
            if not message.components:
                continue
            try:
                await message.delete()
                removed += 1
            except discord.NotFound:
                continue
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not delete panel message %s: %s", message.id, exc)
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Could not read history in %s: %s", getattr(channel, "id", "?"), exc)
    return removed


async def resolve_channel(bot: discord.Client, channel_id: int) -> discord.TextChannel | None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        log.warning("Panel channel %s is not a text channel.", channel_id)
        return None
    return channel


async def repost(
    bot: discord.Client,
    *,
    channel_id: int,
    view: discord.ui.LayoutView,
    label: str,
) -> discord.Message | None:
    channel = await resolve_channel(bot, channel_id)
    if channel is None:
        log.warning("The %s panel channel %s is unreachable.", label, channel_id)
        return None

    removed = await clear_previous(channel, bot.user.id if bot.user else 0)

    try:
        message = await channel.send(view=view)
    except discord.HTTPException:
        log.exception("Could not post the %s panel.", label)
        return None

    log.info("%s panel reposted as %s, removed %d old panel(s).", label.title(), message.id, removed)
    return message
