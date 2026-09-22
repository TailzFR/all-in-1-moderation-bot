from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord
from discord import ui as dui
from discord.ext import commands, tasks

import config
from core import audit, duration as dur, mod_actions, panels, permissions, ui
from core.memory import Memory
from core.rules import rulebook

log = logging.getLogger("manager.appeals")

DECISION_PREFIX = f"{config.COMPONENT_PREFIX}:ap:"

PANEL_DESCRIPTION = (
    f"If you have been banned from the {config.COMMUNITY_NAME} Discord or the {config.GAME_NAME}, "
    "you can ask the staff team to reconsider the decision here.\n\n"
    "Punishments escalate through the rulebook. A first offence is almost never permanent, "
    "and a permanent ban is the end of a ladder rather than the starting point. An appeal is "
    f"read by a {permissions.rank_or_above(config.LEVEL_UNBAN)}.\n\n"
    "**Before you appeal**\n"
    "Be honest about what happened. An appeal that denies something staff have on video is "
    "denied immediately. Saying what you would do differently carries far more weight than "
    "arguing the punishment was unfair."
)

PANEL_FOOTER = (
    "One appeal at a time. A denied appeal cannot be resubmitted for "
    f"{config.APPEAL_COOLDOWN_DAYS} days."
)


class AppealPanelButton(dui.ActionRow):
    @dui.button(
        label="Appeal a Punishment",
        style=discord.ButtonStyle.primary,
        custom_id=f"{config.COMPONENT_PREFIX}:appeal:start",
    )
    async def start(self, interaction: discord.Interaction, button: dui.Button) -> None:
        cog: Appeals | None = interaction.client.get_cog("Appeals")
        if cog is None:
            return
        await cog.begin(interaction)


class AppealPanel(ui.Layout):
    def __init__(self, icon_url: str | None) -> None:
        super().__init__(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_PRIMARY))

        header = f"# {config.COMMUNITY_NAME} Appeals\n-# {config.COMMUNITY_TAGLINE}"
        if icon_url:
            container.add_item(dui.Section(dui.TextDisplay(header), accessory=dui.Thumbnail(icon_url)))
        else:
            container.add_item(dui.TextDisplay(header))

        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(PANEL_DESCRIPTION))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(AppealPanelButton())
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(f"-# {PANEL_FOOTER}"))

        self.add_item(container)


class AppealForm(dui.Modal, title="Appeal a Punishment"):
    reason = dui.Label(
        text="Reason for appeal",
        description="Why should this punishment be lifted or reduced?",
        component=dui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=800,
            required=True,
        ),
    )
    incident = dui.Label(
        text="Explanation of the incident",
        description="In your own words, what actually happened and what did you do?",
        component=dui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=800,
            required=True,
        ),
    )
    commitments = dui.Label(
        text="Commitments",
        description="What will you do differently if you are let back in?",
        component=dui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=600,
            required=True,
        ),
    )
    additional = dui.Label(
        text="Additional comments",
        description="Anything else staff should know. Type none if there is nothing.",
        component=dui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
        ),
    )

    def __init__(self, case_id: int | None) -> None:
        super().__init__(timeout=900)
        self.case_id = case_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        answers = {
            "reason": self.reason.component.value,
            "incident": self.incident.component.value,
            "commitments": self.commitments.component.value,
            "additional": self.additional.component.value or "None provided.",
        }

        cog: Appeals | None = interaction.client.get_cog("Appeals")
        if cog is None:
            await interaction.followup.send(
                view=ui.notice(
                    title="Appeals Unavailable",
                    body="The appeal system is not loaded. Try again later.",
                    accent=config.COLOR_DANGER,
                ),
                ephemeral=True,
            )
            return

        await cog.submit(interaction, self.case_id, answers)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Appeal form failed.", exc_info=error)
        message = ui.notice(
            title="Something Went Wrong",
            body="Your appeal could not be submitted. Try again in a moment.",
            accent=config.COLOR_DANGER,
        )
        if interaction.response.is_done():
            await interaction.followup.send(view=message, ephemeral=True)
        else:
            await interaction.response.send_message(view=message, ephemeral=True)


class ConfirmRow(dui.ActionRow):
    def __init__(self, case_id: int | None) -> None:
        super().__init__()
        self.case_id = case_id

    @dui.button(label="Continue to the form", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await interaction.response.send_modal(AppealForm(self.case_id))

    @dui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: dui.Button) -> None:
        await interaction.response.edit_message(
            view=ui.notice(
                title="Appeal Cancelled",
                body="Nothing was submitted.",
                accent=config.COLOR_NEUTRAL,
            )
        )


