from __future__ import annotations

import logging

import discord
from discord import ui as dui
from discord.ext import commands

import config
from core import audit, mod_actions, permissions, ui
from core.filters import Verdict, chat_filter
from core.memory import Memory
from core.rules import rulebook

log = logging.getLogger("manager.filter")

PUNISH_PREFIX = f"{config.COMPONENT_PREFIX}:fp:"

SEVERITY_ACCENT = {
    "severe": config.COLOR_DANGER,
    "high": config.COLOR_DANGER,
    "moderate": config.COLOR_WARNING,
    "low": config.COLOR_NEUTRAL,
}

CATEGORY_LABEL = {
    "racial": "Racial slur",
    "antisemitic": "Antisemitic language",
    "homophobic": "Homophobic slur",
    "ableist": "Ableist slur",
    "other": "Prohibited language",
    "threats": "Threatening language",
    "sexual": "Sexual or NSFW content",
    "excessive_profanity": "Excessive profanity",
    "targeted_profanity": "Profanity aimed at a member",
    "bypass": "Filter evasion",
    "spam": "Spam",
    "advertising": "Advertising",
    "mass_mention": "Mass mentions",
    "caps": "Excessive capitals",
    "character_spam": "Character spam",
}

MEMBER_ADVICE = {
    "severe": (
        "Slurs and threats are removed on sight and are recorded against your account. "
        "This is reviewed by the staff team and is likely to result in a punishment."
    ),
    "high": (
        "Swearing is allowed here. Saturating a message with it, or aiming it at another "
        "member, is not. Keep it civil and this will not come up again."
    ),
    "moderate": "Keep this out of the public channels. A repeat will be actioned.",
    "low": "Keep messages readable. Avoid walls of capitals or repeated characters.",
}


class PunishButton(dui.ActionRow):
    def __init__(self, user_id: int, rule_id: str, clause_id: str) -> None:
        super().__init__()
        self.add_item(
            dui.Button(
                label="Apply Rulebook Punishment",
                style=discord.ButtonStyle.danger,
                custom_id=f"{PUNISH_PREFIX}{user_id}:{rule_id}:{clause_id}",
            )
        )


