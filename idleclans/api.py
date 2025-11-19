from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote

import aiohttp

from .utils import API_BASE


async def fetch_clan_logs(
    session: aiohttp.ClientSession, clan_name: str, limit: int
) -> List[Dict[str, Any]]:
    safe_name = quote(clan_name, safe="")
    url = f"{API_BASE}/Clan/logs/clan/{safe_name}"
    async with session.get(url, params={"limit": limit}) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Clan not found"
            )
        resp.raise_for_status()
        data = await resp.json()
        if not isinstance(data, list):
            raise aiohttp.ClientResponseError(
                resp.request_info,
                resp.history,
                status=500,
                message="Unexpected IdleClans response format",
            )
        return data


async def fetch_player_profile(
    session: aiohttp.ClientSession, player_name: str
) -> Dict[str, Any]:
    safe_name = quote(player_name, safe="")
    url = f"{API_BASE}/Player/profile/{safe_name}"
    async with session.get(url) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Player not found"
            )
        resp.raise_for_status()
        data = await resp.json()
        if not isinstance(data, dict):
            raise aiohttp.ClientResponseError(
                resp.request_info,
                resp.history,
                status=500,
                message="Unexpected IdleClans response format",
            )
        return data
