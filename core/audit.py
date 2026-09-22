from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

import discord

import config
from core import permissions, ui

log = logging.getLogger("manager.audit")

_AUDIT_WINDOW_SECONDS = 12


async def post(
    bot: discord.Client,
    *,
    title: str,
    body: str | None = None,
    fields: Sequence[tuple[str, str]] | None = None,
    accent: int = config.COLOR_NEUTRAL,
    footer: str | None = None,
    thumbnail: str | None = None,
    subtitle: str | None = None,
    file: discord.File | None = None,
) -> discord.Message | None:
    channel = bot.get_channel(config.SERVER_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(config.SERVER_LOG_CHANNEL_ID)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            log.warning("Server log channel %s is unreachable.", config.SERVER_LOG_CHANNEL_ID)
            return None

    extra = [ui.dui.File(f"attachment://{file.filename}")] if file is not None else None

    view = ui.notice(
        title=title,
        subtitle=subtitle,
        body=body,
        fields=fields,
        footer=footer or config.BOT_NAME,
        accent=accent,
        thumbnail=thumbnail,
        extra_items=extra,
    )
    try:
        if file is not None:
            return await channel.send(view=view, file=file)
        return await channel.send(view=view)
    except discord.HTTPException as exc:
        log.warning("Could not write to the server log: %s", exc)
        return None


def actor_note(actor: discord.abc.User | None, fallback: str = "Performed automatically by the bot.") -> str:
    if actor is None:
        return fallback
    rank = permissions.title_of(actor)
    return f"Performed by {actor} ({actor.id}) - {rank}."


async def find_actor(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int | None = None,
) -> tuple[discord.abc.User | None, str | None]:
    me = guild.me
    if me is None or not me.guild_permissions.view_audit_log:
        return None, None

    now = discord.utils.utcnow()
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            if (now - entry.created_at).total_seconds() > _AUDIT_WINDOW_SECONDS:
                break
            if target_id is not None:
                entry_target = getattr(entry.target, "id", None)
                if entry_target != target_id:
                    continue
            return entry.user, entry.reason
    except (discord.Forbidden, discord.HTTPException):
        return None, None
    except asyncio.TimeoutError:
        return None, None
    return None, None


def describe(user: discord.abc.User | None) -> str:
    if user is None:
        return "Unknown"
    return f"{user} ({user.id})"


def diff_roles(
    before: Sequence[discord.Role],
    after: Sequence[discord.Role],
) -> tuple[list[discord.Role], list[discord.Role]]:
    before_set = {role.id: role for role in before}
    after_set = {role.id: role for role in after}
    added = [role for rid, role in after_set.items() if rid not in before_set]
    removed = [role for rid, role in before_set.items() if rid not in after_set]
    return added, removed


def summarise(value: Any, limit: int = 400) -> str:
    text = "None" if value in (None, "") else str(value)
    return ui.clip(text, limit)
