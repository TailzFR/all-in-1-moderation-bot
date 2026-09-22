from __future__ import annotations

import asyncio
import io
import logging
import re
import statistics
import time
from collections import Counter
from typing import Any

import discord
from discord import ui as dui
from discord.ext import commands, tasks

import config
from core import audit, cards, duration as dur, panels, permissions, transcripts, ui
from core.memory import Memory

log = logging.getLogger("manager.tickets")

RATING_PREFIX = f"{config.COMPONENT_PREFIX}:rate:"
RATING_LABELS = {1: "Poor", 2: "Fair", 3: "Good", 4: "Very Good", 5: "Excellent"}

SNIPPET_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")
RESERVED_SNIPPET_NAMES = {"add", "set", "remove", "delete", "list"}

PANEL_DESCRIPTION = (
    "A ticket gives you direct access to the staff team for reports and support. "
    "Anything related to the game or the server, we can help with. "
    "Open one and a staff member will be with you shortly.\n\n"
    + "\n\n".join(f"**{meta['label']}**\n{meta['description']}" for meta in config.TICKET_TYPES.values())
)

PANEL_FOOTER = (
    "Once your ticket is open, reply to this bot in direct messages. "
    "Everything you send there reaches the staff team."
)

DISCORD_EVIDENCE = (
    "Evidence should not be cropped or edited in any way.\n\n"
    "**For Desktop Users:**\n"
    "1. Start recording.\n"
    "2. Press Ctrl+R (Cmd+R on Mac).\n"
    "3. Go to the evidence.\n"
    "4. Open the user's profile.\n"
    "5. Copy their Discord ID and paste it into any text-space while recording.\n"
    "6. End the recording.\n\n"
    "**For Mobile Users:**\n"
    "1. Start recording.\n"
    "2. Swipe out of Discord to close it completely.\n"
    "3. Re-open Discord.\n"
    "4. Go to the evidence.\n"
    "5. Open the user's profile.\n"
    "6. Copy their Discord ID and paste it into any text-space while recording.\n"
    "7. End the recording."
)

INGAME_EVIDENCE = (
    "Evidence should not be cropped or edited in any way.\n\n"
    "**Recording requirements:**\n"
    "1. Start recording before the incident, not after it.\n"
    "2. Show the full screen, including the in-game chat.\n"
    "3. Make sure the reported player's username is clearly visible.\n"
    "4. Keep recording through the whole incident without cutting.\n"
    "5. Type the current time into chat while still recording.\n"
    "6. End the recording.\n\n"
    "**Not accepted:** cropped clips, screenshots of a video, edited footage, "
    "recordings that do not show the username, and clips that start after the incident."
)

DEFAULT_SNIPPETS = {
    "greeting": (
        "Hello {member}, thank you for opening ticket #{ticket}. A member of the staff team "
        "is looking into this now and will reply here shortly."
    ),
    "moreinfo": (
        "Could you give us a little more detail? Include any names involved, roughly when it "
        "happened, and anything you have already tried."
    ),
    "evidence": (
        "To act on a report we need evidence that meets our standard. Please send an uncropped, "
        "unedited recording in this conversation."
    ),
    "resolved": (
        "It looks like this has been resolved. If there is nothing else we can help with, this "
        "ticket will be closed shortly. Thank you for contacting the {server} staff team."
    ),
}


def scope_label(scope: str | None) -> str:
    return config.SCOPE_LABELS.get(scope or "", str(scope or "Not set").title())


def type_name(ticket: dict[str, Any]) -> str:
    return config.TICKET_TYPES.get(ticket.get("type", ""), {}).get("name", "Ticket")


def rating_text(score: int | None) -> str:
    if not score:
        return "Not rated"
    return f"{int(score)} / 5 ({RATING_LABELS.get(int(score), 'Unknown')})"


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


class PanelButtons(dui.ActionRow):
    @dui.button(
        label="Support Ticket",
        style=discord.ButtonStyle.primary,
        custom_id=f"{config.COMPONENT_PREFIX}:panel:support",
    )
    async def support(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await start_flow(interaction, "support")

    @dui.button(
        label="Report Ticket",
        style=discord.ButtonStyle.secondary,
        custom_id=f"{config.COMPONENT_PREFIX}:panel:report",
    )
    async def report(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await start_flow(interaction, "report")

    @dui.button(
        label="Requesting Logs Ticket",
        style=discord.ButtonStyle.secondary,
        custom_id=f"{config.COMPONENT_PREFIX}:panel:logs",
    )
    async def logs(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await start_flow(interaction, "logs")


class TicketPanel(ui.Layout):
    def __init__(self, icon_url: str | None) -> None:
        super().__init__(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_PRIMARY))

        header = f"# {config.COMMUNITY_NAME} Tickets\n-# {config.COMMUNITY_TAGLINE}"
        if icon_url:
            container.add_item(dui.Section(dui.TextDisplay(header), accessory=dui.Thumbnail(icon_url)))
        else:
            container.add_item(dui.TextDisplay(header))

        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(PANEL_DESCRIPTION))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(PanelButtons())
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(f"-# {PANEL_FOOTER}"))

        self.add_item(container)


async def start_flow(interaction: discord.Interaction, ticket_type: str) -> None:
    bot = interaction.client
    memory: Memory = bot.memory

    profile = memory.user(interaction.user.id)
    if profile.get("blocked_from_tickets"):
        await ui.respond(
            interaction,
            ui.notice(
                title="Tickets Restricted",
                body=(
                    "Your access to the ticket system has been withdrawn by the staff team. "
                    "If you believe this is a mistake, contact a "
                    f"{permissions.rank_name(config.LEVEL_REVOKE_CASE)} directly."
                ),
                accent=config.COLOR_DANGER,
            ),
        )
        return

    existing = memory.open_ticket_for(interaction.user.id)
    if existing is not None:
        await ui.respond(
            interaction,
            ui.notice(
                title="You Already Have an Open Ticket",
                body=(
                    f"Ticket **#{existing['id']}** is still open. Continue the conversation by "
                    "replying to this bot in direct messages. A new ticket cannot be opened "
                    "until the current one is closed."
                ),
                accent=config.COLOR_WARNING,
            ),
        )
        return

    await ui.respond(interaction, WarningStep(ticket_type))


class WarningRow(dui.ActionRow):
    def __init__(self, ticket_type: str) -> None:
        super().__init__()
        self.ticket_type = ticket_type

    @dui.button(label="Yes, I understand", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await interaction.response.edit_message(view=ScopeStep(self.ticket_type))

    @dui.button(label="No, cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await interaction.response.edit_message(
            view=ui.notice(
                title="Ticket Cancelled",
                body="Nothing was created. You can open a ticket from the panel whenever you need to.",
                accent=config.COLOR_NEUTRAL,
            )
        )


class WarningStep(ui.Layout):
    def __init__(self, ticket_type: str) -> None:
        super().__init__(timeout=300)
        label = config.TICKET_TYPES[ticket_type]["label"]
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_WARNING))
        container.add_item(dui.TextDisplay(f"## Before You Continue\n-# {label}"))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                "**Opening a false ticket will result in a punishment.**\n\n"
                "Tickets are read by the staff team and are recorded. Joke tickets, empty "
                "tickets and reports about permitted gameplay are punished under rule "
                "1.5[D] of the rulebook.\n\n"
                "Do you understand, and do you want to continue?"
            )
        )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(WarningRow(ticket_type))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay("-# Only you can see this message."))
        self.add_item(container)


class ScopeRow(dui.ActionRow):
    def __init__(self, ticket_type: str) -> None:
        super().__init__()
        self.ticket_type = ticket_type

    @dui.button(label="Discord", style=discord.ButtonStyle.primary)
    async def discord_scope(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await interaction.response.send_modal(DetailsForm(self.ticket_type, "discord", interaction))

    @dui.button(label="In-Game", style=discord.ButtonStyle.primary)
    async def ingame_scope(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await interaction.response.send_modal(DetailsForm(self.ticket_type, "ingame", interaction))

    @dui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await interaction.response.edit_message(
            view=ui.notice(
                title="Ticket Cancelled",
                body="Nothing was created. You can open a ticket from the panel whenever you need to.",
                accent=config.COLOR_NEUTRAL,
            )
        )


class ScopeStep(ui.Layout):
    def __init__(self, ticket_type: str) -> None:
        super().__init__(timeout=300)
        label = config.TICKET_TYPES[ticket_type]["label"]
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_PRIMARY))
        container.add_item(dui.TextDisplay(f"## Where Is the Issue?\n-# {label}"))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                "Is this issue Discord related or in-game related?\n\n"
                "**Discord** covers this server, its channels, direct messages and member profiles.\n"
                f"**In-Game** covers the {config.GAME_NAME}, its chat, builds and players."
            )
        )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ScopeRow(ticket_type))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay("-# Only you can see this message."))
        self.add_item(container)


