from __future__ import annotations

from typing import Any, Dict, List

import discord

from .utils import (
    SKILL_ICON_MAP,
    XPTable,
    format_number,
    format_timestamp,
    member_color,
    top_stat_lines,
)


def build_player_embed(profile: Dict[str, Any], xp_table: XPTable) -> discord.Embed:
    username = profile.get("username") or "Unknown player"
    embed = discord.Embed(
        title=username,
        color=member_color(username),
    )
    clan_name = profile.get("guildName")
    if clan_name:
        embed.description = f"Member of **{clan_name}**"
    game_mode = (profile.get("gameMode") or "Unknown").title()
    skills = profile.get("skillExperiences") or {}
    total_level = xp_table.total_level(skills)
    hours_offline = profile.get("hoursOffline")
    offline_value = (
        f"{float(hours_offline):.1f}h" if isinstance(hours_offline, (int, float)) else "Unknown"
    )
    task_name = profile.get("taskNameOnLogout") or "Unknown"
    task_type = profile.get("taskTypeOnLogout")
    task_value = f"{task_name} · type {task_type}" if task_type is not None else task_name

    embed.add_field(name="Game mode", value=f"🎮 {game_mode}", inline=True)
    embed.add_field(name="Total level", value=f"⭐ {format_number(total_level)}", inline=True)
    embed.add_field(name="Hours offline", value=f"⏱️ {offline_value}", inline=True)
    embed.add_field(name="Last task", value=f"📋 {task_value}", inline=False)

    pvm_stats = profile.get("pvmStats")
    pvm_lines = top_stat_lines(pvm_stats, suffix=" kills", limit=3)
    if pvm_lines and pvm_lines != "No data":
        embed.add_field(name="Boss clears", value=pvm_lines, inline=False)

    progression_bits: List[str] = []
    equipment = profile.get("equipment") or {}
    upgrades = profile.get("upgrades") or {}
    enchantments = profile.get("enchantmentBoosts") or {}
    if equipment:
        progression_bits.append(f"Equipment slots: {len(equipment)}")
    if upgrades:
        progression_bits.append(f"Upgrades: {len(upgrades)}")
    if enchantments:
        progression_bits.append(f"Enchantments: {len(enchantments)}")
    if progression_bits:
        embed.add_field(name="Progression", value="\n".join(progression_bits), inline=False)

    skill_entries = _build_skill_entries(skills, xp_table)
    if skill_entries:
        _add_skill_grid_fields(
            embed,
            entries=skill_entries,
            columns=2,
            max_rows=3,
            title="Skill highlights",
        )

    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_skills_embed(
    profile: Dict[str, Any], skills: Dict[str, Any], xp_table: XPTable
) -> discord.Embed:
    username = profile.get("username") or "Unknown player"
    embed = discord.Embed(
        title=f"{username}'s skills",
        color=member_color(username),
    )
    entries = _build_skill_entries(skills or {}, xp_table)
    if not entries:
        embed.description = "No skill data found."
        embed.set_footer(text="Data from IdleClans API")
        return embed
    _add_skill_grid_fields(
        embed,
        entries=entries,
        columns=3,
        max_rows=7,
        title="Skills",
    )
    embed.set_footer(text="Data from IdleClans API")
    return embed


def _build_skill_entries(skills: Dict[str, Any], xp_table: XPTable) -> List[str]:
    entries: List[str] = []
    for name, raw_xp in sorted(skills.items(), key=lambda item: float(item[1]), reverse=True):
        try:
            xp_val = float(raw_xp)
        except (TypeError, ValueError):
            continue
        icon = SKILL_ICON_MAP.get(name.lower(), "•")
        pretty = name.replace("_", " ").title()
        level, bar = xp_table.progress(xp_val)
        entries.append(f"{icon} **{pretty}** — Lvl {level}\n{bar}")
    return entries


def _add_skill_grid_fields(
    embed: discord.Embed,
    *,
    entries: List[str],
    columns: int,
    max_rows: int,
    title: Optional[str] = None,
) -> None:
    column_chunks: List[List[str]] = [[] for _ in range(columns)]
    limit = columns * max_rows
    for idx, entry in enumerate(entries[:limit]):
        column_chunks[idx % columns].append(entry)
    header_used = False
    for chunk in column_chunks:
        if not chunk:
            continue
        embed.add_field(
            name=title if title and not header_used else "\u200b",
            value="\n\n".join(chunk),
            inline=True,
        )
        header_used = True or header_used
    remaining = len(entries) - limit
    if remaining > 0:
        embed.add_field(
            name="\u200b",
            value=f"_...and {remaining} more skills_",
            inline=False,
        )
