import asyncio
import hashlib
import logging
from bisect import bisect_right
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
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


def _format_number(value: Optional[float]) -> str:
    if value is None:
        return "0"
    try:
        if isinstance(value, float):
            value = int(round(value))
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _member_color(member: str) -> discord.Color:
    if not member:
        return discord.Color.blurple()
    digest = hashlib.sha1(member.lower().encode("utf-8")).digest()
    value = int.from_bytes(digest[:3], "big")
    return discord.Color(value=value)


def _top_stat_lines(
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
        lines.append(f"**{name}** — {level_text}{_format_number(value)}{suffix}")
    return "\n".join(lines)


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
        self._xp_thresholds, self._xp_levels = self._load_xp_table()

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

    async def _process_guild(self, guild: discord.Guild, settings: Dict[str, Any], *, send_history: bool = False) -> None:
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

    async def _fetch_player_profile(
        self, session: aiohttp.ClientSession, player_name: str
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

    def _build_player_embed(self, profile: Dict[str, Any]) -> discord.Embed:
        username = profile.get("username") or "Unknown player"
        embed = discord.Embed(
            title=f"{username}",
            color=_member_color(username),
        )
        clan_name = profile.get("guildName")
        if clan_name:
            embed.description = f"Member of **{clan_name}**"
        game_mode = profile.get("gameMode") or "Unknown"
        total_level = self._infer_total_level(profile.get("skillExperiences"))
        embed.add_field(name="Game mode", value=f"🎮 {game_mode.title()}", inline=True)
        embed.add_field(name="Total level", value=f"⭐ {_format_number(total_level)}", inline=True)
        hours_offline = profile.get("hoursOffline")
        offline_value = (
            f"{float(hours_offline):.1f}h" if isinstance(hours_offline, (int, float)) else "Unknown"
        )
        embed.add_field(name="Hours offline", value=f"⏱️ {offline_value}", inline=True)
        task_name = profile.get("taskNameOnLogout") or "Unknown"
        task_type = profile.get("taskTypeOnLogout")
        task_value = f"{task_name} · type {task_type}" if task_type is not None else task_name
        embed.add_field(name="Last task", value=f"📋 {task_value}", inline=False)

        skills = profile.get("skillExperiences") or {}
        skill_entries: List[str] = []
        for name, raw_xp in sorted(skills.items(), key=lambda item: float(item[1]), reverse=True):
            try:
                xp_val = float(raw_xp)
            except (TypeError, ValueError):
                continue
            icon = SKILL_ICON_MAP.get(name.lower(), "•")
            pretty = name.replace("_", " ").title()
            level = self._xp_to_level(xp_val)
            skill_entries.append(f"{icon} **{pretty}**\nLvl {level} · {_format_number(xp_val)} xp")
        if skill_entries:
            embed.add_field(name="Skill highlights", value="\u200b", inline=False)
            self._add_skill_grid_fields(embed, entries=skill_entries, columns=2, max_rows=3)

        pvm_stats = profile.get("pvmStats")
        pvm_lines = _top_stat_lines(pvm_stats, suffix=" kills", limit=3)
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

        embed.set_footer(text="Data from IdleClans API")
        return embed

    def _infer_total_level(self, skills: Optional[Dict[str, Any]]) -> int:
        if not skills:
            return 0
        total = 0
        for xp in skills.values():
            try:
                total += self._xp_to_level(float(xp))
            except (TypeError, ValueError):
                continue
        return total

    def _add_skill_grid_fields(
        self, embed: discord.Embed, *, entries: List[str], columns: int, max_rows: int
    ) -> None:
        column_chunks: List[List[str]] = [[] for _ in range(columns)]
        limit = columns * max_rows
        for idx, entry in enumerate(entries[:limit]):
            column_chunks[idx % columns].append(entry)
        for chunk in column_chunks:
            if not chunk:
                continue
            embed.add_field(
                name="\u200b",
                value="\n\n".join(chunk),
                inline=True,
            )
        remaining = len(entries) - limit
        if remaining > 0:
            embed.add_field(
                name="\u200b",
                value=f"...and {remaining} more skills",
                inline=False,
            )

    def _build_skills_embed(self, profile: Dict[str, Any], skills: Dict[str, Any]) -> discord.Embed:
        username = profile.get("username") or "Unknown player"
        embed = discord.Embed(
            title=f"{username}'s skills",
            color=_member_color(username),
        )
        sorted_skills = sorted(
            (skills or {}).items(), key=lambda item: float(item[1]), reverse=True
        )
        entries: List[str] = []
        for name, raw_value in sorted_skills:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            icon = SKILL_ICON_MAP.get(name.lower(), "•")
            pretty = name.replace("_", " ").title()
            level = self._xp_to_level(value)
            entries.append(f"{icon} **{pretty}**\nLvl {level} · {_format_number(value)} xp")
        if not entries:
            embed.description = "No skill data found."
            return embed
        columns = 3
        column_chunks: List[List[str]] = [[] for _ in range(columns)]
        for idx, entry in enumerate(entries):
            column_chunks[idx % columns].append(entry)
        for chunk in column_chunks:
            if not chunk:
                continue
            embed.add_field(
                name="\u200b",
                value="\n\n".join(chunk),
                inline=True,
            )
        embed.set_footer(text="Data from IdleClans API")
        return embed

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

    def _ensure_session(self) -> Optional[aiohttp.ClientSession]:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def _warm_cache(self, guild: discord.Guild) -> None:
        session = self._ensure_session()
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

    def _load_xp_table(self) -> Tuple[List[float], List[int]]:
        data_path = Path(__file__).with_name("data").joinpath("xp_table.csv")
        if not data_path.is_file():
            self.log.warning("XP table file %s missing; skill levels will not display.", data_path)
            return [0.0], [1]
        entries: List[Tuple[float, int]] = []
        with data_path.open("r", encoding="utf-8") as handle:
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
                entries.append((xp_val, level_val))
        if not entries:
            return [0.0], [1]
        entries.sort(key=lambda pair: pair[0])
        xp_thresholds = [pair[0] for pair in entries]
        level_values = [pair[1] for pair in entries]
        return xp_thresholds, level_values

    def _xp_to_level(self, xp_value: float) -> int:
        thresholds = self._xp_thresholds
        levels = self._xp_levels
        if not thresholds:
            return 0
        index = bisect_right(thresholds, xp_value) - 1
        if index < 0:
            index = 0
        if index >= len(levels):
            index = len(levels) - 1
        return levels[index]

    @commands.command(name="player")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def idleclans_player(self, ctx: commands.Context, *, player_name: str) -> None:
        """Show IdleClans profile details for a player."""
        profile = await self._resolve_player_profile(ctx, player_name)
        if not profile:
            return
        embed = self._build_player_embed(profile)
        await ctx.send(embed=embed)

    @commands.command(name="skills")
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
        embed = self._build_skills_embed(profile, skills)
        await ctx.send(embed=embed)

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
            return await self._fetch_player_profile(session, subject)
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
