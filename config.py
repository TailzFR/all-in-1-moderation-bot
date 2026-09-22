from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def _env_id(name: str) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value.isdigit() else 0


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ASSET_DIR = BASE_DIR / "assets"
TRANSCRIPT_DIR = BASE_DIR / "transcripts"
LOG_DIR = BASE_DIR / "logs"

for _directory in (DATA_DIR, ASSET_DIR, TRANSCRIPT_DIR, LOG_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

BOT_NAME = _env_str("BOT_NAME", "Server Manager")
COMMUNITY_NAME = _env_str("COMMUNITY_NAME", "Your Server")
COMMUNITY_TAGLINE = _env_str("COMMUNITY_TAGLINE", "Support and Moderation")
GAME_NAME = _env_str("GAME_NAME", "game server")

TOKEN = _env_str("DISCORD_TOKEN")
if TOKEN == "YOUR_BOT_TOKEN_HERE":
    TOKEN = ""
PREFIX = _env_str("COMMAND_PREFIX", "!")

COMPONENT_PREFIX = "mgr"

TICKET_PANEL_CHANNEL_ID = _env_id("TICKET_PANEL_CHANNEL_ID")
TICKET_CATEGORY_ID = _env_id("TICKET_CATEGORY_ID")
TRANSCRIPT_CHANNEL_ID = _env_id("TRANSCRIPT_CHANNEL_ID")
SERVER_LOG_CHANNEL_ID = _env_id("SERVER_LOG_CHANNEL_ID")

GUILD_ID = _env_id("GUILD_ID") or None

PANEL_ICON_URL = _env_str("PANEL_ICON_URL") or None

APPEAL_GUILD_ID = _env_id("APPEAL_GUILD_ID")
APPEAL_PANEL_CHANNEL_ID = _env_id("APPEAL_PANEL_CHANNEL_ID")
APPEAL_CHANNEL_ID = _env_id("APPEAL_CHANNEL_ID")

APPEAL_INVITE_URL = _env_str("APPEAL_INVITE")

PANEL_REFRESH_MINUTES = 30

APPEAL_COOLDOWN_DAYS = 14
APPEAL_EXCLUDED_RULES = {"0.5"}
APPEAL_DELETE_ON_DECISION = True
APPEAL_ALLOWED_ROLE_ID = _env_id("APPEAL_ALLOWED_ROLE_ID") or None

STAFF_ROLES: list[dict] = [
    {"id": _env_id("ROLE_OWNER_ID"), "name": _env_str("ROLE_OWNER_NAME", "Owner"), "level": 6, "key": "owner"},
    {"id": _env_id("ROLE_CO_OWNER_ID"), "name": _env_str("ROLE_CO_OWNER_NAME", "Co-Owner"), "level": 5, "key": "co_owner"},
    {"id": _env_id("ROLE_HEAD_ADMIN_ID"), "name": _env_str("ROLE_HEAD_ADMIN_NAME", "Head Admin"), "level": 4, "key": "head_admin"},
    {"id": _env_id("ROLE_DEVELOPER_ID"), "name": _env_str("ROLE_DEVELOPER_NAME", "Developer"), "level": 3, "key": "developer"},
    {"id": _env_id("ROLE_GAME_MOD_ID"), "name": _env_str("ROLE_GAME_MOD_NAME", "Game Moderator"), "level": 2, "key": "game_mod"},
    {"id": _env_id("ROLE_DISCORD_MOD_ID"), "name": _env_str("ROLE_DISCORD_MOD_NAME", "Discord Moderator"), "level": 1, "key": "discord_mod"},
]

STAFF_ROLE_IDS: set[int] = {role["id"] for role in STAFF_ROLES if role["id"]}
ROLE_LEVELS: dict[int, int] = {role["id"]: role["level"] for role in STAFF_ROLES if role["id"]}
TOP_LEVEL = max(role["level"] for role in STAFF_ROLES)

LEVEL_TICKET_STAFF = 1
LEVEL_PUNISH = 1
LEVEL_VIEW_CASES = 1
LEVEL_UNBAN = 4
LEVEL_REVOKE_CASE = 4
LEVEL_PROMOTE = 4
LEVEL_FILTER_IMMUNE = 1
LEVEL_MANAGE_PANEL = 4
LEVEL_MANAGE_SNIPPETS = 4

MUTED_ROLE_NAME = "Muted"

COLOR_PRIMARY = 0x0B63E5
COLOR_NEUTRAL = 0x2B2D31
COLOR_SUCCESS = 0x2E7D46
COLOR_WARNING = 0xC98A26
COLOR_DANGER = 0xB4282A
COLOR_INFO = 0x3E82F7

TICKET_TYPES = {
    "support": {
        "label": "Support Ticket",
        "name": "Support",
        "prefix": "support",
        "description": "General help, questions about the server, or account issues.",
    },
    "report": {
        "label": "Report Ticket",
        "name": "Report",
        "prefix": "report",
        "description": "Report a member for breaking the rules. Evidence is required.",
    },
    "logs": {
        "label": "Requesting Logs Ticket",
        "name": "Log Request",
        "prefix": "logs",
        "description": "Request punishment logs, case history, or moderation records.",
    },
}

SCOPE_LABELS = {"discord": "Discord", "ingame": "In-Game"}

MAX_OPEN_TICKETS_PER_USER = 1

TICKET_PING_LEVELS = (1, 2)

TICKET_RATINGS_ENABLED = True

TICKET_AUTOCLOSE_ENABLED = True
TICKET_REMIND_AFTER_HOURS = 24
TICKET_AUTOCLOSE_AFTER_HOURS = 24

TICKET_CARDS_ENABLED = True
FONT_DIR = ASSET_DIR / "fonts"

FILTER_ENABLED = False
FILTER_MAX_CAPS_RATIO = 0.75
FILTER_MIN_CAPS_LENGTH = 12
FILTER_MAX_MENTIONS = 5
FILTER_MAX_PROFANITY_PER_MESSAGE = 3
FILTER_MAX_REPEATED_CHARS = 9
FILTER_SPAM_MESSAGE_COUNT = 6
FILTER_SPAM_WINDOW_SECONDS = 7
FILTER_DUPLICATE_THRESHOLD = 4

FILTER_AUTO_PUNISH_SEVERE = False
FILTER_ESCALATE_AT = 3

STORE_PATH = DATA_DIR / "store.json"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 40
STORE_FLUSH_SECONDS = 5.0

RULES_PATH = DATA_DIR / "rules.json"
FILTERS_PATH = DATA_DIR / "filters.json"

APPEAL_NOTICE = (
    "You may appeal this decision in the appeal server. Read the ladder for the rule "
    "you broke before you appeal, and be honest in the form."
)
APPEAL_NOTICE_FINAL = (
    "This decision is final. Punishments under this rule are not appealable."
)

PROOF_REQUIRED = True
PROOF_ACCEPTED_PREFIXES = ("image/", "video/")
PROOF_MAX_REUPLOAD_BYTES = 24 * 1024 * 1024


_REQUIRED_IDS = {
    "TICKET_PANEL_CHANNEL_ID": TICKET_PANEL_CHANNEL_ID,
    "TICKET_CATEGORY_ID": TICKET_CATEGORY_ID,
    "TRANSCRIPT_CHANNEL_ID": TRANSCRIPT_CHANNEL_ID,
    "SERVER_LOG_CHANNEL_ID": SERVER_LOG_CHANNEL_ID,
    "APPEAL_GUILD_ID": APPEAL_GUILD_ID,
    "APPEAL_PANEL_CHANNEL_ID": APPEAL_PANEL_CHANNEL_ID,
    "APPEAL_CHANNEL_ID": APPEAL_CHANNEL_ID,
}


def missing_settings() -> list[str]:
    missing = [name for name, value in _REQUIRED_IDS.items() if not value]
    if not STAFF_ROLE_IDS:
        missing.append("ROLE_*_ID (no staff roles are configured)")
    return missing
