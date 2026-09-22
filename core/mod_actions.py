from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import discord

import config
from core import audit, duration as dur, ui
from core.memory import Memory
from core.rules import Clause, Punishment

log = logging.getLogger("manager.mod")

ACTION_ACCENT = {
    "warn": config.COLOR_WARNING,
    "mute": config.COLOR_WARNING,
    "kick": config.COLOR_DANGER,
    "ban": config.COLOR_DANGER,
}

ACTION_HEADLINE = {
    "warn": "Warning Issued",
    "mute": "You Have Been Muted",
    "kick": "You Have Been Removed",
    "ban": "You Have Been Banned",
}


@dataclass
class Result:
    case: dict[str, Any]
    punishment: Punishment
    applied: bool
    dm_delivered: bool
    error: str | None = None


async def ensure_muted_role(
    guild: discord.Guild,
    memory: Memory,
    *,
    sync: bool = False,
) -> discord.Role | None:
    stored = memory.guild_setting("muted_role")
    role = guild.get_role(int(stored)) if stored else None

    if role is None:
        role = discord.utils.get(guild.roles, name=config.MUTED_ROLE_NAME)

    created = False
    if role is None:
        try:
            role = await guild.create_role(
                name=config.MUTED_ROLE_NAME,
                colour=discord.Colour(0x546E7A),
                permissions=discord.Permissions.none(),
                hoist=False,
                mentionable=False,
                reason=f"{config.BOT_NAME} requires a mute role for mute cases.",
            )
            created = True
            log.info("Created the %s role in %s.", config.MUTED_ROLE_NAME, guild.name)
        except discord.Forbidden:
            log.error("Missing Manage Roles permission, cannot create the Muted role.")
            return None
        except discord.HTTPException as exc:
            log.error("Could not create the Muted role: %s", exc)
            return None

    memory.set_guild_setting("muted_role", str(role.id))
    if created or sync:
        await sync_muted_role(guild, role)
    return role


MUTED_DENIALS = (
    "send_messages",
    "send_messages_in_threads",
    "create_public_threads",
    "create_private_threads",
    "add_reactions",
    "speak",
)


async def sync_muted_role(guild: discord.Guild, role: discord.Role) -> int:
    updated = 0
    for channel in guild.channels:
        overwrite = channel.overwrites_for(role)
        changed = False
        for name in MUTED_DENIALS:
            if getattr(overwrite, name) is not False:
                setattr(overwrite, name, False)
                changed = True
        if not changed:
            continue
        try:
            await channel.set_permissions(role, overwrite=overwrite, reason="Muted role synchronisation.")
            updated += 1
        except (discord.Forbidden, discord.HTTPException):
            continue
    return updated


