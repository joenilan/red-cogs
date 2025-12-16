from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import discord

from .utils import (
    SKILL_ICON_MAP,
    XPTable,
    format_duration_ms,
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
def build_clancup_top_embed(data: Dict[str, Any], *, game_mode: str = "Default") -> discord.Embed:
    embed = discord.Embed(
        title="Current Clan Cup leaders",
        description=f"Mode: **{game_mode}**",
        color=discord.Color.blue(),
    )

    score_lines: List[str] = []
    for bucket in data.get("topScoreClans") or []:
        if not isinstance(bucket, dict):
            continue
        objective = bucket.get("objective") or "Objective"
        standings = bucket.get("standings") or []
        if not standings or not isinstance(standings, list):
            continue
        top = standings[0] if isinstance(standings[0], dict) else None
        if not top:
            continue
        clan = top.get("clanName") or "Unknown"
        score = top.get("score")
        score_lines.append(f"{_humanize_identifier(objective)}: **{clan}** — {format_number(score)}")

    time_lines: List[str] = []
    for bucket in data.get("topTimeClans") or []:
        if not isinstance(bucket, dict):
            continue
        objective = bucket.get("objective") or "Objective"
        standings = bucket.get("standings") or []
        if not standings or not isinstance(standings, list):
            continue
        top = standings[0] if isinstance(standings[0], dict) else None
        if not top:
            continue
        clan = top.get("clanName") or "Unknown"
        best = top.get("bestTime") or {}
        time_ms = best.get("time")
        time_lines.append(
            f"{_humanize_identifier(objective)}: **{clan}** — {format_duration_ms(time_ms)}"
        )

    _add_chunked_lines(embed, "Top score objectives", score_lines)
    _add_chunked_lines(embed, "Top time objectives", time_lines)
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_clancup_clan_embed(
    clan_name: str,
    standings: List[Dict[str, Any]],
    *,
    game_mode: str = "Default",
    previous_cup: bool = False,
) -> discord.Embed:
    tag = " (previous cup)" if previous_cup else ""
    embed = discord.Embed(
        title=f"Clan Cup standings: {clan_name}",
        description=f"Mode: **{game_mode}**{tag}",
        color=discord.Color.green(),
    )

    score_entries: List[tuple[int, str]] = []
    time_entries: List[tuple[int, str]] = []
    for entry in standings or []:
        if not isinstance(entry, dict):
            continue
        objective = entry.get("objective") or entry.get("objectiveType") or "Objective"
        rank_raw = entry.get("rank")
        try:
            rank = int(rank_raw)
        except (TypeError, ValueError):
            rank = 999999
        best_time = entry.get("bestTime")
        if isinstance(best_time, dict):
            time_ms = best_time.get("time")
            achieved_at = best_time.get("achievedAt")
            achieved = ""
            if achieved_at:
                achieved = format_timestamp(achieved_at).split(" ", 1)[0]
                achieved = f" ({achieved})"
            line = f"#{rank} {_humanize_identifier(objective)}: {format_duration_ms(time_ms)}{achieved}"
            time_entries.append((rank, line))
            continue
        score = entry.get("score")
        line = f"#{rank} {_humanize_identifier(objective)}: {format_number(score)}"
        score_entries.append((rank, line))

    score_entries.sort(key=lambda item: item[0])
    time_entries.sort(key=lambda item: item[0])

    _add_chunked_lines(embed, "Score objectives", [line for _, line in score_entries])
    _add_chunked_lines(embed, "Time objectives", [line for _, line in time_entries])

    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_clancup_objective_embed(
    clan_name: str,
    standing: Dict[str, Any],
    *,
    game_mode: str = "Default",
    previous_cup: bool = False,
) -> discord.Embed:
    objective = standing.get("objective") or standing.get("objectiveType") or "Objective"
    pretty = _humanize_identifier(objective)
    tag = " (previous cup)" if previous_cup else ""
    embed = discord.Embed(
        title=f"{clan_name} — {pretty}",
        description=f"Mode: **{game_mode}**{tag}",
        color=discord.Color.green(),
    )
    rank = standing.get("rank")
    embed.add_field(name="Rank", value=f"#{format_number(rank)}", inline=True)
    if isinstance(standing.get("bestTime"), dict):
        best = standing["bestTime"]
        embed.add_field(name="Best time", value=format_duration_ms(best.get("time")), inline=True)
        achieved = best.get("achievedAt")
        if achieved:
            embed.add_field(name="Achieved", value=format_timestamp(achieved), inline=False)
    else:
        embed.add_field(name="Score", value=format_number(standing.get("score")), inline=True)
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_clan_leaderboard_embed(
    profile: Dict[str, Any],
    *,
    game_mode: str = "Default",
) -> discord.Embed:
    clan_name = profile.get("username") or "Unknown clan"
    total = profile.get("totalLevelResult") or {}
    embed = discord.Embed(
        title=f"{clan_name} clan leaderboard",
        description=f"Mode: **{game_mode}**",
        color=discord.Color.dark_gold(),
    )
    total_level = total.get("totalLevel")
    total_rank = total.get("rank")
    total_score = total.get("score")
    if total_level is not None:
        embed.add_field(
            name="Total level",
            value=f"{format_number(total_level)} (#{format_number(total_rank)})",
            inline=True,
        )
    if total_score is not None:
        embed.add_field(name="Total XP", value=format_number(total_score), inline=True)

    skills = profile.get("fields") or {}
    entries: List[tuple[int, str]] = []
    for skill_key, payload in skills.items():
        if not isinstance(payload, dict):
            continue
        rank_raw = payload.get("rank")
        try:
            rank_val = int(rank_raw)
        except (TypeError, ValueError):
            rank_val = 999999
        score = payload.get("score")
        icon = SKILL_ICON_MAP.get(str(skill_key).lower(), "•")
        pretty = str(skill_key).replace("_", " ").title()
        entries.append(
            (
                rank_val,
                f"{icon} **{pretty}**\n#{format_number(rank_raw)} • {format_number(score)} XP",
            )
        )
    entries.sort(key=lambda item: item[0])
    if entries:
        _add_skill_grid_fields(
            embed,
            entries=[text for _, text in entries],
            columns=3,
            max_rows=7,
            title="Skill ranks",
        )
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_clan_leaderboard_skill_embed(
    profile: Dict[str, Any],
    skill_key: str,
    *,
    game_mode: str = "Default",
) -> discord.Embed:
    clan_name = profile.get("username") or "Unknown clan"
    skills = profile.get("fields") or {}
    payload = skills.get(skill_key.lower()) or skills.get(skill_key) or {}
    if not isinstance(payload, dict):
        payload = {}
    icon = SKILL_ICON_MAP.get(skill_key.lower(), "•")
    pretty = skill_key.replace("_", " ").title()
    embed = discord.Embed(
        title=f"{clan_name} — {pretty}",
        description=f"Mode: **{game_mode}**",
        color=discord.Color.dark_gold(),
    )
    embed.add_field(name="Rank", value=f"#{format_number(payload.get('rank'))}", inline=True)
    embed.add_field(name="XP", value=format_number(payload.get("score")), inline=True)
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


def build_single_skill_embed(
    profile: Dict[str, Any],
    skill_key: str,
    xp_value: Any,
    xp_table: XPTable,
) -> discord.Embed:
    username = profile.get("username") or "Unknown player"
    pretty = skill_key.replace("_", " ").title()
    icon = SKILL_ICON_MAP.get(skill_key.lower(), "⬜")
    try:
        xp = float(xp_value)
    except (TypeError, ValueError):
        xp = 0.0
    level, bar = xp_table.progress(xp)
    current_xp = xp_table.xp_for_level(level)
    next_xp = xp_table.xp_for_level(level + 1)
    to_next = max(0, next_xp - xp) if next_xp > current_xp else 0

    embed = discord.Embed(
        title=f"{username} — {pretty}",
        description=f"{icon} {pretty}",
        color=member_color(username),
    )
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="XP", value=format_number(xp), inline=True)
    embed.add_field(name="Progress", value=bar, inline=False)
    if next_xp > current_xp:
        embed.add_field(
            name="To next level",
            value=format_number(to_next),
            inline=True,
        )
        embed.add_field(
            name="Next level at",
            value=format_number(next_xp),
            inline=True,
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


_CAMEL_SPLIT_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _humanize_identifier(value: str) -> str:
    if not value:
        return "Unknown"
    value = str(value).replace("_", " ").replace("-", " ")
    value = _CAMEL_SPLIT_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _add_chunked_lines(embed: discord.Embed, header: str, lines: List[str]) -> None:
    if not lines:
        embed.add_field(name=header, value="No data.", inline=False)
        return
    chunk: List[str] = []
    chunk_len = 0
    idx = 1
    for line in lines:
        if chunk_len + len(line) + 1 > 900:
            suffix = f" ({idx})" if idx > 1 else ""
            embed.add_field(name=f"{header}{suffix}", value="\n".join(chunk), inline=False)
            idx += 1
            chunk = []
            chunk_len = 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        suffix = f" ({idx})" if idx > 1 else ""
        embed.add_field(name=f"{header}{suffix}", value="\n".join(chunk), inline=False)


def build_active_clans_embed(clans: List[Dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="Most Active Clans",
        description="Top clans by recent activity.",
        color=discord.Color.gold(),
    )
    lines = []
    for idx, clan in enumerate(clans, start=1):
        name = clan.get("clanName") or "Unknown"
        score = clan.get("activityScore") or 0
        members = clan.get("memberCount") or 0
        lines.append(f"**{idx}. {name}** — Score: `{score:.1f}` ({members} members)")

    if not lines:
        embed.description = "No active clans found."
    else:
        embed.description = "\n".join(lines)

    embed.set_footer(text="Data from IdleClans API")
    return embed