class ConfirmStep(ui.Layout):
    def __init__(self, case: dict[str, Any] | None) -> None:
        super().__init__(timeout=300)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_WARNING))
        container.add_item(dui.TextDisplay("## Before You Appeal"))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        if case is not None:
            container.add_item(
                dui.TextDisplay(
                    ui.field_list(
                        [
                            ("Case", f"#{case['id']}"),
                            ("Rule", f"{case.get('rule')}[{case.get('clause')}] {case.get('rule_title')}"),
                            ("Offence", case.get("clause_title", "Unknown")),
                            ("Punishment", case.get("duration_label", "Unknown")),
                        ]
                    )
                )
            )
            container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        container.add_item(
            dui.TextDisplay(
                "**Submitting a dishonest appeal will get it denied on sight.**\n\n"
                "Staff have the evidence that was filed with your case, including the video or "
                "screenshot the moderator uploaded. Denying something they can see does not help "
                "you. Explaining it does.\n\n"
                "You will be asked four questions. Answer all of them properly."
            )
        )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ConfirmRow(case["id"] if case else None))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay("-# Only you can see this message."))
        self.add_item(container)


class DecisionRow(dui.ActionRow):
    def __init__(self, appeal_id: int, claimed_by: str | None = None) -> None:
        super().__init__()
        self.add_item(
            dui.Button(
                label="Claimed" if claimed_by else "Claim",
                style=discord.ButtonStyle.primary,
                disabled=bool(claimed_by),
                custom_id=f"{DECISION_PREFIX}claim:{appeal_id}",
            )
        )
        self.add_item(
            dui.Button(
                label="Accept Appeal",
                style=discord.ButtonStyle.success,
                custom_id=f"{DECISION_PREFIX}accept:{appeal_id}",
            )
        )
        self.add_item(
            dui.Button(
                label="Deny Appeal",
                style=discord.ButtonStyle.danger,
                custom_id=f"{DECISION_PREFIX}deny:{appeal_id}",
            )
        )


