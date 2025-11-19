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


async def fetch_clan_recruitment(
    session: aiohttp.ClientSession, clan_name: str
) -> Dict[str, Any]:
    safe_name = quote(clan_name, safe="")
    url = f"{API_BASE}/Clan/recruitment/{safe_name}"
    async with session.get(url) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Clan not found"
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


async def fetch_market_latest(
    session: aiohttp.ClientSession, include_average: bool = False
) -> List[Dict[str, Any]]:
    url = f"{API_BASE}/PlayerMarket/items/prices/latest"
    params = {"includeAveragePrice": include_average}
    async with session.get(url, params=params) as resp:
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


async def fetch_clan_player_logs(
    session: aiohttp.ClientSession, clan_name: str, player_name: str, limit: int = 20
) -> List[Dict[str, Any]]:
    safe_clan = quote(clan_name, safe="")
    safe_player = quote(player_name, safe="")
    url = f"{API_BASE}/Clan/logs/clan/{safe_clan}/{safe_player}"
    async with session.get(url, params={"limit": limit}) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Logs not found"
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


async def fetch_player_logs(
    session: aiohttp.ClientSession, player_name: str, limit: int = 20
) -> List[Dict[str, Any]]:
    safe_player = quote(player_name, safe="")
    url = f"{API_BASE}/Player/clan-logs/{safe_player}"
    async with session.get(url, params={"limit": limit}) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Logs not found"
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


async def fetch_clancup_standings(
    session: aiohttp.ClientSession, clan_name: str
) -> Dict[str, Any]:
    safe_name = quote(clan_name, safe="")
    url = f"{API_BASE}/ClanCup/standings/{safe_name}"
    async with session.get(url) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Clan not found"
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


async def fetch_clancup_top_current(session: aiohttp.ClientSession) -> Dict[str, Any]:
    url = f"{API_BASE}/ClanCup/top-clans/current"
    async with session.get(url) as resp:
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


async def fetch_chat_recent(session: aiohttp.ClientSession) -> Dict[str, List[Dict[str, Any]]]:
    url = f"{API_BASE}/Chat/recent"
    async with session.get(url) as resp:
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


async def fetch_market_latest_item(
    session: aiohttp.ClientSession, item_id: int, include_average: bool = False
) -> Dict[str, Any]:
    url = f"{API_BASE}/PlayerMarket/items/prices/latest/{item_id}"
    params = {"includeAveragePrice": include_average}
    async with session.get(url, params=params) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Item not found"
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


async def fetch_market_comprehensive_item(
    session: aiohttp.ClientSession, item_id: int
) -> Dict[str, Any]:
    url = f"{API_BASE}/PlayerMarket/items/prices/latest/comprehensive/{item_id}"
    async with session.get(url) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Item not found"
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


async def fetch_market_price_history(
    session: aiohttp.ClientSession, item_id: int, period: str = "7d"
) -> List[Dict[str, Any]]:
    url = f"{API_BASE}/PlayerMarket/items/prices/history/{item_id}"
    async with session.get(url, params={"period": period}) as resp:
        if resp.status == 404:
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=404, message="Item not found"
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


async def fetch_market_value_history(
    session: aiohttp.ClientSession, period: str = "7d", limit: int = 10
) -> List[Dict[str, Any]]:
    url = f"{API_BASE}/PlayerMarket/items/prices/history/value"
    async with session.get(url, params={"period": period, "limit": limit}) as resp:
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


async def fetch_market_volume_history(
    session: aiohttp.ClientSession, period: str = "7d", limit: int = 10
) -> List[Dict[str, Any]]:
    url = f"{API_BASE}/PlayerMarket/items/volume/history"
    async with session.get(url, params={"period": period, "limit": limit}) as resp:
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
