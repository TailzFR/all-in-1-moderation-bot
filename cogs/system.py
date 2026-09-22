from __future__ import annotations

import logging
import platform
import time

import discord
from discord.ext import commands

import config
from core import audit, duration as dur, mod_actions, permissions, ui
from core.memory import Memory
from core.rules import rulebook

log = logging.getLogger("manager.system")

HELP_SECTIONS = [
    (
        "Tickets",
        [
            (f"{config.PREFIX}r <message>", "Send a reply to the member. This is the only way to reach them."),
            (f"{config.PREFIX}rs <name>", "Send a saved reply to the member."),
            (f"{config.PREFIX}ct [reason]", "Close the ticket, archive the transcript, and notify the member."),
            (f"{config.PREFIX}hold", "Pause or resume automatic closing for this ticket."),
            (f"{config.PREFIX}evidence [discord|ingame]", "Send the evidence standard to the member."),
            (f"{config.PREFIX}ticketinfo", "Show the member's history, response time and closing status."),
            (f"{config.PREFIX}block / {config.PREFIX}unblock <member>", "Withdraw or restore ticket access."),
            (f"{config.PREFIX}panel", "Repost the ticket panel immediately."),
        ],
    ),
    (
        "Ticket Tools",
        [
            (f"{config.PREFIX}snippets [name]", "List the saved replies, or read one."),
            (f"{config.PREFIX}snippet add <name> <text>", "Save a reply. Supports {member}, {ticket} and {server}."),
            (f"{config.PREFIX}snippet remove <name>", "Delete a saved reply."),
            (f"{config.PREFIX}ticketstats [member] [days]", "Volume, response times, ratings and staff activity."),
        ],
    ),
    (
        "Appeals",
        [
            (f"{config.PREFIX}appeals", "List the appeals waiting on a decision."),
            (f"{config.PREFIX}appeals <member>", "Read one member's appeal history."),
            (f"{config.PREFIX}appealpanel", "Repost the appeal panel immediately."),
            (
                "Accept / Deny buttons",
                f"On the appeal post itself. {permissions.rank_or_above(config.LEVEL_UNBAN)}.",
            ),
        ],
    ),
    (
        "Moderation",
        [
            ("/punish <user> <rule> <proof>", "Punish under a rulebook clause. Proof is required."),
            ("/unban <user id> [notes]", "Lift a ban and notify the member."),
            ("/cases <user>", "Read a member's full case history."),
            ("/revokecase <case> <reason>", "Revoke a case and lift the punishment it carried."),
            ("/rule <rule>", "Look up any rule and its punishment tiers."),
            (f"{config.PREFIX}promote <member> <role>", "Grant a staff role below your own rank."),
            (f"{config.PREFIX}demote <member> <role>", "Remove a staff role below your own rank."),
        ],
    ),
    (
        "System",
        [
            (f"{config.PREFIX}status", "Uptime, memory size and configuration."),
            (f"{config.PREFIX}memory", "Memory statistics, with a forced save."),
            (f"{config.PREFIX}setupmute", "Recreate the Muted role and reapply it to every channel."),
            (f"{config.PREFIX}sync", "Resynchronise the slash commands."),
        ],
    ),
]