class DetailsForm(dui.Modal):
    def __init__(self, ticket_type: str, scope: str, parent: discord.Interaction) -> None:
        meta = config.TICKET_TYPES[ticket_type]
        super().__init__(title=f"Open a {meta['name']} Ticket", timeout=600)
        self.ticket_type = ticket_type
        self.scope = scope
        self.parent = parent

        self.subject = dui.Label(
            text="Subject",
            description="One line describing what this is about.",
            component=dui.TextInput(
                style=discord.TextStyle.short,
                max_length=100,
                required=True,
                placeholder="Keep it short and specific.",
            ),
        )
        self.add_item(self.subject)

        if ticket_type == "report":
            self.reported = dui.Label(
                text="Who are you reporting?",
                description="Their Discord ID, username, or in-game name.",
                component=dui.TextInput(style=discord.TextStyle.short, max_length=120, required=True),
            )
            self.add_item(self.reported)

        self.details = dui.Label(
            text="Details",
            description="Explain what happened, including names, times and context.",
            component=dui.TextInput(
                style=discord.TextStyle.paragraph,
                max_length=1500,
                required=True,
            ),
        )
        self.add_item(self.details)

        if ticket_type == "report":
            self.evidence = dui.Label(
                text="Evidence",
                description="Link your uncropped, unedited recording. Type none if you have none yet.",
                component=dui.TextInput(style=discord.TextStyle.paragraph, max_length=500, required=True),
            )
            self.add_item(self.evidence)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        answers: dict[str, Any] = {
            "acknowledged": "Yes",
            "scope": self.scope,
            "subject": self.subject.component.value,
            "details": self.details.component.value,
        }
        if self.ticket_type == "report":
            answers["reported_user"] = self.reported.component.value
            answers["evidence"] = self.evidence.component.value

        cog: Tickets | None = interaction.client.get_cog("Tickets")
        if cog is None:
            await interaction.followup.send(
                view=ui.notice(
                    title="Ticket System Unavailable",
                    body="The ticket system is not loaded. Contact a staff member directly.",
                    accent=config.COLOR_DANGER,
                ),
                ephemeral=True,
            )
            return

        await cog.open_ticket(interaction, self.ticket_type, self.scope, answers, self.parent)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Ticket intake form failed.", exc_info=error)
        message = ui.notice(
            title="Something Went Wrong",
            body="Your ticket could not be created. Try again, or contact a staff member directly.",
            accent=config.COLOR_DANGER,
        )
        if interaction.response.is_done():
            await interaction.followup.send(view=message, ephemeral=True)
        else:
            await interaction.response.send_message(view=message, ephemeral=True)


class TicketControls(dui.ActionRow):
    @dui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id=f"{config.COMPONENT_PREFIX}:tk:claim")
    async def claim(self, interaction: discord.Interaction, button: dui.Button) -> None:
        cog: Tickets | None = interaction.client.get_cog("Tickets")
        if cog is None:
            return
        await cog.handle_claim(interaction)

    @dui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id=f"{config.COMPONENT_PREFIX}:tk:close")
    async def close(self, interaction: discord.Interaction, button: dui.Button) -> None:
        cog: Tickets | None = interaction.client.get_cog("Tickets")
        if cog is None:
            return
        await cog.handle_close_button(interaction)


class TicketHeader(ui.Layout):
    def __init__(
        self,
        *,
        ticket: dict[str, Any],
        opener: discord.abc.User,
        icon_url: str | None,
        history: str,
        staff_mention: str = "",
    ) -> None:
        super().__init__(timeout=None)
        answers = ticket.get("answers", {})

        container = dui.Container(accent_colour=discord.Colour(config.COLOR_PRIMARY))
        header = f"## Ticket #{ticket['id']}\n-# {type_name(ticket)} - {scope_label(ticket.get('scope'))}"
        if icon_url:
            container.add_item(dui.Section(dui.TextDisplay(header), accessory=dui.Thumbnail(icon_url)))
        else:
            container.add_item(dui.TextDisplay(header))

        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        rows: list[tuple[str, str]] = [
            ("Opened by", f"{opener.mention}\n{opener} ({opener.id})"),
            ("Account created", discord.utils.format_dt(opener.created_at, "R")),
        ]
        joined = getattr(opener, "joined_at", None)
        if joined is not None:
            rows.append(("Joined server", discord.utils.format_dt(joined, "R")))
        rows.append(("History", history))
        rows.append(("Subject", answers.get("subject", "Not provided")))
        if answers.get("reported_user"):
            rows.append(("Reported member", answers["reported_user"]))

        container.add_item(dui.TextDisplay(ui.field_list(rows)))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        container.add_item(
            dui.TextDisplay(
                ui.clip(f"**Details**\n{ui.sanitise(answers.get('details', 'Not provided'))}", 1700)
            )
        )
        if answers.get("evidence"):
            container.add_item(
                dui.TextDisplay(ui.clip(f"**Evidence**\n{ui.sanitise(answers['evidence'])}", 700))
            )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(TicketControls())
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                f"-# Messages typed here stay internal. Use {config.PREFIX}r to reply to the member, "
                f"{config.PREFIX}rs to send a saved reply, and {config.PREFIX}ct to close the ticket."
            )
        )
        if staff_mention:
            container.add_item(dui.TextDisplay(f"-# {staff_mention}"))
        self.add_item(container)


class TicketHeaderShell(ui.Layout):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_PRIMARY))
        container.add_item(dui.TextDisplay("-# Ticket controls"))
        container.add_item(TicketControls())
        self.add_item(container)


class RatingPrompt(ui.Layout):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_PRIMARY))
        container.add_item(dui.TextDisplay(f"## How Did We Do?\n-# Ticket #{ticket_id}"))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                "Rate the support you received on this ticket. Your rating goes to the staff "
                "team and helps us improve."
            )
        )
        row = dui.ActionRow()
        for score, label in RATING_LABELS.items():
            row.add_item(
                dui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"{RATING_PREFIX}{ticket_id}:{score}",
                )
            )
        container.add_item(row)
        container.add_item(dui.TextDisplay("-# Rating is optional."))
        self.add_item(container)


class RatingThanks(ui.Layout):
    def __init__(self, ticket_id: int, score: int, *, commented: bool) -> None:
        super().__init__(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_SUCCESS))
        container.add_item(dui.TextDisplay(f"## Thank You\n-# Ticket #{ticket_id}"))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        body = f"You rated this ticket **{rating_text(score)}**. Your feedback has been passed to the staff team."
        if commented:
            body += "\n\nYour comment was received as well."
        container.add_item(dui.TextDisplay(body))
        if not commented:
            row = dui.ActionRow()
            row.add_item(
                dui.Button(
                    label="Add a Comment",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"{RATING_PREFIX}{ticket_id}:comment",
                )
            )
            container.add_item(row)
        self.add_item(container)


