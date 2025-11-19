from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from .api import (
    fetch_chat_recent,
    fetch_clan_logs,
    fetch_clan_player_logs,
    fetch_clan_recruitment,
    fetch_clancup_standings,
    fetch_clancup_top_current,
    fetch_market_latest,
    fetch_market_value_history,
    fetch_market_volume_history,
    fetch_player_logs,
    fetch_player_profile,
)
from .embeds import (
    build_chat_recent_embed,
    build_clancup_clan_embed,
    build_clancup_top_embed,
    build_clanhistory_embed,
    build_player_embed,
    build_recruitment_embed,
    build_skills_embed,
)
from .market import (
    build_market_item_embed,
    build_market_movers_embed,
    get_item_market_snapshot,
)
from .utils import (
    DEFAULT_CLAN,
    DEFAULT_INTERVAL,
    DEFAULT_LIMIT,
    POLL_LOOP_SLEEP,
    RECENT_CACHE_LIMIT,
    XPTable,
    entry_identity,
    format_timestamp,
    load_xp_table,
    member_color,
    parse_timestamp,
)


class IdleClans(commands.Cog):
    """Poll IdleClans clan logs and relay new events to Discord channels."""

    __author__ = "DreadedZombie"
    __version__ = "1.1.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.log = logging.getLogger("red.idleclans")
        self.config = Config.get_conf(self, identifier=9876543210, force_registration=True)
        default_guild = {
            "enabled": False,
            "clan_name": DEFAULT_CLAN,
            "output_channel": None,
            "poll_interval": DEFAULT_INTERVAL,
            "request_limit": DEFAULT_LIMIT,
            "recent_ids": [],
        }
        self.config.register_guild(**default_guild)
        self.session: Optional[aiohttp.ClientSession] = None
        self.poll_task: Optional[asyncio.Task] = None
        self._last_poll: Dict[int, float] = {}
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        data_dir = Path(__file__).with_name("data")
        self._xp_table: XPTable = load_xp_table(data_dir.joinpath("xp_table.csv"))
        self._market_cache: Dict[str, Any] = {"items": [], "timestamp": 0}

    async def cog_load(self) -> None:
        self._ensure_session()
        if not self.poll_task or self.poll_task.done():
            self.poll_task = asyncio.create_task(self._poll_loop())

    def cog_unload(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())

    async def _poll_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.log.exception("IdleClans poll loop error: %s", exc)
            await asyncio.sleep(POLL_LOOP_SLEEP)

    async def _poll_once(self) -> None:
        now = asyncio.get_running_loop().time()
        for guild in self.bot.guilds:
            settings = await self.config.guild(guild).all()
            if not settings.get("enabled"):
                continue
            channel_id = settings.get("output_channel")
            if not channel_id:
                continue
            interval = max(30, settings.get("poll_interval", DEFAULT_INTERVAL))
            last_polled = self._last_poll.get(guild.id, 0)
            if now - last_polled < interval:
                continue
            lock = self._get_lock(guild.id)
            async with lock:
                await self._process_guild(guild, settings)
            self._last_poll[guild.id] = now

    async def _process_guild(
        self, guild: discord.Guild, settings: Dict[str, Any], *, send_history: bool = False
    ) -> None:
        session = self._ensure_session()
        if not session:
            return
        channel_id = settings.get("output_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            self.log.warning("Configured channel for guild %s is invalid.", guild.id)
            await self.config.guild(guild).output_channel.set(None)
            return
        me = guild.me
        if me and not channel.permissions_for(me).send_messages:
            self.log.warning("Missing send permissions in #%s (%s)", channel.name, guild.name)
            return
        clan_name: str = settings.get("clan_name") or DEFAULT_CLAN
        limit = max(5, min(200, settings.get("request_limit", DEFAULT_LIMIT)))
        try:
            entries = await fetch_clan_logs(session, clan_name, limit)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                await channel.send(
                    f"IdleClans: Clan `{clan_name}` was not found. Disable the relay or update the clan name.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                self.log.warning("IdleClans API error (%s): %s", exc.status, exc.message)
            return
        except aiohttp.ClientError as exc:
            self.log.warning("IdleClans network error for %s: %s", guild.id, exc)
            return

        recent_ids = settings.get("recent_ids") or []
        if send_history:
            new_entries = sorted(
                entries, key=lambda e: parse_timestamp(e.get("timestamp")) or datetime.min
            )
            new_cache = (recent_ids + [entry_identity(e) for e in new_entries])[
                -RECENT_CACHE_LIMIT:
            ]
        else:
            new_entries, new_cache = self._filter_entries(entries, recent_ids)
        if not new_entries:
            return
        await self._send_entries(channel, clan_name, new_entries)
        await self.config.guild(guild).recent_ids.set(new_cache)

    def _filter_entries(
        self, entries: List[Dict[str, Any]], recent_ids: List[str]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        seen = set(recent_ids or [])
        ordered = sorted(
            entries, key=lambda e: parse_timestamp(e.get("timestamp")) or datetime.min
        )
        fresh: List[Dict[str, Any]] = []
        cache = list(recent_ids or [])
        for entry in ordered:
            identity = entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            cache.append(identity)
            fresh.append(entry)
        return fresh, cache[-RECENT_CACHE_LIMIT:]

    async def _send_entries(
        self, channel: discord.TextChannel, clan_name: str, entries: List[Dict[str, Any]]
    ) -> None:
        for entry in entries:
            timestamp = format_timestamp(entry.get("timestamp"))
            timestamp_dt = parse_timestamp(entry.get("timestamp"))
            member = entry.get("memberUsername") or "Unknown member"
            message = entry.get("message") or "No additional details."
            embed = discord.Embed(
                description=message,
                color=member_color(member),
                timestamp=timestamp_dt,
            )
            embed.set_author(name=member)
            embed.add_field(name="When", value=timestamp, inline=False)
            embed.set_footer(text=f"{clan_name} via IdleClans")
            try:
                await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                self.log.warning("Failed to send IdleClans entry to %s: %s", channel.id, exc)
                break
            await asyncio.sleep(0.3)

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def _ensure_session(self) -> Optional[aiohttp.ClientSession]:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    @commands.group(name="idleclans")
    @commands.guild_only()
    async def idleclans_group(self, ctx: commands.Context) -> None:
        """Configure IdleClans event relays."""

    @idleclans_group.command(name="help")
    async def idleclans_help(self, ctx: commands.Context) -> None:
        """Show IdleClans-specific help."""
        await ctx.send(self._build_help_text(ctx.clean_prefix))

    @idleclans_group.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel]
    ) -> None:
        """Set the channel where clan events are posted."""
        if channel is None:
            await self.config.guild(ctx.guild).output_channel.set(None)
            await ctx.send("IdleClans output channel cleared.")
            return
        await self.config.guild(ctx.guild).output_channel.set(channel.id)
        await ctx.send(f"IdleClans events will post in {channel.mention}.")

    @idleclans_group.command(name="clan")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_clan(self, ctx: commands.Context, *, clan_name: str) -> None:
        """Update the clan name to monitor."""
        clan_name = clan_name.strip()
        if not clan_name:
            await ctx.send("Provide a valid clan name.")
            return
        await self.config.guild(ctx.guild).clan_name.set(clan_name)
        await ctx.send(f"IdleClans will monitor `{clan_name}`.")

    @idleclans_group.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set how often the API is polled (seconds)."""
        seconds = max(30, min(900, seconds))
        await self.config.guild(ctx.guild).poll_interval.set(seconds)
        await ctx.send(f"IdleClans poll interval set to {seconds} seconds.")

    @idleclans_group.command(name="limit")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_limit(self, ctx: commands.Context, limit: int) -> None:
        """Set how many log entries to request per poll."""
        limit = max(5, min(200, limit))
        await self.config.guild(ctx.guild).request_limit.set(limit)
        await ctx.send(f"IdleClans will request up to {limit} logs per poll.")

    @idleclans_group.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_enable(self, ctx: commands.Context) -> None:
        """Enable the IdleClans relay."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("IdleClans relay enabled. Existing logs will be cached to avoid spam.")
        lock = self._get_lock(ctx.guild.id)
        async with lock:
            await self._warm_cache(ctx.guild)

    @idleclans_group.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_disable(self, ctx: commands.Context) -> None:
        """Disable the IdleClans relay."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("IdleClans relay disabled.")

    @idleclans_group.command(name="status")
    async def idleclans_status(self, ctx: commands.Context) -> None:
        """Show the current configuration."""
        data = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(data.get("output_channel")) if data.get("output_channel") else None
        description = (
            f"Enabled: `{data.get('enabled')}`\n"
            f"Clan: `{data.get('clan_name')}`\n"
            f"Channel: {channel.mention if channel else '`Not set`'}\n"
            f"Poll interval: `{data.get('poll_interval')}s`\n"
            f"Request limit: `{data.get('request_limit')}`\n"
            f"Cached entries: `{len(data.get('recent_ids') or [])}`"
        )
        embed = discord.Embed(
            title="IdleClans relay status",
            description=description,
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @idleclans_group.command(name="postnow")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_postnow(
        self, ctx: commands.Context, include_history: Optional[bool] = False
    ) -> None:
        """Poll immediately. Pass `True` to include history."""
        settings = await self.config.guild(ctx.guild).all()
        await ctx.send("Polling IdleClans now...")
        lock = self._get_lock(ctx.guild.id)
        async with lock:
            await self._process_guild(ctx.guild, settings, send_history=bool(include_history))
        await ctx.send("IdleClans poll complete.")

    @idleclans_group.command(name="warmcache")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_warmcache(self, ctx: commands.Context) -> None:
        """Mark the current logs as seen without posting them."""
        lock = self._get_lock(ctx.guild.id)
        async with lock:
            await self._warm_cache(ctx.guild)
        await ctx.send("Current IdleClans logs cached. Future polls will only post new entries.")

    @idleclans_group.command(name="clearcache")
    @commands.admin_or_permissions(manage_guild=True)
    async def idleclans_clearcache(self, ctx: commands.Context) -> None:
        """Forget all cached log entries (next poll may repost history)."""
        await self.config.guild(ctx.guild).recent_ids.set([])
        await ctx.send("IdleClans cache cleared.")

    @commands.command(name="player", aliases=["p"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def idleclans_player(self, ctx: commands.Context, *, player_name: str) -> None:
        """Show IdleClans profile details for a player."""
        profile = await self._resolve_player_profile(ctx, player_name)
        if not profile:
            return
        embed = build_player_embed(profile, self._xp_table)
        await ctx.send(embed=embed)

    @commands.command(name="skills", aliases=["s"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def idleclans_skills(self, ctx: commands.Context, *, player_name: str) -> None:
        """List all skill experience values for a player."""
        profile = await self._resolve_player_profile(ctx, player_name)
        if not profile:
            return
        skills = profile.get("skillExperiences")
        if not skills:
            await ctx.send("IdleClans did not return any skill data for that player.")
            return
        embed = build_skills_embed(profile, skills, self._xp_table)
        await ctx.send(embed=embed)

    @commands.command(name="recruitment", aliases=["rec"])
    async def idleclans_recruitment(
        self, ctx: commands.Context, *, clan_name: Optional[str] = None
    ) -> None:
        """Show recruitment info for the configured clan (or a specific clan)."""
        target = (clan_name or (await self.config.guild(ctx.guild).clan_name())).strip()
        if not target:
            await ctx.send(
                "No clan configured. Set one with `idleclans clan <name>` or pass a name explicitly."
            )
            return
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        try:
            data = await fetch_clan_recruitment(session, target)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                await ctx.send(f"Clan `{target}` was not found.")
                return
            self.log.warning("IdleClans recruitment API error (%s): %s", exc.status, exc.message)
            await ctx.send("IdleClans API returned an error while fetching that clan.")
            return
        except aiohttp.ClientError as exc:
            self.log.warning("Network error while fetching recruitment info: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        embed = build_recruitment_embed(data)
        await ctx.send(embed=embed)

    @commands.group(name="market", aliases=["m"], invoke_without_command=True)
    async def market_group(self, ctx: commands.Context) -> None:
        """Market data commands."""

    @market_group.command(name="item", aliases=["i"])
    async def market_item(self, ctx: commands.Context, *, item_query: str) -> None:
        """Show detailed market info for an item."""
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        item_id = await self._resolve_item_id(session, item_query)
        if item_id is None:
            await ctx.send(
                "Item not found. Use the numeric item ID or ensure the name matches exactly."
            )
            return
        try:
            snapshot = await get_item_market_snapshot(session, item_id, period="7d")
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                await ctx.send("The IdleClans API could not find that item.")
                return
            self.log.warning("Market API error (%s): %s", exc.status, exc.message)
            await ctx.send("The IdleClans API returned an error while fetching the item.")
            return
        except aiohttp.ClientError as exc:
            self.log.warning("Network error while fetching market item: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        embed = build_market_item_embed(snapshot["latest"], snapshot["history"])
        await ctx.send(embed=embed)

    @market_group.command(name="movers", aliases=["mv"])
    async def market_movers(self, ctx: commands.Context, period: str = "7d") -> None:
        """Show top trades and volume movers for a period (1d/7d/30d/1y)."""
        period = period.lower()
        if period not in {"1d", "7d", "30d", "1y"}:
            await ctx.send("Period must be one of: 1d, 7d, 30d, 1y.")
            return
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        try:
            value_history = await fetch_market_value_history(session, period=period, limit=10)
            volume_history = await fetch_market_volume_history(session, period=period, limit=10)
        except aiohttp.ClientError as exc:
            self.log.warning("Market movers fetch error: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        embed = build_market_movers_embed(value_history, volume_history, period)
        await ctx.send(embed=embed)

    @commands.command(name="clanhistory", aliases=["ch"])
    async def idleclans_clanhistory(
        self, ctx: commands.Context, player_name: str, *, clan_name: Optional[str] = None
    ) -> None:
        """Show recent clan log entries for a player."""
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        clan_name = clan_name.strip() if clan_name else None
        use_global = False
        if not clan_name or clan_name.lower() in {"global", "all", "*"}:
            stored = await self.config.guild(ctx.guild).clan_name()
            if clan_name and clan_name.lower() in {"global", "all", "*"}:
                use_global = True
            elif stored:
                clan_name = stored
            else:
                use_global = True
        await ctx.typing()
        try:
            if use_global:
                entries = await fetch_player_logs(session, player_name, limit=20)
                scope = "Global logs"
            else:
                entries = await fetch_clan_player_logs(
                    session, clan_name, player_name, limit=20
                )
                scope = f"Clan {clan_name}"
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                await ctx.send("No logs were found for that query.")
                return
            self.log.warning("Clan history API error (%s): %s", exc.status, exc.message)
            await ctx.send("IdleClans API returned an error while fetching logs.")
            return
        except aiohttp.ClientError as exc:
            self.log.warning("Network error while fetching clan history: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        embed = build_clanhistory_embed(player_name, entries, scope)
        await ctx.send(embed=embed)

    @commands.group(name="clancup", aliases=["cc"], invoke_without_command=True)
    async def clancup_group(
        self, ctx: commands.Context, *, clan_name: Optional[str] = None
    ) -> None:
        """Show Clan Cup info for your clan (or a specific clan)."""
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        target = clan_name.strip() if clan_name else None
        if not target:
            target = await self.config.guild(ctx.guild).clan_name()
        if not target:
            await ctx.send("No clan configured. Pass a clan name to query.")
            return
        await ctx.typing()
        try:
            data = await fetch_clancup_standings(session, target)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                await ctx.send(f"Clan `{target}` not found in current Clan Cup.")
                return
            self.log.warning("Clan Cup API error (%s): %s", exc.status, exc.message)
            await ctx.send("IdleClans API returned an error while fetching standings.")
            return
        except aiohttp.ClientError as exc:
            self.log.warning("Network error while fetching Clan Cup info: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        embed = build_clancup_clan_embed(target, data)
        await ctx.send(embed=embed)

    @clancup_group.command(name="standings")
    async def clancup_standings(self, ctx: commands.Context) -> None:
        """Show the current top clans for each objective."""
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        try:
            data = await fetch_clancup_top_current(session)
        except aiohttp.ClientError as exc:
            self.log.warning("Error fetching Clan Cup standings: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        embed = build_clancup_top_embed(data)
        await ctx.send(embed=embed)

    @commands.group(name="chat", aliases=["c"])
    async def chat_group(self, ctx: commands.Context) -> None:
        """Chat utilities."""

    @chat_group.command(name="recent")
    async def chat_recent(
        self, ctx: commands.Context, channel: str = "General", count: int = 20
    ) -> None:
        """Show recent in-game chat messages."""
        channel = channel.strip()
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        try:
            data = await fetch_chat_recent(session)
        except aiohttp.ClientError as exc:
            self.log.warning("Chat fetch error: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        key = None
        for candidate in data.keys():
            if candidate.lower() == channel.lower():
                key = candidate
                break
        if not key:
            await ctx.send(f"Channel `{channel}` not available.")
            return
        messages = data.get(key) or []
        messages = messages[: max(1, min(50, count))]
        embed = build_chat_recent_embed(key, messages)
        await ctx.send(embed=embed)

    def _build_help_text(self, prefix: str) -> str:
        return (
            "IdleClans commands:\n"
            f"- `{prefix}player <name>` / `{prefix}skills <name>` — Player profiles & skill grids.\n"
            f"- `{prefix}recruitment [clan]` — Recruitment info for this guild’s clan (or a named clan).\n"
            f"- `{prefix}market item <id|name>` / `{prefix}market movers [period]` — Market ladders and movers.\n"
            f"- `{prefix}clanhistory <player> [clan|global]` — Recent clan logs for a player.\n"
            f"- `{prefix}clancup [clan]` / `{prefix}clancup standings` — Clan Cup standings.\n"
            f"- `{prefix}chat recent [channel] [count]` — Recent game chat snapshots.\n"
            f"- `{prefix}idleclans enable|disable|channel|clan|status|postnow|warmcache|clearcache` — Relay controls."
        )

    async def format_help_for_context(self, ctx: commands.Context) -> str:
        return self._build_help_text(ctx.clean_prefix)

    async def _resolve_player_profile(
        self, ctx: commands.Context, player_name: str
    ) -> Optional[Dict[str, Any]]:
        subject = player_name.strip()
        if not subject:
            await ctx.send("Provide a player name to look up.")
            return None
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return None
        await ctx.typing()
        try:
            return await fetch_player_profile(session, subject)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                await ctx.send(f"No IdleClans profile found for `{subject}`.")
                return None
            self.log.warning("IdleClans player API error (%s): %s", exc.status, exc.message)
            await ctx.send("IdleClans API returned an error while fetching that player.")
            return None
        except aiohttp.ClientError as exc:
            self.log.warning("Network error while fetching IdleClans profile: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return None

    async def _warm_cache(self, guild: discord.Guild) -> None:
        session = self._ensure_session()
        if not session:
            return
        data = await self.config.guild(guild).all()
        clan_name: str = data.get("clan_name") or DEFAULT_CLAN
        limit = data.get("request_limit", DEFAULT_LIMIT)
        try:
            entries = await fetch_clan_logs(session, clan_name, limit)
        except aiohttp.ClientError as exc:
            self.log.warning("Failed to warm cache for %s: %s", guild.id, exc)
            return
        cache = [entry_identity(e) for e in entries][-RECENT_CACHE_LIMIT:]
        await self.config.guild(guild).recent_ids.set(cache)

    async def _get_market_items(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        now = time.time()
        if now - self._market_cache["timestamp"] > 300 or not self._market_cache["items"]:
            items = await fetch_market_latest(session, include_average=True)
            self._market_cache = {"items": items, "timestamp": now}
        return self._market_cache["items"]

    async def _resolve_item_id(
        self, session: aiohttp.ClientSession, query: str
    ) -> Optional[int]:
        query = query.strip()
        if query.isdigit():
            return int(query)
        items = await self._get_market_items(session)
        query_lower = query.lower()
        for item in items:
            name = str(item.get("itemName") or "").lower()
            if name == query_lower:
                return item.get("itemId")
        return None
