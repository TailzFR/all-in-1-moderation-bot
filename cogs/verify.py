from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Any

import discord
from discord import ui as dui
from discord.ext import commands, tasks

import config
from core import audit, captcha, panels, permissions, ui
from core.memory import Memory

log = logging.getLogger("manager.verify")

START_BUTTON = f"{config.COMPONENT_PREFIX}:verify:start"

PANEL_DESCRIPTION = (
    "This server is protected by verification. Press **Verify**, read the characters in the "
    "picture you are shown, and pick the matching line from the dropdown.\n\n"
    "Only you can see the picture, and a new one is generated every time."
)


class VerifyButton(dui.ActionRow):
    @dui.button(label="Verify", style=discord.ButtonStyle.success, custom_id=START_BUTTON)
    async def start(self, interaction: discord.Interaction, button: dui.Button) -> None:
        cog: Verification | None = interaction.client.get_cog("Verification")
        if cog is None:
            return
        await cog.begin(interaction)


class VerifyPanel(ui.Layout):
    def __init__(self, icon_url: str | None) -> None:
        super().__init__(timeout=None)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_PRIMARY))

        header = f"# {config.COMMUNITY_NAME} Verification\n-# {config.COMMUNITY_TAGLINE}"
        if icon_url:
            container.add_item(dui.Section(dui.TextDisplay(header), accessory=dui.Thumbnail(icon_url)))
        else:
            container.add_item(dui.TextDisplay(header))

        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(PANEL_DESCRIPTION))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(VerifyButton())
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                "-# Read the picture carefully. "
                + (
                    "Choosing the wrong line removes you from the server, and you will have to rejoin."
                    if config.VERIFY_KICK_ON_FAIL
                    else "Choosing the wrong line means you have to start again."
                )
            )
        )
        self.add_item(container)


class AnswerSelect(dui.Select):
    def __init__(self, cog: "Verification", options: list[str]) -> None:
        super().__init__(
            placeholder="Select the characters you see in the picture",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=option, value=option) for option in options],
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.submit(interaction, self.values[0])