class ChatFilter(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.memory: Memory = bot.memory

    def exempt(self, message: discord.Message) -> bool:
        if message.author.bot or message.guild is None:
            return True
        if not config.FILTER_ENABLED:
            return True
        if permissions.has_level(message.author, config.LEVEL_FILTER_IMMUNE):
            return True
        if self.memory.ticket_by_channel(message.channel.id) is not None:
            return True
        return False

    @commands.Cog.listener("on_message")
    async def scan_message(self, message: discord.Message) -> None:
        if self.exempt(message):
            return
        await self.handle(message, edited=False)

    @commands.Cog.listener("on_message_edit")
    async def scan_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content or self.exempt(after):
            return
        await self.handle(after, edited=True)

    async def handle(self, message: discord.Message, *, edited: bool) -> None:
        verdict = chat_filter.scan(
            message.content,
            author_id=message.author.id,
            mention_count=len(message.mentions) + len(message.role_mentions),
            has_user_mention=bool(message.mentions),
            check_spam=not edited,
        )
        if not verdict.flagged:
            return

        primary = verdict.primary
        if primary is None:
            return

        excerpt = ui.clip(message.content, 900)
        channel = message.channel

        try:
            await message.delete()
            deleted = True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            deleted = False

        strikes = self.memory.add_filter_strike(message.author.id, primary.category, excerpt)
        self.memory.remember_name(message.author.id, str(message.author))

        await self.notify_member(message.author, verdict, strikes)
        await self.report(message, verdict, strikes, deleted, edited, excerpt, channel)

        if verdict.severity == "severe" and config.FILTER_AUTO_PUNISH_SEVERE:
            await self.auto_punish(message, verdict)

    async def notify_member(self, member: discord.abc.User, verdict: Verdict, strikes: int) -> None:
        primary = verdict.primary
        if primary is None:
            return

        reasons = "\n".join(
            f"{CATEGORY_LABEL.get(hit.category, hit.category)}: {hit.detail}" for hit in verdict.hits
        )
        rule = self.rule_reference(verdict)

        fields = [("Why it was removed", reasons)]
        if rule is not None:
            fields.append(("Rule", f"{rule.reference} {rule.section_title}\n{rule.title}"))
        fields.append(("Filter strikes", f"{strikes} on record"))

        await mod_actions.send_dm(
            member,
            ui.notice(
                title="Message Removed",
                subtitle=f"{config.COMMUNITY_NAME} Moderation",
                body=MEMBER_ADVICE.get(verdict.severity, MEMBER_ADVICE["low"]),
                fields=fields,
                footer="Repeated breaches are punished under the rulebook.",
                accent=SEVERITY_ACCENT.get(verdict.severity, config.COLOR_NEUTRAL),
                thumbnail=self.bot.icon_url(),
            ),
        )

    def rule_reference(self, verdict: Verdict):
        for hit in sorted(verdict.hits, key=lambda h: -["low", "moderate", "high", "severe"].index(h.severity)):
            mapped = chat_filter.rule_for(hit.category)
            if mapped and mapped[0]:
                clause = rulebook.get(f"{mapped[0]}:{mapped[1]}")
                if clause is not None:
                    return clause
        return None

    async def report(
        self,
        message: discord.Message,
        verdict: Verdict,
        strikes: int,
        deleted: bool,
        edited: bool,
        excerpt: str,
        channel: discord.abc.GuildChannel,
    ) -> None:
        target = self.bot.get_channel(config.SERVER_LOG_CHANNEL_ID)
        if target is None:
            return

        clause = self.rule_reference(verdict)
        categories = ", ".join(
            CATEGORY_LABEL.get(name, name) for name in dict.fromkeys(verdict.categories)
        )
        terms = ", ".join(verdict.terms()) or "None recorded"

        accent = SEVERITY_ACCENT.get(verdict.severity, config.COLOR_NEUTRAL)
        escalate = strikes >= config.FILTER_ESCALATE_AT or verdict.severity == "severe"

        view = ui.Layout(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(accent))
        header = f"## Filter Triggered\n-# {verdict.severity.title()} - {categories}"
        container.add_item(
            dui.Section(dui.TextDisplay(header), accessory=dui.Thumbnail(str(message.author.display_avatar.url)))
        )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        rows = [
            ("Member", f"{message.author.mention}\n{message.author} ({message.author.id})"),
            ("Channel", getattr(channel, "mention", str(channel))),
            ("Detected", categories),
            ("Matched terms", terms),
            ("Filter strikes", f"{strikes} on record"),
            ("Message removed", "Yes" if deleted else "No, the bot could not delete it."),
        ]
        if edited:
            rows.append(("Trigger", "The message was flagged after being edited."))
        if clause is not None:
            rows.append(("Recommended rule", f"{clause.reference} {clause.section_title}\n{clause.title}"))
        rows.append(("Content", f"```\n{excerpt.replace('`', chr(0x2035))}\n```"))

        container.add_item(dui.TextDisplay(ui.clip(ui.field_list(rows), ui.MAX_TEXT)))

        if clause is not None:
            container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(PunishButton(message.author.id, clause.section_id, clause.clause_id))

        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                "-# Detected automatically. "
                + ("Staff attention required." if escalate else "No action taken beyond removal.")
            )
        )
        view.add_item(container)

        try:
            await target.send(view=view)
        except discord.HTTPException:
            log.exception("Could not post a filter report.")

    async def auto_punish(self, message: discord.Message, verdict: Verdict) -> None:
        clause = self.rule_reference(verdict)
        if clause is None or message.guild is None:
            return
        offense = self.memory.offense_count(message.author.id, clause.section_id, clause.clause_id) + 1
        await mod_actions.apply(
            self.bot,
            guild=message.guild,
            target=message.author,
            moderator=self.bot.user,
            clause=clause,
            offense=offense,
            memory=self.memory,
            reason="Detected automatically by the chat filter.",
            source="filter",
            proof={"url": None, "filename": "chat filter capture", "content_type": "text/plain"},
        )

    @commands.Cog.listener("on_interaction")
    async def punish_from_report(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(PUNISH_PREFIX):
            return

        if not permissions.has_level(interaction.user, config.LEVEL_PUNISH):
            await interaction.response.send_message(
                view=ui.notice(
                    title="Permission Denied",
                    body="Only staff can apply a punishment from a filter report.",
                    accent=config.COLOR_DANGER,
                ),
                ephemeral=True,
            )
            return

        try:
            user_raw, rule_id, clause_id = custom_id[len(PUNISH_PREFIX):].split(":", 2)
            user_id = int(user_raw)
        except (ValueError, TypeError):
            return

        clause = rulebook.get(f"{rule_id}:{clause_id}")
        guild = interaction.guild
        if clause is None or guild is None:
            await interaction.response.send_message(
                view=ui.notice(title="Rule Missing", body="That rule is no longer in the rulebook.", accent=config.COLOR_WARNING),
                ephemeral=True,
            )
            return

        target = self.bot.get_user(user_id)
        if target is None:
            try:
                target = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                await interaction.response.send_message(
                    view=ui.notice(title="Member Unavailable", body="That member could not be resolved.", accent=config.COLOR_DANGER),
                    ephemeral=True,
                )
                return

        member = guild.get_member(user_id)
        if member is not None and not permissions.outranks(interaction.user, member):
            await interaction.response.send_message(
                view=ui.notice(
                    title="Permission Denied",
                    body="That member holds a staff rank equal to or above yours.",
                    accent=config.COLOR_DANGER,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        offense = self.memory.offense_count(user_id, clause.section_id, clause.clause_id) + 1
        result = await mod_actions.apply(
            self.bot,
            guild=guild,
            target=target,
            moderator=interaction.user,
            clause=clause,
            offense=offense,
            memory=self.memory,
            reason="Applied from a chat filter report.",
            source="filter-report",
            proof={"url": None, "filename": "chat filter capture", "content_type": "text/plain"},
        )

        await interaction.followup.send(
            view=ui.notice(
                title="Punishment Applied" if result.applied else "Case Filed, Action Failed",
                subtitle=f"Case #{result.case['id']}",
                fields=[
                    ("Member", audit.describe(target)),
                    ("Rule", f"{clause.reference} {clause.section_title}"),
                    ("Punishment", result.punishment.label),
                    ("Notice delivered", "Yes" if result.dm_delivered else "No, direct messages are closed."),
                ]
                + ([("Warning", result.error or "")] if not result.applied else []),
                footer=audit.actor_note(interaction.user),
                accent=config.COLOR_SUCCESS if result.applied else config.COLOR_DANGER,
            ),
            ephemeral=True,
        )

    @commands.group(name="filter", invoke_without_command=True)
    async def filter_group(self, ctx: commands.Context) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_TICKET_STAFF):
            raise commands.CheckFailure()

        await ctx.send(
            view=ui.notice(
                title="Chat Filter",
                body=(
                    "Swearing is allowed. Slurs, threats, targeted abuse, saturation swearing, "
                    "spam and advertising are removed automatically. Staff are exempt."
                ),
                fields=[
                    ("Status", "Enabled" if config.FILTER_ENABLED else "Disabled"),
                    ("Auto punish slurs", "Yes" if config.FILTER_AUTO_PUNISH_SEVERE else "No, reported for staff review."),
                    ("Word lists", f"{len(chat_filter.slur_canon)} slur terms, {len(chat_filter.allow)} protected words"),
                    (
                        "Commands",
                        f"{config.PREFIX}filter test <text>\n"
                        f"{config.PREFIX}filter reload\n"
                        f"{config.PREFIX}filter strikes <member>\n"
                        f"{config.PREFIX}filter clear <member>",
                    ),
                ],
                accent=config.COLOR_INFO,
            )
        )

    @filter_group.command(name="test")
    async def filter_test(self, ctx: commands.Context, *, text: str) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_TICKET_STAFF):
            raise commands.CheckFailure()

        verdict = chat_filter.scan(text, author_id=ctx.author.id, check_spam=False)
        if not verdict.flagged:
            await ctx.send(
                view=ui.notice(
                    title="Filter Test",
                    body="That message would pass the filter.",
                    accent=config.COLOR_SUCCESS,
                )
            )
            return

        clause = self.rule_reference(verdict)
        await ctx.send(
            view=ui.notice(
                title="Filter Test",
                subtitle=f"Severity: {verdict.severity}",
                fields=[
                    ("Detected", ", ".join(CATEGORY_LABEL.get(c, c) for c in dict.fromkeys(verdict.categories))),
                    ("Matched terms", ", ".join(verdict.terms()) or "None"),
                    ("Recommended rule", f"{clause.reference} {clause.section_title}" if clause else "None mapped"),
                ],
                accent=SEVERITY_ACCENT.get(verdict.severity, config.COLOR_NEUTRAL),
            )
        )

    @filter_group.command(name="reload")
    async def filter_reload(self, ctx: commands.Context) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_REVOKE_CASE):
            raise commands.CheckFailure()

        chat_filter.load()
        rulebook.load()
        await ctx.send(
            view=ui.notice(
                title="Lists Reloaded",
                fields=[
                    ("Slur terms", str(len(chat_filter.slur_canon))),
                    ("Protected words", str(len(chat_filter.allow))),
                    ("Rulebook clauses", str(len(rulebook.clauses))),
                ],
                accent=config.COLOR_SUCCESS,
                footer=audit.actor_note(ctx.author),
            )
        )

    @filter_group.command(name="strikes")
    async def filter_strikes(self, ctx: commands.Context, member: discord.User) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_TICKET_STAFF):
            raise commands.CheckFailure()

        profile = self.memory.user(member.id)
        history = profile.get("filter_history", [])[-8:]
        listed = "\n".join(
            f"<t:{int(entry['at'])}:f> - {CATEGORY_LABEL.get(entry['category'], entry['category'])}"
            for entry in history
        ) or "No filter history on record."

        await ctx.send(
            view=ui.notice(
                title="Filter Strikes",
                subtitle=str(member),
                fields=[
                    ("Member", audit.describe(member)),
                    ("Strikes", str(profile.get("filter_strikes", 0))),
                    ("Recent hits", listed),
                ],
                accent=config.COLOR_INFO,
                thumbnail=str(member.display_avatar.url),
            )
        )

    @filter_group.command(name="clear")
    async def filter_clear(self, ctx: commands.Context, member: discord.User) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_REVOKE_CASE):
            raise commands.CheckFailure()

        self.memory.clear_filter_strikes(member.id)
        chat_filter.forget(member.id)
        await ctx.send(
            view=ui.notice(
                title="Filter Strikes Cleared",
                fields=[("Member", audit.describe(member))],
                accent=config.COLOR_SUCCESS,
                footer=audit.actor_note(ctx.author),
            )
        )
        await audit.post(
            self.bot,
            title="Filter Strikes Cleared",
            accent=config.COLOR_SUCCESS,
            fields=[("Member", audit.describe(member))],
            footer=audit.actor_note(ctx.author),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatFilter(bot))