class RatingComment(dui.Modal):
    def __init__(self, cog: "Tickets", ticket_id: int) -> None:
        super().__init__(title="Add a Comment", timeout=600)
        self.cog = cog
        self.ticket_id = ticket_id
        self.comment = dui.Label(
            text="Comment",
            description="What went well, or what could we have done better?",
            component=dui.TextInput(style=discord.TextStyle.paragraph, max_length=800, required=True),
        )
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_rating_comment(interaction, self.ticket_id, self.comment.component.value)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.memory: Memory = bot.memory
        self._panel_lock = asyncio.Lock()
        self._open_lock = asyncio.Lock()
        self._closing: set[int] = set()
        self._background: set[asyncio.Task] = set()
        self._typing: dict[int, float] = {}

    async def cog_load(self) -> None:
        self.bot.add_view(TicketPanel(None))
        self.bot.add_view(TicketHeaderShell())
        self.memory.seed_snippets(DEFAULT_SNIPPETS, author_id=0)
        self.panel_refresh.start()
        if config.TICKET_AUTOCLOSE_ENABLED:
            self.inactivity_sweep.start()

    async def cog_unload(self) -> None:
        self.panel_refresh.cancel()
        self.inactivity_sweep.cancel()

    @tasks.loop(minutes=config.PANEL_REFRESH_MINUTES)
    async def panel_refresh(self) -> None:
        await self.repost_panel()

    @panel_refresh.before_loop
    async def before_panel_refresh(self) -> None:
        await self.bot.wait_until_ready()

    async def repost_panel(self) -> discord.Message | None:
        async with self._panel_lock:
            channel = await panels.resolve_channel(self.bot, config.TICKET_PANEL_CHANNEL_ID)
            if channel is None:
                log.warning("Ticket panel channel %s is unreachable.", config.TICKET_PANEL_CHANNEL_ID)
                return None

            message = await panels.repost(
                self.bot,
                channel_id=config.TICKET_PANEL_CHANNEL_ID,
                view=TicketPanel(self.bot.icon_url(channel.guild)),
                label="ticket",
            )
            if message is None:
                self.memory.clear_panel_message()
                return None

            self.memory.set_panel_message(channel.id, message.id)
            return message

    async def open_ticket(
        self,
        interaction: discord.Interaction,
        ticket_type: str,
        scope: str,
        answers: dict[str, Any],
        parent: discord.Interaction | None = None,
    ) -> None:
        guild = interaction.guild or self.bot.primary_guild
        user = interaction.user

        if guild is None:
            await interaction.followup.send(
                view=ui.notice(
                    title="Ticket Failed",
                    body="The server could not be resolved. Contact a staff member directly.",
                    accent=config.COLOR_DANGER,
                ),
                ephemeral=True,
            )
            return

        async with self._open_lock:
            if self.memory.open_ticket_for(user.id) is not None:
                await interaction.followup.send(
                    view=ui.notice(
                        title="You Already Have an Open Ticket",
                        body="Continue the conversation in your direct messages with this bot.",
                        accent=config.COLOR_WARNING,
                    ),
                    ephemeral=True,
                )
                return

            category = guild.get_channel(config.TICKET_CATEGORY_ID)
            if not isinstance(category, discord.CategoryChannel):
                log.error("Ticket category %s was not found.", config.TICKET_CATEGORY_ID)
                await interaction.followup.send(
                    view=ui.notice(
                        title="Ticket Failed",
                        body="The ticket category is misconfigured. Contact a staff member directly.",
                        accent=config.COLOR_DANGER,
                    ),
                    ephemeral=True,
                )
                return

            next_id = self.bot.store.data.get("counters", {}).get("ticket", 0) + 1
            prefix = config.TICKET_TYPES[ticket_type]["prefix"]

            try:
                channel = await guild.create_text_channel(
                    name=f"{prefix}-{next_id:04d}",
                    category=category,
                    overwrites=permissions.staff_overwrites(guild),
                    topic=f"Ticket #{next_id} | {user} ({user.id}) | {config.TICKET_TYPES[ticket_type]['name']}",
                    reason=f"Ticket opened by {user} ({user.id}).",
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    view=ui.notice(
                        title="Ticket Failed",
                        body="The bot cannot create channels in the ticket category. Contact a staff member directly.",
                        accent=config.COLOR_DANGER,
                    ),
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                log.exception("Could not create a ticket channel.")
                await interaction.followup.send(
                    view=ui.notice(
                        title="Ticket Failed",
                        body="The ticket channel could not be created. Try again in a moment.",
                        accent=config.COLOR_DANGER,
                    ),
                    ephemeral=True,
                )
                return

            ticket = self.memory.create_ticket(
                user_id=user.id,
                channel_id=channel.id,
                ticket_type=ticket_type,
                scope=scope,
                answers=answers,
            )
            self.memory.remember_name(user.id, str(user))

        icon = self.bot.icon_url(guild)
        previous = max(0, len(self.memory.tickets_for(user.id)) - 1)
        active_cases = len(self.memory.cases_for(user.id, include_revoked=False))
        history = f"{plural(previous, 'previous ticket')}, {plural(active_cases, 'active case')}"

        await channel.send(
            view=TicketHeader(
                ticket=ticket,
                opener=user,
                icon_url=icon,
                history=history,
                staff_mention=self._staff_mention(guild),
            ),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        self.memory.log_ticket_message(
            ticket["id"],
            author_id=self.bot.user.id,
            author_name=config.BOT_NAME,
            author_avatar=str(self.bot.user.display_avatar.url) if self.bot.user else None,
            role="system",
            content=(
                f"## Ticket Opened\n"
                f"A **{config.TICKET_TYPES[ticket_type]['name']} Ticket** was opened by {user} ({user.id}).\n\n"
                f"**Subject**\n{answers.get('subject', 'Not provided')}\n\n"
                f"**Details**\n{answers.get('details', 'Not provided')}"
            ),
        )

        if ticket_type == "report":
            await self.send_evidence(ticket, channel, scope, announce=False)

        dm_ok = await self._dm_open_confirmation(user, ticket, guild, icon)

        await audit.post(
            self.bot,
            title=f"Ticket Opened - #{ticket['id']}",
            subtitle=f"{config.TICKET_TYPES[ticket_type]['name']} - {scope_label(scope)}",
            accent=config.COLOR_PRIMARY,
            fields=[
                ("Member", audit.describe(user)),
                ("Channel", channel.mention),
                ("Subject", answers.get("subject", "Not provided")),
                ("Direct messages", "Open" if dm_ok else "Closed, the member cannot be reached."),
            ],
            footer="Opened from the ticket panel.",
            thumbnail=str(user.display_avatar.url),
        )

        body = (
            f"Ticket **#{ticket['id']}** is open in {channel.mention}.\n\n"
            "From here, talk to the staff team by replying to this bot in **direct messages**. "
            "Everything you send there is delivered to the ticket."
        )
        if not dm_ok:
            body += (
                "\n\n**Your direct messages are closed.** Enable direct messages from server members "
                "so staff can reach you, otherwise this ticket cannot proceed."
            )

        await interaction.followup.send(
            view=ui.notice(
                title="Ticket Opened",
                body=body,
                accent=config.COLOR_SUCCESS if dm_ok else config.COLOR_WARNING,
                thumbnail=icon,
            ),
            ephemeral=True,
        )

        if parent is not None:
            try:
                await parent.edit_original_response(
                    view=ui.notice(
                        title="Ticket Submitted",
                        body=f"Ticket **#{ticket['id']}** was created. Check your direct messages.",
                        accent=config.COLOR_SUCCESS,
                    )
                )
            except discord.HTTPException:
                pass

    @staticmethod
    def _staff_mention(guild: discord.Guild) -> str:
        mentions = []
        for entry in config.STAFF_ROLES:
            if entry["level"] in config.TICKET_PING_LEVELS and entry["id"]:
                role = guild.get_role(entry["id"])
                if role is not None:
                    mentions.append(role.mention)
        return " ".join(mentions)

    async def _dm_open_confirmation(
        self,
        user: discord.abc.User,
        ticket: dict[str, Any],
        guild: discord.Guild,
        icon: str | None,
    ) -> bool:
        try:
            await user.send(
                view=ui.notice(
                    title=f"Ticket #{ticket['id']} Opened",
                    subtitle=f"{guild.name} - {type_name(ticket)}",
                    body=(
                        "Your ticket has been created and the staff team has been notified.\n\n"
                        "**Reply to this conversation to talk to staff.** Every message you send here "
                        "is delivered to the ticket, including attachments. You will receive staff "
                        "replies in this conversation."
                    ),
                    fields=[
                        ("Subject", ticket["answers"].get("subject", "Not provided")),
                        ("Scope", scope_label(ticket.get("scope"))),
                    ],
                    footer="Opening a false ticket will result in a punishment under rule 1.5[D].",
                    accent=config.COLOR_PRIMARY,
                    thumbnail=icon,
                )
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    @commands.Cog.listener("on_message")
    async def relay_from_member(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        ticket = self.memory.open_ticket_for(message.author.id)
        if ticket is None:
            await self._dm_no_ticket(message)
            return

        channel = self.bot.get_channel(int(ticket["channel"]))
        if channel is None:
            log.warning("Ticket #%s has no channel; closing the record.", ticket["id"])
            self.memory.close_ticket(ticket["id"], closed_by=self.bot.user.id, reason="The channel no longer exists.")
            return

        attachments = [
            {
                "filename": a.filename,
                "url": a.url,
                "size": a.size,
                "content_type": a.content_type,
            }
            for a in message.attachments
        ]

        await channel.send(
            view=ui.relay(
                heading="User",
                body=message.content,
                accent=config.COLOR_INFO,
                footer=f"{message.author} ({message.author.id})",
                attachments=[(a["filename"], a["url"]) for a in attachments],
                thumbnail=str(message.author.display_avatar.url),
            )
        )

        self.memory.log_ticket_message(
            ticket["id"],
            author_id=message.author.id,
            author_name=str(message.author),
            author_avatar=str(message.author.display_avatar.url),
            role="user",
            content=message.content,
            attachments=attachments,
        )
        self.memory.record_member_message(ticket["id"])

        try:
            await message.add_reaction("\N{WHITE HEAVY CHECK MARK}")
        except (discord.HTTPException, discord.Forbidden):
            pass

    @commands.Cog.listener()
    async def on_typing(self, channel: discord.abc.Messageable, user: discord.abc.User, when: Any) -> None:
        if getattr(user, "bot", False) or not isinstance(channel, discord.DMChannel):
            return

        ticket = self.memory.open_ticket_for(user.id)
        if ticket is None:
            return

        now = time.monotonic()
        ticket_id = int(ticket["id"])
        if now - self._typing.get(ticket_id, 0.0) < 8:
            return
        self._typing[ticket_id] = now

        target = self.bot.get_channel(int(ticket["channel"]))
        if target is None:
            return
        try:
            await target.typing()
        except discord.HTTPException:
            pass

    async def _dm_no_ticket(self, message: discord.Message) -> None:
        if self.memory.pending_appeal_for(message.author.id) is not None:
            return

        profile = self.memory.user(message.author.id)
        last = profile.get("last_dm_at") or 0
        now = time.time()
        if now - last < 120:
            return
        profile["last_dm_at"] = now
        self.bot.store.mark_dirty()

        guild = self.bot.primary_guild
        channel_hint = ""
        if guild is not None:
            panel_channel = guild.get_channel(config.TICKET_PANEL_CHANNEL_ID)
            if panel_channel is not None:
                channel_hint = f" in {panel_channel.mention}"

        try:
            await message.channel.send(
                view=ui.notice(
                    title="No Open Ticket",
                    body=(
                        "You do not have an open ticket, so this message was not delivered to anyone.\n\n"
                        f"Open a ticket from the ticket panel{channel_hint} and your messages here "
                        "will reach the staff team."
                    ),
                    accent=config.COLOR_NEUTRAL,
                    thumbnail=self.bot.icon_url(guild),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener("on_message")
    async def record_internal(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not message.content:
            return
        if message.content.startswith(config.PREFIX):
            return

        ticket = self.memory.ticket_by_channel(message.channel.id)
        if ticket is None or ticket.get("status") != "open":
            return

        self.memory.log_ticket_message(
            ticket["id"],
            author_id=message.author.id,
            author_name=str(message.author),
            author_avatar=str(message.author.display_avatar.url),
            role="staff",
            content=message.content,
            attachments=[
                {"filename": a.filename, "url": a.url, "size": a.size, "content_type": a.content_type}
                for a in message.attachments
            ],
            internal=True,
        )

    async def _require_ticket(self, ctx: commands.Context) -> dict[str, Any] | None:
        if not permissions.has_level(ctx.author, config.LEVEL_TICKET_STAFF):
            await ctx.send(
                view=ui.notice(
                    title="Permission Denied",
                    body="Only staff can use ticket commands.",
                    accent=config.COLOR_DANGER,
                )
            )
            return None

        ticket = self.memory.ticket_by_channel(ctx.channel.id)
        if ticket is None or ticket.get("status") != "open":
            await ctx.send(
                view=ui.notice(
                    title="Not a Ticket Channel",
                    body="This command only works inside an open ticket channel.",
                    accent=config.COLOR_WARNING,
                )
            )
            return None
        return ticket

    async def _fetch_user(self, user_id: int | str | None) -> discord.User | None:
        if not user_id:
            return None
        user = self.bot.get_user(int(user_id))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(user_id))
            except discord.HTTPException:
                return None
        return user

    async def _fetch_opener(self, ticket: dict[str, Any]) -> discord.User | None:
        return await self._fetch_user(ticket["user"])

    def _handler_id(self, ticket: dict[str, Any]) -> str | None:
        bot_id = str(self.bot.user.id) if self.bot.user else None
        for key in ("claimed_by", "first_responder", "closed_by"):
            value = ticket.get(key)
            if value and value != bot_id:
                return str(value)
        return None

    async def _handler_name(self, ticket: dict[str, Any]) -> str:
        handler = await self._fetch_user(self._handler_id(ticket))
        return str(handler) if handler else "Unclaimed"

    @commands.command(name="r")
    async def reply_to_member(self, ctx: commands.Context, *, message: str) -> None:
        appeals = self.bot.get_cog("Appeals")
        if appeals is not None and self.memory.ticket_by_channel(ctx.channel.id) is None:
            appeal = self.memory.appeal_by_channel(ctx.channel.id)
            if appeal is not None:
                if not permissions.effective_level(self.bot, ctx.author) >= config.LEVEL_TICKET_STAFF:
                    raise commands.CheckFailure()
                await appeals.ask_member(ctx, appeal, message)
                return

        ticket = await self._require_ticket(ctx)
        if ticket is None:
            return
        await self._deliver_reply(ctx, ticket, message)

    @commands.command(name="rs")
    async def reply_with_snippet(self, ctx: commands.Context, name: str) -> None:
        ticket = await self._require_ticket(ctx)
        if ticket is None:
            return

        entry = self.memory.snippet(name)
        if entry is None:
            names = ", ".join(f"`{n}`" for n in sorted(self.memory.snippets())) or "None saved yet."
            await ctx.send(
                view=ui.notice(
                    title="Unknown Saved Reply",
                    body=f"There is no saved reply called `{name}`.",
                    fields=[("Available", names)],
                    accent=config.COLOR_WARNING,
                )
            )
            return

        user = await self._fetch_opener(ticket)
        text = self._fill_snippet(entry["text"], ticket, user, ctx.guild)
        if await self._deliver_reply(ctx, ticket, text, user=user, note=f"Saved reply: {entry['name']}"):
            self.memory.use_snippet(entry["name"])

    @staticmethod
    def _fill_snippet(
        text: str,
        ticket: dict[str, Any],
        user: discord.abc.User | None,
        guild: discord.Guild | None,
    ) -> str:
        values = {
            "{member}": user.display_name if user else "there",
            "{ticket}": str(ticket["id"]),
            "{server}": guild.name if guild else config.COMMUNITY_NAME,
        }
        for token, value in values.items():
            text = text.replace(token, value)
        return text

    async def _deliver_reply(
        self,
        ctx: commands.Context,
        ticket: dict[str, Any],
        text: str,
        *,
        user: discord.User | None = None,
        note: str | None = None,
    ) -> bool:
        user = user or await self._fetch_opener(ticket)
        if user is None:
            await ctx.send(
                view=ui.notice(
                    title="Member Unavailable",
                    body="That member could not be resolved. They may have deleted their account.",
                    accent=config.COLOR_DANGER,
                )
            )
            return False

        attachments = [
            {"filename": a.filename, "url": a.url, "size": a.size, "content_type": a.content_type}
            for a in ctx.message.attachments
        ]

        outbound = ui.relay(
            heading="Staff Member",
            body=text,
            accent=config.COLOR_PRIMARY,
            footer=f"{ctx.guild.name} - Ticket #{ticket['id']}" if ctx.guild else f"Ticket #{ticket['id']}",
            attachments=[(a["filename"], a["url"]) for a in attachments],
        )

        try:
            await user.send(view=outbound)
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(
                view=ui.notice(
                    title="Reply Not Delivered",
                    body=(
                        "The member has direct messages closed, so the reply could not be sent. "
                        "Nothing was delivered."
                    ),
                    accent=config.COLOR_DANGER,
                )
            )
            return False

        self.memory.log_ticket_message(
            ticket["id"],
            author_id=ctx.author.id,
            author_name=str(ctx.author),
            author_avatar=str(ctx.author.display_avatar.url),
            role="staff",
            content=text,
            attachments=attachments,
        )
        self.memory.record_staff_reply(ticket["id"], ctx.author.id)
        if not ticket.get("claimed_by"):
            self.memory.claim_ticket(ticket["id"], ctx.author.id)

        footer = f"Sent by {ctx.author} - {permissions.title_of(ctx.author)}"
        if note:
            footer += f" - {note}"
        await ctx.send(
            view=ui.relay(
                heading="Staff Member",
                body=text,
                accent=config.COLOR_PRIMARY,
                footer=footer,
                attachments=[(a["filename"], a["url"]) for a in attachments],
            )
        )
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        return True

    @commands.group(name="snippet", aliases=["snippets"], invoke_without_command=True)
    async def snippet_group(self, ctx: commands.Context, name: str | None = None) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_TICKET_STAFF):
            raise commands.CheckFailure()

        if name is not None:
            entry = self.memory.snippet(name)
            if entry is None:
                await ctx.send(
                    view=ui.notice(
                        title="Unknown Saved Reply",
                        body=f"There is no saved reply called `{name}`.",
                        accent=config.COLOR_WARNING,
                    )
                )
                return
            await ctx.send(
                view=ui.notice(
                    title=f"Saved Reply - {entry['name']}",
                    body=entry["text"],
                    fields=[("Used", plural(int(entry.get("uses", 0)), "time"))],
                    footer=f"Send it inside a ticket with {config.PREFIX}rs {entry['name']}.",
                    accent=config.COLOR_INFO,
                )
            )
            return

        entries = sorted(self.memory.snippets().values(), key=lambda e: e["name"])
        listed = "\n".join(
            f"`{e['name']}` - {ui.clip(e['text'], 70)}" for e in entries
        ) or "No saved replies yet."
        await ctx.send(
            view=ui.notice(
                title="Saved Replies",
                body=listed,
                fields=[
                    (
                        "Placeholders",
                        "`{member}` the member's name, `{ticket}` the ticket number, `{server}` the server name.",
                    ),
                ],
                footer=(
                    f"{config.PREFIX}rs <name> sends one. {config.PREFIX}snippet add <name> <text> and "
                    f"{config.PREFIX}snippet remove <name> manage them."
                ),
                accent=config.COLOR_INFO,
            )
        )

    @snippet_group.command(name="add", aliases=["set"])
    async def snippet_add(self, ctx: commands.Context, name: str, *, text: str) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_MANAGE_SNIPPETS):
            raise commands.CheckFailure()

        key = name.casefold()
        if not SNIPPET_NAME.match(key) or key in RESERVED_SNIPPET_NAMES:
            await ctx.send(
                view=ui.notice(
                    title="Invalid Name",
                    body="Use up to 32 lowercase letters, numbers, dashes or underscores.",
                    accent=config.COLOR_WARNING,
                )
            )
            return

        existed = self.memory.snippet(key) is not None
        self.memory.save_snippet(key, text, ctx.author.id)
        await ctx.send(
            view=ui.notice(
                title="Saved Reply Updated" if existed else "Saved Reply Added",
                body=text,
                fields=[("Name", f"`{key}`")],
                footer=audit.actor_note(ctx.author),
                accent=config.COLOR_SUCCESS,
            )
        )

    @snippet_group.command(name="remove", aliases=["delete"])
    async def snippet_remove(self, ctx: commands.Context, name: str) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_MANAGE_SNIPPETS):
            raise commands.CheckFailure()

        removed = self.memory.delete_snippet(name)
        await ctx.send(
            view=ui.notice(
                title="Saved Reply Removed" if removed else "Unknown Saved Reply",
                body=f"`{name.casefold()}` " + ("was removed." if removed else "does not exist."),
                footer=audit.actor_note(ctx.author) if removed else None,
                accent=config.COLOR_SUCCESS if removed else config.COLOR_WARNING,
            )
        )

    @commands.command(name="evidence")
    async def evidence_command(self, ctx: commands.Context, kind: str | None = None) -> None:
        ticket = await self._require_ticket(ctx)
        if ticket is None:
            return

        scope = (kind or ticket.get("scope") or "discord").lower()
        if scope not in ("discord", "ingame"):
            scope = "discord"

        await self.send_evidence(ticket, ctx.channel, scope, announce=True, actor=ctx.author)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def send_evidence(
        self,
        ticket: dict[str, Any],
        channel: discord.abc.Messageable,
        scope: str,
        *,
        announce: bool,
        actor: discord.abc.User | None = None,
    ) -> None:
        if scope == "ingame":
            title = "In-Game Report Evidence"
            body = INGAME_EVIDENCE
        else:
            title = "Discord Report Evidence"
            body = DISCORD_EVIDENCE

        view = ui.notice(
            title=title,
            body=body,
            accent=config.COLOR_INFO,
            footer="Evidence that does not meet this standard is not accepted.",
        )

        user = await self._fetch_opener(ticket)
        delivered = False
        if user is not None:
            try:
                await user.send(view=view)
                delivered = True
            except (discord.Forbidden, discord.HTTPException):
                delivered = False

        await channel.send(view=view)

        if announce and not delivered:
            await channel.send(
                view=ui.notice(
                    title="Not Delivered",
                    body="The member has direct messages closed, so they did not receive this.",
                    accent=config.COLOR_WARNING,
                )
            )

        self.memory.log_ticket_message(
            ticket["id"],
            author_id=actor.id if actor else self.bot.user.id,
            author_name=str(actor) if actor else config.BOT_NAME,
            author_avatar=str(actor.display_avatar.url) if actor else None,
            role="system",
            content=f"## {title}\n{body}",
        )

    @commands.command(name="ct", aliases=["close"])
    async def close_ticket(self, ctx: commands.Context, *, reason: str | None = None) -> None:
        ticket = await self._require_ticket(ctx)
        if ticket is None:
            return
        await self.finish_ticket(ticket, ctx.channel, ctx.author, reason)

    @commands.command(name="hold")
    async def hold_ticket(self, ctx: commands.Context) -> None:
        ticket = await self._require_ticket(ctx)
        if ticket is None:
            return

        held = not ticket.get("on_hold")
        self.memory.set_ticket_hold(ticket["id"], held)

        body = (
            "Automatic closing is paused for this ticket, however long the member takes to reply. "
            f"Run `{config.PREFIX}hold` again to resume it."
            if held
            else "Automatic closing applies to this ticket again."
        )
        if not config.TICKET_AUTOCLOSE_ENABLED:
            body += "\n\nAutomatic closing is currently switched off for every ticket in the configuration."

        await ctx.send(
            view=ui.notice(
                title="Ticket On Hold" if held else "Hold Released",
                body=body,
                footer=f"{ctx.author} - {permissions.title_of(ctx.author)}",
                accent=config.COLOR_INFO if held else config.COLOR_NEUTRAL,
            )
        )

    async def handle_claim(self, interaction: discord.Interaction) -> None:
        if not permissions.has_level(interaction.user, config.LEVEL_TICKET_STAFF):
            await ui.respond(
                interaction,
                ui.notice(title="Permission Denied", body="Only staff can claim tickets.", accent=config.COLOR_DANGER),
            )
            return

        ticket = self.memory.ticket_by_channel(interaction.channel_id)
        if ticket is None or ticket.get("status") != "open":
            await ui.respond(
                interaction,
                ui.notice(title="Not a Ticket", body="This is not an open ticket channel.", accent=config.COLOR_WARNING),
            )
            return

        held = ticket.get("claimed_by")
        if held and held != str(interaction.user.id):
            await ui.respond(
                interaction,
                ui.notice(
                    title="Already Claimed",
                    body=f"<@{held}> is already handling this ticket.",
                    accent=config.COLOR_WARNING,
                ),
            )
            return

        self.memory.claim_ticket(ticket["id"], interaction.user.id)
        await interaction.response.send_message(
            view=ui.notice(
                title="Ticket Claimed",
                body=f"{interaction.user.mention} is handling this ticket.",
                footer=f"{interaction.user} - {permissions.title_of(interaction.user)}",
                accent=config.COLOR_SUCCESS,
            )
        )

    async def handle_close_button(self, interaction: discord.Interaction) -> None:
        if not permissions.has_level(interaction.user, config.LEVEL_TICKET_STAFF):
            await ui.respond(
                interaction,
                ui.notice(title="Permission Denied", body="Only staff can close tickets.", accent=config.COLOR_DANGER),
            )
            return

        ticket = self.memory.ticket_by_channel(interaction.channel_id)
        if ticket is None or ticket.get("status") != "open":
            await ui.respond(
                interaction,
                ui.notice(title="Not a Ticket", body="This is not an open ticket channel.", accent=config.COLOR_WARNING),
            )
            return

        await interaction.response.send_message(
            view=ui.notice(
                title="Closing Ticket",
                body="The transcript is being generated and the member is being notified.",
                accent=config.COLOR_NEUTRAL,
            )
        )
        await self.finish_ticket(ticket, interaction.channel, interaction.user, None)

    async def finish_ticket(
        self,
        ticket: dict[str, Any],
        channel: discord.abc.Messageable,
        closer: discord.abc.User,
        reason: str | None,
        *,
        automatic: bool = False,
    ) -> None:
        ticket_id = int(ticket["id"])
        current = self.memory.ticket(ticket_id)
        if current is None or current.get("status") != "open" or ticket_id in self._closing:
            return

        self._closing.add(ticket_id)
        try:
            await self._finish(current, channel, closer, reason, automatic)
        finally:
            self._closing.discard(ticket_id)
            self._typing.pop(ticket_id, None)

    async def _finish(
        self,
        ticket: dict[str, Any],
        channel: discord.abc.Messageable,
        closer: discord.abc.User,
        reason: str | None,
        automatic: bool,
    ) -> None:
        guild = getattr(channel, "guild", None) or self.bot.primary_guild
        user = await self._fetch_opener(ticket)
        icon = self.bot.icon_url(guild)
        community = guild.name if guild else config.COMMUNITY_NAME
        closer_label = "Automatic" if automatic else str(closer)

        self.memory.log_ticket_message(
            ticket["id"],
            author_id=closer.id,
            author_name=config.BOT_NAME if automatic else str(closer),
            author_avatar=str(closer.display_avatar.url),
            role="system",
            content=(
                "## Ticket Closed\n"
                + ("Closed automatically after inactivity." if automatic else f"Closed by {closer}.")
                + (f"\n\n**Reason**\n{reason}" if reason else "")
            ),
        )

        record = self.memory.close_ticket(ticket["id"], closed_by=closer.id, reason=reason) or ticket

        channel_name = getattr(channel, "name", f"ticket-{ticket['id']}")
        opener_name = str(user) if user else f"Unknown ({ticket['user']})"
        handler_name = await self._handler_name(record)

        staff_html = transcripts.build(
            record,
            guild_name=community,
            channel_name=channel_name,
            opener_name=opener_name,
            opener_id=str(ticket["user"]),
            closer_name=closer_label,
            handler_name=handler_name,
            icon_url=icon,
            include_internal=True,
        )
        member_html = transcripts.build(
            record,
            guild_name=community,
            channel_name=channel_name,
            opener_name=opener_name,
            opener_id=str(ticket["user"]),
            closer_name=closer_label,
            handler_name="Staff Team",
            icon_url=icon,
            include_internal=False,
        )

        path = transcripts.write(staff_html, ticket["id"])
        record["transcript"] = str(path)
        self.bot.store.mark_dirty()

        avatar = await self._avatar_bytes(user)
        staff_card = await self._render_card(
            record, community, opener_name, handler_name, closer_label, avatar, automatic
        )
        member_card = await self._render_card(
            record, community, opener_name, "Staff Team", "Automatic" if automatic else "Staff Team", avatar, automatic
        )

        html_name = f"ticket-{ticket['id']}-transcript.html"
        card_name = cards.filename(ticket["id"])

        delivered = False
        if user is not None:
            delivered = await self._send_member_closure(
                user, record, community, icon, reason, automatic, member_html, html_name, member_card, card_name
            )

        await self._archive(
            record,
            staff_html,
            staff_card,
            {
                "opener_name": opener_name,
                "handler_name": handler_name,
                "closer_name": closer_label,
                "closer_id": str(closer.id),
                "community": community,
                "automatic": automatic,
                "delivered": delivered,
                "html": html_name,
                "card": card_name if staff_card else None,
            },
        )

        await audit.post(
            self.bot,
            title=f"Ticket Closed - #{ticket['id']}",
            accent=config.COLOR_NEUTRAL,
            fields=[
                ("Member", opener_name),
                ("Closed by", "Automatic, after inactivity" if automatic else audit.describe(closer)),
                ("Handled by", handler_name),
                ("Reason", reason or "No reason given."),
                ("Messages", str(len(record.get("messages", [])))),
                ("Transcript delivered", "Yes" if delivered else "No, direct messages are closed."),
            ],
            footer=audit.actor_note(None if automatic else closer, fallback="Closed automatically by the bot."),
        )

        footer = (
            "Closed automatically after inactivity."
            if automatic
            else f"Closed by {closer} - {permissions.title_of(closer)}"
        )
        try:
            await channel.send(
                view=ui.notice(
                    title="Ticket Closed",
                    body=(
                        "The transcript has been archived"
                        + (" and delivered to the member." if delivered else ", but the member has direct messages closed.")
                        + "\n\nThis channel will be deleted in ten seconds."
                    ),
                    footer=footer,
                    accent=config.COLOR_NEUTRAL,
                )
            )
        except discord.HTTPException:
            pass

        await asyncio.sleep(10)
        try:
            await channel.delete(reason=f"Ticket #{ticket['id']} closed by {closer_label}.")
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    async def _send_member_closure(
        self,
        user: discord.User,
        record: dict[str, Any],
        community: str,
        icon: str | None,
        reason: str | None,
        automatic: bool,
        member_html: str,
        html_name: str,
        member_card: bytes | None,
        card_name: str,
    ) -> bool:
        if automatic:
            body = (
                "This ticket was closed automatically because we did not receive a reply after "
                "our last message.\n\n"
                "If you still need help, open a new ticket from the ticket panel and a staff "
                "member will be with you shortly."
            )
        else:
            body = (
                "This ticket has been closed by the staff team.\n\n"
                "If you have any further questions, comments or concerns, open a new ticket "
                "from the ticket panel and a staff member will be with you shortly. "
                "Messages sent to this bot without an open ticket are not delivered to anyone."
            )

        gallery = (
            [dui.MediaGallery(discord.MediaGalleryItem(f"attachment://{card_name}"))]
            if member_card
            else None
        )
        closing = ui.notice(
            title="Ticket Closed",
            subtitle=f"{community} - Ticket #{record['id']}",
            body=body,
            fields=[("Reason", reason)] if reason and not automatic else None,
            footer="A copy of this conversation is attached below.",
            accent=config.COLOR_NEUTRAL,
            thumbnail=icon,
            extra_items=gallery,
        )

        try:
            if member_card:
                await user.send(view=closing, file=discord.File(io.BytesIO(member_card), filename=card_name))
            else:
                await user.send(view=closing)
            await user.send(
                view=ui.file_notice(
                    title=f"Transcript - Ticket #{record['id']}",
                    body="Open this file in any browser to read the full conversation.",
                    filename=html_name,
                    accent=config.COLOR_INFO,
                    footer=f"{config.BOT_NAME} - {config.COMMUNITY_NAME}",
                ),
                file=discord.File(io.BytesIO(member_html.encode("utf-8")), filename=html_name),
            )
        except (discord.Forbidden, discord.HTTPException):
            return False

        if config.TICKET_RATINGS_ENABLED:
            try:
                await user.send(view=RatingPrompt(int(record["id"])))
            except (discord.Forbidden, discord.HTTPException):
                pass
        return True

    @staticmethod
    async def _avatar_bytes(user: discord.abc.User | None) -> bytes | None:
        if user is None or not cards.available():
            return None
        try:
            return await user.display_avatar.replace(size=128, static_format="png").read()
        except (discord.HTTPException, discord.NotFound, ValueError):
            return None

    @staticmethod
    async def _render_card(
        ticket: dict[str, Any],
        community: str,
        opener_name: str,
        handler_name: str,
        closer_name: str,
        avatar: bytes | None,
        automatic: bool,
    ) -> bytes | None:
        if not cards.available():
            return None
        return await asyncio.to_thread(
            cards.render_ticket_card,
            ticket,
            community=community,
            opener_name=opener_name,
            handler_name=handler_name,
            closer_name=closer_name,
            avatar=avatar,
            status="Auto-closed" if automatic else "Closed",
            accent=config.COLOR_WARNING if automatic else config.COLOR_PRIMARY,
        )

    def _archive_view(self, record: dict[str, Any], meta: dict[str, Any], with_card: bool) -> ui.Layout:
        opened = record.get("created_at")
        closed = record.get("closed_at")
        first = record.get("first_response_at")

        rows = [
            ("Member", f"<@{record['user']}>\n{meta['opener_name']} ({record['user']})"),
            ("Scope", scope_label(record.get("scope"))),
            ("Subject", record.get("answers", {}).get("subject", "Not provided")),
            ("Opened", f"<t:{int(opened)}:F>" if opened else "Unknown"),
            ("Closed", f"<t:{int(closed)}:F>" if closed else "Unknown"),
            (
                "Closed by",
                "Automatic, after inactivity"
                if meta.get("automatic")
                else f"{meta['closer_name']} ({meta['closer_id']})",
            ),
            ("Handled by", meta["handler_name"]),
            ("First response", dur.humanise(int(first - opened)) if first and opened else "No staff reply"),
            ("Messages", str(len(record.get("messages", [])))),
            ("Close reason", record.get("close_reason") or "No reason given."),
            ("Delivered to member", "Yes" if meta.get("delivered") else "No, direct messages are closed."),
        ]
        if config.TICKET_RATINGS_ENABLED:
            rows.append(("Member rating", rating_text(record.get("rating"))))
            if record.get("rating_comment"):
                rows.append(("Member comment", record["rating_comment"]))

        view = ui.Layout()
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_INFO))
        container.add_item(dui.TextDisplay(f"## Transcript - Ticket #{record['id']}\n-# {type_name(record)}"))
        if with_card and meta.get("card"):
            container.add_item(dui.MediaGallery(discord.MediaGalleryItem(f"attachment://{meta['card']}")))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(ui.clip(ui.field_list(rows), ui.MAX_TEXT)))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.File(f"attachment://{meta['html']}"))
        view.add_item(container)
        return view

    async def _transcript_channel(self) -> discord.abc.Messageable | None:
        target = self.bot.get_channel(config.TRANSCRIPT_CHANNEL_ID)
        if target is None:
            try:
                target = await self.bot.fetch_channel(config.TRANSCRIPT_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Transcript channel %s is unreachable.", config.TRANSCRIPT_CHANNEL_ID)
                return None
        return target

    async def _archive(
        self,
        record: dict[str, Any],
        html_text: str,
        card: bytes | None,
        meta: dict[str, Any],
    ) -> None:
        target = await self._transcript_channel()
        if target is None:
            return

        files = [discord.File(io.BytesIO(html_text.encode("utf-8")), filename=meta["html"])]
        if card:
            files.append(discord.File(io.BytesIO(card), filename=meta["card"]))

        try:
            message = await target.send(view=self._archive_view(record, meta, with_card=bool(card)), files=files)
        except discord.HTTPException:
            log.exception("Could not archive the transcript for ticket #%s.", record["id"])
            return

        self.memory.set_ticket_archive(record["id"], {**meta, "channel": str(target.id), "message": str(message.id)})

    async def _refresh_archive(self, ticket: dict[str, Any]) -> None:
        meta = ticket.get("archive") or {}
        if not meta.get("message"):
            return

        channel = self.bot.get_channel(int(meta["channel"]))
        try:
            if channel is None:
                channel = await self.bot.fetch_channel(int(meta["channel"]))
            message = await channel.fetch_message(int(meta["message"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        attachments: list[discord.Attachment | discord.File] = [
            a for a in message.attachments if a.filename == meta.get("html")
        ]
        card = None
        if meta.get("card"):
            user = await self._fetch_opener(ticket)
            card = await self._render_card(
                ticket,
                meta.get("community", config.COMMUNITY_NAME),
                meta["opener_name"],
                meta["handler_name"],
                meta["closer_name"],
                await self._avatar_bytes(user),
                bool(meta.get("automatic")),
            )
        if card:
            attachments.append(discord.File(io.BytesIO(card), filename=meta["card"]))
        else:
            attachments.extend(a for a in message.attachments if a.filename == meta.get("card"))

        with_card = any(getattr(a, "filename", None) == meta.get("card") for a in attachments)
        try:
            await message.edit(view=self._archive_view(ticket, meta, with_card), attachments=attachments)
        except discord.HTTPException:
            log.warning("Could not update the archived transcript for ticket #%s.", ticket["id"])

    @commands.Cog.listener("on_interaction")
    async def rating_buttons(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(RATING_PREFIX):
            return

        try:
            raw_id, action = custom_id[len(RATING_PREFIX):].split(":", 1)
            ticket_id = int(raw_id)
        except ValueError:
            return

        ticket = self.memory.ticket(ticket_id)
        if ticket is None or ticket.get("user") != str(interaction.user.id):
            await ui.respond(
                interaction,
                ui.notice(
                    title="Rating Unavailable",
                    body="This ticket could not be found.",
                    accent=config.COLOR_WARNING,
                ),
            )
            return

        if action == "comment":
            if not ticket.get("rating") or ticket.get("rating_comment"):
                await interaction.response.edit_message(
                    view=RatingThanks(ticket_id, ticket.get("rating") or 0, commented=True)
                )
                return
            await interaction.response.send_modal(RatingComment(self, ticket_id))
            return

        try:
            score = int(action)
        except ValueError:
            return
        if score not in RATING_LABELS:
            return

        if ticket.get("rating"):
            await interaction.response.edit_message(
                view=RatingThanks(ticket_id, ticket["rating"], commented=bool(ticket.get("rating_comment")))
            )
            return

        self.memory.rate_ticket(ticket_id, score)
        await interaction.response.edit_message(view=RatingThanks(ticket_id, score, commented=False))
        await self.publish_rating(ticket, comment_added=False)

    async def save_rating_comment(self, interaction: discord.Interaction, ticket_id: int, comment: str) -> None:
        ticket = self.memory.ticket(ticket_id)
        if ticket is None or ticket.get("user") != str(interaction.user.id) or ticket.get("rating_comment"):
            await ui.respond(
                interaction,
                ui.notice(title="Comment Not Saved", body="A comment is already on record.", accent=config.COLOR_WARNING),
            )
            return

        self.memory.comment_on_rating(ticket_id, comment)
        thanks = RatingThanks(ticket_id, ticket["rating"], commented=True)
        if interaction.message is not None:
            await interaction.response.edit_message(view=thanks)
        else:
            await interaction.response.send_message(view=thanks)
        await self.publish_rating(ticket, comment_added=True)

    async def publish_rating(self, ticket: dict[str, Any], *, comment_added: bool) -> None:
        score = int(ticket.get("rating") or 0)
        handler = self._handler_id(ticket)
        if score >= 4:
            accent = config.COLOR_SUCCESS
        elif score == 3:
            accent = config.COLOR_WARNING
        else:
            accent = config.COLOR_DANGER

        fields = [
            ("Member", f"<@{ticket['user']}> ({ticket['user']})"),
            ("Rating", rating_text(score)),
            ("Handled by", f"<@{handler}>" if handler else "Unclaimed"),
        ]
        if ticket.get("rating_comment"):
            fields.append(("Comment", ticket["rating_comment"]))

        await audit.post(
            self.bot,
            title=f"{'Rating Comment' if comment_added else 'Ticket Rated'} - #{ticket['id']}",
            accent=accent,
            fields=fields,
            footer="Submitted by the member after the ticket closed.",
        )
        await self._refresh_archive(ticket)

    @tasks.loop(minutes=10)
    async def inactivity_sweep(self) -> None:
        now = time.time()
        remind_after = config.TICKET_REMIND_AFTER_HOURS * 3600
        close_after = config.TICKET_AUTOCLOSE_AFTER_HOURS * 3600

        for ticket in self.memory.open_tickets():
            if ticket.get("on_hold") or int(ticket["id"]) in self._closing:
                continue

            last_staff = ticket.get("last_staff_at")
            last_member = ticket.get("last_member_at") or ticket.get("created_at") or 0
            if not last_staff or last_staff <= last_member:
                continue

            try:
                reminded = ticket.get("reminded_at")
                if not reminded:
                    if now - last_staff >= remind_after:
                        await self._send_reminder(ticket, close_after)
                elif now - reminded >= close_after:
                    self._auto_close(ticket)
            except Exception:
                log.exception("Inactivity handling failed for ticket #%s.", ticket.get("id"))

    @inactivity_sweep.before_loop
    async def before_inactivity_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_reminder(self, ticket: dict[str, Any], close_after: float) -> None:
        deadline = time.time() + close_after
        user = await self._fetch_opener(ticket)

        delivered = False
        if user is not None:
            try:
                await user.send(
                    view=ui.notice(
                        title="Are You Still There?",
                        subtitle=f"Ticket #{ticket['id']}",
                        body=(
                            "The staff team replied to your ticket and is waiting on your response.\n\n"
                            "Reply in this conversation to keep the ticket open. If we do not hear from "
                            f"you, it will close automatically {dur.timestamp(deadline, 'R')}."
                        ),
                        accent=config.COLOR_WARNING,
                        thumbnail=self.bot.icon_url(),
                    )
                )
                delivered = True
            except (discord.Forbidden, discord.HTTPException):
                delivered = False

        self.memory.mark_ticket_reminded(ticket["id"])
        self.memory.log_ticket_message(
            ticket["id"],
            author_id=self.bot.user.id,
            author_name=config.BOT_NAME,
            author_avatar=str(self.bot.user.display_avatar.url),
            role="system",
            content=(
                "## Inactivity Reminder\n"
                + ("The member was reminded to reply." if delivered else "The reminder could not be delivered.")
            ),
        )

        channel = self.bot.get_channel(int(ticket["channel"]))
        if channel is None:
            return
        try:
            await channel.send(
                view=ui.notice(
                    title="Inactivity Reminder Sent" if delivered else "Inactivity Reminder Not Delivered",
                    body=(
                        "The member has not replied since the last staff message. This ticket closes "
                        f"automatically {dur.timestamp(deadline, 'R')} unless they reply.\n\n"
                        f"Run `{config.PREFIX}hold` to keep it open."
                    ),
                    accent=config.COLOR_WARNING,
                )
            )
        except discord.HTTPException:
            pass

    def _auto_close(self, ticket: dict[str, Any]) -> None:
        if self.bot.user is None:
            return
        channel = self.bot.get_channel(int(ticket["channel"]))
        if channel is None:
            self.memory.close_ticket(ticket["id"], closed_by=self.bot.user.id, reason="The channel no longer exists.")
            return

        hours = config.TICKET_REMIND_AFTER_HOURS + config.TICKET_AUTOCLOSE_AFTER_HOURS
        reason = f"No reply from the member for {hours} hours after the last staff message."
        task = asyncio.create_task(self.finish_ticket(ticket, channel, self.bot.user, reason, automatic=True))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @commands.command(name="ticketinfo", aliases=["tinfo"])
    async def ticket_info(self, ctx: commands.Context) -> None:
        ticket = await self._require_ticket(ctx)
        if ticket is None:
            return

        user = await self._fetch_opener(ticket)
        profile = self.memory.user(ticket["user"])
        cases = self.memory.cases_for(ticket["user"], include_revoked=False)
        history = self.memory.tickets_for(ticket["user"])

        recent = "\n".join(
            f"Case #{c['id']} - {c['rule']}[{c['clause']}] - {c['duration_label']}"
            for c in cases[-5:]
        ) or "No cases on record."

        last_staff = ticket.get("last_staff_at")
        last_member = ticket.get("last_member_at") or ticket.get("created_at") or 0
        waiting_on = "Member" if last_staff and last_staff > last_member else "Staff"

        if not config.TICKET_AUTOCLOSE_ENABLED:
            autoclose = "Switched off"
        elif ticket.get("on_hold"):
            autoclose = "On hold"
        elif ticket.get("reminded_at"):
            deadline = float(ticket["reminded_at"]) + config.TICKET_AUTOCLOSE_AFTER_HOURS * 3600
            autoclose = f"Reminder sent, closes {dur.timestamp(deadline, 'R')}"
        else:
            autoclose = "Active"

        first = ticket.get("first_response_at")
        claimed = ticket.get("claimed_by")
        ratings = [int(t["rating"]) for t in history if t.get("rating")]

        await ctx.send(
            view=ui.notice(
                title=f"Member Record - Ticket #{ticket['id']}",
                subtitle=str(user) if user else str(ticket["user"]),
                fields=[
                    ("Member", f"<@{ticket['user']}> ({ticket['user']})"),
                    ("Cases", f"{len(cases)} active, {len(self.memory.cases_for(ticket['user']))} total"),
                    ("Tickets", f"{len(history)} on record"),
                    (
                        "Past ratings",
                        f"{statistics.mean(ratings):.1f} / 5 across {plural(len(ratings), 'ticket')}"
                        if ratings
                        else "None given",
                    ),
                    ("Filter strikes", str(profile.get("filter_strikes", 0))),
                    ("Claimed by", f"<@{claimed}>" if claimed else "Unclaimed"),
                    (
                        "First response",
                        dur.humanise(int(first - ticket["created_at"])) if first else "No staff reply yet",
                    ),
                    ("Waiting on", waiting_on),
                    ("Automatic closing", autoclose),
                    ("Recent cases", recent),
                ],
                accent=config.COLOR_INFO,
                thumbnail=str(user.display_avatar.url) if user else None,
            )
        )

    @commands.command(name="ticketstats", aliases=["tstats"])
    async def ticket_stats(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        days: int = 30,
    ) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_TICKET_STAFF):
            raise commands.CheckFailure()

        days = max(1, min(days, 365))
        since = time.time() - days * 86400
        tickets = list(self.memory.store.section("tickets").values())
        if member is not None:
            tickets = [t for t in tickets if self._handler_id(t) == str(member.id)]

        opened = [t for t in tickets if (t.get("created_at") or 0) >= since]
        closed = [t for t in tickets if (t.get("closed_at") or 0) >= since]
        open_now = [t for t in tickets if t.get("status") == "open"]

        responses = [t["first_response_at"] - t["created_at"] for t in opened if t.get("first_response_at")]
        resolutions = [t["closed_at"] - t["created_at"] for t in closed if t.get("created_at")]
        ratings = [int(t["rating"]) for t in closed if t.get("rating")]
        by_type = Counter(type_name(t) for t in opened)

        fields = [
            ("Volume", f"{len(opened)} opened, {len(closed)} closed, {len(open_now)} open now"),
            ("First response", self._spread(responses)),
            ("Time to close", self._spread(resolutions)),
            (
                "Satisfaction",
                f"{statistics.mean(ratings):.1f} / 5 from {plural(len(ratings), 'rating')}"
                if ratings
                else "No ratings in this period.",
            ),
            ("By type", ", ".join(f"{name}: {count}" for name, count in by_type.most_common()) or "None opened."),
        ]

        if member is None:
            handled = Counter(h for h in (self._handler_id(t) for t in closed) if h)
            lines = []
            for staff_id, count in handled.most_common(5):
                scores = [int(t["rating"]) for t in closed if t.get("rating") and self._handler_id(t) == staff_id]
                average = f", {statistics.mean(scores):.1f} / 5" if scores else ""
                lines.append(f"<@{staff_id}> - {plural(count, 'ticket')}{average}")
            fields.append(("Most tickets handled", "\n".join(lines) or "No closed tickets in this period."))

        await ctx.send(
            view=ui.notice(
                title="Ticket Statistics",
                subtitle=f"Last {plural(days, 'day')}" + (f" - {member}" if member else ""),
                fields=fields,
                footer="Response times count from when the ticket was opened to the first staff reply.",
                accent=config.COLOR_INFO,
                thumbnail=str(member.display_avatar.url) if member else self.bot.icon_url(ctx.guild),
            )
        )

    @staticmethod
    def _spread(values: list[float]) -> str:
        if not values:
            return "No data in this period."
        average = dur.humanise(int(statistics.mean(values)))
        median = dur.humanise(int(statistics.median(values)))
        return f"Average {average}, median {median}"

    @commands.command(name="block")
    async def block_member(self, ctx: commands.Context, member: discord.User, *, reason: str = "No reason given.") -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_REVOKE_CASE):
            raise commands.CheckFailure()

        self.memory.set_ticket_block(member.id, True)
        await ctx.send(
            view=ui.notice(
                title="Ticket Access Withdrawn",
                fields=[("Member", audit.describe(member)), ("Reason", reason)],
                accent=config.COLOR_DANGER,
                footer=audit.actor_note(ctx.author),
            )
        )
        await audit.post(
            self.bot,
            title="Ticket Access Withdrawn",
            accent=config.COLOR_DANGER,
            fields=[("Member", audit.describe(member)), ("Reason", reason)],
            footer=audit.actor_note(ctx.author),
        )

    @commands.command(name="unblock")
    async def unblock_member(self, ctx: commands.Context, member: discord.User) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_REVOKE_CASE):
            raise commands.CheckFailure()

        self.memory.set_ticket_block(member.id, False)
        await ctx.send(
            view=ui.notice(
                title="Ticket Access Restored",
                fields=[("Member", audit.describe(member))],
                accent=config.COLOR_SUCCESS,
                footer=audit.actor_note(ctx.author),
            )
        )
        await audit.post(
            self.bot,
            title="Ticket Access Restored",
            accent=config.COLOR_SUCCESS,
            fields=[("Member", audit.describe(member))],
            footer=audit.actor_note(ctx.author),
        )

    @commands.command(name="panel")
    async def panel_command(self, ctx: commands.Context) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_MANAGE_PANEL):
            raise commands.CheckFailure()

        message = await self.repost_panel()
        await ctx.send(
            view=ui.notice(
                title="Panel Reposted" if message else "Panel Failed",
                body=(
                    f"The ticket panel was reposted in <#{config.TICKET_PANEL_CHANNEL_ID}>."
                    if message
                    else "The panel channel could not be reached. Check the channel ID and the bot's permissions."
                ),
                accent=config.COLOR_SUCCESS if message else config.COLOR_DANGER,
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