async def appeal_invite(bot: discord.Client, memory: Memory) -> str | None:
    if config.APPEAL_INVITE_URL:
        return config.APPEAL_INVITE_URL

    cached = memory.guild_setting("appeal_invite")
    if cached:
        return cached

    guild = bot.get_guild(config.APPEAL_GUILD_ID)
    if guild is None:
        return None

    channel = guild.get_channel(config.APPEAL_PANEL_CHANNEL_ID)
    if channel is None:
        channel = next((c for c in guild.text_channels), None)
    if channel is None:
        return None

    try:
        invite = await channel.create_invite(
            max_age=0,
            max_uses=0,
            unique=False,
            reason="Permanent appeal server link for ban notices.",
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Could not create an appeal server invite: %s", exc)
        return None

    memory.set_guild_setting("appeal_invite", invite.url)
    return invite.url


def build_notice(
    *,
    guild: discord.Guild,
    action: str,
    punishment: Punishment,
    clause: Clause,
    offense: int,
    case_id: int,
    reason: str,
    expires_at: float | None,
    invite: str | None = None,
) -> ui.Layout:
    fields: list[tuple[str, str]] = [
        ("Rule", f"{clause.reference} {clause.section_title}"),
        ("Offence", clause.title),
        ("Punishment", punishment.label),
    ]
    if action in {"mute", "ban"}:
        fields.append(("Expires", dur.timestamp(expires_at) if expires_at else "Never"))
    fields.append(("Offence number", f"{offense}"))
    fields.append(("Case", f"#{case_id}"))
    if reason:
        fields.append(("Staff notes", reason))

    appealable = action == "ban" and clause.section_id not in config.APPEAL_EXCLUDED_RULES

    body = (
        f"This notice concerns your conduct in **{guild.name}**.\n"
        f"The punishment below was applied under the {config.COMMUNITY_NAME} rulebook."
    )

    extra: list = []

    if action == "ban" and not appealable:
        footer = config.APPEAL_NOTICE_FINAL
    elif action == "ban":
        footer = config.APPEAL_NOTICE
        fields.append(("Punishment ladder for this rule", "\n".join(clause.tier_labels)))
        if invite:
            fields.append(("Appeal server", invite))
            body += (
                "\n\nYou can appeal this decision. Join the appeal server below and use the "
                "appeal panel there."
            )
            row = ui.dui.ActionRow()
            row.add_item(
                ui.dui.Button(
                    label="Join the Appeal Server",
                    style=discord.ButtonStyle.link,
                    url=invite,
                )
            )
            extra.append(row)
        else:
            body += "\n\nYou can appeal this in the appeal server. Ask a staff member for the link."
    elif action == "mute":
        footer = "You may open a ticket to discuss this decision. Do not evade the mute."
    else:
        footer = "Repeat offences escalate automatically under the rulebook."

    return ui.notice(
        title=ACTION_HEADLINE.get(action, "Moderation Notice"),
        subtitle=f"{config.COMMUNITY_NAME} Moderation",
        body=body,
        fields=fields,
        footer=footer,
        accent=ACTION_ACCENT.get(action, config.COLOR_NEUTRAL),
        thumbnail=guild.icon.url if guild.icon else None,
        extra_items=extra or None,
    )


async def send_dm(user: discord.abc.User, view: ui.Layout) -> bool:
    try:
        await user.send(view=view)
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False


async def apply(
    bot: discord.Client,
    *,
    guild: discord.Guild,
    target: discord.Member | discord.User,
    moderator: discord.abc.User,
    clause: Clause,
    offense: int,
    memory: Memory,
    reason: str = "",
    evidence: str | None = None,
    source: str = "command",
    notify: bool = True,
    proof: dict[str, Any] | None = None,
    proof_file: discord.File | None = None,
) -> Result:
    punishment = clause.punishment(offense)
    expires = dur.expiry(punishment.seconds)
    expires_at = expires.timestamp() if expires else None

    case = memory.create_case(
        user_id=target.id,
        moderator_id=moderator.id,
        rule_id=clause.section_id,
        clause_id=clause.clause_id,
        rule_title=clause.section_title,
        clause_title=clause.title,
        offense=offense,
        action=punishment.action,
        duration=punishment.seconds,
        duration_label=punishment.label,
        reason=reason,
        evidence=evidence,
        source=source,
    )
    case["expires_at"] = expires_at
    if proof:
        case["proof"] = proof.get("url")
        case["proof_meta"] = proof
    memory.store.mark_dirty()

    audit_reason = ui.clip(
        f"Case #{case['id']} | {clause.reference} | {punishment.label} | by {moderator}",
        480,
    )

    dm_delivered = False
    if notify:
        invite = await appeal_invite(bot, memory) if punishment.action == "ban" else None
        notice = build_notice(
            guild=guild,
            action=punishment.action,
            punishment=punishment,
            clause=clause,
            offense=offense,
            case_id=case["id"],
            reason=reason,
            expires_at=expires_at,
            invite=invite,
        )
        dm_delivered = await send_dm(target, notice)

    applied = True
    error: str | None = None

    try:
        if punishment.action == "mute":
            member = guild.get_member(target.id)
            if member is None:
                applied = False
                error = "The member is not in the server, so the mute could not be applied."
            else:
                role = await ensure_muted_role(guild, memory)
                if role is None:
                    applied = False
                    error = "The Muted role could not be created or found."
                else:
                    await member.add_roles(role, reason=audit_reason)
                    memory.drop_schedules_for(target.id, "unmute")
                    if punishment.seconds is not None:
                        memory.schedule(
                            kind="unmute",
                            user_id=target.id,
                            guild_id=guild.id,
                            expires_at=expires_at or 0,
                            case_id=case["id"],
                        )

        elif punishment.action == "ban":
            await guild.ban(
                discord.Object(id=target.id),
                reason=audit_reason,
                delete_message_seconds=0,
            )
            memory.drop_schedules_for(target.id, "unban")
            if punishment.seconds is not None:
                memory.schedule(
                    kind="unban",
                    user_id=target.id,
                    guild_id=guild.id,
                    expires_at=expires_at or 0,
                    case_id=case["id"],
                )

        elif punishment.action == "kick":
            member = guild.get_member(target.id)
            if member is None:
                applied = False
                error = "The member is not in the server."
            else:
                await member.kick(reason=audit_reason)

    except discord.Forbidden:
        applied = False
        error = "The bot lacks the permission required to apply this punishment."
    except discord.HTTPException as exc:
        applied = False
        error = f"Discord rejected the action: {exc}"

    if not applied:
        case["active"] = False
        case["error"] = error
        memory.store.mark_dirty()

    log_fields = [
        ("Member", audit.describe(target)),
        ("Punishment", punishment.label),
        ("Offence", f"{clause.title} ({_ordinal(offense)} offence)"),
        ("Expires", dur.timestamp(expires_at) if expires_at else "Never"),
        ("Reason", reason or "No additional notes."),
    ]
    if proof:
        log_fields.append(
            ("Proof", f"{proof.get('filename', 'attachment')} ({proof.get('content_type', 'unknown')})")
        )
        if proof_file is None and proof.get("url"):
            log_fields.append(("Proof link", proof["url"]))
    if evidence:
        log_fields.append(("Evidence", evidence))
    log_fields.append(("Notice delivered", "Yes" if dm_delivered else "No, direct messages are closed."))
    log_fields.append(("Status", "Applied" if applied else f"Failed - {error}"))

    entry = await audit.post(
        bot,
        title=f"Punishment Applied - Case #{case['id']}",
        subtitle=f"{clause.reference} {clause.section_title}",
        accent=ACTION_ACCENT.get(punishment.action, config.COLOR_NEUTRAL),
        fields=log_fields,
        footer=audit.actor_note(moderator),
        thumbnail=getattr(target.display_avatar, "url", None),
        file=proof_file,
    )

    if entry is not None:
        case["log_message"] = entry.jump_url
        if proof_file is not None and entry.attachments:
            case["proof"] = entry.attachments[0].url
            case.setdefault("proof_meta", {})["archived_url"] = entry.attachments[0].url
        memory.store.mark_dirty()

    return Result(case=case, punishment=punishment, applied=applied, dm_delivered=dm_delivered, error=error)


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


async def lift_mute(
    bot: discord.Client,
    guild: discord.Guild,
    user_id: int,
    memory: Memory,
    *,
    reason: str,
    moderator: discord.abc.User | None = None,
    case_id: int | None = None,
) -> bool:
    member = guild.get_member(user_id)
    role_id = memory.guild_setting("muted_role")
    role = guild.get_role(int(role_id)) if role_id else None

    removed = False
    if member is not None and role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason=ui.clip(reason, 480))
            removed = True
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not remove the Muted role from %s: %s", user_id, exc)

    memory.drop_schedules_for(user_id, "unmute")
    if case_id is not None:
        memory.deactivate_case(case_id)

    if removed:
        if member is not None:
            await send_dm(
                member,
                ui.notice(
                    title="Mute Lifted",
                    subtitle=f"{config.COMMUNITY_NAME} Moderation",
                    body=f"Your mute in **{guild.name}** has ended. You can speak in the server again.",
                    fields=[("Reason", reason)],
                    footer="Further breaches of the rulebook escalate automatically.",
                    accent=config.COLOR_SUCCESS,
                    thumbnail=guild.icon.url if guild.icon else None,
                ),
            )
        await audit.post(
            bot,
            title="Mute Lifted",
            accent=config.COLOR_SUCCESS,
            fields=[
                ("Member", audit.describe(member) if member else str(user_id)),
                ("Case", f"#{case_id}" if case_id else "Not linked"),
                ("Reason", reason),
            ],
            footer=audit.actor_note(moderator, "Mute expired automatically."),
        )
    return removed


