from __future__ import annotations

import io
import logging
from typing import Any

import discord
from discord import app_commands
from discord import ui as dui
from discord.ext import commands

import config
from core import audit, duration as dur, mod_actions, permissions, ui
from core.memory import Memory
from core.rules import Clause, rulebook

log = logging.getLogger("manager.moderation")

PAGE_SIZE = 5
CASE_BLOCK_BUDGET = 3200


def staff_check(level: int):
    async def predicate(interaction: discord.Interaction) -> bool:
        if permissions.has_level(interaction.user, level):
            return True
        raise app_commands.CheckFailure("insufficient_level")

    return app_commands.check(predicate)


class ConfirmPunishment(dui.ActionRow):
    def __init__(self, cog: "Moderation", payload: dict[str, Any]) -> None:
        super().__init__()
        self.cog = cog
        self.payload = payload

    @dui.button(label="Apply Punishment", style=discord.ButtonStyle.danger)
    async def apply(self, interaction: discord.Interaction, button: dui.Button) -> None:
        if interaction.user.id != self.payload["moderator"].id:
            await interaction.response.send_message(
                view=ui.notice(
                    title="Not Your Confirmation",
                    body="Only the moderator who ran the command can confirm it.",
                    accent=config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.cog.execute(interaction, self.payload)

    @dui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: dui.Button) -> None:
        if interaction.user.id != self.payload["moderator"].id:
            await interaction.response.send_message(
                view=ui.notice(
                    title="Not Your Confirmation",
                    body="Only the moderator who ran the command can cancel it.",
                    accent=config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            view=ui.notice(
                title="Punishment Cancelled",
                body="No case was filed and nothing was applied.",
                accent=config.COLOR_NEUTRAL,
            )
        )


class CasePages(dui.ActionRow):
    def __init__(self, cog: "Moderation", user_id: int, page: int, pages: int, viewer_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.user_id = user_id
        self.page = page
        self.pages = pages
        self.viewer_id = viewer_id
        self.previous.disabled = page <= 0
        self.next.disabled = page >= pages - 1

    @dui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: dui.Button) -> None:
        if interaction.user.id != self.viewer_id:
            await interaction.response.defer()
            return
        await interaction.response.edit_message(
            view=await self.cog.build_case_view(interaction, self.user_id, self.page - 1)
        )

    @dui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: dui.Button) -> None:
        if interaction.user.id != self.viewer_id:
            await interaction.response.defer()
            return
        await interaction.response.edit_message(
            view=await self.cog.build_case_view(interaction, self.user_id, self.page + 1)
        )


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.memory: Memory = bot.memory

    async def rule_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        matches = rulebook.search(current, limit=25)
        return [
            app_commands.Choice(name=ui.clip(clause.display, 100), value=clause.key)
            for clause in matches
        ]

    @app_commands.command(name="punish", description="Punish a member under a rule from the rulebook.")
    @app_commands.describe(
        user="The member being punished.",
        rule="Pick the rule and clause that was broken. Only rulebook entries are accepted.",
        proof="Required. Upload the video or screenshot that proves the offence.",
        reason="Optional notes recorded on the case and shown to the member.",
        evidence="Optional extra link, such as a longer recording.",
        offense="Override the offence number. Leave empty to count previous cases automatically.",
    )
    @app_commands.autocomplete(rule=rule_autocomplete)
    @staff_check(config.LEVEL_PUNISH)
    async def punish(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        rule: str,
        proof: discord.Attachment,
        reason: str | None = None,
        evidence: str | None = None,
        offense: app_commands.Range[int, 1, 10] | None = None,
    ) -> None:
        content_type = (proof.content_type or "").lower()
        if config.PROOF_REQUIRED and not content_type.startswith(config.PROOF_ACCEPTED_PREFIXES):
            await ui.respond(
                interaction,
                ui.notice(
                    title="Proof Rejected",
                    body=(
                        "A punishment cannot be filed without proof of the offence.\n\n"
                        "Upload a **video or a screenshot**. Text files, documents and archives "
                        "are not accepted, because an appeal is judged on what staff can actually show."
                    ),
                    fields=[
                        ("You uploaded", f"{proof.filename} ({content_type or 'unknown type'})"),
                        ("Accepted", "Any image or video file"),
                    ],
                    accent=config.COLOR_DANGER,
                ),
            )
            return

        clause = rulebook.get(rule)
        if clause is None:
            await ui.respond(
                interaction,
                ui.notice(
                    title="Unknown Rule",
                    body=(
                        "That rule is not in the rulebook. Choose an entry from the list rather than "
                        "typing your own, so the punishment matches what the rulebook authorises."
                    ),
                    accent=config.COLOR_DANGER,
                ),
            )
            return

        guild = interaction.guild
        if guild is None:
            await ui.respond(
                interaction,
                ui.notice(title="Server Only", body="This command must be used inside the server.", accent=config.COLOR_WARNING),
            )
            return

        if user.id == interaction.user.id:
            await ui.respond(
                interaction,
                ui.notice(title="Invalid Target", body="You cannot punish yourself.", accent=config.COLOR_WARNING),
            )
            return

        if user.bot:
            await ui.respond(
                interaction,
                ui.notice(title="Invalid Target", body="Bots cannot be punished.", accent=config.COLOR_WARNING),
            )
            return

        member = guild.get_member(user.id)
        if member is not None and not permissions.outranks(interaction.user, member):
            await ui.respond(
                interaction,
                ui.notice(
                    title="Permission Denied",
                    body=(
                        f"{member.mention} holds a staff rank equal to or above yours "
                        f"({permissions.title_of(member)}). Escalate to a "
                        f"{permissions.rank_name(config.LEVEL_REVOKE_CASE)} instead."
                    ),
                    accent=config.COLOR_DANGER,
                ),
            )
            return

        prior = self.memory.offense_count(user.id, clause.section_id, clause.clause_id)
        number = offense if offense is not None else prior + 1
        punishment = clause.punishment(number)

        payload = {
            "moderator": interaction.user,
            "target": user,
            "clause": clause,
            "offense": number,
            "reason": reason or "",
            "evidence": evidence,
            "guild": guild,
            "proof": {
                "url": proof.url,
                "filename": proof.filename,
                "content_type": content_type,
                "size": proof.size,
            },
            "proof_attachment": proof,
        }

        view = ui.Layout(timeout=180)
        container = dui.Container(accent_colour=discord.Colour(mod_actions.ACTION_ACCENT.get(punishment.action, config.COLOR_WARNING)))
        container.add_item(dui.TextDisplay(f"## Confirm Punishment\n-# {clause.reference} {clause.section_title}"))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        rows = [
            ("Member", f"{user.mention}\n{user} ({user.id})"),
            ("Offence", clause.title),
            ("Offence number", f"{_ordinal(number)}" + ("" if offense is None else " (set manually)")),
            ("Prior live cases", f"{prior} for this clause"),
            ("Punishment", punishment.label),
            ("Rulebook tiers", "\n".join(clause.tier_labels)),
            ("Proof", f"[{proof.filename}]({proof.url})\n{content_type}, {proof.size / 1024:.0f} KB"),
        ]
        if clause.note:
            rows.append(("Staff guidance", clause.note))
        if reason:
            rows.append(("Notes", reason))
        if evidence:
            rows.append(("Evidence", evidence))

        container.add_item(dui.TextDisplay(ui.field_list(rows)))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ConfirmPunishment(self, payload))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            dui.TextDisplay(
                "-# The member is notified by direct message before a ban is placed. "
                "There is no appeal process."
            )
        )
        view.add_item(container)

        await ui.respond(interaction, view, ephemeral=True)

    async def execute(self, interaction: discord.Interaction, payload: dict[str, Any]) -> None:
        proof_file = await self._archive_proof(payload.get("proof_attachment"))

        result = await mod_actions.apply(
            self.bot,
            guild=payload["guild"],
            target=payload["target"],
            moderator=payload["moderator"],
            clause=payload["clause"],
            offense=payload["offense"],
            memory=self.memory,
            reason=payload["reason"],
            evidence=payload["evidence"],
            source="punish",
            proof=payload.get("proof"),
            proof_file=proof_file,
        )

        clause: Clause = payload["clause"]
        expires = result.case.get("expires_at")

        fields = [
            ("Member", audit.describe(payload["target"])),
            ("Rule", f"{clause.reference} {clause.section_title}"),
            ("Offence", f"{clause.title} ({_ordinal(payload['offense'])} offence)"),
            ("Punishment", result.punishment.label),
            ("Expires", dur.timestamp(expires) if expires else "Never"),
            ("Case", f"#{result.case['id']}"),
            ("Proof archived", "Yes, attached to the case in the server log." if proof_file else "Linked only, the file was too large to copy."),
            ("Notice delivered", "Yes" if result.dm_delivered else "No, direct messages are closed."),
        ]
        if not result.applied:
            fields.append(("Warning", result.error or "The punishment could not be applied."))

        await interaction.edit_original_response(
            view=ui.notice(
                title="Punishment Applied" if result.applied else "Case Filed, Action Failed",
                subtitle=f"Case #{result.case['id']}",
                fields=fields,
                footer=audit.actor_note(payload["moderator"]),
                accent=config.COLOR_SUCCESS if result.applied else config.COLOR_DANGER,
                thumbnail=str(payload["target"].display_avatar.url),
            )
        )

    async def _archive_proof(self, attachment: discord.Attachment | None) -> discord.File | None:
        if attachment is None or attachment.size > config.PROOF_MAX_REUPLOAD_BYTES:
            return None
        try:
            data = await attachment.read()
        except (discord.HTTPException, discord.NotFound):
            log.warning("Could not download the proof attachment %s.", attachment.filename)
            return None
        return discord.File(io.BytesIO(data), filename=attachment.filename)

    @app_commands.command(name="unban", description="Lift a ban and record the reason.")
    @app_commands.describe(
        user="The user ID of the member to unban. Banned members cannot be picked from the list.",
        notes="Additional notes recorded on the case and sent to the member.",
    )
    @staff_check(config.LEVEL_UNBAN)
    async def unban(
        self,
        interaction: discord.Interaction,
        user: str,
        notes: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await ui.respond(
                interaction,
                ui.notice(title="Server Only", body="This command must be used inside the server.", accent=config.COLOR_WARNING),
            )
            return

        raw = user.strip().strip("<@!>").strip()
        if not raw.isdigit():
            await ui.respond(
                interaction,
                ui.notice(
                    title="Invalid User",
                    body=(
                        "Provide the user ID of the banned member. Enable Developer Mode in Discord, "
                        "open the ban list, and copy their ID."
                    ),
                    accent=config.COLOR_WARNING,
                ),
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        user_id = int(raw)
        try:
            target = await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            target = None

        ok, error = await mod_actions.lift_ban(
            self.bot,
            guild,
            user_id,
            self.memory,
            reason=f"Unbanned by {interaction.user}.",
            moderator=interaction.user,
            notes=notes or "",
        )

        if not ok:
            await interaction.followup.send(
                view=ui.notice(title="Unban Failed", body=error or "The unban could not be completed.", accent=config.COLOR_DANGER),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            view=ui.notice(
                title="Ban Lifted",
                fields=[
                    ("Member", audit.describe(target) if target else str(user_id)),
                    ("Notes", notes or "None provided."),
                ],
                footer=audit.actor_note(interaction.user),
                accent=config.COLOR_SUCCESS,
                thumbnail=str(target.display_avatar.url) if target else None,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="cases", description="View a member's case history.")
    @app_commands.describe(user="The member whose cases you want to see.")
    @staff_check(config.LEVEL_VIEW_CASES)
    async def cases(self, interaction: discord.Interaction, user: discord.User) -> None:
        await ui.respond(interaction, await self.build_case_view(interaction, user.id, 0), ephemeral=True)

    async def build_case_view(self, interaction: discord.Interaction, user_id: int, page: int) -> ui.Layout:
        records = list(reversed(self.memory.cases_for(user_id)))
        profile = self.memory.user(user_id)
        pages = max(1, (len(records) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        window = records[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

        user = self.bot.get_user(user_id)
        name = str(user) if user else str(user_id)

        view = ui.Layout(timeout=300)
        container = dui.Container(accent_colour=discord.Colour(config.COLOR_INFO))

        header = f"## Case History\n-# {name} ({user_id})"
        avatar = str(user.display_avatar.url) if user else None
        if avatar:
            container.add_item(dui.Section(dui.TextDisplay(header), accessory=dui.Thumbnail(avatar)))
        else:
            container.add_item(dui.TextDisplay(header))

        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        active = [c for c in records if not c.get("revoked")]
        summary = (
            f"**{len(records)}** cases on record, **{len(active)}** live, "
            f"**{len(records) - len(active)}** revoked.\n"
            f"Filter strikes: **{profile.get('filter_strikes', 0)}**. "
            f"Tickets opened: **{len(profile.get('tickets', []))}**."
        )
        container.add_item(dui.TextDisplay(summary))
        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))

        if not window:
            container.add_item(dui.TextDisplay("This member has no cases on record."))
        else:
            blocks = []
            for case in window:
                status = "Revoked" if case.get("revoked") else ("Active" if case.get("active") else "Expired")
                created = f"<t:{int(case.get('created_at', 0))}:f>"
                lines = [
                    f"**Case #{case['id']}** - {status}",
                    f"{case.get('rule')}[{case.get('clause')}] {case.get('rule_title')}",
                    f"{case.get('clause_title')}",
                    f"Punishment: {case.get('duration_label')} ({_ordinal(case.get('offense', 1))} offence)",
                    f"Moderator: <@{case.get('moderator')}> on {created}",
                ]
                if case.get("reason"):
                    lines.append(f"Notes: {ui.clip(case['reason'], 300)}")
                if case.get("revoked"):
                    lines.append(f"Revoked by <@{case.get('revoked_by')}>: {ui.clip(case.get('revoke_reason') or '', 200)}")
                blocks.append("\n".join(lines))
            container.add_item(dui.TextDisplay(ui.clip("\n\n".join(blocks), CASE_BLOCK_BUDGET)))

        if pages > 1:
            container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(CasePages(self, user_id, page, pages, interaction.user.id))

        container.add_item(dui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(dui.TextDisplay(f"-# Page {page + 1} of {pages}"))
        view.add_item(container)
        return view

    @app_commands.command(name="revokecase", description="Revoke a case by its number.")
    @app_commands.describe(
        case="The case number to revoke.",
        reason="Why the case is being revoked. This is recorded permanently.",
    )
    @staff_check(config.LEVEL_REVOKE_CASE)
    async def revokecase(
        self,
        interaction: discord.Interaction,
        case: int,
        reason: str,
    ) -> None:
        record = self.memory.case(case)
        if record is None:
            await ui.respond(
                interaction,
                ui.notice(title="Case Not Found", body=f"No case exists with the number **{case}**.", accent=config.COLOR_WARNING),
            )
            return

        if record.get("revoked"):
            await ui.respond(
                interaction,
                ui.notice(
                    title="Already Revoked",
                    body=f"Case **#{case}** was already revoked by <@{record.get('revoked_by')}>.",
                    accent=config.COLOR_WARNING,
                ),
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        user_id = int(record["user"])
        lifted = ""

        self.memory.revoke_case(case, interaction.user.id, reason)

        if guild is not None:
            if record.get("action") == "mute":
                removed = await mod_actions.lift_mute(
                    self.bot, guild, user_id, self.memory,
                    reason=f"Case #{case} revoked: {reason}",
                    moderator=interaction.user,
                    case_id=case,
                )
                lifted = "The mute was lifted." if removed else "The member was not muted."
            elif record.get("action") == "ban":
                ok, error = await mod_actions.lift_ban(
                    self.bot, guild, user_id, self.memory,
                    reason=f"Case #{case} revoked: {reason}",
                    moderator=interaction.user,
                    case_id=case,
                    notes=reason,
                )
                lifted = "The ban was lifted." if ok else f"The ban was not lifted: {error}"

        user = self.bot.get_user(user_id)
        if user is not None:
            await mod_actions.send_dm(
                user,
                ui.notice(
                    title="Case Revoked",
                    subtitle=f"{config.COMMUNITY_NAME} Moderation",
                    body=(
                        f"Case **#{case}** against you has been revoked by the staff team. "
                        "It no longer counts towards your offence history."
                    ),
                    fields=[
                        ("Rule", f"{record.get('rule')}[{record.get('clause')}] {record.get('rule_title')}"),
                        ("Reason", reason),
                    ],
                    accent=config.COLOR_SUCCESS,
                    thumbnail=self.bot.icon_url(guild),
                ),
            )

        await audit.post(
            self.bot,
            title=f"Case Revoked - #{case}",
            accent=config.COLOR_SUCCESS,
            fields=[
                ("Member", audit.describe(user) if user else str(user_id)),
                ("Original punishment", record.get("duration_label", "Unknown")),
                ("Rule", f"{record.get('rule')}[{record.get('clause')}] {record.get('rule_title')}"),
                ("Reason", reason),
                ("Outcome", lifted or "No active punishment to lift."),
            ],
            footer=audit.actor_note(interaction.user),
        )

        await interaction.followup.send(
            view=ui.notice(
                title="Case Revoked",
                subtitle=f"Case #{case}",
                fields=[
                    ("Member", audit.describe(user) if user else str(user_id)),
                    ("Original punishment", record.get("duration_label", "Unknown")),
                    ("Reason", reason),
                    ("Outcome", lifted or "No active punishment to lift."),
                ],
                footer=audit.actor_note(interaction.user),
                accent=config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="rule", description="Look up a rule from the rulebook.")
    @app_commands.describe(rule="The rule and clause to display.")
    @app_commands.autocomplete(rule=rule_autocomplete)
    async def rule_lookup(self, interaction: discord.Interaction, rule: str) -> None:
        clause = rulebook.get(rule)
        if clause is None:
            await ui.respond(
                interaction,
                ui.notice(title="Unknown Rule", body="That rule is not in the rulebook.", accent=config.COLOR_WARNING),
            )
            return

        section = rulebook.section(clause.section_id) or {}
        rows = [
            ("Rule", f"{clause.section_id} {clause.section_title}"),
            ("Clause", f"[{clause.clause_id}] {clause.title}"),
            ("Punishments", "\n".join(clause.tier_labels)),
        ]
        if clause.note:
            rows.append(("Staff guidance", clause.note))

        await ui.respond(
            interaction,
            ui.notice(
                title=f"Rule {clause.reference}",
                subtitle=section.get("summary", ""),
                body=ui.clip(section.get("description", ""), 1200),
                fields=rows,
                footer=f"{config.COMMUNITY_NAME} rulebook, version {rulebook.meta.get('version', '1.0')}",
                accent=config.COLOR_INFO,
                thumbnail=self.bot.icon_url(interaction.guild),
            ),
            ephemeral=True,
        )

    @commands.command(name="promote")
    async def promote(self, ctx: commands.Context, member: discord.Member, *, role_name: str) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_PROMOTE):
            raise commands.CheckFailure()

        guild = ctx.guild
        if guild is None:
            return

        role = permissions.resolve_staff_role(guild, role_name)
        if role is None:
            available = "\n".join(f"{entry['name']} ({entry['id']})" for entry in config.STAFF_ROLES)
            await ctx.send(
                view=ui.notice(
                    title="Unknown Staff Role",
                    body=(
                        f"No staff role matches `{ui.clip(role_name, 80)}`. Only the roles below can be "
                        "granted with this command."
                    ),
                    fields=[("Staff roles", available)],
                    accent=config.COLOR_WARNING,
                )
            )
            return

        entry = permissions.staff_role_entry(role.id)
        target_level = entry["level"] if entry else 0
        actor_level = permissions.level_of(ctx.author)

        if target_level >= actor_level:
            await ctx.send(
                view=ui.notice(
                    title="Permission Denied",
                    body=(
                        f"You cannot grant **{role.name}**, which sits at or above your own rank "
                        f"({permissions.title_of(ctx.author)}). Ask someone above you to do it."
                    ),
                    accent=config.COLOR_DANGER,
                )
            )
            return

        if role in member.roles:
            await ctx.send(
                view=ui.notice(
                    title="Already Holds That Role",
                    body=f"{member.mention} already has **{role.name}**.",
                    accent=config.COLOR_WARNING,
                )
            )
            return

        if role >= guild.me.top_role:
            await ctx.send(
                view=ui.notice(
                    title="Role Too High",
                    body=(
                        f"**{role.name}** sits above the bot's highest role, so it cannot be granted. "
                        "Move the bot's role above it in the server settings."
                    ),
                    accent=config.COLOR_DANGER,
                )
            )
            return

        try:
            await member.add_roles(role, reason=f"Promoted by {ctx.author} ({ctx.author.id}).")
        except discord.Forbidden:
            await ctx.send(
                view=ui.notice(
                    title="Promotion Failed",
                    body="The bot lacks the Manage Roles permission.",
                    accent=config.COLOR_DANGER,
                )
            )
            return
        except discord.HTTPException as exc:
            await ctx.send(
                view=ui.notice(title="Promotion Failed", body=f"Discord rejected the change: {exc}", accent=config.COLOR_DANGER)
            )
            return

        self.memory.add_note(member.id, ctx.author.id, f"Promoted to {role.name}.")

        await mod_actions.send_dm(
            member,
            ui.notice(
                title="Staff Promotion",
                subtitle=guild.name,
                body=(
                    f"You have been promoted to **{role.name}**.\n\n"
                    "Read the rulebook before you take any moderation action. Every command you run "
                    "is recorded against your name in the server log."
                ),
                footer=f"Promoted by {ctx.author}.",
                accent=config.COLOR_SUCCESS,
                thumbnail=self.bot.icon_url(guild),
            ),
        )

        await ctx.send(
            view=ui.notice(
                title="Member Promoted",
                fields=[
                    ("Member", f"{member.mention}\n{member} ({member.id})"),
                    ("Role", f"{role.mention}\n{role.name} ({role.id})"),
                    ("Rank", entry["name"] if entry else role.name),
                ],
                footer=audit.actor_note(ctx.author),
                accent=config.COLOR_SUCCESS,
                thumbnail=str(member.display_avatar.url),
            )
        )

        await audit.post(
            self.bot,
            title="Staff Promotion",
            accent=config.COLOR_SUCCESS,
            fields=[
                ("Member", audit.describe(member)),
                ("Role granted", f"{role.name} ({role.id})"),
            ],
            footer=audit.actor_note(ctx.author),
            thumbnail=str(member.display_avatar.url),
        )

    @commands.command(name="demote")
    async def demote(self, ctx: commands.Context, member: discord.Member, *, role_name: str) -> None:
        if not permissions.has_level(ctx.author, config.LEVEL_PROMOTE):
            raise commands.CheckFailure()

        guild = ctx.guild
        if guild is None:
            return

        role = permissions.resolve_staff_role(guild, role_name)
        if role is None or role not in member.roles:
            await ctx.send(
                view=ui.notice(
                    title="Role Not Held",
                    body=f"{member.mention} does not hold a staff role matching `{ui.clip(role_name, 80)}`.",
                    accent=config.COLOR_WARNING,
                )
            )
            return

        entry = permissions.staff_role_entry(role.id)
        if (entry["level"] if entry else 0) >= permissions.level_of(ctx.author):
            await ctx.send(
                view=ui.notice(
                    title="Permission Denied",
                    body="You cannot remove a role at or above your own rank.",
                    accent=config.COLOR_DANGER,
                )
            )
            return

        try:
            await member.remove_roles(role, reason=f"Demoted by {ctx.author} ({ctx.author.id}).")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(view=ui.notice(title="Demotion Failed", body=str(exc), accent=config.COLOR_DANGER))
            return

        self.memory.add_note(member.id, ctx.author.id, f"Removed from {role.name}.")

        await ctx.send(
            view=ui.notice(
                title="Member Demoted",
                fields=[("Member", audit.describe(member)), ("Role removed", f"{role.name} ({role.id})")],
                footer=audit.actor_note(ctx.author),
                accent=config.COLOR_WARNING,
            )
        )
        await audit.post(
            self.bot,
            title="Staff Demotion",
            accent=config.COLOR_WARNING,
            fields=[("Member", audit.describe(member)), ("Role removed", f"{role.name} ({role.id})")],
            footer=audit.actor_note(ctx.author),
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = ui.notice(
                title="Permission Denied",
                body="You do not hold a staff role with the authority to run that command.",
                accent=config.COLOR_DANGER,
            )
        else:
            log.exception("Application command failed.", exc_info=error)
            message = ui.notice(
                title="Command Failed",
                body="Something went wrong while running that command. The error has been logged.",
                accent=config.COLOR_DANGER,
            )
        try:
            await ui.respond(interaction, message, ephemeral=True)
        except discord.HTTPException:
            pass


def _ordinal(number: int) -> str:
    number = int(number or 1)
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
