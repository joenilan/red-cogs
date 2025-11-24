from __future__ import annotations

import asyncio
import random
import re
import time
import contextlib
from typing import Any, Dict, List, Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from .embeds import build_raffle_embed, entrants_grid


def parse_duration(text: Optional[str]) -> int:
    if not text:
        return 3600
    text = text.strip().lower()
    if text in {"0", "forever", "none", "manual", "infinite"}:
        return 0
    match = re.match(r"(\d+)\s*([smhd]?)", text)
    if not match:
        return 3600
    value = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return max(30, value)
    if unit == "m":
        return max(30, value * 60)
    if unit == "h":
        return max(60, value * 3600)
    if unit == "d":
        return max(3600, value * 86400)
    return max(30, value)


class RaffleViewOpen(discord.ui.View):
    def __init__(self, cog: "Raffles", guild_id: int, raffle_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.raffle_id = raffle_id
        self._enter.custom_id = f"raffle:enter:{raffle_id}"
        self._enter.emoji = "🎟️"
        self._leave.custom_id = f"raffle:leave:{raffle_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(style=discord.ButtonStyle.success, label="Enter")
    async def _enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # type: ignore
        await self.cog.handle_enter(interaction, self.guild_id, self.raffle_id)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Leave")
    async def _leave(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # type: ignore
        await self.cog.handle_leave(interaction, self.guild_id, self.raffle_id)


class RaffleViewPendingPick(discord.ui.View):
    def __init__(self, cog: "Raffles", guild_id: int, raffle_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.raffle_id = raffle_id
        self._pick.custom_id = f"raffle:pick:{raffle_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(style=discord.ButtonStyle.success, label="Pick winners")
    async def _pick(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # type: ignore
        await self.cog.handle_pick_button(interaction, self.guild_id, self.raffle_id)


class RaffleViewClosed(discord.ui.View):
    def __init__(self, cog: "Raffles", guild_id: int, raffle_id: int, message_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.raffle_id = raffle_id
        self.message_id = message_id
        self._remove.custom_id = f"raffle:remove:{raffle_id}:{message_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(style=discord.ButtonStyle.danger, label="Remove", emoji="🗑️")
    async def _remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # type: ignore
        await self.cog.handle_remove_button(interaction, self.guild_id, self.raffle_id, self.message_id)


class RaffleHostView(discord.ui.View):
    def __init__(self, cog: "Raffles", guild_id: int, raffle_id: int, *, allow_pick: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.raffle_id = raffle_id
        self._end.custom_id = f"raffle:end:{raffle_id}"
        self._pick.custom_id = f"raffle:pick:{raffle_id}"
        if not allow_pick:
            self._pick.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(style=discord.ButtonStyle.danger, label="End now")
    async def _end(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # type: ignore
        await self.cog.handle_end_button(interaction, self.guild_id, self.raffle_id)

    @discord.ui.button(style=discord.ButtonStyle.success, label="Pick winners")
    async def _pick(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # type: ignore
        await self.cog.handle_pick_button(interaction, self.guild_id, self.raffle_id)


class Raffles(commands.Cog):
    """Simple button-only raffles/giveaways."""

    __author__ = "DreadedZombie"
    __version__ = "0.2.1"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)
        default_guild = {
            "raffles": {},
            "next_id": 1,
            "manager_role": None,
            "raffle_channel": None,
        }
        self.config.register_guild(**default_guild)
        self._loop: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        if not self._loop or self._loop.done():
            self._loop = asyncio.create_task(self._raffle_loop())
        await self._restore_views()

    def cog_unload(self) -> None:
        if self._loop:
            self._loop.cancel()

    @commands.group(name="raffle", aliases=["ra"], invoke_without_command=True)
    @commands.guild_only()
    async def raffle_group(self, ctx: commands.Context) -> None:
        """Manage raffles."""
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=self._build_help_embed(ctx.clean_prefix))

    @raffle_group.command(name="start", hidden=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def raffle_start(
        self,
        ctx: commands.Context,
        prize: str,
        duration: Optional[str] = "1h",
        max_winners: Optional[int] = 1,
        quantity: Optional[int] = None,
    ) -> None:
        """Start a raffle. Duration examples: 30m, 1h, 1d."""
        seconds = parse_duration(duration)
        max_winners = max(1, min(25, max_winners or 1))
        raffle_id = await self._next_id(ctx.guild.id)
        ends_at = int(time.time() + seconds) if seconds > 0 else None
        channel = await self._resolve_channel(ctx)
        if channel is None:
            await ctx.send("Raffle channel is not set and I cannot use this channel.", delete_after=10)
            await self._maybe_delete_command(ctx)
            return
        raffle = {
            "id": raffle_id,
            "title": f"Raffle #{raffle_id}",
            "prize": prize,
            "quantity": max(1, quantity) if quantity else None,
            "host_id": ctx.author.id,
            "channel_id": channel.id,
            "message_id": None,
            "thread_id": None,
            "thread_message_id": None,
            "max_winners": max_winners,
            "ends_at": ends_at,
            "entrants": [],
            "winners": [],
            "status": "open",
            "created_at": int(time.time()),
        }
        view = RaffleViewOpen(self, ctx.guild.id, raffle_id)
        embed = build_raffle_embed(raffle, ctx.guild)
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            await ctx.send("Could not post the raffle in the target channel.", delete_after=10)
            await self._maybe_delete_command(ctx)
            return
        raffle["message_id"] = message.id
        # Try to create a thread for archival/trophy; channel perms will govern visibility.
        thread_name = f"raffle-{raffle_id}-{prize}".replace(" ", "-")[:90]
        try:
            thread = await channel.create_thread(
                name=thread_name,
                message=message,
                auto_archive_duration=4320,  # 3 days
                type=discord.ChannelType.public_thread,
                reason="Raffle thread",
            )
            raffle["thread_id"] = thread.id
            # seed entrant list in thread
            thread_msg = await thread.send("No entrants yet.")
            raffle["thread_message_id"] = thread_msg.id
        except discord.HTTPException:
            pass
        await self._save_raffle(ctx.guild.id, raffle)
        await self._sync_thread_listing(ctx.guild, raffle)
        await self._ensure_host_controls(ctx.guild, raffle)
        await self._maybe_delete_command(ctx)

    @raffle_group.command(name="end", hidden=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def raffle_end(self, ctx: commands.Context, raffle_id: int) -> None:
        """Close a raffle immediately (no winners drawn)."""
        raffle = await self._get_raffle(ctx.guild.id, raffle_id)
        if not raffle:
            await ctx.send("Raffle not found.", delete_after=10)
            return
        if raffle.get("status") == "closed":
            await ctx.send("That raffle is already closed.", delete_after=10)
            return
        await self._close_raffle(ctx.guild, raffle, roll=False, delete_thread=True)
        await ctx.send(
            f"Raffle {raffle_id} closed (no winners drawn). Use `raffle pick {raffle_id}` to draw.",
            delete_after=15,
        )
        await self._maybe_delete_command(ctx)

    @raffle_group.command(name="pick", hidden=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def raffle_pick(self, ctx: commands.Context, raffle_id: int) -> None:
        """Draw winners for a closed raffle."""
        raffle = await self._get_raffle(ctx.guild.id, raffle_id)
        if not raffle:
            await ctx.send("Raffle not found.", delete_after=10)
            return
        if raffle.get("status") != "closed" or raffle.get("winners"):
            await ctx.send("Raffle is not pending a draw.", delete_after=10)
            return
        await self._roll_winners(ctx.guild, raffle)
        await ctx.send(f"Winners picked for raffle {raffle_id}.", delete_after=15)
        await self._maybe_delete_command(ctx)

    @raffle_group.command(name="list", hidden=True)
    async def raffle_list(self, ctx: commands.Context) -> None:
        """List active raffles."""
        raffles = await self.config.guild(ctx.guild).raffles()
        open_raffles = [r for r in raffles.values() if r.get("status") == "open"]
        if not open_raffles:
            await ctx.send("No active raffles.", delete_after=10)
            return
        lines = []
        for raffle in sorted(open_raffles, key=lambda r: r.get("id")):
            ends_at = raffle.get("ends_at")
            end_text = f"ends <t:{int(ends_at)}:R>" if ends_at else "manual close"
            lines.append(f"ID {raffle.get('id')}: {raffle.get('prize')} ({end_text})")
        await ctx.send("\n".join(lines), delete_after=15)
        await self._maybe_delete_command(ctx)

    @raffle_group.command(name="managerrole", hidden=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def raffle_manager_role(
        self, ctx: commands.Context, role: Optional[discord.Role] = None
    ) -> None:
        """Set/clear an extra role allowed to end/pick winners."""
        await self.config.guild(ctx.guild).manager_role.set(role.id if role else None)
        if role:
            await ctx.send(f"Manager role set to {role.mention}.", delete_after=10)
        else:
            await ctx.send("Manager role cleared.", delete_after=10)
        await self._maybe_delete_command(ctx)

    @raffle_group.command(name="channel", hidden=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def raffle_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set/clear the dedicated raffle channel."""
        await self.config.guild(ctx.guild).raffle_channel.set(channel.id if channel else None)
        if channel:
            await ctx.send(f"Raffles will post in {channel.mention}.", delete_after=10)
        else:
            await ctx.send("Raffle channel cleared. Current channel will be used when starting.", delete_after=10)
        await self._maybe_delete_command(ctx)

    @raffle_group.command(name="resetids", hidden=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def raffle_reset_ids(self, ctx: commands.Context) -> None:
        """Reset the raffle ID counter to 1 (for development/testing)."""
        await self.config.guild(ctx.guild).next_id.set(1)
        await ctx.send("Raffle IDs reset to 1.", delete_after=10)
        await self._maybe_delete_command(ctx)

    @raffle_group.command(name="setup", hidden=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def raffle_setup(self, ctx: commands.Context, *, name: str = "raffles") -> None:
        """Create a dedicated raffle channel (private by default) and store it."""
        guild = ctx.guild
        overwrite = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        try:
            channel = await guild.create_text_channel(name=name, overwrites=overwrite, reason="Raffle setup")
        except discord.HTTPException:
            await ctx.send("Could not create the raffle channel.", delete_after=10)
            await self._maybe_delete_command(ctx)
            return
        await self.config.guild(guild).raffle_channel.set(channel.id)
        await ctx.send(
            f"Raffle channel created at {channel.mention}. Adjust permissions when ready.",
            delete_after=15,
        )
        await self._maybe_delete_command(ctx)


    async def handle_enter(self, interaction: discord.Interaction, guild_id: int, raffle_id: int) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            raffle = await self._get_raffle(guild_id, raffle_id)
            if not raffle or raffle.get("status") != "open":
                await interaction.followup.send("This raffle is closed.", ephemeral=True)
                return
            user_id = interaction.user.id
            entrants: List[int] = raffle.get("entrants") or []
            if user_id in entrants:
                await interaction.followup.send("You are already entered.", ephemeral=True)
                return
            entrants.append(user_id)
            raffle["entrants"] = entrants
            await self._save_raffle(guild_id, raffle)
            guild = interaction.guild or self.bot.get_guild(guild_id)
            await self._refresh_message(guild_id, raffle)
            await self._sync_thread_listing(guild, raffle)
            await self._ensure_host_controls(guild, raffle)
            await interaction.followup.send("You have entered the raffle.", ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("Raffle enter handler failed: %s", exc)
            if interaction.response.is_done():
                with contextlib.suppress(Exception):
                    await interaction.followup.send("Something went wrong processing your entry.", ephemeral=True)
            else:
                with contextlib.suppress(Exception):
                    await interaction.response.send_message(
                        "Something went wrong processing your entry.", ephemeral=True
                    )

    async def handle_leave(self, interaction: discord.Interaction, guild_id: int, raffle_id: int) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            raffle = await self._get_raffle(guild_id, raffle_id)
            if not raffle or raffle.get("status") != "open":
                await interaction.followup.send("This raffle is closed.", ephemeral=True)
                return
            user_id = interaction.user.id
            entrants: List[int] = raffle.get("entrants") or []
            if user_id not in entrants:
                await interaction.followup.send("You are not entered.", ephemeral=True)
                return
            entrants = [uid for uid in entrants if uid != user_id]
            raffle["entrants"] = entrants
            await self._save_raffle(guild_id, raffle)
            guild = interaction.guild or self.bot.get_guild(guild_id)
            await self._refresh_message(guild_id, raffle)
            await self._sync_thread_listing(guild, raffle)
            await self._ensure_host_controls(guild, raffle)
            await interaction.followup.send("You have left the raffle.", ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("Raffle leave handler failed: %s", exc)
            if interaction.response.is_done():
                with contextlib.suppress(Exception):
                    await interaction.followup.send("Something went wrong processing your request.", ephemeral=True)
            else:
                with contextlib.suppress(Exception):
                    await interaction.response.send_message(
                        "Something went wrong processing your request.", ephemeral=True
                    )

    async def handle_end_button(
        self, interaction: discord.Interaction, guild_id: int, raffle_id: int
    ) -> None:
        raffle = await self._get_raffle(guild_id, raffle_id)
        if not raffle:
            await self._safe_interaction_reply(interaction, "Raffle not found.")
            return
        guild = interaction.guild or self.bot.get_guild(guild_id)
        allowed = await self._is_host_or_manager(interaction.user, guild, raffle.get("host_id"), guild_id)
        if not allowed:
            await self._safe_interaction_reply(
                interaction,
                "Only the host or a server manager can end this raffle.",
            )
            return
        if not guild:
            await self._safe_interaction_reply(interaction, "Guild not found.")
            return
        if not interaction.response.is_done():
            with contextlib.suppress(Exception):
                await interaction.response.defer(ephemeral=True)
        await self._close_raffle(guild, raffle, roll=False, delete_thread=True)
        await self._safe_interaction_reply(interaction, "Raffle closed. Use Pick winners to draw when ready.")

    async def handle_pick_button(
        self, interaction: discord.Interaction, guild_id: int, raffle_id: int
    ) -> None:
        raffle = await self._get_raffle(guild_id, raffle_id)
        guild = interaction.guild or self.bot.get_guild(guild_id)
        allowed = await self._is_host_or_manager(interaction.user, guild, raffle.get("host_id"), guild_id)
        if not allowed:
            await self._safe_interaction_reply(
                interaction, "Only the host or a server manager can pick winners."
            )
            return
        if not raffle:
            await self._safe_interaction_reply(interaction, "Raffle not found.")
            return
        status = raffle.get("status")
        if status == "closed" and raffle.get("winners"):
            await self._safe_interaction_reply(interaction, "Winners have already been picked for this raffle.")
            return
        if not guild:
            await self._safe_interaction_reply(interaction, "Guild not found.")
            return
        # If still open, close and roll; if closed without winners, just roll
        if not interaction.response.is_done():
            with contextlib.suppress(Exception):
                await interaction.response.defer(ephemeral=True)
        raffle["status"] = "closed"
        raffle["ends_at"] = None
        await self._roll_winners(guild, raffle, delete_thread=True)
        await self._safe_interaction_reply(interaction, "Winners picked.")

    async def handle_remove_button(
        self, interaction: discord.Interaction, guild_id: int, raffle_id: int, message_id: int
    ) -> None:
        raffle = await self._get_raffle(guild_id, raffle_id)
        guild = interaction.guild or self.bot.get_guild(guild_id)
        allowed = await self._is_host_or_manager(interaction.user, guild, raffle.get("host_id") if raffle else None, guild_id)
        if not allowed:
            await self._safe_interaction_reply(interaction, "Only the host or a server manager can remove this message.")
            return
        channel = None
        if raffle:
            channel = guild.get_channel(raffle.get("channel_id")) if guild else None
        if not channel and interaction.channel:
            channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await self._safe_interaction_reply(interaction, "Channel not found.")
            return
        try:
            msg = await channel.fetch_message(message_id)
            await msg.delete()
        except Exception:
            pass
        await self._safe_interaction_reply(interaction, "Raffle message removed.")

    async def _raffle_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                for guild in self.bot.guilds:
                    raffles = await self.config.guild(guild).raffles()
                    for raffle in raffles.values():
                        if raffle.get("status") != "open":
                            continue
                        ends_at = raffle.get("ends_at")
                        if ends_at and time.time() >= ends_at:
                            await self._close_raffle(guild, raffle, roll=True)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(30)

    async def _close_raffle(
        self, guild: discord.Guild, raffle: Dict[str, Any], *, roll: bool, delete_thread: bool = True
    ) -> None:
        entrants: List[int] = raffle.get("entrants") or []
        raffle["status"] = "closed"
        raffle["ends_at"] = None
        if roll:
            await self._roll_winners(guild, raffle, entrants=entrants)
        else:
            raffle["winners"] = []
            await self._save_raffle(guild.id, raffle)
            await self._refresh_message(guild.id, raffle)
            if delete_thread:
                thread_id = raffle.get("thread_id")
                if thread_id:
                    thread = guild.get_thread(thread_id)
                    if thread:
                        try:
                            await thread.delete(reason="Raffle closed")
                        except discord.HTTPException:
                            pass
        # Remove thread if it exists (channel message remains as trophy)
        if delete_thread:
            thread_id = raffle.get("thread_id")
            if thread_id:
                thread = guild.get_thread(thread_id)
                if thread:
                    try:
                        await thread.delete(reason="Raffle closed")
                    except discord.HTTPException:
                        pass

    async def _roll_winners(
        self,
        guild: discord.Guild,
        raffle: Dict[str, Any],
        entrants: Optional[List[int]] = None,
        delete_thread: bool = True,
    ) -> None:
        entrants = entrants if entrants is not None else (raffle.get("entrants") or [])
        max_winners = raffle.get("max_winners") or 1
        winners: List[int] = []
        if entrants:
            winners = random.sample(entrants, k=min(len(entrants), max_winners))
        raffle["status"] = "closed"
        raffle["winners"] = winners
        await self._save_raffle(guild.id, raffle)
        await self._refresh_message(guild.id, raffle)
        if delete_thread:
            thread_id = raffle.get("thread_id")
            if thread_id:
                thread = guild.get_thread(thread_id)
                if thread:
                    try:
                        await thread.delete(reason="Raffle closed")
                    except discord.HTTPException:
                        pass
        await self._sync_thread_listing(guild, raffle)
        await self._ensure_host_controls(guild, raffle)

    async def _maybe_delete_command(self, ctx: commands.Context) -> None:
        try:
            await ctx.message.delete()
        except Exception:
            pass

    async def _is_host_or_manager(
        self,
        user: discord.abc.User,
        guild: Optional[discord.Guild],
        host_id: Optional[int],
        guild_id: int,
    ) -> bool:
        if host_id and user.id == host_id:
            return True
        if guild and isinstance(user, discord.Member):
            if user.guild_permissions.manage_guild:
                return True
            manager_role_id = await self.config.guild_from_id(guild_id).manager_role()
            if manager_role_id and user.get_role(manager_role_id):
                return True
        return False

    async def _resolve_channel(self, ctx: commands.Context) -> Optional[discord.TextChannel]:
        channel_id = await self.config.guild(ctx.guild).raffle_channel()
        if channel_id:
            channel = ctx.guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                me = ctx.guild.me
                if me and channel.permissions_for(me).send_messages:
                    return channel
                return None
        # fallback to current channel if allowed
        if isinstance(ctx.channel, discord.TextChannel):
            me = ctx.guild.me
            if me and ctx.channel.permissions_for(me).send_messages:
                return ctx.channel
        return None

    async def _sync_thread_listing(self, guild: discord.Guild, raffle: Dict[str, Any]) -> None:
        thread_id = raffle.get("thread_id")
        if not thread_id:
            return
        thread = guild.get_thread(thread_id)
        if not thread:
            return
        if raffle.get("status") == "closed":
            return
        entries = raffle.get("entrants") or []
        grid = entrants_grid(entries, guild)
        embed = discord.Embed(title=f"Entrants ({len(entries)})", color=discord.Color.dark_teal())
        for idx, col in enumerate(grid, start=1):
            if not col:
                continue
            embed.add_field(name="Entrants" if idx == 1 else "\u200b", value="\n".join(col), inline=True)
        if not any(grid):
            embed.description = "No entrants yet."
        msg_id = raffle.get("thread_message_id")
        if msg_id:
            try:
                msg = await thread.fetch_message(msg_id)
                await msg.edit(content="", embed=embed)
                return
            except discord.HTTPException:
                pass
        try:
            msg = await thread.send(content="", embed=embed)
            raffle["thread_message_id"] = msg.id
            await self._save_raffle(guild.id, raffle)
        except discord.HTTPException:
            pass

    async def _safe_interaction_reply(self, interaction: discord.Interaction, text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass

    async def _ensure_host_controls(self, guild: Optional[discord.Guild], raffle: Dict[str, Any]) -> None:
        if not guild:
            return
        thread_id = raffle.get("thread_id")
        if not thread_id:
            return
        thread = guild.get_thread(thread_id)
        if not thread:
            return
        # Only skip when fully closed with winners
        if raffle.get("status") == "closed" and raffle.get("winners"):
            return
        show_pick = bool((raffle.get("entrants") or []))
        try:
            async for msg in thread.history(limit=10):
                if msg.author == guild.me and any(
                    btn.custom_id and btn.custom_id.startswith("raffle:end:")
                    for row in (msg.components or [])
                    for btn in row.children
                ):
                    await msg.edit(view=RaffleHostView(self, guild.id, raffle.get("id"), allow_pick=show_pick))
                    return
        except Exception:
            pass
        try:
            await thread.send(
                "Host controls:",
                view=RaffleHostView(self, guild.id, raffle.get("id"), allow_pick=show_pick),
            )
        except discord.HTTPException:
            pass

    async def _refresh_message(self, guild_id: int, raffle: Dict[str, Any]) -> None:
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(raffle.get("channel_id"))
        if not isinstance(channel, discord.TextChannel):
            return
        message_id = raffle.get("message_id")
        if not message_id:
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return
        embed = build_raffle_embed(raffle, guild)
        view = None
        status = raffle.get("status")
        if status == "open":
            view = RaffleViewOpen(self, guild_id, raffle.get("id"))
        elif status == "closed":
            entrants = raffle.get("entrants") or []
            winners = raffle.get("winners") or []
            if not winners and entrants:
                view = RaffleViewPendingPick(self, guild_id, raffle.get("id"))
            else:
                view = RaffleViewClosed(self, guild_id, raffle.get("id"), message_id)
        await message.edit(embed=embed, view=view)
        await self._sync_thread_listing(guild, raffle)
        await self._ensure_host_controls(guild, raffle)

    async def _get_raffle(self, guild_id: int, raffle_id: int) -> Optional[Dict[str, Any]]:
        raffles = await self.config.guild_from_id(guild_id).raffles()
        return raffles.get(str(raffle_id)) or raffles.get(int(raffle_id))  # type: ignore

    async def _save_raffle(self, guild_id: int, raffle: Dict[str, Any]) -> None:
        raffles = await self.config.guild_from_id(guild_id).raffles()
        raffles[str(raffle["id"])] = raffle
        await self.config.guild_from_id(guild_id).raffles.set(raffles)

    async def _next_id(self, guild_id: int) -> int:
        next_id = await self.config.guild_from_id(guild_id).next_id()
        await self.config.guild_from_id(guild_id).next_id.set(next_id + 1)
        return next_id

    async def _restore_views(self) -> None:
        for guild in self.bot.guilds:
            raffles = await self.config.guild(guild).raffles()
            for raffle in raffles.values():
                if raffle.get("status") == "open":
                    self.bot.add_view(RaffleViewOpen(self, guild.id, raffle.get("id")))
                elif raffle.get("status") == "closed" and not raffle.get("winners"):
                    entrants = raffle.get("entrants") or []
                    if entrants:
                        self.bot.add_view(RaffleViewPendingPick(self, guild.id, raffle.get("id")))
                if raffle.get("status") == "closed":
                    message_id = raffle.get("message_id")
                    if message_id:
                        self.bot.add_view(
                            RaffleViewClosed(
                                self,
                                guild.id,
                                raffle.get("id"),
                                message_id,
                            )
                        )
                # Host controls live in the thread; register both variants for persistence
                self.bot.add_view(RaffleHostView(self, guild.id, raffle.get("id"), allow_pick=True))
                self.bot.add_view(RaffleHostView(self, guild.id, raffle.get("id"), allow_pick=False))

    def _build_help_embed(self, prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title="Raffle commands",
            description="Button-only giveaways. Use the Enter button to join.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Basics",
            value=(
                f"`{prefix}raffle start <prize> [duration] [max_winners] [quantity]` - start a raffle\n"
                f"`{prefix}raffle end <id>` - end now (no draw) then use Pick winners\n"
                f"`{prefix}raffle list` - list active raffles"
            ),
            inline=False,
        )
        embed.add_field(
            name="Duration tips",
            value="Use `30m`, `1h`, `1d`, or `0/forever` for manual close.",
            inline=False,
        )
        embed.add_field(
            name="Permissions",
            value=(
                "Host controls are on the raffle message. Host, Manage Server, or the configured "
                "manager role can end/pick. Set a manager role with "
                f"`{prefix}raffle managerrole @Role` (clear with no role)."
            ),
            inline=False,
        )
        embed.add_field(
            name="Channel",
            value=(
                f"Set a dedicated channel with `{prefix}raffle channel #raffles` or auto-create one with "
                f"`{prefix}raffle setup [name]` (private by default)."
            ),
            inline=False,
        )
        embed.set_footer(text="Aliases: raffle / ra")
        return embed
