from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord

from .api import (
    fetch_market_comprehensive_item,
    fetch_market_latest_item,
    fetch_market_price_history,
    fetch_market_value_history,
    fetch_market_volume_history,
)
from .utils import format_number


def find_item_id_by_name(items: Iterable[Dict[str, Any]], name: str) -> Optional[int]:
    name = name.lower().strip()
    for item in items:
        if str(item.get("name", "")).lower() == name:
            return item.get("itemId")
    return None


async def get_item_market_snapshot(
    session, item_id: int, period: str = "7d"
) -> Dict[str, Any]:
    latest = await fetch_market_comprehensive_item(session, item_id)
    history = await fetch_market_price_history(session, item_id, period=period)
    return {"latest": latest, "history": history}


def build_market_item_embed(
    item_data: Dict[str, Any], history: List[Dict[str, Any]]
) -> discord.Embed:
    item_name = item_data.get("itemName") or f"Item {item_data.get('itemId')}"
    embed = discord.Embed(
        title=f"Market snapshot: {item_name}",
        color=discord.Color.gold(),
    )
    averages = item_data.get("averagePrices") or {}
    embed.add_field(
        name="Averages",
        value="\n".join(
            f"{period}: {format_number(value)} coins"
            for period, value in averages.items()
            if value is not None
        )
        or "No average data",
        inline=False,
    )
    lowest = item_data.get("lowestPrices") or []
    highest = item_data.get("highestPrices") or []
    if lowest:
        embed.add_field(
            name="Top 5 lowest",
            value="\n".join(
                f"{format_number(entry.get('price'))} (x{entry.get('volume')})"
                for entry in lowest[:5]
            ),
            inline=True,
        )
    if highest:
        embed.add_field(
            name="Top 5 highest",
            value="\n".join(
                f"{format_number(entry.get('price'))} (x{entry.get('volume')})"
                for entry in highest[:5]
            ),
            inline=True,
        )
    if history:
        sparkline = build_sparkline([entry.get("averagePrice", 0) for entry in history])
        embed.add_field(name="Price trend", value=sparkline, inline=False)
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_market_movers_embed(
    value_history: List[Dict[str, Any]],
    volume_history: List[Dict[str, Any]],
    period: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Market movers ({period})",
        color=discord.Color.dark_teal(),
    )
    if value_history:
        embed.add_field(
            name="Most valuable trades",
            value="\n".join(
                f"{entry.get('itemName')} — {format_number(entry.get('price'))} coins (x{entry.get('quantity')})"
                for entry in value_history
            ),
            inline=False,
        )
    if volume_history:
        embed.add_field(
            name="Top volume items",
            value="\n".join(
                f"{entry.get('itemName')} — {format_number(entry.get('quantity'))} items"
                for entry in volume_history
            ),
            inline=False,
        )
    embed.set_footer(text="Data from IdleClans API")
    return embed


def build_sparkline(values: List[float], segments: int = 20) -> str:
    if not values:
        return "No data"
    min_val = min(values)
    max_val = max(values)
    if math.isclose(min_val, max_val):
        return "—" * segments
    scaled = [
        int((val - min_val) / (max_val - min_val) * (segments - 1)) for val in values[-segments:]
    ]
    glyphs = "▁▂▃▄▅▆▇█"
    return "".join(glyphs[min(len(glyphs) - 1, max(0, int(x / segments * (len(glyphs) - 1))))] for x in scaled)
