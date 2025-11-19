from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import discord

API_BASE = "https://query.idleclans.com/api"
DEFAULT_CLAN = "TheCoalition"
DEFAULT_LIMIT = 100
DEFAULT_INTERVAL = 120
RECENT_CACHE_LIMIT = 200
POLL_LOOP_SLEEP = 20

SKILL_ICON_MAP = {
    "attack": "⚔️",
    "strength": "💪",
    "defence": "🛡️",
    "archery": "🏹",
    "magic": "✨",
    "health": "❤️",
    "crafting": "✂️",
    "woodcutting": "🪓",
    "carpentry": "🪚",
    "fishing": "🎣",
    "cooking": "🍳",
    "mining": "⛏️",
    "smithing": "⚒️",
    "foraging": "🌿",
    "farming": "🌾",
    "agility": "🤸",
    "plundering": "💰",
    "enchanting": "🔮",
    "brewing": "🧪",
    "exterminating": "☠️",
}


def entry_identity(entry: Dict[str, Any]) -> str:
    return "|".join(
        [
            entry.get("clanName") or "",
            entry.get("memberUsername") or "",
            entry.get("timestamp") or "",
            entry.get("message") or "",
        ]
    )


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    formats = [
        "%m/%d/%Y %I:%M:%S %p",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_timestamp(value: Optional[str]) -> str:
    parsed = parse_timestamp(value)
    if parsed:
        return parsed.isoformat(sep=" ", timespec="seconds")
    return value or "Unknown time"


def format_number(value: Optional[float]) -> str:
    if value is None:
        return "0"
    try:
        if isinstance(value, float):
            value = int(round(value))
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def member_color(member: str) -> discord.Color:
    if not member:
        return discord.Color.blurple()
    digest = hashlib.sha1(member.lower().encode("utf-8")).digest()
    value = int.from_bytes(digest[:3], "big")
    return discord.Color(value=value)


def top_stat_lines(
    entries: Optional[Dict[str, Any]],
    *,
    suffix: str,
    limit: int = 5,
    xp_to_level: Optional[Callable[[float], int]] = None,
) -> str:
    if not entries:
        return "No data"
    sortable: List[Tuple[str, float]] = []
    for key, raw_value in entries.items():
        try:
            sortable.append((key, float(raw_value)))
        except (TypeError, ValueError):
            continue
    sortable.sort(key=lambda item: item[1], reverse=True)
    if not sortable:
        return "No data"
    lines = []
    for name, value in sortable[:limit]:
        level_text = ""
        if xp_to_level:
            level = xp_to_level(value)
            level_text = f"Lvl {level} · "
        lines.append(f"**{name}** — {level_text}{format_number(value)}{suffix}")
    return "\n".join(lines)


class XPTable:
    def __init__(
        self,
        thresholds: List[float],
        levels: List[int],
        level_to_xp: Dict[int, float],
    ) -> None:
        self._thresholds = thresholds or [0.0]
        self._levels = levels or [1]
        self._level_to_xp = level_to_xp or {1: 0.0}
        self._max_level = max(self._level_to_xp.keys())

    def level_from_xp(self, xp_value: float) -> int:
        from bisect import bisect_right

        index = bisect_right(self._thresholds, xp_value) - 1
        if index < 0:
            index = 0
        if index >= len(self._levels):
            index = len(self._levels) - 1
        return self._levels[index]

    def xp_for_level(self, level: int) -> float:
        if level <= 1:
            return 0.0
        level = min(max(level, 1), self._max_level)
        return self._level_to_xp.get(level, self._level_to_xp.get(self._max_level, 0.0))

    def progress(self, xp_value: float, segments: int = 10) -> Tuple[int, str]:
        level = self.level_from_xp(xp_value)
        current = self.xp_for_level(level)
        next_threshold = self.xp_for_level(level + 1)
        if next_threshold <= current:
            percent = 1.0
        else:
            percent = (xp_value - current) / (next_threshold - current)
            percent = max(0.0, min(1.0, percent))
        filled = round(percent * segments)
        filled = max(0, min(segments, filled))
        bar = "█" * filled + "░" * (segments - filled)
        return level, f"[{bar}] {percent*100:>4.1f}%"

    def total_level(self, skills: Optional[Dict[str, Any]]) -> int:
        if not skills:
            return 0
        total = 0
        for xp in skills.values():
            try:
                total += self.level_from_xp(float(xp))
            except (TypeError, ValueError):
                continue
        return total


def load_xp_table(path: Path) -> XPTable:
    if not path.is_file():
        return XPTable([0.0], [1], {1: 0.0})
    thresholds: List[float] = []
    levels: List[int] = []
    level_to_xp: Dict[int, float] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or "," not in line:
                continue
            level_str, xp_str = line.split(",", 1)
            try:
                level_val = int(level_str)
                xp_val = float(xp_str)
            except ValueError:
                continue
            thresholds.append(xp_val)
            levels.append(level_val)
            level_to_xp[level_val] = xp_val
    if not thresholds:
        thresholds = [0.0]
    if not levels:
        levels = [1]
    return XPTable(thresholds, levels, level_to_xp or {1: 0.0})