class System(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.memory: Memory = bot.memory

    @commands.command(name="help", aliases=["commands"])
    async def help_command(self, ctx: commands.Context) -> None:
        level = permissions.level_of(ctx.author)
        if level < 1:
            await ctx.send(
                view=ui.notice(
                    title=f"{config.BOT_NAME}",
                    body=(
                        "This bot handles tickets and moderation for the server.\n\n"
                        f"To contact the staff team, open a ticket from <#{config.TICKET_PANEL_CHANNEL_ID}>. "
                        "Once your ticket is open, reply to this bot in direct messages."
                    ),
                    footer=f"{config.COMMUNITY_NAME} - {config.COMMUNITY_TAGLINE}",
                    accent=config.COLOR_PRIMARY,
                    thumbnail=self.bot.icon_url(ctx.guild),
                )
            )
            return

        fields = [
            (name, "\n".join(f"`{command}`\n{description}" for command, description in entries))
            for name, entries in HELP_SECTIONS
        ]
        await ctx.send(
            view=ui.notice(
                title="Staff Commands",
                subtitle=f"Your rank: {permissions.title_of(ctx.author)}",
                body="Commands you do not have the rank for will be refused.",
                fields=fields,
                footer=f"{config.BOT_NAME} - {config.COMMUNITY_NAME}",
                accent=config.COLOR_PRIMARY,
                thumbnail=self.bot.icon_url(ctx.guild),
            )
        )

    @commands.command(name="status", aliases=["about"])
    async def status(self, ctx: commands.Context) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_TICKET_STAFF):
            raise commands.CheckFailure()

        store = self.bot.store
        uptime = time.time() - self.bot.started_at
        stats = store.section("stats")
        open_tickets = sum(
            1 for t in store.section("tickets").values() if t.get("status") == "open"
        )

        await ctx.send(
            view=ui.notice(
                title=config.BOT_NAME,
                subtitle=f"{config.COMMUNITY_NAME} - {config.COMMUNITY_TAGLINE}",
                fields=[
                    ("Uptime", dur.humanise(int(uptime))),
                    ("Latency", f"{self.bot.latency * 1000:.0f} ms"),
                    ("Tickets", f"{open_tickets} open, {stats.get('tickets_opened', 0)} opened in total"),
                    ("Cases", f"{len(store.section('cases'))} filed, {stats.get('cases_revoked', 0)} revoked"),
                    (
                        "Appeals",
                        f"{sum(1 for a in store.section('appeals').values() if a.get('status') == 'pending')} pending, "
                        f"{stats.get('appeals_accepted', 0)} accepted, {stats.get('appeals_denied', 0)} denied",
                    ),
                    ("Members tracked", str(len(store.section("users")))),
                    ("Rulebook", f"{len(rulebook.clauses)} clauses, version {rulebook.meta.get('version', '1.0')}"),
                    ("Pending expiries", str(len(store.data.get("schedules", [])))),
                    ("Runtime", f"discord.py {discord.__version__}, Python {platform.python_version()}"),
                ],
                footer="All state is stored locally and survives a restart.",
                accent=config.COLOR_INFO,
                thumbnail=self.bot.icon_url(ctx.guild),
            )
        )

    @commands.command(name="memory")
    async def memory_command(self, ctx: commands.Context) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_REVOKE_CASE):
            raise commands.CheckFailure()

        store = self.bot.store
        saved = store.flush(force=True)
        size = config.STORE_PATH.stat().st_size if config.STORE_PATH.exists() else 0
        backups = sorted(config.BACKUP_DIR.glob("store-*.json"), reverse=True)

        await ctx.send(
            view=ui.notice(
                title="Local Memory",
                fields=[
                    ("Store", f"`{config.STORE_PATH.name}` ({size / 1024:.1f} KB)"),
                    ("Saved just now", "Yes" if saved else "Nothing had changed."),
                    ("Backups kept", f"{len(backups)} of {config.BACKUP_KEEP}"),
                    ("Most recent backup", backups[0].name if backups else "None yet."),
                    ("Members", str(len(store.section("users")))),
                    ("Tickets", str(len(store.section("tickets")))),
                    ("Cases", str(len(store.section("cases")))),
                    ("Autosave", f"Every {config.STORE_FLUSH_SECONDS:.0f} seconds when changed."),
                ],
                footer="Memory is written atomically, so an interrupted save cannot corrupt it.",
                accent=config.COLOR_INFO,
            )
        )

    @commands.command(name="setupmute")
    async def setupmute(self, ctx: commands.Context) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_REVOKE_CASE):
            raise commands.CheckFailure()
        if ctx.guild is None:
            return

        role = await mod_actions.ensure_muted_role(ctx.guild, self.memory, sync=True)
        if role is None:
            await ctx.send(
                view=ui.notice(
                    title="Muted Role Failed",
                    body="The bot needs the Manage Roles permission and a role above the Muted role.",
                    accent=config.COLOR_DANGER,
                )
            )
            return

        updated = await mod_actions.sync_muted_role(ctx.guild, role)
        await ctx.send(
            view=ui.notice(
                title="Muted Role Ready",
                fields=[
                    ("Role", f"{role.mention}\n{role.name} ({role.id})"),
                    ("Channels updated", str(updated)),
                    ("Position", f"{role.position} of {len(ctx.guild.roles)}"),
                ],
                footer=audit.actor_note(ctx.author),
                accent=config.COLOR_SUCCESS,
            )
        )

    @commands.command(name="sync")
    async def sync(self, ctx: commands.Context) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_PROMOTE):
            raise commands.CheckFailure()
        if ctx.guild is None:
            return

        async with ctx.typing():
            synced = await self.bot.sync_commands(ctx.guild)

        if not synced:
            await ctx.send(
                view=ui.notice(
                    title="Sync Failed",
                    body=(
                        "No commands were published. The bot was almost certainly invited "
                        "without the `applications.commands` scope.\n\n"
                        "Re-invite it using an authorisation link that includes both "
                        "`bot` and `applications.commands`, then run this command again. "
                        "The bot does not need to be kicked first."
                    ),
                    accent=config.COLOR_DANGER,
                )
            )
            return

        await ctx.send(
            view=ui.notice(
                title="Commands Synchronised",
                body=f"Published to **{ctx.guild.name}**. They appear immediately.",
                fields=[("Commands", "\n".join(f"/{c.name}" for c in synced))],
                accent=config.COLOR_SUCCESS,
                footer=audit.actor_note(ctx.author),
            )
        )

    @commands.command(name="rules")
    async def rules_command(self, ctx: commands.Context) -> None:
        sections = rulebook.sections()
        conduct = [s for s in sections if not s["id"].startswith("A")]
        gameplay = [s for s in sections if s["id"].startswith("A")]

        await ctx.send(
            view=ui.notice(
                title=rulebook.meta.get("name", "Rulebook"),
                subtitle=f"Version {rulebook.meta.get('version', '1.0')}, updated {rulebook.meta.get('updated', '')}",
                body=ui.clip(rulebook.meta.get("philosophy", ""), 900),
                fields=[
                    ("Conduct rules", "\n".join(f"{s['id']} {s['title']}" for s in conduct)),
                    ("Gameplay rules", "\n".join(f"{s['id']} {s['title']}" for s in gameplay)),
                    ("Lookup", "Use `/rule` to read any clause and its punishment tiers."),
                ],
                footer=f"{len(rulebook.clauses)} punishable clauses on record.",
                accent=config.COLOR_INFO,
                thumbnail=self.bot.icon_url(ctx.guild),
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(System(bot))
