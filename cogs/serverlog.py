from __future__ import annotations

import logging

import discord
from discord.ext import commands

import config
from core import audit, permissions, ui
from core.memory import Memory

log = logging.getLogger("manager.serverlog")

MAX_CONTENT = 900


class ServerLog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.memory: Memory = bot.memory

    def skip_channel(self, channel: discord.abc.GuildChannel | None) -> bool:
        if channel is None:
            return True
        if channel.id in (config.SERVER_LOG_CHANNEL_ID, config.TRANSCRIPT_CHANNEL_ID):
            return True
        category_id = getattr(channel, "category_id", None)
        return category_id == config.TICKET_CATEGORY_ID

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        self.memory.remember_name(member.id, str(member))
        self.memory.record_join(member.id, "join")

        cases = self.memory.cases_for(member.id, include_revoked=False)
        await audit.post(
            self.bot,
            title="Member Joined",
            accent=config.COLOR_SUCCESS,
            fields=[
                ("Member", audit.describe(member)),
                ("Account created", discord.utils.format_dt(member.created_at, "R")),
                ("Cases on record", f"{len(cases)} live" if cases else "None"),
                ("Members", str(member.guild.member_count)),
            ],
            footer="Joined the server.",
            thumbnail=str(member.display_avatar.url),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        self.memory.record_join(member.id, "leave")

        actor, reason = await audit.find_actor(member.guild, discord.AuditLogAction.kick, member.id)
        if actor is not None:
            await audit.post(
                self.bot,
                title="Member Kicked",
                accent=config.COLOR_DANGER,
                fields=[
                    ("Member", audit.describe(member)),
                    ("Reason", reason or "No reason recorded."),
                ],
                footer=audit.actor_note(actor),
                thumbnail=str(member.display_avatar.url),
            )
            return

        roles = ", ".join(r.name for r in member.roles if not r.is_default()) or "None"
        await audit.post(
            self.bot,
            title="Member Left",
            accent=config.COLOR_NEUTRAL,
            fields=[
                ("Member", audit.describe(member)),
                ("Roles held", ui.clip(roles, 500)),
                (
                    "Joined",
                    discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown",
                ),
            ],
            footer="Left the server.",
            thumbnail=str(member.display_avatar.url),
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        actor, reason = await audit.find_actor(guild, discord.AuditLogAction.ban, user.id)
        by_bot = actor is not None and self.bot.user is not None and actor.id == self.bot.user.id

        await audit.post(
            self.bot,
            title="Member Banned",
            accent=config.COLOR_DANGER,
            fields=[
                ("Member", audit.describe(user)),
                ("Reason", reason or "No reason recorded."),
            ],
            footer=(
                "Applied by the bot under the rulebook."
                if by_bot
                else audit.actor_note(actor, "Banned outside the bot, with no audit entry available.")
            ),
            thumbnail=str(user.display_avatar.url),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        actor, reason = await audit.find_actor(guild, discord.AuditLogAction.unban, user.id)
        by_bot = actor is not None and self.bot.user is not None and actor.id == self.bot.user.id
        if by_bot:
            return

        await audit.post(
            self.bot,
            title="Member Unbanned",
            accent=config.COLOR_SUCCESS,
            fields=[
                ("Member", audit.describe(user)),
                ("Reason", reason or "No reason recorded."),
            ],
            footer=audit.actor_note(actor, "Unbanned outside the bot."),
            thumbnail=str(user.display_avatar.url),
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles != after.roles:
            await self._log_role_change(before, after)

        if before.nick != after.nick:
            actor, _ = await audit.find_actor(after.guild, discord.AuditLogAction.member_update, after.id)
            self.memory.remember_name(after.id, after.display_name)
            await audit.post(
                self.bot,
                title="Nickname Changed",
                accent=config.COLOR_NEUTRAL,
                fields=[
                    ("Member", audit.describe(after)),
                    ("Before", before.nick or "None"),
                    ("After", after.nick or "None"),
                ],
                footer=audit.actor_note(actor, "Changed by the member."),
                thumbnail=str(after.display_avatar.url),
            )

        if before.timed_out_until != after.timed_out_until:
            actor, reason = await audit.find_actor(after.guild, discord.AuditLogAction.member_update, after.id)
            if after.timed_out_until is not None:
                await audit.post(
                    self.bot,
                    title="Member Timed Out",
                    accent=config.COLOR_WARNING,
                    fields=[
                        ("Member", audit.describe(after)),
                        ("Expires", discord.utils.format_dt(after.timed_out_until, "F")),
                        ("Reason", reason or "No reason recorded."),
                    ],
                    footer=audit.actor_note(actor),
                    thumbnail=str(after.display_avatar.url),
                )
            else:
                await audit.post(
                    self.bot,
                    title="Timeout Removed",
                    accent=config.COLOR_SUCCESS,
                    fields=[("Member", audit.describe(after))],
                    footer=audit.actor_note(actor, "The timeout expired."),
                    thumbnail=str(after.display_avatar.url),
                )

    async def _log_role_change(self, before: discord.Member, after: discord.Member) -> None:
        added, removed = audit.diff_roles(before.roles, after.roles)
        if not added and not removed:
            return

        actor, reason = await audit.find_actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
        muted_id = self.memory.guild_setting("muted_role")
        muted_id = int(muted_id) if muted_id else None

        if muted_id is not None:
            if any(r.id == muted_id for r in added):
                await self._log_mute(after, actor, reason, muted=True)
                added = [r for r in added if r.id != muted_id]
            if any(r.id == muted_id for r in removed):
                await self._log_mute(after, actor, reason, muted=False)
                removed = [r for r in removed if r.id != muted_id]

        if not added and not removed:
            return

        fields = [("Member", audit.describe(after))]
        if added:
            fields.append(("Roles added", "\n".join(f"{r.name} ({r.id})" for r in added)))
        if removed:
            fields.append(("Roles removed", "\n".join(f"{r.name} ({r.id})" for r in removed)))

        staff_changed = [r for r in added + removed if r.id in config.STAFF_ROLE_IDS]
        if staff_changed:
            fields.append(("Staff rank now", permissions.title_of(after)))

        await audit.post(
            self.bot,
            title="Staff Roles Updated" if staff_changed else "Roles Updated",
            accent=config.COLOR_WARNING if staff_changed else config.COLOR_NEUTRAL,
            fields=fields,
            footer=audit.actor_note(actor),
            thumbnail=str(after.display_avatar.url),
        )

    async def _log_mute(
        self,
        member: discord.Member,
        actor: discord.abc.User | None,
        reason: str | None,
        *,
        muted: bool,
    ) -> None:
        by_bot = actor is not None and self.bot.user is not None and actor.id == self.bot.user.id
        if by_bot:
            return

        active = self.memory.active_cases_of_action(member.id, "mute")
        await audit.post(
            self.bot,
            title="Member Muted" if muted else "Member Unmuted",
            accent=config.COLOR_WARNING if muted else config.COLOR_SUCCESS,
            fields=[
                ("Member", audit.describe(member)),
                ("Reason", reason or "No reason recorded."),
                ("Linked case", f"#{active[-1]['id']}" if active else "None, applied by hand."),
            ],
            footer=audit.actor_note(
                actor,
                "The Muted role was changed with no audit entry available.",
            ),
            thumbnail=str(member.display_avatar.url),
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or self.skip_channel(message.channel):
            return

        actor, _ = await audit.find_actor(message.guild, discord.AuditLogAction.message_delete, message.author.id)
        attachments = ", ".join(a.filename for a in message.attachments) or "None"

        await audit.post(
            self.bot,
            title="Message Deleted",
            accent=config.COLOR_NEUTRAL,
            fields=[
                ("Author", audit.describe(message.author)),
                ("Channel", message.channel.mention),
                ("Content", f"```\n{ui.clip(message.content or 'No text content.', MAX_CONTENT)}\n```"),
                ("Attachments", attachments),
            ],
            footer=audit.actor_note(actor, "Deleted by the author, or by the chat filter."),
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages:
            return
        first = messages[0]
        if first.guild is None or self.skip_channel(first.channel):
            return

        actor, _ = await audit.find_actor(first.guild, discord.AuditLogAction.message_bulk_delete)
        await audit.post(
            self.bot,
            title="Messages Purged",
            accent=config.COLOR_WARNING,
            fields=[
                ("Channel", first.channel.mention),
                ("Messages removed", str(len(messages))),
                (
                    "Authors",
                    ui.clip(", ".join(dict.fromkeys(str(m.author) for m in messages)), 500),
                ),
            ],
            footer=audit.actor_note(actor),
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is None or after.author.bot or self.skip_channel(after.channel):
            return
        if before.content == after.content:
            return

        await audit.post(
            self.bot,
            title="Message Edited",
            accent=config.COLOR_NEUTRAL,
            fields=[
                ("Author", audit.describe(after.author)),
                ("Channel", after.channel.mention),
                ("Before", f"```\n{ui.clip(before.content or 'Empty.', 450)}\n```"),
                ("After", f"```\n{ui.clip(after.content or 'Empty.', 450)}\n```"),
                ("Jump", f"[Open the message]({after.jump_url})"),
            ],
            footer="Edited by the author.",
        )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if getattr(channel, "category_id", None) == config.TICKET_CATEGORY_ID:
            return
        actor, _ = await audit.find_actor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        await audit.post(
            self.bot,
            title="Channel Created",
            accent=config.COLOR_SUCCESS,
            fields=[
                ("Channel", f"{channel.mention}\n{channel.name} ({channel.id})"),
                ("Type", str(channel.type).replace("_", " ").title()),
            ],
            footer=audit.actor_note(actor),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if getattr(channel, "category_id", None) == config.TICKET_CATEGORY_ID:
            return
        actor, _ = await audit.find_actor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        await audit.post(
            self.bot,
            title="Channel Deleted",
            accent=config.COLOR_DANGER,
            fields=[
                ("Channel", f"{channel.name} ({channel.id})"),
                ("Type", str(channel.type).replace("_", " ").title()),
            ],
            footer=audit.actor_note(actor),
        )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        actor, _ = await audit.find_actor(role.guild, discord.AuditLogAction.role_create, role.id)
        await audit.post(
            self.bot,
            title="Role Created",
            accent=config.COLOR_SUCCESS,
            fields=[("Role", f"{role.name} ({role.id})")],
            footer=audit.actor_note(actor),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        actor, _ = await audit.find_actor(role.guild, discord.AuditLogAction.role_delete, role.id)
        await audit.post(
            self.bot,
            title="Role Deleted",
            accent=config.COLOR_DANGER,
            fields=[
                ("Role", f"{role.name} ({role.id})"),
                ("Members affected", str(len(role.members))),
            ],
            footer=audit.actor_note(actor),
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        changes: list[tuple[str, str]] = []
        if before.name != after.name:
            changes.append(("Name", f"{before.name} to {after.name}"))
        if before.colour != after.colour:
            changes.append(("Colour", f"{before.colour} to {after.colour}"))
        if before.permissions != after.permissions:
            gained = [
                name.replace("_", " ").title()
                for name, value in after.permissions
                if value and not getattr(before.permissions, name)
            ]
            lost = [
                name.replace("_", " ").title()
                for name, value in before.permissions
                if value and not getattr(after.permissions, name)
            ]
            if gained:
                changes.append(("Permissions granted", ui.clip(", ".join(gained), 500)))
            if lost:
                changes.append(("Permissions removed", ui.clip(", ".join(lost), 500)))
        if not changes:
            return

        actor, _ = await audit.find_actor(after.guild, discord.AuditLogAction.role_update, after.id)
        is_staff_role = after.id in config.STAFF_ROLE_IDS
        await audit.post(
            self.bot,
            title="Staff Role Updated" if is_staff_role else "Role Updated",
            accent=config.COLOR_WARNING if is_staff_role else config.COLOR_NEUTRAL,
            fields=[("Role", f"{after.name} ({after.id})")] + changes,
            footer=audit.actor_note(actor),
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if before.channel == after.channel:
            if before.mute != after.mute or before.deaf != after.deaf:
                actor, _ = await audit.find_actor(member.guild, discord.AuditLogAction.member_update, member.id)
                await audit.post(
                    self.bot,
                    title="Voice State Changed",
                    accent=config.COLOR_NEUTRAL,
                    fields=[
                        ("Member", audit.describe(member)),
                        ("Server muted", "Yes" if after.mute else "No"),
                        ("Server deafened", "Yes" if after.deaf else "No"),
                    ],
                    footer=audit.actor_note(actor),
                )
            return

        if before.channel is None and after.channel is not None:
            title, accent, detail = "Joined Voice", config.COLOR_SUCCESS, after.channel.mention
        elif after.channel is None and before.channel is not None:
            title, accent, detail = "Left Voice", config.COLOR_NEUTRAL, before.channel.mention
        else:
            title, accent, detail = (
                "Moved Voice Channel",
                config.COLOR_NEUTRAL,
                f"{before.channel.mention} to {after.channel.mention}",
            )

        await audit.post(
            self.bot,
            title=title,
            accent=accent,
            fields=[("Member", audit.describe(member)), ("Channel", detail)],
            footer="Voice activity.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerLog(bot))
