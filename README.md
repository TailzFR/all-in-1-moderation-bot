# METRO

A Discord bot for tickets, ban appeals, rulebook-based moderation and server logging, built on discord.py Components V2.

## Setup

1. Install Python 3.10 or newer, then the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Create an application at https://discord.com/developers/applications, add a bot, and enable the **Server Members** and **Message Content** intents.

3. Copy the example environment file and fill it in:

   ```bash
   cp .env.example .env
   ```

4. Invite the bot with the `bot` and `applications.commands` scopes and these permissions: Manage Roles, Manage Channels, Kick Members, Ban Members, Moderate Members, View Audit Log, View Channels, Send Messages, Manage Messages, Embed Links, Attach Files, Read Message History. Invite it to the appeal server too.

5. Move the bot's role above every staff role, then start it:

   ```bash
   python bot.py
   ```

On startup the bot logs a warning for every setting still missing from `.env`, and the "Bot Online" entry in the log channel lists them too.

## Configuration

Everything server-specific lives in `.env`. To copy an ID, turn on Developer Mode in Discord (Settings > Advanced) and right-click the server, channel or role. An ID left as zeros counts as not set.

| Key | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token |
| `COMMAND_PREFIX` | Prefix for staff commands, `!` by default |
| `BOT_NAME`, `COMMUNITY_NAME`, `COMMUNITY_TAGLINE` | Names shown on panels, notices, transcripts and cards |
| `GAME_NAME` | Read after "the", for example `Minecraft server` |
| `PANEL_ICON_URL` | Optional panel thumbnail. The server icon is used when blank |
| `GUILD_ID` | Optional. Pins the main server |
| `TICKET_PANEL_CHANNEL_ID` | Where the ticket panel is posted |
| `TICKET_CATEGORY_ID` | Category that ticket channels are created in |
| `TRANSCRIPT_CHANNEL_ID` | Where closed ticket transcripts are archived |
| `SERVER_LOG_CHANNEL_ID` | Moderation and activity log |
| `VERIFY_CHANNEL_ID` | Where the verification panel is posted |
| `VERIFIED_ROLE_ID` | Role granted once a member passes verification |
| `UNVERIFIED_ROLE_ID` | Optional role applied on join and removed on success |
| `APPEAL_GUILD_ID` | Separate server that banned members can join |
| `APPEAL_PANEL_CHANNEL_ID` | Where the appeal panel is posted |
| `APPEAL_CHANNEL_ID` | Text channel for appeals, or a category to give each appeal its own channel |
| `APPEAL_INVITE` | Optional invite sent with ban notices. The bot creates one when blank |
| `APPEAL_ALLOWED_ROLE_ID` | Optional role that may appeal with a clean record |
| `ROLE_*_ID`, `ROLE_*_NAME` | The six staff ranks and how each is named in messages |

Staff ranks run from level 6 (Owner) down to level 1 (Discord Moderator). The minimum level for each action, ticket behaviour, colours and filter thresholds are set in `config.py`.

## Verification

New members have to pass a captcha before they get access. Set up the channel so unverified members can only see the verification channel, and give `VERIFIED_ROLE_ID` access to the rest of the server.

When someone joins, the bot applies `UNVERIFIED_ROLE_ID` if you set one and sends them a DM pointing at the verification channel. In that channel they press **Verify** and get a private message containing a freshly generated picture of six distorted characters, plus a dropdown with five options. Only one matches the picture.

- Right answer: they get the verified role, the unverified role comes off, and the log records it.
- Wrong answer: they are DMed an explanation and kicked, so they can rejoin and try again.

Staff, bots, the server owner and anyone with Manage Server are never kicked; they just get told to try again. A challenge expires after 3 minutes, and an expired or missing challenge never kicks anyone. Every picture is generated on the spot, so no two members see the same one.

Settings live in `config.py`: `VERIFY_ENABLED`, `VERIFY_CODE_LENGTH`, `VERIFY_OPTION_COUNT`, `VERIFY_MAX_ATTEMPTS`, `VERIFY_KICK_ON_FAIL` (set it to `False` to let people retry instead of being kicked), `VERIFY_TIMEOUT_SECONDS` and `VERIFY_DM_ON_JOIN`.

The bot needs Kick Members, and its role must sit above the verified role. Image generation needs Pillow.

| Command | Purpose |
|---|---|
| `!verify <member>` | Verify a member by hand |
| `!verifypanel` | Repost the verification panel |

## Tickets

Members open a ticket from the panel, confirm the false-ticket warning, choose Discord or In-Game, and fill in a short form. The bot creates a private staff channel and the member talks to staff through direct messages. Anything typed in the channel stays internal; only `!r` and `!rs` reach the member.

When the member is typing in DMs, the ticket channel shows it. The ticket header shows the member's previous tickets and active cases.

Closing a ticket:

- sends the member a closing notice, a summary card and an HTML transcript with internal notes removed
- archives the full transcript and summary card in the transcript channel
- asks the member to rate the support from Poor to Excellent, with an optional comment. The rating is posted to the log and added to the archived transcript and card

The summary card is a PNG showing who opened, handled and closed the ticket, how long it was open, the first response time, the message count and the rating. It needs Pillow. Without Pillow the card is left out. To use a custom typeface, put `Regular.ttf` and `Bold.ttf` in `assets/fonts/`.

Inactive tickets: if staff have replied and the member goes quiet for 24 hours, the bot sends a reminder and posts a notice in the channel. If there is still no reply 24 hours later, the ticket closes itself. This never happens while the ticket is waiting on staff. `!hold` pauses it for one ticket, and `TICKET_AUTOCLOSE_ENABLED` in `config.py` turns it off completely.

| Command | Purpose |
|---|---|
| `!r <message>` | Reply to the member |
| `!rs <name>` | Send a saved reply |
| `!ct [reason]` | Close the ticket |
| `!hold` | Pause or resume automatic closing |
| `!claim` | Claim the ticket |
| `!evidence [discord\|ingame]` | Send the evidence requirements |
| `!ticketinfo` | Member history, first response time, what the ticket is waiting on, closing status |
| `!snippets [name]` | List saved replies, or read one |
| `!snippet add <name> <text>` | Save a reply. `{member}`, `{ticket}` and `{server}` are filled in when it is sent |
| `!snippet remove <name>` | Delete a saved reply |
| `!ticketstats [member] [days]` | Volume, first response and resolution times, satisfaction, most active staff |
| `!block` / `!unblock <member>` | Remove or restore ticket access |
| `!panel` | Repost the ticket panel |

Four saved replies are created on first run: `greeting`, `moreinfo`, `evidence` and `resolved`.

## Appeals

Banned members join the appeal server and submit an appeal from the panel. Staff claim it, can question the member with `!ask` or `!r`, and accept or deny it with buttons. Accepting lifts the ban in the main server. A denied member has to wait 14 days before appealing again. Rules listed in `APPEAL_EXCLUDED_RULES` cannot be appealed.

| Command | Purpose |
|---|---|
| `!appeals [member]` | Pending appeals, or one member's history |
| `!ask <question>` | Send a question to the member |
| `!claim` | Claim the appeal |
| `!appealpanel` | Repost the appeal panel |

## Moderation

Punishments come from the rulebook in `data/rules.json`. `/punish` only accepts clauses listed in the rulebook, requires image or video proof, and works out the punishment from the member's previous offences for that clause. The bot DMs the member before a ban and includes the appeal invite. Mutes and temporary bans are lifted automatically, including after a restart.

In `data/rules.json`, `{community}` and `{game}` are replaced with `COMMUNITY_NAME` and `GAME_NAME` when the rulebook loads.

| Command | Level | Purpose |
|---|---|---|
| `/punish <user> <rule> <proof>` | 1 | Punish under a rulebook clause |
| `/cases <user>` | 1 | Case history |
| `/rule <rule>` | any | Look up a rule |
| `/unban <user id>` | 4 | Lift a ban |
| `/revokecase <case> <reason>` | 4 | Revoke a case and lift its punishment |
| `!promote` / `!demote <member> <role>` | 4 | Grant or remove a staff role below your own |

## Maintenance

| Command | Purpose |
|---|---|
| `!help` | Command list for your rank |
| `!status` | Uptime, latency and totals |
| `!memory` | Save to disk now and show storage stats |
| `!setupmute` | Rebuild the Muted role and its channel overrides |
| `!sync` | Resync the slash commands |

Data is stored in `data/store.json` and saved every few seconds. Backups go to `data/backups/`. Do not delete these files: they hold every case, ticket and appeal, and any mutes or bans still waiting to expire.

## Project layout

```
bot.py              Startup, expiry timer, error handling
config.py           Settings and .env loading
cogs/
  verify.py         Join captcha, verified role, kick on failure
  tickets.py        Panel, intake, relay, ratings, saved replies, auto-close, stats
  appeals.py        Appeal panel, form, claiming, decisions
  moderation.py     Slash commands and staff role management
  serverlog.py      Activity logging
  system.py         Help, status, maintenance
  chatfilter.py     Chat filter (not loaded by default)
core/
  captcha.py        Captcha image and answer options
  cards.py          Ticket summary card rendering
  fonts.py          Font loading for generated images
  transcripts.py    HTML transcripts
  memory.py         Cases, tickets, appeals, saved replies
  storage.py        JSON storage with backups
  ui.py             Components V2 builders
  permissions.py    Staff ranks
  rules.py          Rulebook loader
  mod_actions.py    Applying and lifting punishments
  audit.py          Log channel writer
  panels.py         Panel reposting
  duration.py       Duration parsing
  filters.py        Filter engine
data/
  rules.json        Rulebook
  filters.json      Filter word lists
```