async def lift_ban(
    bot: discord.Client,
    guild: discord.Guild,
    user_id: int,
    memory: Memory,
    *,
    reason: str,
    moderator: discord.abc.User | None = None,
    case_id: int | None = None,
    notes: str = "",
) -> tuple[bool, str | None]:
    try:
        await guild.unban(discord.Object(id=user_id), reason=ui.clip(reason, 480))
    except discord.NotFound:
        memory.drop_schedules_for(user_id, "unban")
        return False, "That user is not banned from this server."
    except discord.Forbidden:
        return False, "The bot lacks the Ban Members permission."
    except discord.HTTPException as exc:
        return False, f"Discord rejected the unban: {exc}"

    memory.drop_schedules_for(user_id, "unban")
    if case_id is not None:
        memory.deactivate_case(case_id)
    else:
        for case in memory.active_cases_of_action(user_id, "ban"):
            memory.deactivate_case(case["id"])

    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            user = None

    if user is not None:
        await send_dm(
            user,
            ui.notice(
                title="Ban Lifted",
                subtitle=f"{config.COMMUNITY_NAME} Moderation",
                body=(
                    f"Your ban from **{guild.name}** has been lifted. "
                    "You are welcome to rejoin the server."
                ),
                fields=[("Notes", notes)] if notes else None,
                footer="Read the rulebook before you return. Further breaches escalate automatically.",
                accent=config.COLOR_SUCCESS,
                thumbnail=guild.icon.url if guild.icon else None,
            ),
        )

    await audit.post(
        bot,
        title="Ban Lifted",
        accent=config.COLOR_SUCCESS,
        fields=[
            ("Member", audit.describe(user) if user else str(user_id)),
            ("Case", f"#{case_id}" if case_id else "Not linked"),
            ("Reason", reason),
            ("Notes", notes or "None provided."),
        ],
        footer=audit.actor_note(moderator, "Ban expired automatically."),
    )
    return True, None
