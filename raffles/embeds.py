from __future__ import annotations

import discord
from typing import Dict, List, Optional


def build_raffle_embed(raffle: Dict[str, any], guild: discord.Guild) -> discord.Embed:
    title = raffle.get("title") or "New raffle"
    prize = raffle.get("prize") or "Mystery prize"
    host_id = raffle.get("host_id")
    host = guild.get_member(host_id) if host_id else None
    entries = raffle.get("entrants") or []
    max_winners = raffle.get("max_winners") or 1
    ends_at = raffle.get("ends_at")
    status = raffle.get("status", "open")
    winners = raffle.get("winners") or []

    embed = discord.Embed(
        title=title,
        description=f"Prize: **{prize}**",
        color=discord.Color.blurple() if status == "open" else discord.Color.gold(),
    )
    if host:
        embed.set_author(name=f"Hosted by {host.display_name}", icon_url=host.display_avatar.url)
    embed.add_field(name="Entries", value=str(len(entries)), inline=True)
    embed.add_field(name="Max winners", value=str(max_winners), inline=True)
    if ends_at:
        embed.add_field(name="Ends", value=f"<t:{int(ends_at)}:R>", inline=True)
    else:
        embed.add_field(name="Ends", value="Manual close", inline=True)
    if status == "closed":
        if winners:
            winners_text = ", ".join(f"<@{uid}>" for uid in winners)
        else:
            winners_text = "No entrants"
        embed.add_field(name="Winners", value=winners_text, inline=False)
    embed.set_footer(text=f"Raffle ID: {raffle.get('id')}")
    return embed


def build_entrants_embed(
    raffle: Dict[str, any], guild: discord.Guild, page: int = 0, page_size: int = 25
) -> discord.Embed:
    entries: List[int] = raffle.get("entrants") or []
    total = len(entries)
    start = page * page_size
    end = start + page_size
    chunk = entries[start:end]
    lines: List[str] = []
    for uid in chunk:
        member = guild.get_member(uid)
        lines.append(member.mention if member else f"<@{uid}>")
    if not lines:
        lines.append("No entrants yet.")
    embed = discord.Embed(
        title=f"Entrants ({total})",
        description="\n".join(lines),
        color=discord.Color.dark_teal(),
    )
    embed.set_footer(text=f"Page {page+1} of {max(1, (total + page_size - 1) // page_size)}")
    return embed
