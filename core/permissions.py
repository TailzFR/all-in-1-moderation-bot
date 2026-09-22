from __future__ import annotations

import discord

import config


def role_level(role_id: int) -> int:
    return config.ROLE_LEVELS.get(role_id, 0)


def level_of(member: discord.abc.User | None) -> int:
    if not isinstance(member, discord.Member):
        return 0
    if member.guild is not None and member.guild.owner_id == member.id:
        return config.TOP_LEVEL
    return max((role_level(role.id) for role in member.roles), default=0)


def rank_name(level: int) -> str:
    if level <= 0:
        return "Member"
    for entry in config.STAFF_ROLES:
        if entry["level"] == level:
            return entry["name"]
    return "Staff"


def rank_or_above(level: int) -> str:
    name = rank_name(level)
    return name if level >= config.TOP_LEVEL else f"{name} or above"


def title_of(member: discord.abc.User | None) -> str:
    return rank_name(level_of(member))


def effective_level(bot: discord.Client, user: discord.abc.User | None) -> int:
    if user is None:
        return 0

    here = level_of(user)
    if here > 0:
        return here

    main = getattr(bot, "primary_guild", None)
    if main is None:
        return here
    member = main.get_member(user.id)
    return level_of(member) if member is not None else here


def effective_title(bot: discord.Client, user: discord.abc.User | None) -> str:
    return rank_name(effective_level(bot, user))


def is_staff(member: discord.abc.User | None) -> bool:
    return level_of(member) >= 1


def has_level(member: discord.abc.User | None, minimum: int) -> bool:
    return level_of(member) >= minimum


def outranks(actor: discord.abc.User | None, target: discord.abc.User | None) -> bool:
    return level_of(actor) > level_of(target)


def staff_role_entry(role_id: int) -> dict | None:
    if not role_id:
        return None
    for entry in config.STAFF_ROLES:
        if entry["id"] == role_id:
            return entry
    return None


def resolve_staff_role(guild: discord.Guild, query: str) -> discord.Role | None:
    cleaned = query.strip().strip("<@&>").strip()

    if cleaned.isdigit():
        role_id = int(cleaned)
        if role_id in config.STAFF_ROLE_IDS:
            return guild.get_role(role_id)
        return None

    needle = cleaned.casefold().replace("-", " ").replace("_", " ")
    needle = " ".join(needle.split())

    exact: discord.Role | None = None
    partial: discord.Role | None = None
    for entry in config.STAFF_ROLES:
        name = entry["name"].casefold()
        role = guild.get_role(entry["id"]) if entry["id"] else None
        if role is None:
            continue
        if name == needle or entry["key"] == needle.replace(" ", "_"):
            exact = role
            break
        if needle and (needle in name or name in needle):
            partial = partial or role
    return exact or partial


def staff_overwrites(guild: discord.Guild) -> dict:
    overwrites: dict = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
        ),
    }
    for entry in config.STAFF_ROLES:
        role = guild.get_role(entry["id"]) if entry["id"] else None
        if role is not None:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                attach_files=True,
                embed_links=True,
                read_message_history=True,
            )
    return overwrites
