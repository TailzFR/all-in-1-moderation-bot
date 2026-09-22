from __future__ import annotations

from typing import Iterable, Sequence

import discord
from discord import ui

import config

dui = ui

MAX_TEXT = 3900
MAX_FIELD = 1024


def clip(text: str, limit: int = MAX_FIELD) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def sanitise(text: str) -> str:
    if not text:
        return ""
    return text.replace("@everyone", "@ everyone").replace("@here", "@ here")


def field(name: str, value: str) -> str:
    return f"**{name}**\n{sanitise(clip(value))}"


def field_list(pairs: Sequence[tuple[str, str]], inline: bool = False) -> str:
    if inline:
        return "\n".join(f"**{name}:** {sanitise(clip(value, 512))}" for name, value in pairs)
    return "\n\n".join(field(name, value) for name, value in pairs)


class Layout(ui.LayoutView):
    def __init__(self, *, timeout: float | None = None) -> None:
        super().__init__(timeout=timeout)


def build_container(
    *,
    title: str | None = None,
    subtitle: str | None = None,
    body: str | None = None,
    fields: Sequence[tuple[str, str]] | None = None,
    footer: str | None = None,
    accent: int = config.COLOR_PRIMARY,
    thumbnail: str | None = None,
    heading_level: int = 2,
    extra_items: Iterable[ui.Item] | None = None,
) -> ui.Container:
    container = ui.Container(accent_colour=discord.Colour(accent))

    header_lines: list[str] = []
    if title:
        header_lines.append(f"{'#' * max(1, min(3, heading_level))} {title}")
    if subtitle:
        header_lines.append(f"-# {subtitle}")
    header = "\n".join(header_lines)

    if header and thumbnail:
        container.add_item(
            ui.Section(
                ui.TextDisplay(header),
                accessory=ui.Thumbnail(thumbnail),
            )
        )
    elif header:
        container.add_item(ui.TextDisplay(header))

    if header and (body or fields):
        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

    if body:
        container.add_item(ui.TextDisplay(clip(sanitise(body), MAX_TEXT)))

    if fields:
        if body:
            container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ui.TextDisplay(clip(field_list(fields), MAX_TEXT)))

    for item in extra_items or ():
        container.add_item(item)

    if footer:
        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ui.TextDisplay(f"-# {clip(sanitise(footer), 512)}"))

    return container


def notice(
    *,
    title: str | None = None,
    subtitle: str | None = None,
    body: str | None = None,
    fields: Sequence[tuple[str, str]] | None = None,
    footer: str | None = None,
    accent: int = config.COLOR_PRIMARY,
    thumbnail: str | None = None,
    extra_items: Iterable[ui.Item] | None = None,
) -> Layout:
    view = Layout()
    view.add_item(
        build_container(
            title=title,
            subtitle=subtitle,
            body=body,
            fields=fields,
            footer=footer,
            accent=accent,
            thumbnail=thumbnail,
            extra_items=extra_items,
        )
    )
    return view


def relay(
    *,
    heading: str,
    body: str,
    accent: int,
    footer: str | None = None,
    attachments: Sequence[tuple[str, str]] = (),
    thumbnail: str | None = None,
) -> Layout:
    text = sanitise(body).strip() or "-# No message content."
    lines = [f"### {heading}", text]
    if attachments:
        listed = "\n".join(f"[{name}]({url})" for name, url in attachments)
        lines.append(f"**Attachments**\n{listed}")

    view = Layout()
    container = ui.Container(accent_colour=discord.Colour(accent))
    joined = clip("\n\n".join(lines), MAX_TEXT)
    if thumbnail:
        container.add_item(ui.Section(ui.TextDisplay(joined), accessory=ui.Thumbnail(thumbnail)))
    else:
        container.add_item(ui.TextDisplay(joined))
    if footer:
        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ui.TextDisplay(f"-# {clip(footer, 400)}"))
    view.add_item(container)
    return view


def file_notice(
    *,
    title: str,
    body: str,
    filename: str,
    accent: int = config.COLOR_INFO,
    footer: str | None = None,
) -> Layout:
    view = Layout()
    container = ui.Container(accent_colour=discord.Colour(accent))
    container.add_item(ui.TextDisplay(f"## {title}"))
    container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
    container.add_item(ui.TextDisplay(clip(sanitise(body), MAX_TEXT)))
    container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
    container.add_item(ui.File(f"attachment://{filename}"))
    if footer:
        container.add_item(ui.TextDisplay(f"-# {clip(footer, 400)}"))
    view.add_item(container)
    return view


async def send_layout(
    destination: discord.abc.Messageable,
    view: Layout,
    **kwargs,
) -> discord.Message:
    return await destination.send(view=view, **kwargs)


async def respond(
    interaction: discord.Interaction,
    view: Layout,
    *,
    ephemeral: bool = True,
    **kwargs,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=ephemeral, **kwargs)
    else:
        await interaction.response.send_message(view=view, ephemeral=ephemeral, **kwargs)


async def edit_response(interaction: discord.Interaction, view: Layout) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)
