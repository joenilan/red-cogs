from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    build_clan_bank_embed,
    build_clancup_clan_embed,
    build_clancup_top_embed,
    build_clanhistory_embed,
    build_player_embed,
    build_recruitment_embed,
    build_single_skill_embed,
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
    format_number,
    format_timestamp,
    load_xp_table,
    member_color,
    parse_timestamp,
)


BANK_EVENT_RE = re.compile(
    r"^(?P<actor>.+?)\s+(?P<action>added|withdrew)\s+(?P<amount>[\d,]+)x\s+(?P<item>.+?)\.$",
    re.IGNORECASE,
)


class IdleClans(commands.Cog):
    """Poll IdleClans clan logs and relay new events to Discord channels."""

    __author__ = "DreadedZombie"
    __version__ = "1.4.0"

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
            "market_watch_interval": 180,
            "market_watches": [],
            "log_watch_interval": 120,
            "log_watches": [],
            "last_log_timestamp": None,
            "summary_channel": None,
            "summary_enabled": False,
            "summary_interval_hours": 24,
            "summary_last_post": 0.0,
            "summary_limit": 50,
            "chat_relay_enabled": False,
            "chat_relay_channel": None,
            "chat_relay_source": "General",
            "chat_relay_interval": 120,
            "chat_relay_last": None,
        }
        self.config.register_guild(**default_guild)
        self.session: Optional[aiohttp.ClientSession] = None
        self.poll_task: Optional[asyncio.Task] = None
        self._last_poll: Dict[int, float] = {}
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        data_dir = Path(__file__).with_name("data")
        self._xp_table: XPTable = load_xp_table(data_dir.joinpath("xp_table.csv"))
        self._market_cache: Dict[str, Any] = {"items": [], "timestamp": 0}
        self._market_watch_task: Optional[asyncio.Task] = None
        self._chat_relay_task: Optional[asyncio.Task] = None
        self._summary_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        self._ensure_session()
        if not self.poll_task or self.poll_task.done():
            self.poll_task = asyncio.create_task(self._poll_loop())
        if not self._market_watch_task or self._market_watch_task.done():
            self._market_watch_task = asyncio.create_task(self._market_watch_loop())
        if not self._chat_relay_task or self._chat_relay_task.done():
            self._chat_relay_task = asyncio.create_task(self._chat_relay_loop())
        if not self._summary_task or self._summary_task.done():
            self._summary_task = asyncio.create_task(self._summary_loop())

    def cog_unload(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())
        if self._market_watch_task:
            self._market_watch_task.cancel()
        if self._chat_relay_task:
            self._chat_relay_task.cancel()
        if self._summary_task:
            self._summary_task.cancel()

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

    async def _market_watch_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                session = self._ensure_session()
                if not session:
                    await asyncio.sleep(60)
                    continue
                items = await self._get_market_items(session)
                items_by_id = {item.get("itemId"): item for item in items if item.get("itemId")}
                for guild in self.bot.guilds:
                    await self._process_market_watch_for_guild(guild, items_by_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Market watch loop error: %s", exc)
            intervals = []
            for guild in self.bot.guilds:
                try:
                    watches = await self.config.guild(guild).market_watches()
                except Exception:
                    continue
                if not watches:
                    continue
                interval = await self.config.guild(guild).market_watch_interval()
                intervals.append(interval)
            sleep_for = min(intervals) if intervals else 180
            await asyncio.sleep(max(60, sleep_for))

    async def _chat_relay_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            active_guilds: List[discord.Guild] = []
            try:
                for guild in self.bot.guilds:
                    enabled = await self.config.guild(guild).chat_relay_enabled()
                    channel_id = await self.config.guild(guild).chat_relay_channel()
                    if enabled and channel_id:
                        active_guilds.append(guild)
                if not active_guilds:
                    await asyncio.sleep(120)
                    continue
                session = self._ensure_session()
                if not session:
                    await asyncio.sleep(60)
                    continue
                data = await fetch_chat_recent(session)
                for guild in active_guilds:
                    await self._process_chat_relay_for_guild(guild, data)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Chat relay loop error: %s", exc)
                active_guilds = []
            intervals = []
            for guild in active_guilds:
                try:
                    interval = await self.config.guild(guild).chat_relay_interval()
                except Exception:  # noqa: BLE001
                    continue
                intervals.append(interval)
            sleep_for = min(intervals) if intervals else 150
            await asyncio.sleep(max(30, sleep_for))

    async def _summary_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            next_sleep_candidates: List[float] = []
            try:
                session = self._ensure_session()
                if not session:
                    await asyncio.sleep(60)
                    continue
                now = time.time()
                for guild in self.bot.guilds:
                    data = await self.config.guild(guild).all()
                    if not data.get("summary_enabled"):
                        continue
                    channel_id = data.get("summary_channel")
                    channel = guild.get_channel(channel_id) if channel_id else None
                    if not isinstance(channel, discord.TextChannel):
                        continue
                    me = guild.me
                    if me and not channel.permissions_for(me).send_messages:
                        continue
                    interval_hours = max(1, int(data.get("summary_interval_hours", 24)))
                    interval_seconds = interval_hours * 3600
                    last_post = float(data.get("summary_last_post") or 0.0)
                    elapsed = now - last_post if last_post else interval_seconds
                    if last_post and elapsed < interval_seconds:
                        next_sleep_candidates.append(interval_seconds - elapsed)
                        continue
                    clan_name = data.get("clan_name") or DEFAULT_CLAN
                    limit = max(10, min(100, int(data.get("summary_limit", 50))))
                    try:
                        entries = await fetch_clan_logs(session, clan_name, limit)
                    except aiohttp.ClientError as exc:
                        self.log.warning("Summary fetch error for %s: %s", guild.id, exc)
                        next_sleep_candidates.append(interval_seconds)
                        continue
                    embed = self._build_summary_embed(clan_name, entries[:limit])
                    try:
                        await channel.send(
                            embed=embed, allowed_mentions=discord.AllowedMentions.none()
                        )
                    except discord.HTTPException as exc:
                        self.log.warning(
                            "Failed to post summary to #%s (%s): %s", channel.id, guild.id, exc
                        )
                        next_sleep_candidates.append(interval_seconds)
                        continue
                    await self.config.guild(guild).summary_last_post.set(now)
                    next_sleep_candidates.append(interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Summary loop error: %s", exc)
            sleep_for = min(next_sleep_candidates) if next_sleep_candidates else 600
            await asyncio.sleep(max(60, min(900, sleep_for)))

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
        await self._process_log_watch_for_guild(guild, new_entries)
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

    @commands.group(name="idleclans", aliases=["ic", "idc"], invoke_without_command=True)
    @commands.guild_only()
    async def idleclans_group(self, ctx: commands.Context) -> None:
        """Configure IdleClans event relays."""
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=self._build_help_embed(ctx.clean_prefix))

    @idleclans_group.command(name="help")
    async def idleclans_help(self, ctx: commands.Context) -> None:
        """Show IdleClans-specific help."""
        await ctx.send(embed=self._build_help_embed(ctx.clean_prefix))

    @idleclans_group.command(name="summary")
    async def idleclans_summary(self, ctx: commands.Context, limit: int = 50) -> None:
        """Show a quick summary of recent clan log activity."""
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        clan_name = await self.config.guild(ctx.guild).clan_name() or DEFAULT_CLAN
        limit = max(10, min(100, limit))
        await ctx.typing()
        try:
            entries = await fetch_clan_logs(session, clan_name, limit)
        except aiohttp.ClientError as exc:
            self.log.warning("Summary fetch failed: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        embed = self._build_summary_embed(clan_name, entries)
        await ctx.send(embed=embed)

    @commands.command(name="skill", aliases=["sk"], hidden=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def idleclans_skill(
        self, ctx: commands.Context, player_name: str, *, skill_name: str
    ) -> None:
        """Show a single skill highlight for a player."""
        profile = await self._resolve_player_profile(ctx, player_name)
        if not profile:
            return
        skills = profile.get("skillExperiences") or {}
        match = self._match_skill_key(skill_name, skills)
        if not match:
            suggestions = ", ".join(list(skills.keys())[:6]) or "no skills returned"
            await ctx.send(f"Skill `{skill_name}` was not found. Try one of: {suggestions}")
            return
        xp_value = skills.get(match, 0)
        embed = build_single_skill_embed(profile, match, xp_value, self._xp_table)
        await ctx.send(embed=embed)

    @commands.command(name="bank", aliases=["clanbank"], hidden=True)
    async def idleclans_bank(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        """Summarize recent clan bank deposits and withdrawals."""
        if not ctx.guild:
            await ctx.send("This command can only be used inside a server.")
            return
        configured_clan = await self.config.guild(ctx.guild).clan_name()
        clan_name = configured_clan or DEFAULT_CLAN
        limit = 50
        query_text = (query or "").strip()
        if query_text:
            clan_name, limit = self._parse_bank_query(
                query_text, fallback_clan=clan_name, fallback_limit=limit
            )
        clan_name = (clan_name or "").strip()
        if not clan_name:
            await ctx.send(
                "No clan is configured for this server. Use `idleclans clan <name>` or pass a clan name."
            )
            return
        limit = max(10, min(200, limit))
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        try:
            entries = await fetch_clan_logs(session, clan_name, limit)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                await ctx.send(f"Clan `{clan_name}` was not found.")
                return
            self.log.warning("Bank logs API error (%s): %s", exc.status, exc.message)
            await ctx.send("IdleClans API returned an error while fetching bank entries.")
            return
        except aiohttp.ClientError as exc:
            self.log.warning("Network error while fetching bank entries: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        events = self._extract_bank_events(entries)
        if not events:
            await ctx.send(
                f"No deposits or withdrawals found in the last {len(entries)} clan logs."
            )
            return
        summary = self._summarize_bank_events(events, logs_analyzed=len(entries))
        embed = build_clan_bank_embed(clan_name, summary)
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _build_summary_embed(
        self, clan_name: str, entries: List[Dict[str, Any]]
    ) -> discord.Embed:
        join = sum("joined" in (entry.get("message") or "").lower() for entry in entries)
        leave = sum("left" in (entry.get("message") or "").lower() for entry in entries)
        bosses = sum("boss" in (entry.get("message") or "").lower() for entry in entries)
        embed = discord.Embed(
            title=f"{clan_name} recent summary",
            description=f"Last {len(entries)} log entries." if entries else "No activity.",
            color=discord.Color.dark_teal(),
        )
        embed.add_field(
            name="Overview",
            value=f"Joins: `{join}` | Leaves: `{leave}` | Boss mentions: `{bosses}`",
            inline=False,
        )
        preview = []
        for entry in entries[:10]:
            timestamp = format_timestamp(entry.get("timestamp"))
            preview.append(f"`{timestamp}` {entry.get('message')}")
        embed.add_field(
            name="Recent activity",
            value="\n".join(preview) or "No activity logged.",
            inline=False,
        )
        embed.set_footer(text="Data from IdleClans API")
        return embed

    def _match_skill_key(self, name: str, skills: Dict[str, Any]) -> Optional[str]:
        if not name or not skills:
            return None
        cleaned = name.replace(" ", "").replace("_", "").lower()
        for key in skills.keys():
            candidate = (key or "").replace(" ", "").replace("_", "").lower()
            if candidate == cleaned:
                return key
        for key in skills.keys():
            candidate = (key or "").replace(" ", "").replace("_", "").lower()
            if cleaned in candidate:
                return key
        return None

    def _parse_bank_query(
        self, query: str, *, fallback_clan: str, fallback_limit: int
    ) -> Tuple[str, int]:
        limit = fallback_limit
        clan = fallback_clan
        text = query
        limit_match = re.search(r"(?:limit|count)\s*=\s*(\d+)", text, flags=re.IGNORECASE)
        if limit_match:
            try:
                limit = int(limit_match.group(1))
            except ValueError:
                pass
            text = (text[: limit_match.start()] + text[limit_match.end() :]).strip()
        text = text.strip()
        if not text:
            return clan, limit
        if text.lower().startswith("clan="):
            candidate = text.split("=", 1)[1].strip()
            if candidate:
                clan = candidate
        else:
            clan = text
        return clan, limit

    def _extract_bank_events(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for entry in entries:
            message = (entry.get("message") or "").strip()
            if not message:
                continue
            match = BANK_EVENT_RE.match(message)
            if not match:
                continue
            amount_text = match.group("amount").replace(",", "")
            try:
                amount = int(amount_text)
            except ValueError:
                continue
            actor = entry.get("memberUsername") or match.group("actor").strip()
            action = match.group("action").lower()
            item = match.group("item").strip().rstrip(".")
            timestamp = entry.get("timestamp")
            events.append(
                {
                    "actor": actor,
                    "action": action,
                    "amount": amount,
                    "item": item,
                    "timestamp": timestamp,
                    "dt": parse_timestamp(timestamp) or datetime.min,
                }
            )
        events.sort(key=lambda evt: evt["dt"], reverse=True)
        return events

    def _summarize_bank_events(
        self, events: List[Dict[str, Any]], *, logs_analyzed: int
    ) -> Dict[str, Any]:
        summary = {
            "logs_analyzed": logs_analyzed,
            "event_total": len(events),
            "deposit_count": 0,
            "withdraw_count": 0,
            "deposit_units": 0,
            "withdraw_units": 0,
            "item_totals": [],
            "gold_positive": [],
            "gold_negative": [],
            "recent_events": events[:6],
        }
        if not events:
            return summary
        item_totals: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"added": 0, "withdrew": 0}
        )
        gold_balances: Dict[str, int] = defaultdict(int)
        for event in events:
            action = event.get("action")
            amount = event.get("amount", 0)
            if action == "added":
                summary["deposit_count"] += 1
                summary["deposit_units"] += amount
            else:
                summary["withdraw_count"] += 1
                summary["withdraw_units"] += amount
            item = event.get("item") or "Unknown"
            bucket = item_totals[item]
            bucket_key = action if action in {"added", "withdrew"} else "added"
            bucket[bucket_key] += amount
            if (event.get("item") or "").lower() == "gold":
                actor = event.get("actor") or "Unknown"
                if action == "added":
                    gold_balances[actor] += amount
                else:
                    gold_balances[actor] -= amount
        sorted_items = sorted(
            item_totals.items(),
            key=lambda kv: kv[1]["added"] + kv[1]["withdrew"],
            reverse=True,
        )
        summary["item_totals"] = sorted_items[:6]
        gold_positive = sorted(
            [(name, amt) for name, amt in gold_balances.items() if amt > 0],
            key=lambda pair: pair[1],
            reverse=True,
        )
        gold_negative = sorted(
            [(name, -amt) for name, amt in gold_balances.items() if amt < 0],
            key=lambda pair: pair[1],
            reverse=True,
        )
        summary["gold_positive"] = gold_positive[:5]
        summary["gold_negative"] = gold_negative[:5]
        summary["recent_events"] = events[:6]
        return summary

    @idleclans_group.group(name="summaryauto", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def summaryauto_group(self, ctx: commands.Context) -> None:
        """Configure automatic summary posts."""
        if ctx.invoked_subcommand is None:
            prefix = ctx.clean_prefix
            await ctx.send(
                f"Use `{prefix}idleclans summaryauto enable #channel [hours] [post_now]` "
                "to start scheduled summaries, or `summaryauto status` to inspect the current setup."
            )

    @summaryauto_group.command(name="enable")
    async def summaryauto_enable(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        hours: int = 24,
        post_now: bool = False,
    ) -> None:
        """Enable scheduled summary posts for this guild."""
        me = ctx.guild.me if ctx.guild else None
        if me and not channel.permissions_for(me).send_messages:
            await ctx.send(f"I cannot send messages in {channel.mention}.")
            return
        hours = max(1, min(168, hours))
        await self.config.guild(ctx.guild).summary_channel.set(channel.id)
        await self.config.guild(ctx.guild).summary_interval_hours.set(hours)
        await self.config.guild(ctx.guild).summary_enabled.set(True)
        await self.config.guild(ctx.guild).summary_last_post.set(0.0 if post_now else time.time())
        await ctx.send(
            f"Automatic summaries enabled for {channel.mention} every {hours}h. "
            + ("First summary will post shortly." if post_now else "Next post scheduled after the first interval.")
        )

    @summaryauto_group.command(name="disable")
    async def summaryauto_disable(self, ctx: commands.Context) -> None:
        """Disable scheduled summaries."""
        await self.config.guild(ctx.guild).summary_enabled.set(False)
        await ctx.send("Automatic IdleClans summaries disabled for this server.")

    @summaryauto_group.command(name="channel")
    async def summaryauto_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Update the channel used for scheduled summaries."""
        me = ctx.guild.me if ctx.guild else None
        if me and not channel.permissions_for(me).send_messages:
            await ctx.send(f"I cannot send messages in {channel.mention}.")
            return
        await self.config.guild(ctx.guild).summary_channel.set(channel.id)
        await ctx.send(f"Summary posts will use {channel.mention}.")

    @summaryauto_group.command(name="interval")
    async def summaryauto_interval(self, ctx: commands.Context, hours: int) -> None:
        """Change how many hours are between summary posts (1-168)."""
        hours = max(1, min(168, hours))
        await self.config.guild(ctx.guild).summary_interval_hours.set(hours)
        await ctx.send(f"Summary interval set to {hours} hours.")

    @summaryauto_group.command(name="limit")
    async def summaryauto_limit(self, ctx: commands.Context, limit: int) -> None:
        """Change how many log entries are summarized (10-100)."""
        limit = max(10, min(100, limit))
        await self.config.guild(ctx.guild).summary_limit.set(limit)
        await ctx.send(f"Summaries will consider the last {limit} log entries.")

    @summaryauto_group.command(name="status")
    async def summaryauto_status(self, ctx: commands.Context) -> None:
        """Show the current automatic summary configuration."""
        data = await self.config.guild(ctx.guild).all()
        enabled = data.get("summary_enabled")
        channel_id = data.get("summary_channel")
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        hours = data.get("summary_interval_hours", 24)
        limit = data.get("summary_limit", 50)
        last_post = float(data.get("summary_last_post") or 0.0)
        interval_seconds = max(1, int(hours)) * 3600
        if last_post:
            last_dt = datetime.utcfromtimestamp(last_post)
            last_text = last_dt.isoformat(sep=" ", timespec="seconds") + " UTC"
            next_run = last_post + interval_seconds
            if next_run <= time.time():
                next_text = "pending soon"
            else:
                next_dt = datetime.utcfromtimestamp(next_run)
                next_text = next_dt.isoformat(sep=" ", timespec="seconds") + " UTC"
        else:
            last_text = "never"
            next_text = "pending when enabled" if enabled else "n/a"
        embed = discord.Embed(title="Summary automation", color=discord.Color.dark_teal())
        embed.add_field(name="Enabled", value=str(enabled), inline=True)
        embed.add_field(
            name="Channel",
            value=channel.mention if isinstance(channel, discord.TextChannel) else "Not set",
            inline=True,
        )
        embed.add_field(name="Interval", value=f"{hours} hours", inline=True)
        embed.add_field(name="Entries", value=str(limit), inline=True)
        embed.add_field(name="Last post", value=last_text, inline=False)
        embed.add_field(name="Next post", value=next_text, inline=False)
        await ctx.send(embed=embed)

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

    @commands.command(name="player", aliases=["p"], hidden=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def idleclans_player(self, ctx: commands.Context, *, player_name: str) -> None:
        """Show IdleClans profile details for a player."""
        profile = await self._resolve_player_profile(ctx, player_name)
        if not profile:
            return
        embed = build_player_embed(profile, self._xp_table)
        await ctx.send(embed=embed)

    @commands.command(name="skills", aliases=["s"], hidden=True)
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

    @commands.command(name="recruitment", aliases=["rec"], hidden=True)
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

    @commands.group(name="market", aliases=["m"], invoke_without_command=True, hidden=True)
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

    @market_group.command(name="search", aliases=["find", "list"])
    async def market_search(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        """Search known market items by name."""
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        try:
            items = await self._get_market_items(session)
        except aiohttp.ClientError as exc:
            self.log.warning("Market search error: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        if not items:
            await ctx.send("No market data available yet. Try again shortly.")
            return
        if query:
            query_lower = query.strip().lower()
            matches = [
                item
                for item in items
                if query_lower in str(item.get("itemName") or "").lower()
            ]
        else:
            matches = items
        if not matches:
            await ctx.send(f"No items matched `{query}`.")
            return
        lines = []
        for item in matches[:15]:
            item_id = item.get("itemId")
            name = item.get("itemName") or "Unknown"
            avg = item.get("averagePrice") or item.get("averagePrices", {}).get("1d")
            lines.append(
                f"`{item_id}` — {name} (avg: {format_number(avg) if avg else 'n/a'})"
            )
        if len(matches) > 15:
            lines.append(f"...and {len(matches) - 15} more matches.")
        await ctx.send("\n".join(lines))

    @market_group.group(name="watch", invoke_without_command=True)
    async def market_watch_group(self, ctx: commands.Context) -> None:
        """Manage market price watches."""
        await ctx.send(
            f"Use `{ctx.clean_prefix}market watch add <item> <metric> <above|below> <price> [#channel]` "
            "to create a watch. Metrics: low, high, avg."
        )

    @market_watch_group.command(name="add")
    async def market_watch_add(
        self,
        ctx: commands.Context,
        item_query: str,
        metric: str,
        condition: str,
        target: float,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Add a new market watch (default delivery is DM)."""
        metric = metric.lower()
        if metric not in {"low", "high", "avg"}:
            await ctx.send("Metric must be one of: low, high, avg.")
            return
        condition = condition.lower()
        if condition not in {"above", "below"}:
            await ctx.send("Condition must be `above` or `below`.")
            return
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        item_id = await self._resolve_item_id(session, item_query)
        if item_id is None:
            await ctx.send("Item was not found. Use `!market search <text>` to find IDs.")
            return
        items = await self._get_market_items(session)
        item_name = next(
            (item.get("itemName") for item in items if item.get("itemId") == item_id),
            f"Item {item_id}",
        )
        watches = await self.config.guild(ctx.guild).market_watches()
        next_id = 1 + max((watch.get("id", 0) for watch in watches), default=0)
        watch = {
            "id": next_id,
            "item_id": int(item_id),
            "item_name": item_name,
            "metric": metric,
            "condition": condition,
            "target": float(target),
            "scope": "channel" if channel else "dm",
            "destination": channel.id if channel else None,
            "owner": ctx.author.id,
            "last_triggered": False,
        }
        watches.append(watch)
        await self.config.guild(ctx.guild).market_watches.set(watches)
        destination = channel.mention if channel else "DM"
        await ctx.send(
            f"Watching **{item_name}** `{metric}` price {condition} {format_number(target)} "
            f"(ID {next_id}, delivery: {destination})."
        )

    @market_watch_group.command(name="list")
    async def market_watch_list(self, ctx: commands.Context) -> None:
        """List market watches configured for this guild."""
        watches = await self.config.guild(ctx.guild).market_watches()
        if not watches:
            await ctx.send("No market watches are active.")
            return
        lines = []
        for watch in watches[:20]:
            destination = (
                f"<#{watch.get('destination')}>" if watch.get("scope") == "channel" else "DM"
            )
            lines.append(
                f"ID {watch.get('id')}: {watch.get('item_name')} "
                f"({watch.get('metric')} {watch.get('condition')} {format_number(watch.get('target'))}) "
                f"→ {destination}"
            )
        if len(watches) > 20:
            lines.append(f"...and {len(watches) - 20} more.")
        await ctx.send("\n".join(lines))

    @market_watch_group.command(name="remove")
    async def market_watch_remove(self, ctx: commands.Context, watch_id: int) -> None:
        """Remove a market watch by ID."""
        watches = await self.config.guild(ctx.guild).market_watches()
        new_watches = []
        removed = False
        for watch in watches:
            if watch.get("id") == watch_id:
                if watch.get("owner") != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
                    new_watches.append(watch)
                    continue
                removed = True
                continue
            new_watches.append(watch)
        if not removed:
            await ctx.send("Watch not found or you do not have permission to remove it.")
            return
        await self.config.guild(ctx.guild).market_watches.set(new_watches)
        await ctx.send(f"Removed watch {watch_id}.")

    @market_watch_group.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def market_watch_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set how often the market watch background task runs."""
        seconds = max(60, min(1800, seconds))
        await self.config.guild(ctx.guild).market_watch_interval.set(seconds)
        await ctx.send(f"Market watch interval set to {seconds} seconds.")

    @commands.group(name="logwatch", invoke_without_command=True, hidden=True)
    async def logwatch_group(self, ctx: commands.Context) -> None:
        """Alerts when clan log entries contain specific text."""
        prefix = ctx.clean_prefix
        await ctx.send(
            f"Use `{prefix}logwatch add <text> [#channel]` to create alerts. "
            "Example: `!logwatch add Queost joined`."
        )

    @logwatch_group.command(name="add")
    async def logwatch_add(
        self, ctx: commands.Context, *, text: str
    ) -> None:
        """Add a new log entry watch."""
        text = text.strip()
        if not text:
            await ctx.send("Provide the text to watch for (e.g., a player name or phrase).")
            return
        channel = None
        if ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            text = text.replace(channel.mention, "").strip()
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        watches = await self.config.guild(ctx.guild).log_watches()
        if len(watches) >= 50:
            await ctx.send("You have reached the maximum of 50 log watches for this server.")
            return
        next_id = 1 + max((watch.get("id", 0) for watch in watches), default=0)
        watch = {
            "id": next_id,
            "text": text,
            "scope": "channel" if channel else "dm",
            "destination": channel.id if channel else None,
            "owner": ctx.author.id,
            "last_timestamp": None,
        }
        watches.append(watch)
        await self.config.guild(ctx.guild).log_watches.set(watches)
        destination = channel.mention if channel else "DM"
        await ctx.send(f"Watching for `{text}` (watch ID {next_id}, delivery: {destination}).")

    @logwatch_group.command(name="list")
    async def logwatch_list(self, ctx: commands.Context) -> None:
        """List log watches."""
        watches = await self.config.guild(ctx.guild).log_watches()
        if not watches:
            await ctx.send("No log watches are configured.")
            return
        lines = []
        for watch in watches[:20]:
            destination = (
                f"<#{watch.get('destination')}>" if watch.get("scope") == "channel" else "DM"
            )
            lines.append(f"ID {watch.get('id')}: `{watch.get('text')}` → {destination}")
        if len(watches) > 20:
            lines.append(f"...and {len(watches) - 20} more.")
        await ctx.send("\n".join(lines))

    @logwatch_group.command(name="remove")
    async def logwatch_remove(self, ctx: commands.Context, watch_id: int) -> None:
        """Remove a log watch by ID."""
        watches = await self.config.guild(ctx.guild).log_watches()
        new_watches = []
        removed = False
        for watch in watches:
            if watch.get("id") == watch_id:
                if watch.get("owner") != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
                    new_watches.append(watch)
                    continue
                removed = True
                continue
            new_watches.append(watch)
        if not removed:
            await ctx.send("Watch not found or you do not have permission to remove it.")
            return
        await self.config.guild(ctx.guild).log_watches.set(new_watches)
        await ctx.send(f"Removed log watch {watch_id}.")

    @commands.command(name="clanhistory", aliases=["ch"], hidden=True)
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

    @commands.group(name="clancup", aliases=["cc"], invoke_without_command=True, hidden=True)
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

    @commands.group(name="chat", aliases=["c"], hidden=True)
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

    @chat_group.group(name="relay")
    async def chat_relay_group(self, ctx: commands.Context) -> None:
        """Configure automatic chat relays."""
        if ctx.invoked_subcommand is None:
            prefix = ctx.clean_prefix
            await ctx.send(
                f"Use `{prefix}chat relay enable #channel [source] [post_history]` to mirror in-game chat. "
                "Sources include General, Trade, Help, etc. Try `chat relay status` to inspect the current setup."
            )

    @chat_relay_group.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def chat_relay_enable(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        source: str = "General",
        post_history: bool = False,
    ) -> None:
        """Enable automatic relays into a Discord channel."""
        me = ctx.guild.me if ctx.guild else None
        if me and not channel.permissions_for(me).send_messages:
            await ctx.send(f"I cannot send messages in {channel.mention}.")
            return
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        try:
            chat_payload = await fetch_chat_recent(session)
        except aiohttp.ClientError as exc:
            self.log.warning("Chat relay enable fetch failed: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        canonical = self._match_chat_source(source, chat_payload.keys())
        if not canonical:
            await ctx.send(
                "That chat channel was not found. Available options: "
                + ", ".join(sorted(chat_payload.keys()))
            )
            return
        await self.config.guild(ctx.guild).chat_relay_enabled.set(True)
        await self.config.guild(ctx.guild).chat_relay_channel.set(channel.id)
        await self.config.guild(ctx.guild).chat_relay_source.set(canonical)
        if post_history:
            await self.config.guild(ctx.guild).chat_relay_last.set(None)
        else:
            latest_ts = self._latest_chat_timestamp(chat_payload.get(canonical) or [])
            await self.config.guild(ctx.guild).chat_relay_last.set(latest_ts)
        history_note = (
            "History will be replayed on the next pull." if post_history else "History skipped; only new messages will be relayed."
        )
        await ctx.send(
            f"Relaying IdleClans `{canonical}` chat into {channel.mention}. {history_note}"
        )

    @chat_relay_group.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def chat_relay_disable(self, ctx: commands.Context) -> None:
        """Disable the chat relay."""
        await self.config.guild(ctx.guild).chat_relay_enabled.set(False)
        await ctx.send("Chat relay disabled for this server.")

    @chat_relay_group.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def chat_relay_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set how often the relay checks for new chat messages."""
        seconds = max(30, min(600, seconds))
        await self.config.guild(ctx.guild).chat_relay_interval.set(seconds)
        await ctx.send(f"Chat relay interval set to {seconds} seconds.")

    @chat_relay_group.command(name="source")
    @commands.admin_or_permissions(manage_guild=True)
    async def chat_relay_source(self, ctx: commands.Context, *, source: str) -> None:
        """Change which in-game chat channel is relayed."""
        enabled = await self.config.guild(ctx.guild).chat_relay_enabled()
        if not enabled:
            await ctx.send("Enable the relay first with `chat relay enable`.")
            return
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        await ctx.typing()
        try:
            chat_payload = await fetch_chat_recent(session)
        except aiohttp.ClientError as exc:
            self.log.warning("Chat relay source fetch failed: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        canonical = self._match_chat_source(source, chat_payload.keys())
        if not canonical:
            await ctx.send(
                "That chat channel was not found. Available options: "
                + ", ".join(sorted(chat_payload.keys()))
            )
            return
        await self.config.guild(ctx.guild).chat_relay_source.set(canonical)
        latest_ts = self._latest_chat_timestamp(chat_payload.get(canonical) or [])
        await self.config.guild(ctx.guild).chat_relay_last.set(latest_ts)
        await ctx.send(f"Chat relay source updated to `{canonical}`.")

    @chat_relay_group.command(name="status")
    async def chat_relay_status(self, ctx: commands.Context) -> None:
        """Show the current chat relay configuration."""
        data = await self.config.guild(ctx.guild).all()
        channel_id = data.get("chat_relay_channel")
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        embed = discord.Embed(
            title="Chat relay status",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Enabled", value=str(data.get("chat_relay_enabled")), inline=True)
        embed.add_field(
            name="Channel",
            value=channel.mention if isinstance(channel, discord.TextChannel) else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Source",
            value=data.get("chat_relay_source") or "General",
            inline=True,
        )
        embed.add_field(
            name="Interval",
            value=f"{data.get('chat_relay_interval', 120)} seconds",
            inline=True,
        )
        last_seen = data.get("chat_relay_last") or "n/a"
        embed.add_field(name="Last timestamp", value=last_seen, inline=True)
        await ctx.send(embed=embed)

    @chat_relay_group.command(name="warm")
    @commands.admin_or_permissions(manage_guild=True)
    async def chat_relay_warm(self, ctx: commands.Context) -> None:
        """Skip all currently visible chat messages."""
        enabled = await self.config.guild(ctx.guild).chat_relay_enabled()
        if not enabled:
            await ctx.send("Enable the relay first with `chat relay enable`.")
            return
        session = self._ensure_session()
        if not session:
            await ctx.send("IdleClans session is not ready yet. Try again in a moment.")
            return
        source = await self.config.guild(ctx.guild).chat_relay_source() or "General"
        await ctx.typing()
        try:
            chat_payload = await fetch_chat_recent(session)
        except aiohttp.ClientError as exc:
            self.log.warning("Chat relay warm fetch failed: %s", exc)
            await ctx.send("Could not reach the IdleClans API. Try again shortly.")
            return
        canonical = self._match_chat_source(source, chat_payload.keys())
        if not canonical:
            await ctx.send("IdleClans did not return that chat channel. Reconfigure the relay.")
            return
        latest_ts = self._latest_chat_timestamp(chat_payload.get(canonical) or [])
        await self.config.guild(ctx.guild).chat_relay_last.set(latest_ts)
        await ctx.send("Chat relay cache warmed. New messages will post going forward.")

    def _build_help_embed(self, prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title="IdleClans command reference",
            description="Use the shorter aliases (shown in parentheses) where you like. "
            "Most commands default to this guild’s configured clan unless you pass one.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Player",
            value=(
                f"`{prefix}player <ign>` (`{prefix}p`) - profile summary\n"
                f"`{prefix}skills <ign>` (`{prefix}s`) - level grid with progress bars\n"
                f"`{prefix}skill <ign> <skill>` (`{prefix}sk`) - highlight one skill"
            ),
            inline=False,
        )
        embed.add_field(
            name="Clan intel",
            value=(
                f"`{prefix}recruitment [clan]` (`{prefix}rec`) - recruitment snapshot\n"
                f"`{prefix}clanhistory <ign> [clan|global]` (`{prefix}ch`) - recent logs\n"
                f"`{prefix}bank [clan] [limit=50]` (`{prefix}clanbank`) - recent bank deposits/withdrawals\n"
                f"`{prefix}clancup [clan]` / `{prefix}clancup standings` (`{prefix}cc`) - objective standings"
            ),
            inline=False,
        )
        embed.add_field(
            name="Market",
            value=(
                f"`{prefix}market item <id|name>` (`{prefix}m i`) - ladder + averages\n"
                f"`{prefix}market movers [period]` (`{prefix}m mv`) - value & volume leaders\n"
                f"`{prefix}market search <text>` (`{prefix}m search/list`) - browse item IDs\n"
                f"`{prefix}market watch add` / `list` / `remove` — price alerts"
            ),
            inline=False,
        )
        embed.add_field(
            name="Chat / relay",
            value=(
                f"`{prefix}chat recent [channel] [count]` (`{prefix}c`) - latest in-game chat messages\n"
                f"`{prefix}chat relay enable|disable|status` - mirror IdleClans chat into Discord"
            ),
            inline=False,
        )
        embed.add_field(
            name="Alerts",
            value=(
                f"`{prefix}logwatch add <text>` (`{prefix}logwatch list/remove`) - clan log alerts\n"
                f"`{prefix}market watch add` / `list` / `remove` - price alerts"
            ),
            inline=False,
        )
        embed.add_field(
            name="Reports",
            value=(
                f"`{prefix}idleclans summary [count]` - overview of recent clan logs\n"
                f"`{prefix}idleclans summaryauto enable|status|limit|interval|disable` - scheduled summaries"
            ),
            inline=False,
        )
        embed.add_field(
            name="Relay controls",
            value=(
                f"`{prefix}idleclans enable|disable`\n"
                f"`{prefix}idleclans channel #chan`\n"
                f"`{prefix}idleclans clan <name>`\n"
                f"`{prefix}idleclans status|postnow|warmcache|clearcache|interval|limit`"
            ),
            inline=False,
        )
        embed.set_footer(text="IdleClans cog • https://query.idleclans.com/api-docs/index.html")
        return embed

    async def format_help_for_context(self, ctx: commands.Context) -> str:
        return f"Use `{ctx.clean_prefix}idleclans help` for the full IdleClans command reference."

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

    async def _process_market_watch_for_guild(
        self, guild: discord.Guild, items_by_id: Dict[int, Dict[str, Any]]
    ) -> None:
        watches = await self.config.guild(guild).market_watches()
        if not watches:
            return
        changed = False
        for watch in watches:
            item_id = watch.get("item_id")
            item = items_by_id.get(item_id)
            value = self._get_metric_value(item, watch.get("metric"))
            target = watch.get("target")
            if value is None or target is None:
                continue
            condition = (watch.get("condition") or "below").lower()
            triggered = value < target if condition == "below" else value > target
            if triggered and not watch.get("last_triggered"):
                await self._notify_market_watch(guild, watch, value)
            if watch.get("last_triggered") != triggered:
                watch["last_triggered"] = triggered
                changed = True
        if changed:
            await self.config.guild(guild).market_watches.set(watches)

    def _get_metric_value(
        self, item: Optional[Dict[str, Any]], metric: Optional[str]
    ) -> Optional[float]:
        if not item or not metric:
            return None
        metric = metric.lower()
        if metric in {"low", "lowest"}:
            lowest = item.get("lowest") or {}
            return item.get("lowestPrice") or lowest.get("price")
        if metric in {"high", "highest"}:
            highest = item.get("highest") or {}
            return item.get("highestPrice") or highest.get("price")
        if metric in {"avg", "average"}:
            avg_prices = item.get("averagePrices") or {}
            return item.get("averagePrice") or avg_prices.get("1d")
        return None

    async def _notify_market_watch(
        self, guild: discord.Guild, watch: Dict[str, Any], value: float
    ) -> None:
        condition = watch.get("condition", "below")
        item_name = watch.get("item_name") or f"Item {watch.get('item_id')}"
        message = (
            f"Market watch triggered: **{item_name}** {watch.get('metric')} is "
            f"{format_number(value)} ({condition} {format_number(watch.get('target'))})."
        )
        scope = watch.get("scope", "dm")
        if scope == "channel":
            channel = self.bot.get_channel(watch.get("destination"))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(message)
                except discord.HTTPException:
                    pass
                return
        owner_id = watch.get("owner")
        if owner_id:
            member = guild.get_member(owner_id) or self.bot.get_user(owner_id)
            if member:
                try:
                    await member.send(message)
                except discord.HTTPException:
                    pass

    async def _process_log_watch_for_guild(
        self, guild: discord.Guild, entries: List[Dict[str, Any]]
    ) -> None:
        watches = await self.config.guild(guild).log_watches()
        if not watches:
            return
        changed = False
        for watch in watches:
            text = watch.get("text")
            if not text:
                continue
            text_lower = text.lower()
            for entry in entries:
                message = (entry.get("message") or "").lower()
                if text_lower in message:
                    await self._notify_log_watch(guild, watch, entry)
                    watch["last_timestamp"] = entry.get("timestamp")
                    changed = True
                    break
        if changed:
            await self.config.guild(guild).log_watches.set(watches)

    async def _notify_log_watch(
        self, guild: discord.Guild, watch: Dict[str, Any], entry: Dict[str, Any]
    ) -> None:
        timestamp = format_timestamp(entry.get("timestamp"))
        message = entry.get("message") or "No details provided."
        payload = f"[{timestamp}] {message}"
        scope = watch.get("scope", "dm")
        if scope == "channel":
            channel = self.bot.get_channel(watch.get("destination"))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(payload)
                except discord.HTTPException:
                    pass
                return
        owner_id = watch.get("owner")
        if owner_id:
            member = guild.get_member(owner_id) or self.bot.get_user(owner_id)
            if member:
                try:
                    await member.send(payload)
                except discord.HTTPException:
                    pass

    async def _process_chat_relay_for_guild(
        self, guild: discord.Guild, chat_payload: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        enabled = await self.config.guild(guild).chat_relay_enabled()
        channel_id = await self.config.guild(guild).chat_relay_channel()
        if not enabled or not channel_id:
            return
        source = (await self.config.guild(guild).chat_relay_source()) or "General"
        canonical = self._match_chat_source(source, chat_payload.keys())
        if not canonical:
            return
        messages = chat_payload.get(canonical)
        if not messages:
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        last_timestamp = await self.config.guild(guild).chat_relay_last()
        new_entries = []
        for msg in reversed(messages):
            ts = msg.get("timestamp") or msg.get("time") or msg.get("sentAt")
            if not ts:
                continue
            if last_timestamp and ts <= last_timestamp:
                continue
            new_entries.append(msg)
        if not new_entries:
            return
        for entry in new_entries:
            timestamp = format_timestamp(entry.get("timestamp") or entry.get("time"))
            author = entry.get("author") or entry.get("username") or entry.get("sender") or "Unknown"
            content = entry.get("message") or entry.get("content") or entry.get("text") or ""
            text = f"`{timestamp}` **{author}**: {content}"
            try:
                await channel.send(text)
            except discord.HTTPException:
                break
        await self.config.guild(guild).chat_relay_last.set(
            new_entries[-1].get("timestamp") or new_entries[-1].get("time")
        )

    @staticmethod
    def _match_chat_source(source: str, candidates: Iterable[str]) -> Optional[str]:
        if not source:
            return None
        needle = source.lower()
        for name in candidates:
            if name and name.lower() == needle:
                return name
        return None

    @staticmethod
    def _latest_chat_timestamp(messages: Iterable[Dict[str, Any]]) -> Optional[str]:
        latest_value: Optional[str] = None
        latest_dt: Optional[datetime] = None
        for entry in messages:
            candidate = entry.get("timestamp") or entry.get("time") or entry.get("sentAt")
            parsed = parse_timestamp(candidate)
            if not parsed:
                continue
            if not latest_dt or parsed > latest_dt:
                latest_dt = parsed
                latest_value = candidate
        return latest_value
