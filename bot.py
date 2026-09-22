from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import time

import discord
from discord.ext import commands, tasks

import config
from core import audit, mod_actions, ui
from core.memory import Memory
from core.storage import JSONStore

EXTENSIONS = (
    "cogs.verify",
    "cogs.tickets",
    "cogs.appeals",
    "cogs.moderation",
    "cogs.serverlog",
    "cogs.system",
)


def setup_logging() -> None:
    formatter = logging.Formatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}",
        datefmt="%Y-%m-%d %H:%M:%S",
        style="{",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        filename=config.LOG_DIR / "bot.log",
        encoding="utf-8",
        maxBytes=8 * 1024 * 1024,
        backupCount=5,
    )
    rotating.setFormatter(formatter)
    root.addHandler(rotating)

    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


log = logging.getLogger("manager")


class ManagerBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.guilds = True
        intents.moderation = True
        intents.voice_states = True

        super().__init__(
            command_prefix=commands.when_mentioned_or(config.PREFIX),
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            case_insensitive=True,
            activity=discord.Activity(type=discord.ActivityType.watching, name=config.COMMUNITY_NAME),
        )

        self.store = JSONStore(
            path=config.STORE_PATH,
            backup_dir=config.BACKUP_DIR,
            backup_keep=config.BACKUP_KEEP,
            flush_interval=config.STORE_FLUSH_SECONDS,
        )
        self.memory = Memory(self.store)
        self.started_at = time.time()
        self._ready_once = False

    @property
    def primary_guild(self) -> discord.Guild | None:
        if config.GUILD_ID:
            guild = self.get_guild(config.GUILD_ID)
            if guild is not None:
                return guild
        panel_channel = self.get_channel(config.TICKET_PANEL_CHANNEL_ID)
        if panel_channel is not None and getattr(panel_channel, "guild", None) is not None:
            return panel_channel.guild
        return self.guilds[0] if self.guilds else None

    def icon_url(self, guild: discord.Guild | None = None) -> str | None:
        if config.PANEL_ICON_URL:
            return config.PANEL_ICON_URL
        guild = guild or self.primary_guild
        if guild is not None and guild.icon is not None:
            return guild.icon.url
        return None

    async def setup_hook(self) -> None:
        self.store.start(asyncio.get_running_loop())

        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
                log.info("Loaded extension %s.", extension)
            except Exception:
                log.exception("Failed to load extension %s.", extension)

        self.expiry_worker.start()

    async def on_ready(self) -> None:
        log.info("Connected as %s (%s).", self.user, self.user.id if self.user else "?")

        if self._ready_once:
            return
        self._ready_once = True

        guild = self.primary_guild
        if guild is None:
            log.error("The bot is not in any guild. Invite it before continuing.")
            return

        log.info("Primary guild: %s (%s).", guild.name, guild.id)

        missing = config.missing_settings()
        for name in missing:
            log.warning("%s is not set in .env. The feature that uses it is disabled.", name)

        role = await mod_actions.ensure_muted_role(guild, self.memory, sync=True)
        if role is not None:
            log.info("Muted role ready: %s (%s).", role.name, role.id)

        synced = await self.sync_commands(guild)

        await audit.post(
            self,
            title="Bot Online",
            accent=config.COLOR_SUCCESS,
            fields=[
                ("Guild", f"{guild.name} ({guild.id})"),
                ("Members", str(guild.member_count)),
                (
                    "Slash commands",
                    ", ".join(f"/{c.name}" for c in synced)
                    if synced
                    else "None synced. Check the applications.commands scope.",
                ),
                ("Cases on record", str(len(self.store.section("cases")))),
                ("Tickets on record", str(len(self.store.section("tickets")))),
                ("Configuration", "Complete" if not missing else "Missing: " + ", ".join(missing)),
            ],
            footer=f"{config.BOT_NAME} started successfully.",
            thumbnail=self.icon_url(guild),
        )

    async def sync_commands(self, guild: discord.Guild) -> list[discord.app_commands.AppCommand]:
        target = discord.Object(id=guild.id)
        self.tree.copy_global_to(guild=target)

        try:
            synced = await self.tree.sync(guild=target)
        except discord.Forbidden:
            log.error(
                "Discord refused the command sync for %s. The bot was invited without the "
                "applications.commands scope. Re-invite it with that scope and restart.",
                guild.name,
            )
            return []
        except discord.HTTPException:
            log.exception("Could not sync application commands to %s.", guild.name)
            return []

        log.info(
            "Synced %d application commands to %s: %s",
            len(synced), guild.name, ", ".join(f"/{c.name}" for c in synced) or "none",
        )
        return synced

    async def close(self) -> None:
        log.info("Shutting down, flushing memory to disk.")
        try:
            self.expiry_worker.cancel()
        except Exception:
            pass
        await self.store.close()
        await super().close()

    @tasks.loop(seconds=30)
    async def expiry_worker(self) -> None:
        due = self.memory.due_schedules()
        if not due:
            return

        for entry in due:
            guild = self.get_guild(int(entry.get("guild", 0))) or self.primary_guild
            if guild is None:
                self.memory.drop_schedule(entry)
                continue

            user_id = int(entry["user"])
            case_id = entry.get("case")
            try:
                if entry["kind"] == "unmute":
                    await mod_actions.lift_mute(
                        self, guild, user_id, self.memory,
                        reason="The mute duration has elapsed.",
                        case_id=case_id,
                    )
                elif entry["kind"] == "unban":
                    await mod_actions.lift_ban(
                        self, guild, user_id, self.memory,
                        reason="The ban duration has elapsed.",
                        case_id=case_id,
                    )
            except Exception:
                log.exception("Failed to process a scheduled expiry for %s.", user_id)
            finally:
                self.memory.drop_schedule(entry)

    @expiry_worker.before_loop
    async def before_expiry_worker(self) -> None:
        await self.wait_until_ready()

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            await ctx.send(
                view=ui.notice(
                    title="Permission Denied",
                    body="You do not hold a staff role with the authority to run that command.",
                    accent=config.COLOR_DANGER,
                )
            )
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                view=ui.notice(
                    title="Missing Argument",
                    body=f"That command requires `{error.param.name}`. Run `{config.PREFIX}help` for the correct usage.",
                    accent=config.COLOR_WARNING,
                )
            )
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(
                view=ui.notice(
                    title="Invalid Argument",
                    body=str(error) or "One of the values you provided could not be understood.",
                    accent=config.COLOR_WARNING,
                )
            )
            return

        log.exception("Unhandled command error in %s.", ctx.command, exc_info=error)
        await ctx.send(
            view=ui.notice(
                title="Command Failed",
                body="Something went wrong while running that command. The error has been logged.",
                accent=config.COLOR_DANGER,
            )
        )


async def main() -> None:
    setup_logging()

    if not config.TOKEN:
        log.error("DISCORD_TOKEN is not set. Copy .env.example to .env and replace the placeholder token.")
        return

    bot = ManagerBot()
    async with bot:
        await bot.start(config.TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