class ChallengeView(ui.Layout):
    def __init__(self, cog: "Verification", options: list[str], *, retry: bool = False) -> None:
        super().__init__(timeout=config.VERIFY_TIMEOUT_SECONDS)
        container = dui.Container(
            accent_colour=discord.Colour(config.COLOR_WARNING if retry else config.COLOR_PRIMARY)
        )
        container.add_item(
            dui.TextDisplay(
                ("## Wrong Characters\n-# One attempt left" if retry else "## Verification\n-# Match the characters in the picture")
            )
        )
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.MediaGallery(discord.MediaGalleryItem(f"attachment://{captcha.FILENAME}")))
        container.add_item(
            dui.TextDisplay(
                f"The picture contains **{config.VERIFY_CODE_LENGTH} characters**. "
                "Pick the line below that matches it."
            )
        )
        container.add_item(dui.ActionRow(AnswerSelect(cog, options)))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        warning = (
            "A wrong answer removes you from the server."
            if config.VERIFY_KICK_ON_FAIL
            else "A wrong answer ends this attempt."
        )
        container.add_item(
            dui.TextDisplay(f"-# {warning} This expires in {config.VERIFY_TIMEOUT_SECONDS // 60} minutes.")
        )
        self.add_item(container)


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.memory: Memory = bot.memory
        self._panel_lock = asyncio.Lock()
        self._pending: dict[int, dict[str, Any]] = {}

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyPanel(None))
        if config.VERIFY_ENABLED:
            self.panel_refresh.start()

    async def cog_unload(self) -> None:
        self.panel_refresh.cancel()

    @tasks.loop(minutes=config.PANEL_REFRESH_MINUTES)
    async def panel_refresh(self) -> None:
        await self.repost_panel()

    @panel_refresh.before_loop
    async def before_panel_refresh(self) -> None:
        await self.bot.wait_until_ready()

    async def repost_panel(self) -> discord.Message | None:
        if not config.VERIFY_CHANNEL_ID:
            return None
        async with self._panel_lock:
            channel = await panels.resolve_channel(self.bot, config.VERIFY_CHANNEL_ID)
            if channel is None:
                log.warning("Verification channel %s is unreachable.", config.VERIFY_CHANNEL_ID)
                return None
            return await panels.repost(
                self.bot,
                channel_id=config.VERIFY_CHANNEL_ID,
                view=VerifyPanel(self.bot.icon_url(channel.guild)),
                label="verification",
            )

    def verified_role(self, guild: discord.Guild) -> discord.Role | None:
        return guild.get_role(config.VERIFIED_ROLE_ID) if config.VERIFIED_ROLE_ID else None

    def unverified_role(self, guild: discord.Guild) -> discord.Role | None:
        return guild.get_role(config.UNVERIFIED_ROLE_ID) if config.UNVERIFIED_ROLE_ID else None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not config.VERIFY_ENABLED:
            return

        role = self.unverified_role(member.guild)
        if role is not None:
            try:
                await member.add_roles(role, reason="Awaiting verification.")
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not apply the unverified role to %s.", member)

        if not config.VERIFY_DM_ON_JOIN or not config.VERIFY_CHANNEL_ID:
            return

        try:
            await member.send(
                view=ui.notice(
                    title=f"Welcome to {member.guild.name}",
                    body=(
                        f"Before you can talk here, verify yourself in <#{config.VERIFY_CHANNEL_ID}>.\n\n"
                        "Press **Verify**, read the characters in the picture, and pick the matching "
                        "line from the dropdown."
                    ),
                    accent=config.COLOR_PRIMARY,
                    thumbnail=self.bot.icon_url(member.guild),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def begin(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = guild.get_member(interaction.user.id) if guild else None
        if guild is None or member is None:
            await ui.respond(
                interaction,
                ui.notice(
                    title="Verification Unavailable",
                    body="Verification only works inside the server.",
                    accent=config.COLOR_WARNING,
                ),
            )
            return

        if not config.VERIFY_ENABLED:
            await ui.respond(
                interaction,
                ui.notice(
                    title="Verification Disabled",
                    body="Verification is switched off at the moment.",
                    accent=config.COLOR_NEUTRAL,
                ),
            )
            return

        role = self.verified_role(guild)
        if role is None:
            log.error("VERIFIED_ROLE_ID is not configured or the role is missing.")
            await ui.respond(
                interaction,
                ui.notice(
                    title="Verification Unavailable",
                    body="The verified role is not set up. Contact a staff member.",
                    accent=config.COLOR_DANGER,
                ),
            )
            return

        if role in member.roles:
            await ui.respond(
                interaction,
                ui.notice(
                    title="Already Verified",
                    body="You already have access to the server.",
                    accent=config.COLOR_SUCCESS,
                ),
            )
            return

        code, options, image = captcha.challenge()
        if image is None:
            log.error("The captcha image could not be generated. Is Pillow installed?")
            await ui.respond(
                interaction,
                ui.notice(
                    title="Verification Unavailable",
                    body="The picture could not be generated. Contact a staff member.",
                    accent=config.COLOR_DANGER,
                ),
            )
            return

        self._pending[member.id] = {
            "code": code,
            "attempts": 0,
            "expires": time.time() + config.VERIFY_TIMEOUT_SECONDS,
        }

        await interaction.response.send_message(
            view=ChallengeView(self, options),
            file=discord.File(io.BytesIO(image), filename=captcha.FILENAME),
            ephemeral=True,
        )

    async def submit(self, interaction: discord.Interaction, answer: str) -> None:
        guild = interaction.guild
        member = guild.get_member(interaction.user.id) if guild else None
        if member is None:
            return

        pending = self._pending.get(member.id)
        if pending is None or pending["expires"] < time.time():
            self._pending.pop(member.id, None)
            await interaction.response.edit_message(
                view=ui.notice(
                    title="Verification Expired",
                    body="That picture is no longer valid. Press **Verify** again for a new one.",
                    accent=config.COLOR_WARNING,
                ),
                attachments=[],
            )
            return

        if answer == pending["code"]:
            await self._pass(interaction, member)
            return

        pending["attempts"] += 1
        if pending["attempts"] < config.VERIFY_MAX_ATTEMPTS:
            code, options, image = captcha.challenge()
            if image is not None:
                pending["code"] = code
                pending["expires"] = time.time() + config.VERIFY_TIMEOUT_SECONDS
                await interaction.response.edit_message(
                    view=ChallengeView(self, options, retry=True),
                    attachments=[discord.File(io.BytesIO(image), filename=captcha.FILENAME)],
                )
                return

        await self._fail(interaction, member, answer, pending["code"])

    async def _pass(self, interaction: discord.Interaction, member: discord.Member) -> None:
        self._pending.pop(member.id, None)
        guild = member.guild
        role = self.verified_role(guild)

        granted = False
        if role is not None:
            try:
                await member.add_roles(role, reason="Passed verification.")
                granted = True
            except (discord.Forbidden, discord.HTTPException):
                log.error("Could not grant the verified role to %s. Check the bot's role position.", member)

        drop = self.unverified_role(guild)
        if drop is not None and drop in member.roles:
            try:
                await member.remove_roles(drop, reason="Passed verification.")
            except (discord.Forbidden, discord.HTTPException):
                pass

        self.memory.record_verification(member.id, passed=True)

        body = (
            f"You now have access to {guild.name}. Welcome."
            if granted
            else "Your answer was correct, but the role could not be applied. A staff member has been notified."
        )
        await interaction.response.edit_message(
            view=ui.notice(
                title="Verified" if granted else "Almost There",
                body=body,
                accent=config.COLOR_SUCCESS if granted else config.COLOR_WARNING,
            ),
            attachments=[],
        )

        await audit.post(
            self.bot,
            title="Member Verified",
            accent=config.COLOR_SUCCESS,
            fields=[
                ("Member", audit.describe(member)),
                ("Role granted", role.mention if granted and role else "Failed, check the bot's role position."),
                ("Account created", discord.utils.format_dt(member.created_at, "R")),
            ],
            footer="Passed the verification captcha.",
            thumbnail=str(member.display_avatar.url),
        )

    def _protected(self, member: discord.Member) -> bool:
        return (
            member.bot
            or member.id == member.guild.owner_id
            or permissions.is_staff(member)
            or member.guild_permissions.manage_guild
        )

    async def _fail(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        answer: str,
        code: str,
    ) -> None:
        self._pending.pop(member.id, None)
        self.memory.record_verification(member.id, passed=False)

        kick = config.VERIFY_KICK_ON_FAIL and not self._protected(member)

        await interaction.response.edit_message(
            view=ui.notice(
                title="Verification Failed",
                body=(
                    "That was not the right line. You are being removed from the server, and you "
                    "can rejoin and try again."
                    if kick
                    else "That was not the right line. Press **Verify** in the verification channel to try again."
                ),
                accent=config.COLOR_DANGER,
            ),
            attachments=[],
        )

        if not kick:
            await audit.post(
                self.bot,
                title="Verification Failed",
                accent=config.COLOR_WARNING,
                fields=[
                    ("Member", audit.describe(member)),
                    ("Answer given", answer),
                    ("Correct answer", code),
                    ("Outcome", "Not removed, staff are exempt." if self._protected(member) else "Not removed."),
                ],
                footer="Failed the verification captcha.",
            )
            return

        try:
            await member.send(
                view=ui.notice(
                    title="Removed From the Server",
                    body=(
                        f"You picked the wrong characters when verifying in **{member.guild.name}**, "
                        "so you were removed.\n\n"
                        "This is nothing personal. Rejoin with an invite and read the picture carefully "
                        "on your next attempt."
                    ),
                    accent=config.COLOR_DANGER,
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        removed = True
        try:
            await member.kick(reason="Failed verification.")
        except discord.Forbidden:
            removed = False
            log.error("Could not kick %s. The bot needs Kick Members and a higher role.", member)
        except discord.HTTPException:
            removed = False
            log.exception("Kicking %s after a failed verification did not work.", member)

        await audit.post(
            self.bot,
            title="Verification Failed",
            accent=config.COLOR_DANGER,
            fields=[
                ("Member", audit.describe(member)),
                ("Answer given", answer),
                ("Correct answer", code),
                ("Outcome", "Removed from the server." if removed else "Could not be removed, check the bot's permissions."),
            ],
            footer="Failed the verification captcha.",
            thumbnail=str(member.display_avatar.url),
        )

    @commands.command(name="verifypanel")
    async def verify_panel_command(self, ctx: commands.Context) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_MANAGE_VERIFY):
            raise commands.CheckFailure()

        message = await self.repost_panel()
        await ctx.send(
            view=ui.notice(
                title="Verification Panel Reposted" if message else "Verification Panel Failed",
                body=(
                    f"The panel was reposted in <#{config.VERIFY_CHANNEL_ID}>."
                    if message
                    else "The verification channel could not be reached. Check VERIFY_CHANNEL_ID and the bot's permissions."
                ),
                accent=config.COLOR_SUCCESS if message else config.COLOR_DANGER,
            )
        )

    @commands.command(name="verify")
    async def verify_member(self, ctx: commands.Context, member: discord.Member) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_TICKET_STAFF):
            raise commands.CheckFailure()
        if ctx.guild is None:
            return

        role = self.verified_role(ctx.guild)
        if role is None:
            await ctx.send(
                view=ui.notice(
                    title="Not Configured",
                    body="VERIFIED_ROLE_ID is not set, so there is no role to grant.",
                    accent=config.COLOR_DANGER,
                )
            )
            return

        try:
            await member.add_roles(role, reason=f"Verified by {ctx.author}.")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(
                view=ui.notice(
                    title="Role Not Applied",
                    body="The bot could not apply that role. Move its role above the verified role.",
                    accent=config.COLOR_DANGER,
                )
            )
            return

        drop = self.unverified_role(ctx.guild)
        if drop is not None and drop in member.roles:
            try:
                await member.remove_roles(drop, reason=f"Verified by {ctx.author}.")
            except (discord.Forbidden, discord.HTTPException):
                pass

        self.memory.record_verification(member.id, passed=True)
        await ctx.send(
            view=ui.notice(
                title="Member Verified",
                fields=[("Member", audit.describe(member)), ("Role", role.mention)],
                footer=audit.actor_note(ctx.author),
                accent=config.COLOR_SUCCESS,
            )
        )
        await audit.post(
            self.bot,
            title="Member Verified by Staff",
            accent=config.COLOR_SUCCESS,
            fields=[("Member", audit.describe(member)), ("Role", role.mention)],
            footer=audit.actor_note(ctx.author),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Verification(bot))
