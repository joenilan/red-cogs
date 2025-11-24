from __future__ import annotations

import discord
from typing import Dict, List, Optional


def build_raffle_embed(raffle: Dict[str, any], guild: discord.Guild) -> discord.Embed:
    title = raffle.get("title") or "New raffle"
    prize = raffle.get("prize") or "Mystery prize"
    quantity = raffle.get("quantity")
    host_id = raffle.get("host_id")
    host = guild.get_member(host_id) if host_id else None
    entries = raffle.get("entrants") or []
    max_winners = raffle.get("max_winners") or 1
    ends_at = raffle.get("ends_at")
    status = raffle.get("status", "open")
    winners = raffle.get("winners") or []
    pending_draw = status == "closed" and not winners

    desc = f"Prize: **{prize}**"
    if quantity:
        desc += f" x{quantity}"
    embed = discord.Embed(
        title=title,
        description=desc,
        color=discord.Color.blurple() if status == "open" else discord.Color.gold(),
    )
    if host:
        embed.set_author(name=f"Hosted by {host.display_name}", icon_url=host.display_avatar.url)
    embed.add_field(name="Entries", value=str(len(entries)), inline=True)
    embed.add_field(name="Max winners", value=str(max_winners), inline=True)
    if status == "open":
        if ends_at:
            embed.add_field(name="Ends", value=f"<t:{int(ends_at)}:R>", inline=True)
        else:
            embed.add_field(name="Ends", value="Manual close", inline=True)
    else:
        embed.add_field(name="Ends", value="Closed", inline=True)
    if status == "closed":
        if winners:
            winners_text = ", ".join(f"<@{uid}>" for uid in winners)
            embed.add_field(name="Winners", value=winners_text, inline=False)
        elif pending_draw:
            embed.add_field(name="Status", value="Closed — ready to draw winners", inline=False)
        else:
            embed.add_field(name="Winners", value="No entrants", inline=False)
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


def entrants_grid(entries: list[int], guild: discord.Guild, columns: int = 3, per_col: int = 15) -> list[list[str]]:
    cols: list[list[str]] = [[] for _ in range(columns)]
    for idx, uid in enumerate(entries):
        col = idx % columns
        member = guild.get_member(uid)
        mention = member.mention if member else f"<@{uid}>"
        cols[col].append(mention)
    trimmed: list[list[str]] = []
    for col in cols:
        trimmed.append(col[:per_col])
    return trimmed
