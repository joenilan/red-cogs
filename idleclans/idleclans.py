import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

API_BASE = "https://query.idleclans.com/api"
DEFAULT_CLAN = "TheCoalition"
DEFAULT_LIMIT = 100
DEFAULT_INTERVAL = 120
RECENT_CACHE_LIMIT = 200
POLL_LOOP_SLEEP = 20


def _entry_identity(entry: Dict[str, Any]) -> str:
    return "|".join(
        [
            entry.get("clanName") or "",
            entry.get("memberUsername") or "",
            entry.get("timestamp") or "",
            entry.get("message") or "",
        ]
    )


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
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


def _format_timestamp(value: Optional[str]) -> str:
    parsed = _parse_timestamp(value)
    if parsed:
        return parsed.isoformat(sep=" ", timespec="seconds")
    return value or "Unknown time"


def _member_color(member: str) -> discord.Color:
    if not member:
        return discord.Color.blurple()
    digest = hashlib.sha1(member.lower().encode("utf-8")).digest()
    value = int.from_bytes(digest[:3], "big")
    return discord.Color(value=value)


class IdleClans(commands.Cog):
    """Poll IdleClans clan logs and relay new events to Discord channels."""

    __author__ = "DreadedZombie"
    __version__ = "1.0.0"

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

    async def cog_load(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(timeout=timeout)
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

    async def _process_guild(self, guild: discord.Guild, settings: Dict[str, Any], *, send_history: bool = False) -> None:
        session = self.session
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
            entries = await self._fetch_clan_logs(session, clan_name, limit)
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
            new_entries = sorted(entries, key=lambda e: _parse_timestamp(e.get("timestamp")) or datetime.min)
            new_cache = (recent_ids + [_entry_identity(e) for e in new_entries])[-RECENT_CACHE_LIMIT:]
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
        ordered = sorted(entries, key=lambda e: _parse_timestamp(e.get("timestamp")) or datetime.min)
        fresh: List[Dict[str, Any]] = []
        cache = list(recent_ids or [])
        for entry in ordered:
            identity = _entry_identity(entry)
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
            timestamp = _format_timestamp(entry.get("timestamp"))
            timestamp_dt = _parse_timestamp(entry.get("timestamp"))
            member = entry.get("memberUsername") or "Unknown member"
            message = entry.get("message") or "No additional details."
            embed = discord.Embed(
                description=message,
                color=_member_color(member),
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

    async def _fetch_clan_logs(
        self, session: aiohttp.ClientSession, clan_name: str, limit: int
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

    @commands.group(name="idleclans")
    @commands.guild_only()
    async def idleclans_group(self, ctx: commands.Context) -> None:
        """Configure IdleClans event relays."""

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
    async def idleclans_postnow(self, ctx: commands.Context, include_history: Optional[bool] = False) -> None:
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

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    async def _warm_cache(self, guild: discord.Guild) -> None:
        session = self.session
        if not session:
            return
        data = await self.config.guild(guild).all()
        clan_name: str = data.get("clan_name") or DEFAULT_CLAN
        limit = data.get("request_limit", DEFAULT_LIMIT)
        try:
            entries = await self._fetch_clan_logs(session, clan_name, limit)
        except aiohttp.ClientError as exc:
            self.log.warning("Failed to warm cache for %s: %s", guild.id, exc)
            return
        cache = [_entry_identity(e) for e in entries][-RECENT_CACHE_LIMIT:]
        await self.config.guild(guild).recent_ids.set(cache)
