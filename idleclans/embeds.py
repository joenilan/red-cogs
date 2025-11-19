from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

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


def build_recruitment_embed(data: Dict[str, Any]) -> discord.Embed:
    clan_name = data.get("clanName") or "Unknown clan"
    message = data.get("recruitmentMessage")
    if not message or message == "undefined":
        message = "No recruitment message provided."
    embed = discord.Embed(
        title=f"{clan_name} recruitment",
        description=message,
        color=discord.Color.blurple(),
    )
    recruiting = "✅ Recruiting" if data.get("isRecruiting") else "❌ Closed"
    embed.add_field(name="Status", value=recruiting, inline=True)
    embed.add_field(
        name="Minimum total level",
        value=format_number(data.get("minimumTotalLevelRequired")),
        inline=True,
    )
    embed.add_field(
        name="Language",
        value=data.get("language") or "Unknown",
        inline=True,
    )
    embed.add_field(
        name="Activity score",
        value=f"{data.get('activityScore') or 0:.1f}",
        inline=True,
    )
    memberlist = data.get("memberlist") or []
    member_count = data.get("memberCount") or len(memberlist)
    embed.add_field(name="Members", value=str(member_count), inline=True)
    embed.add_field(name="House", value=str(data.get("houseId") or "N/A"), inline=True)

    rank_names = {2: "Leader", 1: "Officer", 0: "Member"}
    member_lines = []
    for member in memberlist[:10]:
        name = member.get("memberName") or "Unknown"
        rank_value = member.get("rank")
        label = rank_names.get(rank_value, f"Rank {rank_value}")
        emoji = "👑" if rank_value == 2 else "🎖️" if rank_value == 1 else "•"
        member_lines.append(f"{emoji} **{name}** — {label}")
    if member_lines:
        embed.add_field(
            name="Team",
            value="\n".join(member_lines)
            + ("\n_...and more_" if len(memberlist) > len(member_lines) else ""),
            inline=False,
        )

    skills_text = data.get("serializedSkills") or "{}"
    try:
        skills_data = json.loads(skills_text)
    except json.JSONDecodeError:
        skills_data = {}
    if skills_data:
        lines = []
        for skill, xp in sorted(
            skills_data.items(), key=lambda item: float(item[1]), reverse=True
        )[:6]:
            lines.append(f"{skill.title()}: {format_number(xp)} xp")
        embed.add_field(name="Skill focus", value="\n".join(lines), inline=False)

    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_clan_bank_embed(clan_name: str, summary: Dict[str, Any]) -> discord.Embed:
    logs_analyzed = summary.get("logs_analyzed", 0)
    event_total = summary.get("event_total", 0)
    embed = discord.Embed(
        title=f"{clan_name} bank activity",
        color=discord.Color.gold(),
        description=(
            f"Analyzed {format_number(logs_analyzed)} clan logs; "
            f"found {event_total} bank entries."
        ),
    )
    if not event_total:
        embed.description += " No deposits or withdrawals were detected in this sample."
        return embed
    overview_lines = [
        f"Deposits: `{summary.get('deposit_count', 0)}` "
        f"(+{format_number(summary.get('deposit_units', 0))} items)",
        f"Withdrawals: `{summary.get('withdraw_count', 0)}` "
        f"(-{format_number(summary.get('withdraw_units', 0))} items)",
    ]
    embed.add_field(name="Overview", value="\n".join(overview_lines), inline=False)
    item_totals = summary.get("item_totals") or []
    if item_totals:
        item_lines = []
        for item, data in item_totals:
            added = data.get("added", 0)
            withdrew = data.get("withdrew", 0)
            segment = f"+{format_number(added)}"
            if withdrew:
                segment += f" | -{format_number(withdrew)}"
            item_lines.append(f"**{item}** — {segment}")
        embed.add_field(
            name="Popular items",
            value="\n".join(item_lines),
            inline=False,
        )
    gold_positive = summary.get("gold_positive") or []
    if gold_positive:
        pos_lines = [
            f"**{name}** +{format_number(amount)}"
            for name, amount in gold_positive
        ]
        embed.add_field(
            name="Gold contributors",
            value="\n".join(pos_lines),
            inline=True,
        )
    gold_negative = summary.get("gold_negative") or []
    if gold_negative:
        neg_lines = [
            f"**{name}** -{format_number(amount)}"
            for name, amount in gold_negative
        ]
        embed.add_field(
            name="Gold spent",
            value="\n".join(neg_lines),
            inline=True,
        )
    recent_events = summary.get("recent_events") or []
    if recent_events:
        recent_lines = []
        for event in recent_events:
            timestamp = format_timestamp(event.get("timestamp"))
            actor = event.get("actor") or "Unknown"
            action = "deposited" if event.get("action") == "added" else "withdrew"
            amount = format_number(event.get("amount"))
            item = event.get("item") or "Unknown item"
            recent_lines.append(f"`{timestamp}` **{actor}** {action} {amount}x {item}")
        embed.add_field(
            name="Recent movements",
            value="\n".join(recent_lines),
            inline=False,
        )
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_clanhistory_embed(
    player_name: str, entries: List[Dict[str, Any]], scope: str
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Clan history: {player_name}",
        description=f"Source: {scope}",
        color=discord.Color.dark_purple(),
    )
    if not entries:
        embed.add_field(name="Logs", value="No entries found.", inline=False)
    else:
        lines = []
        for entry in entries[:20]:
            timestamp = format_timestamp(entry.get("timestamp"))
            message = entry.get("message") or "No message"
            clan = entry.get("clanName")
            if clan:
                message = f"[{clan}] {message}"
            lines.append(f"{timestamp} — {message}")
        chunk: List[str] = []
        chunk_len = 0
        idx = 1
        for line in lines:
            if chunk_len + len(line) + 1 > 900:  # keep headroom under 1024
                embed.add_field(name=f"Logs ({idx})", value="\n".join(chunk), inline=False)
                idx += 1
                chunk = []
                chunk_len = 0
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            embed.add_field(name=f"Logs ({idx})", value="\n".join(chunk), inline=False)
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_clancup_clan_embed(clan_name: str, data: Dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"Clan Cup standings: {clan_name}",
        color=discord.Color.green(),
    )
    standings = data.get("objectiveStandings") or []
    for standing in standings:
        objective = standing.get("objectiveType") or "Objective"
        rank = standing.get("rank")
        score = standing.get("score")
        embed.add_field(
            name=objective,
            value=f"Rank {rank} — {format_number(score)} pts",
            inline=False,
        )
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_clancup_top_embed(data: Dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="Current Clan Cup leaders",
        color=discord.Color.blue(),
    )
    for category, standings in data.items():
        if not isinstance(standings, list):
            continue
        lines = []
        for entry in standings[:5]:
            clan = entry.get("clanName") or "Unknown"
            score = entry.get("score")
            lines.append(f"{clan} — {format_number(score)} pts")
        if lines:
            embed.add_field(name=category, value="\n".join(lines), inline=False)
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_chat_recent_embed(channel: str, messages: List[Dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title=f"Chat — {channel}",
        color=discord.Color.orange(),
    )
    if not messages:
        embed.description = "No messages found."
    else:
        lines = []
        for msg in messages[:20]:
            timestamp = format_timestamp(
                msg.get("timestamp") or msg.get("time") or msg.get("sentAt")
            )
            author = msg.get("author") or msg.get("username") or msg.get("sender") or "Unknown"
            content = msg.get("message") or msg.get("content") or msg.get("text") or ""
            lines.append(f"`{timestamp}` **{author}**: {content}")
        embed.description = "\n".join(lines)
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
