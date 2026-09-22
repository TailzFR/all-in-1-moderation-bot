from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

DEFAULT_AVATAR = "data:image/svg+xml;base64," + base64.b64encode(
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'>"
    b"<circle cx='20' cy='20' r='20' fill='#4e5058'/></svg>"
).decode("ascii")

_CODE_BLOCK = re.compile(r"```(?:([a-zA-Z0-9+#-]*)\n)?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_UNDERLINE = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\*)(.+?)(?<!\*)\*(?![\w*])", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK = re.compile(r"(https?://[^\s<>\"]+)")
_HEADING = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
_SUBTEXT = re.compile(r"^-#\s+(.+)$", re.MULTILINE)
_QUOTE = re.compile(r"^&gt;\s+(.+)$", re.MULTILINE)
_MENTION = re.compile(r"&lt;@!?(\d+)&gt;")
_CHANNEL = re.compile(r"&lt;#(\d+)&gt;")
_ROLE = re.compile(r"&lt;@&amp;(\d+)&gt;")
_TIMESTAMP = re.compile(r"&lt;t:(\d+)(?::([tTdDfFR]))?&gt;")


def render_markdown(text: str) -> str:
    if not text:
        return ""

    escaped = html.escape(text)
    blocks: list[str] = []

    def stash_block(match: re.Match) -> str:
        language = match.group(1) or ""
        body = match.group(2)
        blocks.append(f'<pre class="block"><code data-lang="{html.escape(language)}">{body}</code></pre>')
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    escaped = _CODE_BLOCK.sub(stash_block, escaped)

    inlines: list[str] = []

    def stash_inline(match: re.Match) -> str:
        inlines.append(f'<code class="inline">{match.group(1)}</code>')
        return f"\x00INLINE{len(inlines) - 1}\x00"

    escaped = _INLINE_CODE.sub(stash_inline, escaped)

    escaped = _HEADING.sub(lambda m: f'<div class="h{len(m.group(1))}">{m.group(2)}</div>', escaped)
    escaped = _SUBTEXT.sub(lambda m: f'<div class="subtext">{m.group(1)}</div>', escaped)
    escaped = _QUOTE.sub(lambda m: f'<div class="quote">{m.group(1)}</div>', escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _UNDERLINE.sub(r"<u>\1</u>", escaped)
    escaped = _STRIKE.sub(r"<s>\1</s>", escaped)
    escaped = _ITALIC.sub(r"<em>\1</em>", escaped)
    escaped = _MENTION.sub(r'<span class="mention">@\1</span>', escaped)
    escaped = _ROLE.sub(r'<span class="mention">@role</span>', escaped)
    escaped = _CHANNEL.sub(r'<span class="mention">#\1</span>', escaped)
    escaped = _TIMESTAMP.sub(lambda m: f'<span class="ts-chip">{_format_epoch(int(m.group(1)))}</span>', escaped)
    escaped = _LINK.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', escaped)
    escaped = escaped.replace("\n", "<br>")

    for index, block in enumerate(blocks):
        escaped = escaped.replace(f"\x00BLOCK{index}\x00", block)
    for index, inline in enumerate(inlines):
        escaped = escaped.replace(f"\x00INLINE{index}\x00", inline)
    return escaped


def _format_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%d %b %Y %H:%M")


def _format_full(value: float | None) -> str:
    if not value:
        return "Unknown"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%d %B %Y at %H:%M UTC")


def _format_short(value: float | None) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%d/%m/%Y %H:%M")


def _duration_between(start: float | None, end: float | None) -> str:
    if not start or not end:
        return "Unknown"
    total = int(end - start)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


ROLE_STYLE = {
    "user": ("Member", "#f2f3f5"),
    "staff": ("Staff", "#5eb0ff"),
    "system": ("System", "#f0b232"),
}

STYLESHEET = """
:root{
  --bg:#313338; --bg-alt:#2b2d31; --bg-dark:#1e1f22; --bg-hover:#35373c;
  --text:#dbdee1; --text-strong:#f2f3f5; --muted:#949ba4; --faint:#80848e;
  --accent:#0b63e5; --link:#00a8fc; --border:#3f4147; --mention:#3c4270;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--text);
  font-family:"gg sans","Noto Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  font-size:16px; line-height:1.375;
}
.wrap{max-width:1100px;margin:0 auto;padding:0 0 64px}
.topbar{
  position:sticky;top:0;z-index:10;background:var(--bg);
  border-bottom:1px solid var(--bg-dark);padding:14px 24px;
  display:flex;align-items:center;gap:10px;box-shadow:0 1px 0 rgba(0,0,0,.2);
}
.topbar .hash{color:var(--faint);font-size:22px;font-weight:600;line-height:1}
.topbar .name{color:var(--text-strong);font-weight:600;font-size:16px}
.topbar .sep{width:1px;height:22px;background:var(--border);margin:0 6px}
.topbar .meta{color:var(--muted);font-size:13px}
.summary{
  margin:24px;padding:0;border-left:4px solid var(--accent);
  background:var(--bg-alt);border-radius:4px;overflow:hidden;
}
.summary .head{padding:16px 18px 4px}
.summary h1{margin:0 0 4px;font-size:18px;color:var(--text-strong);font-weight:600}
.summary .sub{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:2px 20px;padding:14px 18px 18px}
.cell .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.02em;font-weight:700;margin-bottom:2px}
.cell .v{color:var(--text-strong);font-size:14px;word-break:break-word}
.divider{display:flex;align-items:center;gap:12px;margin:26px 24px 8px;color:var(--faint);font-size:12px;font-weight:600;text-transform:uppercase}
.divider:before,.divider:after{content:"";height:1px;background:var(--border);flex:1}
.msg{display:flex;gap:16px;padding:6px 24px;position:relative}
.msg:hover{background:var(--bg-hover)}
.msg.start{margin-top:14px}
.avatar{width:40px;height:40px;border-radius:50%;flex:0 0 40px;background:var(--bg-dark);object-fit:cover}
.avatar.spacer{background:none}
.body{min-width:0;flex:1}
.head-line{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.author{font-weight:500;font-size:16px;color:var(--text-strong)}
.tag{
  font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.02em;
  padding:1px 5px;border-radius:3px;background:var(--accent);color:#fff;position:relative;top:-1px;
}
.tag.system{background:#f0b232;color:#1e1f22}
.tag.member{background:#4e5058}
.time{color:var(--faint);font-size:12px}
.content{color:var(--text);white-space:normal;word-wrap:break-word;padding-top:1px}
.content a{color:var(--link);text-decoration:none}
.content a:hover{text-decoration:underline}
.content .h1{font-size:24px;font-weight:700;color:var(--text-strong);margin:8px 0 4px}
.content .h2{font-size:20px;font-weight:700;color:var(--text-strong);margin:8px 0 4px}
.content .h3{font-size:16px;font-weight:700;color:var(--text-strong);margin:6px 0 2px}
.content .subtext{font-size:12px;color:var(--muted)}
.content .quote{border-left:4px solid var(--border);padding-left:12px;margin:2px 0;color:var(--text)}
.mention{background:var(--mention);color:#c9cdfb;border-radius:3px;padding:0 2px;font-weight:500}
.ts-chip{background:var(--mention);border-radius:3px;padding:0 2px}
code.inline{background:var(--bg-dark);border-radius:3px;padding:1px 4px;font-family:Consolas,"Andale Mono WT",monospace;font-size:13px}
pre.block{
  background:var(--bg-dark);border:1px solid #2a2c30;border-radius:4px;
  padding:8px 10px;margin:6px 0;overflow-x:auto;
}
pre.block code{font-family:Consolas,"Andale Mono WT",monospace;font-size:13px;color:#b5bac1;white-space:pre}
.attachments{margin-top:6px;display:flex;flex-direction:column;gap:4px}
.att{
  display:inline-flex;align-items:center;gap:8px;background:var(--bg-alt);
  border:1px solid var(--border);border-radius:4px;padding:8px 10px;max-width:420px;
}
.att a{color:var(--link);text-decoration:none;font-size:14px;word-break:break-all}
.att .size{color:var(--faint);font-size:12px;white-space:nowrap}
.att img{max-width:400px;max-height:300px;border-radius:4px;display:block}
.internal{border-left:3px solid #f0b232;padding-left:12px;margin-left:-15px}
.internal .content{color:var(--muted);font-style:italic}
.empty{margin:40px 24px;color:var(--muted);text-align:center;font-size:14px}
.foot{
  margin:40px 24px 0;padding-top:16px;border-top:1px solid var(--border);
  color:var(--faint);font-size:12px;text-align:center;line-height:1.7;
}
@media (max-width:640px){
  .msg{padding:6px 12px;gap:12px}
  .summary{margin:12px}
  .grid{grid-template-columns:1fr 1fr}
}
"""


def build(
    ticket: dict[str, Any],
    *,
    guild_name: str,
    channel_name: str,
    opener_name: str,
    opener_id: str,
    closer_name: str,
    handler_name: str | None = None,
    icon_url: str | None = None,
    include_internal: bool = True,
) -> str:
    messages = ticket.get("messages", [])
    if not include_internal:
        messages = [m for m in messages if not m.get("internal")]
    ticket_id = ticket.get("id")
    type_key = ticket.get("type", "support")
    type_label = config.TICKET_TYPES.get(type_key, {}).get("name", type_key.title())

    answers = ticket.get("answers") or {}
    scope = ticket.get("scope") or answers.get("scope") or "Not specified"

    rows: list[str] = []
    previous_author: str | None = None
    previous_at: float = 0.0

    for entry in messages:
        author_id = str(entry.get("author"))
        at = float(entry.get("at") or 0)
        role = entry.get("role", "user")
        grouped = author_id == previous_author and (at - previous_at) < 420 and role != "system"

        label, colour = ROLE_STYLE.get(role, ROLE_STYLE["user"])
        avatar = entry.get("author_avatar") or DEFAULT_AVATAR
        name = html.escape(entry.get("author_name") or "Unknown")
        body = render_markdown(entry.get("content") or "")

        attachments = ""
        files = entry.get("attachments") or []
        if files:
            items = []
            for att in files:
                url = html.escape(str(att.get("url", "")))
                fname = html.escape(str(att.get("filename", "attachment")))
                size = att.get("size")
                size_text = f'<span class="size">{_human_size(size)}</span>' if size else ""
                if str(att.get("content_type", "")).startswith("image/"):
                    items.append(f'<div class="att"><a href="{url}" target="_blank" rel="noopener noreferrer"><img src="{url}" alt="{fname}"></a></div>')
                else:
                    items.append(f'<div class="att"><a href="{url}" target="_blank" rel="noopener noreferrer">{fname}</a>{size_text}</div>')
            attachments = f'<div class="attachments">{"".join(items)}</div>'

        classes = ["msg"]
        if not grouped:
            classes.append("start")
        if entry.get("internal"):
            classes.append("internal")

        if grouped:
            header = ""
            avatar_html = '<div class="avatar spacer"></div>'
        else:
            tag_class = {"staff": "", "system": "system", "user": "member"}.get(role, "member")
            header = (
                '<div class="head-line">'
                f'<span class="author" style="color:{colour}">{name}</span>'
                f'<span class="tag {tag_class}">{label}</span>'
                f'<span class="time">{_format_short(at)}</span>'
                "</div>"
            )
            avatar_html = f'<img class="avatar" src="{html.escape(avatar)}" alt="">'

        rows.append(
            f'<div class="{" ".join(classes)}">{avatar_html}'
            f'<div class="body">{header}<div class="content">{body}</div>{attachments}</div></div>'
        )

        previous_author = author_id
        previous_at = at

    if not rows:
        rows.append('<div class="empty">This ticket was closed before any messages were exchanged.</div>')

    first_response = ticket.get("first_response_at")
    cells = [
        ("Ticket", f"#{ticket_id}"),
        ("Type", type_label),
        ("Scope", config.SCOPE_LABELS.get(str(scope), str(scope).title())),
        ("Opened by", f"{html.escape(opener_name)}<br><span style='color:var(--muted);font-size:12px'>{opener_id}</span>"),
        ("Opened", _format_full(ticket.get("created_at"))),
        ("Closed", _format_full(ticket.get("closed_at"))),
        ("Closed by", html.escape(closer_name)),
        ("Handled by", html.escape(handler_name or "Unclaimed")),
        ("Open for", _duration_between(ticket.get("created_at"), ticket.get("closed_at"))),
        (
            "First response",
            _duration_between(ticket.get("created_at"), first_response) if first_response else "No staff reply",
        ),
        ("Messages", str(len(messages))),
    ]
    if ticket.get("close_reason"):
        cells.append(("Close reason", html.escape(str(ticket["close_reason"]))))

    grid = "".join(
        f'<div class="cell"><div class="k">{key}</div><div class="v">{value}</div></div>'
        for key, value in cells
    )

    answer_rows = ""
    if answers:
        pretty = {
            "scope": "Issue scope",
            "subject": "Subject",
            "details": "Details",
            "reported_user": "Reported member",
            "evidence": "Evidence",
            "acknowledged": "Acknowledged the false ticket warning",
        }
        parts = []
        for key, value in answers.items():
            if value in (None, ""):
                continue
            label = pretty.get(key, key.replace("_", " ").title())
            parts.append(
                f'<div class="cell"><div class="k">{html.escape(label)}</div>'
                f'<div class="v">{render_markdown(str(value))}</div></div>'
            )
        if parts:
            answer_rows = (
                '<div class="divider">Intake answers</div>'
                f'<div class="summary"><div class="grid">{"".join(parts)}</div></div>'
            )

    title = f"{config.COMMUNITY_NAME} Ticket #{ticket_id}"
    icon_tag = ""
    if icon_url:
        icon_tag = f'<img src="{html.escape(icon_url)}" alt="" style="width:24px;height:24px;border-radius:50%">'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLESHEET}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    {icon_tag}
    <span class="hash">#</span>
    <span class="name">{html.escape(channel_name)}</span>
    <span class="sep"></span>
    <span class="meta">{html.escape(guild_name)} &middot; {type_label} &middot; {len(messages)} messages</span>
  </div>

  <div class="summary">
    <div class="head">
      <h1>{html.escape(title)}</h1>
      <div class="sub">Transcript generated by {config.BOT_NAME}</div>
    </div>
    <div class="grid">{grid}</div>
  </div>

  {answer_rows}

  <div class="divider">Conversation</div>
  {"".join(rows)}

  <div class="foot">
    {html.escape(config.BOT_NAME)} &middot; {html.escape(config.COMMUNITY_NAME)}<br>
    This transcript is an exact record of the messages exchanged in this ticket.
  </div>
</div>
</body>
</html>"""


def _human_size(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def write(html_text: str, ticket_id: int | str) -> Path:
    path = Path(config.TRANSCRIPT_DIR) / f"ticket-{ticket_id}.html"
    path.write_text(html_text, encoding="utf-8")
    return path