class DecisionReason(dui.Modal):
    def __init__(self, cog: "Appeals", appeal_id: int, accept: bool) -> None:
        super().__init__(title="Accept Appeal" if accept else "Deny Appeal", timeout=600)
        self.cog = cog
        self.appeal_id = appeal_id
        self.accept = accept
        self.note = dui.Label(
            text="Message to the member",
            description="This is sent to them word for word. Keep it professional.",
            component=dui.TextInput(
                style=discord.TextStyle.paragraph,
                max_length=1000,
                required=True,
            ),
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.decide(interaction, self.appeal_id, self.accept, self.note.component.value)


class Appeals(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.memory: Memory = bot.memory
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.bot.add_view(AppealPanel(None))
        self.panel_refresh.start()

    async def cog_unload(self) -> None:
        self.panel_refresh.cancel()

    @property
    def appeal_guild(self) -> discord.Guild | None:
        return self.bot.get_guild(config.APPEAL_GUILD_ID)

    @tasks.loop(minutes=config.PANEL_REFRESH_MINUTES)
    async def panel_refresh(self) -> None:
        await self.repost_panel()

    @panel_refresh.before_loop
    async def before_panel_refresh(self) -> None:
        await self.bot.wait_until_ready()

    async def repost_panel(self) -> discord.Message | None:
        async with self._lock:
            message = await panels.repost(
                self.bot,
                channel_id=config.APPEAL_PANEL_CHANNEL_ID,
                view=AppealPanel(self.bot.icon_url(self.bot.primary_guild)),
                label="appeal",
            )
            state = self.memory.panel_state()
            if message is None:
                state.pop("appeal_message", None)
                self.bot.store.mark_dirty()
                return None

            state["appeal_channel"] = str(config.APPEAL_PANEL_CHANNEL_ID)
            state["appeal_message"] = str(message.id)
            state["appeal_posted_at"] = time.time()
            self.bot.store.mark_dirty()
            return message

    async def begin(self, interaction: discord.Interaction) -> None:
        user = interaction.user

        if not await self.may_appeal(user):
            await ui.respond(
                interaction,
                ui.notice(
                    title="Nothing to Appeal",
                    body=(
                        "There is no punishment on your record to appeal.\n\n"
                        f"This panel is for members who have been punished in {config.COMMUNITY_NAME}. "
                        "If you were punished and still see this, contact a "
                        f"{permissions.rank_name(config.LEVEL_UNBAN)}."
                    ),
                    accent=config.COLOR_NEUTRAL,
                ),
            )
            return

        pending = self.memory.pending_appeal_for(user.id)
        if pending is not None:
            await ui.respond(
                interaction,
                ui.notice(
                    title="Appeal Already Open",
                    body=(
                        f"Appeal **#{pending['id']}** is still waiting on a decision. "
                        "You will be sent a direct message when staff have reviewed it."
                    ),
                    fields=[("Submitted", dur.timestamp(pending.get("created_at"), "R"))],
                    accent=config.COLOR_WARNING,
                ),
            )
            return

        last = self.memory.last_appeal_decision(user.id)
        if last is not None and last.get("status") == "denied":
            elapsed = time.time() - float(last.get("decided_at") or 0)
            cooldown = config.APPEAL_COOLDOWN_DAYS * 86400
            if elapsed < cooldown:
                await ui.respond(
                    interaction,
                    ui.notice(
                        title="Appeal on Cooldown",
                        body=(
                            f"Your last appeal was denied. You can submit another one "
                            f"{dur.timestamp(float(last['decided_at']) + cooldown, 'R')}."
                        ),
                        fields=[("Previous decision", last.get("decision_reason") or "No reason recorded.")],
                        accent=config.COLOR_WARNING,
                    ),
                )
                return

        case = self.active_ban_case(user.id)
        if case is None and not await self.is_banned(user.id):
            await ui.respond(
                interaction,
                ui.notice(
                    title="Nothing to Appeal",
                    body=(
                        "There is no active ban on your account, so there is nothing to appeal.\n\n"
                        "If you are muted rather than banned, open a ticket in the main server "
                        "instead. If you believe this is wrong, contact a "
                        f"{permissions.rank_name(config.LEVEL_UNBAN)}."
                    ),
                    accent=config.COLOR_NEUTRAL,
                ),
            )
            return

        if case is not None and str(case.get("rule")) in config.APPEAL_EXCLUDED_RULES:
            await ui.respond(
                interaction,
                ui.notice(
                    title="Not Appealable",
                    body=(
                        f"Punishments under rule {case.get('rule')} are never lifted. "
                        "This decision is final."
                    ),
                    accent=config.COLOR_DANGER,
                ),
            )
            return

        await ui.respond(interaction, ConfirmStep(case))

    async def may_appeal(self, user: discord.abc.User) -> bool:
        if self.memory.cases_for(user.id):
            return True

        if await self.is_banned(user.id):
            return True

        role_id = config.APPEAL_ALLOWED_ROLE_ID
        if role_id is None:
            return True
        if not isinstance(user, discord.Member):
            return False
        return any(role.id == role_id for role in getattr(user, "roles", ()))

    def active_ban_case(self, user_id: int) -> dict[str, Any] | None:
        live = self.memory.active_cases_of_action(user_id, "ban")
        return live[-1] if live else None

    async def is_banned(self, user_id: int) -> bool:
        guild = self.bot.primary_guild
        if guild is None:
            return False
        try:
            await guild.fetch_ban(discord.Object(id=user_id))
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    async def submit(
        self,
        interaction: discord.Interaction,
        case_id: int | None,
        answers: dict[str, Any],
    ) -> None:
        user = interaction.user
        appeal = self.memory.create_appeal(user_id=user.id, case_id=case_id, answers=answers)
        self.memory.remember_name(user.id, str(user))

        posted = await self.post_appeal(appeal, user)

        await interaction.followup.send(
            view=ui.notice(
                title="Appeal Submitted",
                subtitle=f"Appeal #{appeal['id']}",
                body=(
                    "Your appeal has been sent to the staff team.\n\n"
                    "You will receive a direct message with the decision. Do not open another "
                    "appeal while this one is pending, and do not message staff privately about it."
                    if posted
                    else "Your appeal was recorded, but the staff channel could not be reached. "
                         f"Contact a {permissions.rank_name(config.LEVEL_UNBAN)} directly."
                ),
                footer="Keep your direct messages open so the decision can reach you.",
                accent=config.COLOR_SUCCESS if posted else config.COLOR_WARNING,
                thumbnail=self.bot.icon_url(self.bot.primary_guild),
            ),
            ephemeral=True,
        )

    async def resolve_target(
        self,
        appeal: dict[str, Any],
        user: discord.abc.User,
    ) -> tuple[discord.abc.Messageable | None, bool]:
        target = self.bot.get_channel(config.APPEAL_CHANNEL_ID)
        if target is None:
            try:
                target = await self.bot.fetch_channel(config.APPEAL_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Appeal destination %s is unreachable.", config.APPEAL_CHANNEL_ID)
                return None, False

        if not isinstance(target, discord.CategoryChannel):
            return target, False

        try:
            created = await target.guild.create_text_channel(
                name=f"appeal-{appeal['id']:04d}",
                category=target,
                topic=f"Appeal #{appeal['id']} | {user} ({user.id})",
                reason=f"Appeal opened by {user} ({user.id}).",
            )
        except discord.Forbidden:
            log.error("The bot cannot create channels in the appeal category %s.", target.id)
            return None, False
        except discord.HTTPException:
            log.exception("Could not create an appeal channel.")
            return None, False

        return created, True

    async def post_appeal(self, appeal: dict[str, Any], user: discord.abc.User) -> bool:
        channel, dedicated = await self.resolve_target(appeal, user)
        if channel is None:
            return False

        case = self.memory.case(appeal["case"]) if appeal.get("case") else None
        history = self.memory.cases_for(user.id, include_revoked=False)
        answers = appeal["answers"]

        view = ui.Layout(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_WARNING))
        container.add_item(
            dui.Section(
                dui.TextDisplay(f"## Appeal #{appeal['id']}\n-# {user} ({user.id})"),
                accessory=dui.Thumbnail(str(user.display_avatar.url)),
            )
        )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        rows: list[tuple[str, str]] = [("Member", f"<@{user.id}>\n{user} ({user.id})")]
        if case is not None:
            rows.append(("Case", f"#{case['id']} - {case.get('duration_label', 'Unknown')}"))
            rows.append(("Rule", f"{case.get('rule')}[{case.get('clause')}] {case.get('rule_title')}"))
            rows.append(("Offence", case.get("clause_title", "Unknown")))
            rows.append(("Filed", dur.timestamp(case.get("created_at"))))
            rows.append(("Moderator", f"<@{case.get('moderator')}>"))
            if case.get("proof"):
                rows.append(("Proof on file", case["proof"]))
        else:
            rows.append(("Case", "No case on record. The ban was placed outside the bot."))
        rows.append(("Live cases", f"{len(history)} on record"))
        rows.append(("Previous appeals", str(len(self.memory.appeals_for(user.id)) - 1)))

        container.add_item(dui.TextDisplay(ui.clip(ui.field_list(rows), 700)))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        container.add_item(
            dui.TextDisplay(ui.clip(f"**Reason for appeal**\n{ui.sanitise(answers['reason'])}", 850))
        )
        container.add_item(
            dui.TextDisplay(ui.clip(f"**Explanation of the incident**\n{ui.sanitise(answers['incident'])}", 850))
        )
        container.add_item(
            dui.TextDisplay(ui.clip(f"**Commitments**\n{ui.sanitise(answers['commitments'])}", 650))
        )
        container.add_item(
            dui.TextDisplay(ui.clip(f"**Additional comments**\n{ui.sanitise(answers['additional'])}", 550))
        )

        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(DecisionRow(appeal["id"]))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                f"-# Decided by a {permissions.rank_or_above(config.LEVEL_UNBAN)}. "
                f"Accepting lifts the ban in {config.COMMUNITY_NAME} immediately."
            )
        )
        view.add_item(container)

        try:
            message = await channel.send(view=view)
        except discord.HTTPException:
            log.exception("Could not post appeal #%s.", appeal["id"])
            if dedicated:
                try:
                    await channel.delete(reason="The appeal post failed.")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            return False

        self.memory.set_appeal_location(appeal["id"], channel.id, message.id, dedicated=dedicated)

        await audit.post(
            self.bot,
            title=f"Appeal Submitted - #{appeal['id']}",
            accent=config.COLOR_WARNING,
            fields=[
                ("Member", audit.describe(user)),
                ("Case", f"#{case['id']}" if case else "None on record"),
                ("Reason", ui.clip(answers["reason"], 500)),
            ],
            footer="Submitted from the appeal server.",
            thumbnail=str(user.display_avatar.url),
        )
        return True

    @commands.Cog.listener("on_interaction")
    async def decision_buttons(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(DECISION_PREFIX):
            return

        try:
            action, raw = custom_id[len(DECISION_PREFIX):].split(":", 1)
            appeal_id = int(raw)
        except (ValueError, TypeError):
            return

        needed = config.LEVEL_TICKET_STAFF if action == "claim" else config.LEVEL_UNBAN
        if permissions.effective_level(self.bot, interaction.user) < needed:
            await interaction.response.send_message(
                view=ui.notice(
                    title="Permission Denied",
                    body=(
                        "Only staff can claim an appeal."
                        if action == "claim"
                        else f"Appeals are decided by a {permissions.rank_or_above(config.LEVEL_UNBAN)}."
                    ),
                    accent=config.COLOR_DANGER,
                ),
                ephemeral=True,
            )
            return

        appeal = self.memory.appeal(appeal_id)
        if appeal is None:
            await interaction.response.send_message(
                view=ui.notice(title="Appeal Missing", body="That appeal is no longer on record.", accent=config.COLOR_WARNING),
                ephemeral=True,
            )
            return

        if appeal.get("status") != "pending":
            await interaction.response.send_message(
                view=ui.notice(
                    title="Already Decided",
                    body=(
                        f"Appeal **#{appeal_id}** was already {appeal['status']} by "
                        f"<@{appeal.get('decided_by')}>."
                    ),
                    accent=config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        if action == "claim":
            await self.handle_claim(interaction, appeal)
            return

        await interaction.response.send_modal(
            DecisionReason(self, appeal_id, accept=action == "accept")
        )

    async def handle_claim(self, interaction: discord.Interaction, appeal: dict[str, Any]) -> None:
        existing = appeal.get("claimed_by")
        if existing and existing != str(interaction.user.id):
            await interaction.response.send_message(
                view=ui.notice(
                    title="Already Claimed",
                    body=f"<@{existing}> is already handling this appeal.",
                    accent=config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        self.memory.claim_appeal(appeal["id"], interaction.user.id)
        await interaction.response.send_message(
            view=ui.notice(
                title="Appeal Claimed",
                body=(
                    f"{interaction.user.mention} is handling appeal **#{appeal['id']}**.\n\n"
                    f"Use `{config.PREFIX}ask <question>` in this channel to put a question to the "
                    "member. Their reply is delivered here. Anything else typed here stays internal."
                ),
                footer=f"{interaction.user} - {permissions.effective_title(self.bot, interaction.user)}",
                accent=config.COLOR_SUCCESS,
            )
        )

        try:
            message = interaction.message
            if message is not None and message.components:
                await self.refresh_buttons(appeal, message)
        except discord.HTTPException:
            pass

    async def refresh_buttons(self, appeal: dict[str, Any], message: discord.Message) -> None:
        try:
            await message.edit(view=self.rebuild_post(appeal))
        except discord.HTTPException:
            pass

    def rebuild_post(self, appeal: dict[str, Any]) -> ui.Layout:
        view = ui.Layout(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_WARNING))
        answers = appeal["answers"]
        claimed = appeal.get("claimed_by")

        container.add_item(dui.TextDisplay(f"## Appeal #{appeal['id']}\n-# <@{appeal['user']}> ({appeal['user']})"))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                ui.field_list(
                    [
                        ("Member", f"<@{appeal['user']}> ({appeal['user']})"),
                        ("Case", f"#{appeal['case']}" if appeal.get("case") else "None on record"),
                        ("Claimed by", f"<@{claimed}>" if claimed else "Unclaimed"),
                    ]
                )
            )
        )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(ui.clip(f"**Reason for appeal**\n{ui.sanitise(answers['reason'])}", 850)))
        container.add_item(dui.TextDisplay(ui.clip(f"**Explanation of the incident**\n{ui.sanitise(answers['incident'])}", 850)))
        container.add_item(dui.TextDisplay(ui.clip(f"**Commitments**\n{ui.sanitise(answers['commitments'])}", 650)))
        container.add_item(dui.TextDisplay(ui.clip(f"**Additional comments**\n{ui.sanitise(answers['additional'])}", 550)))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(DecisionRow(appeal["id"], claimed_by=claimed))
        view.add_item(container)
        return view

    async def decide(
        self,
        interaction: discord.Interaction,
        appeal_id: int,
        accept: bool,
        note: str,
    ) -> None:
        appeal = self.memory.appeal(appeal_id)
        if appeal is None or appeal.get("status") != "pending":
            await interaction.followup.send(
                view=ui.notice(title="Already Decided", body="Another staff member got there first.", accent=config.COLOR_WARNING),
                ephemeral=True,
            )
            return

        status = "accepted" if accept else "denied"
        self.memory.decide_appeal(appeal_id, status=status, staff_id=interaction.user.id, reason=note)

        user_id = int(appeal["user"])
        main = self.bot.primary_guild
        outcome = ""

        if accept and main is not None:
            ok, error = await mod_actions.lift_ban(
                self.bot,
                main,
                user_id,
                self.memory,
                reason=f"Appeal #{appeal_id} accepted by {interaction.user}.",
                moderator=interaction.user,
                case_id=appeal.get("case"),
                notes=note,
            )
            outcome = "The ban was lifted." if ok else f"The ban was not lifted: {error}"

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                user = None

        delivered = False
        if user is not None:
            if accept:
                body = (
                    f"Your appeal has been **accepted**. Your ban from **{main.name if main else config.COMMUNITY_NAME}** "
                    "has been lifted and you may rejoin.\n\n"
                    "Read the rulebook before you return. A further breach is judged on top of your "
                    "existing record, not from a clean slate."
                )
            else:
                body = (
                    "Your appeal has been **denied**. The punishment stands.\n\n"
                    f"You may submit another appeal in {config.APPEAL_COOLDOWN_DAYS} days. "
                    "Do not message staff privately about this decision."
                )
            delivered = await mod_actions.send_dm(
                user,
                ui.notice(
                    title="Appeal Accepted" if accept else "Appeal Denied",
                    subtitle=f"Appeal #{appeal_id}",
                    body=body,
                    fields=[("Message from staff", note)],
                    footer=f"{config.BOT_NAME} - {config.COMMUNITY_NAME}",
                    accent=config.COLOR_SUCCESS if accept else config.COLOR_DANGER,
                    thumbnail=self.bot.icon_url(main),
                ),
            )

        await self.mark_decided(appeal, interaction.user, accept, note, outcome, delivered)

        await audit.post(
            self.bot,
            title=f"Appeal {status.title()} - #{appeal_id}",
            accent=config.COLOR_SUCCESS if accept else config.COLOR_DANGER,
            fields=[
                ("Member", audit.describe(user) if user else appeal["user"]),
                ("Case", f"#{appeal['case']}" if appeal.get("case") else "None on record"),
                ("Message to member", ui.clip(note, 600)),
                ("Outcome", outcome or "No ban to lift."),
                ("Notice delivered", "Yes" if delivered else "No, direct messages are closed."),
            ],
            footer=f"Decided by {interaction.user} ({interaction.user.id}) - "
                   f"{permissions.effective_title(self.bot, interaction.user)}.",
        )

        await interaction.followup.send(
            view=ui.notice(
                title=f"Appeal {status.title()}",
                subtitle=f"Appeal #{appeal_id}",
                fields=[
                    ("Member", audit.describe(user) if user else appeal["user"]),
                    ("Outcome", outcome or "No ban to lift."),
                    ("Notice delivered", "Yes" if delivered else "No, direct messages are closed."),
                ],
                accent=config.COLOR_SUCCESS if accept else config.COLOR_DANGER,
            ),
            ephemeral=True,
        )

    async def mark_decided(
        self,
        appeal: dict[str, Any],
        staff: discord.abc.User,
        accept: bool,
        note: str,
        outcome: str,
        delivered: bool,
    ) -> None:
        message_id = appeal.get("message")
        channel_id = appeal.get("channel") or config.APPEAL_CHANNEL_ID
        if not message_id:
            return

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        status = "Accepted" if accept else "Denied"
        answers = appeal["answers"]

        view = ui.Layout(timeout=None)
        container = dui.Container(
            accent_colour=discord.Colour(config.COLOR_SUCCESS if accept else config.COLOR_DANGER)
        )
        container.add_item(dui.TextDisplay(f"## Appeal #{appeal['id']} - {status}"))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                ui.field_list(
                    [
                        ("Member", f"<@{appeal['user']}> ({appeal['user']})"),
                        ("Case", f"#{appeal['case']}" if appeal.get("case") else "None on record"),
                        ("Decision", status),
                        ("Decided by", f"{staff} ({staff.id})"),
                        ("Outcome", outcome or "No ban to lift."),
                        ("Notice delivered", "Yes" if delivered else "No, direct messages are closed."),
                    ]
                )
            )
        )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(ui.clip(f"**Message sent to the member**\n{ui.sanitise(note)}", 1000)))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(ui.clip(f"**Reason for appeal**\n{ui.sanitise(answers['reason'])}", 800))
        )
        container.add_item(
            dui.TextDisplay(ui.clip(f"**Commitments**\n{ui.sanitise(answers['commitments'])}", 700))
        )
        view.add_item(container)

        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass

        if appeal.get("dedicated_channel") and config.APPEAL_DELETE_ON_DECISION:
            try:
                await channel.send(
                    view=ui.notice(
                        title=f"Appeal {status}",
                        body="This channel will be deleted in fifteen seconds. "
                             "The decision is recorded in the server log.",
                        footer=f"Decided by {staff} - {permissions.effective_title(self.bot, staff)}",
                        accent=config.COLOR_NEUTRAL,
                    )
                )
            except discord.HTTPException:
                pass

            await asyncio.sleep(15)
            try:
                await channel.delete(reason=f"Appeal #{appeal['id']} {status.lower()} by {staff}.")
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def ask_member(
        self,
        ctx: commands.Context,
        appeal: dict[str, Any],
        question: str,
    ) -> bool:
        user_id = int(appeal["user"])
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                user = None

        if user is None:
            await ctx.send(
                view=ui.notice(
                    title="Member Unavailable",
                    body="That member could not be resolved.",
                    accent=config.COLOR_DANGER,
                )
            )
            return False

        delivered = await mod_actions.send_dm(
            user,
            ui.relay(
                heading="Staff Member",
                body=question,
                accent=config.COLOR_PRIMARY,
                footer=(
                    f"Appeal #{appeal['id']} - reply to this bot in direct messages "
                    "and your answer reaches the staff team."
                ),
            ),
        )

        if not delivered:
            await ctx.send(
                view=ui.notice(
                    title="Question Not Delivered",
                    body="The member has direct messages closed, so nothing was sent.",
                    accent=config.COLOR_DANGER,
                )
            )
            return False

        self.memory.log_appeal_message(
            appeal["id"],
            author_id=ctx.author.id,
            author_name=str(ctx.author),
            role="staff",
            content=question,
        )
        if not appeal.get("claimed_by"):
            self.memory.claim_appeal(appeal["id"], ctx.author.id)

        await ctx.send(
            view=ui.relay(
                heading="Staff Member",
                body=question,
                accent=config.COLOR_PRIMARY,
                footer=f"Sent by {ctx.author} - {permissions.effective_title(self.bot, ctx.author)}",
            )
        )
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        return True

    @commands.command(name="ask")
    async def ask_command(self, ctx: commands.Context, *, question: str) -> None:
        if permissions.effective_level(self.bot, ctx.author) < config.LEVEL_TICKET_STAFF:
            raise commands.CheckFailure()

        appeal = self.memory.appeal_by_channel(ctx.channel.id)
        if appeal is None:
            await ctx.send(
                view=ui.notice(
                    title="Not an Appeal Channel",
                    body="This command only works inside a pending appeal channel.",
                    accent=config.COLOR_WARNING,
                )
            )
            return

        await self.ask_member(ctx, appeal, question)

    @commands.command(name="claim")
    async def claim_command(self, ctx: commands.Context) -> None:
        if permissions.effective_level(self.bot, ctx.author) < config.LEVEL_TICKET_STAFF:
            raise commands.CheckFailure()

        appeal = self.memory.appeal_by_channel(ctx.channel.id)
        if appeal is None:
            ticket = self.memory.ticket_by_channel(ctx.channel.id)
            if ticket is not None and ticket.get("status") == "open":
                held = ticket.get("claimed_by")
                if held and held != str(ctx.author.id):
                    await ctx.send(
                        view=ui.notice(
                            title="Already Claimed",
                            body=f"<@{held}> is already handling this ticket.",
                            accent=config.COLOR_WARNING,
                        )
                    )
                    return
                self.memory.claim_ticket(ticket["id"], ctx.author.id)
                await ctx.send(
                    view=ui.notice(
                        title="Ticket Claimed",
                        body=f"{ctx.author.mention} is handling ticket **#{ticket['id']}**.",
                        footer=f"{ctx.author} - {permissions.effective_title(self.bot, ctx.author)}",
                        accent=config.COLOR_SUCCESS,
                    )
                )
                return

            await ctx.send(
                view=ui.notice(
                    title="Nothing to Claim",
                    body="This command only works inside a pending appeal or an open ticket.",
                    accent=config.COLOR_WARNING,
                )
            )
            return

        existing = appeal.get("claimed_by")
        if existing and existing != str(ctx.author.id):
            await ctx.send(
                view=ui.notice(
                    title="Already Claimed",
                    body=f"<@{existing}> is already handling this appeal.",
                    accent=config.COLOR_WARNING,
                )
            )
            return

        self.memory.claim_appeal(appeal["id"], ctx.author.id)
        await ctx.send(
            view=ui.notice(
                title="Appeal Claimed",
                body=(
                    f"{ctx.author.mention} is handling appeal **#{appeal['id']}**.\n\n"
                    f"Use `{config.PREFIX}ask <question>` to put a question to the member."
                ),
                footer=f"{ctx.author} - {permissions.effective_title(self.bot, ctx.author)}",
                accent=config.COLOR_SUCCESS,
            )
        )

    @commands.Cog.listener("on_message")
    async def relay_appeal_reply(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        if message.content.startswith(config.PREFIX):
            return

        if self.memory.open_ticket_for(message.author.id) is not None:
            return

        appeal = self.memory.pending_appeal_for(message.author.id)
        if appeal is None or not appeal.get("channel"):
            return

        channel = self.bot.get_channel(int(appeal["channel"]))
        if channel is None:
            return

        attachments = [
            {"filename": a.filename, "url": a.url, "size": a.size, "content_type": a.content_type}
            for a in message.attachments
        ]

        try:
            await channel.send(
                view=ui.relay(
                    heading="Member",
                    body=message.content,
                    accent=config.COLOR_INFO,
                    footer=f"{message.author} ({message.author.id}) - Appeal #{appeal['id']}",
                    attachments=[(a["filename"], a["url"]) for a in attachments],
                    thumbnail=str(message.author.display_avatar.url),
                )
            )
        except discord.HTTPException:
            return

        self.memory.log_appeal_message(
            appeal["id"],
            author_id=message.author.id,
            author_name=str(message.author),
            role="member",
            content=message.content,
            attachments=attachments,
        )

        try:
            await message.add_reaction("\N{WHITE HEAVY CHECK MARK}")
        except (discord.HTTPException, discord.Forbidden):
            pass

    @commands.command(name="appealpanel")
    async def appeal_panel_command(self, ctx: commands.Context) -> None:
        if permissions.effective_level(self.bot, ctx.author) < config.LEVEL_MANAGE_PANEL:
            raise commands.CheckFailure()

        message = await self.repost_panel()
        await ctx.send(
            view=ui.notice(
                title="Appeal Panel Reposted" if message else "Appeal Panel Failed",
                body=(
                    f"Reposted in <#{config.APPEAL_PANEL_CHANNEL_ID}>."
                    if message
                    else "The appeal panel channel could not be reached. Check that the bot is in "
                         "the appeal server and can post there."
                ),
                accent=config.COLOR_SUCCESS if message else config.COLOR_DANGER,
            )
        )

    @commands.command(name="appeals")
    async def list_appeals(self, ctx: commands.Context, member: discord.User | None = None) -> None:
        if permissions.effective_level(self.bot, ctx.author) < config.LEVEL_TICKET_STAFF:
            raise commands.CheckFailure()

        if member is not None:
            records = self.memory.appeals_for(member.id)
            listed = "\n\n".join(
                f"**Appeal #{a['id']}** - {a['status'].title()}\n"
                f"Submitted {dur.timestamp(a.get('created_at'), 'f')}\n"
                f"{ui.clip(a['answers'].get('reason', ''), 200)}"
                for a in records[-5:]
            ) or "No appeals on record."
            await ctx.send(
                view=ui.notice(
                    title="Appeal History",
                    subtitle=str(member),
                    fields=[("Member", audit.describe(member)), ("Appeals", listed)],
                    accent=config.COLOR_INFO,
                    thumbnail=str(member.display_avatar.url),
                )
            )
            return

        pending = [
            a for a in self.memory.store.section("appeals").values() if a.get("status") == "pending"
        ]
        listed = "\n".join(
            f"**Appeal #{a['id']}** - <@{a['user']}> - submitted {dur.timestamp(a.get('created_at'), 'R')}"
            for a in sorted(pending, key=lambda a: a.get("created_at", 0))[:15]
        ) or "No appeals are waiting on a decision."

        await ctx.send(
            view=ui.notice(
                title="Pending Appeals",
                fields=[("Waiting", listed), ("Total", str(len(pending)))],
                footer=f"Decided from the buttons in <#{config.APPEAL_CHANNEL_ID}>.",
                accent=config.COLOR_INFO,
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Appeals(bot))
