import discord
import aiohttp
from redbot.core import commands, Config
from discord.ext.commands import guild_only
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import re
import random
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlparse

class ChampionsCircle(commands.Cog):
    """Champions Circle tournament management system"""  # This description will show in [p]help
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        default_guild = {
            "champions_channel": None,
            "champions_forum": None,  # New: Forum channel for applications
            "champions_role_id": None,
            "active_applications": [],
            "application_threads": {},  # New: Store thread IDs for applications
            "cancelled_applications": [],
            "approved_applications": [],
            "denied_applications": [],
            "champions_message_id": None,
            "applications_open": False,
            "applications_opened_at": None,
            "roster_thread_id": None,
            "roster_anchor_message_id": None,
            "roster_message_ids": {},
            "application_duration": 7,  # days
            "custom_questions": [
                "Epic Account ID:",
                "Rank:",
                "Primary Platform (PC, Xbox, PlayStation, Switch):",
                "Preferred Region for Matches (NA East, NA West, EU):",
                "RL Tracker Link:",
                "Age:",
                "Timezone:",
                "Twitch Channel (optional):"
            ],
            "tourney_title": "Champions Circle Tournament",
            "tourney_description": "Join our exciting tournament!",
            "tourney_time": None,  # We'll store this as a UTC timestamp
            "tourney_challonge_signup_url": None,
            "tourney_challonge_bracket_url": None,
            "tourney_challonge_api_key": None,
            "tourney_challonge_slug": None,
            "tourney_challonge_participant_map": {},
            "game_mode": "3v3",
            "team_count": 0,
            "team_voice_category_id": None,
            "team_voice_channel_ids": [],
            "team_voice_channel_prefix": "Team",
            "team_captain_role_id": None,
            "team_role_ids": [],
            "team_text_channel_ids": [],
            "challonge_links": {},
            "challonge_link_last_reminder": {},
            "challonge_link_required": True,
            "challonge_link_grace_hours": 48,
            "challonge_link_reminder_hours": 12,
            "challonge_team_map": {},
            "challonge_sync_roles_enabled": True,
            "challonge_sync_roles_interval": 15,
            "score_panel_message_ids": {},
            # Draft system
            "draft_assignments": {},  # {team_number: [user_id, user_id, ...]}
            "draft_captains": {},  # {team_number: user_id}
            "draft_order": [],  # [team_number, team_number, ...] - snake draft order
            "draft_current_pick": 0,  # index into draft_order
            "draft_pick_log": [],  # [{"team": int, "user_id": int}]
            "draft_started": False,
            "draft_locked": False,
            "teams_created_on_challonge": False,
        }
        self.config.register_guild(**default_guild)
        self.logger = logging.getLogger("red.championsCircle")
        self.application_cooldowns = commands.CooldownMapping.from_cooldown(1, 3600, commands.BucketType.user)
        self._expiry_task: Optional[asyncio.Task] = None
        self._roles_sync_task: Optional[asyncio.Task] = None
        self._application_lists = [
            "active_applications",
            "approved_applications",
            "denied_applications",
            "cancelled_applications",
        ]

    def reset_cooldowns(self):
        self.application_cooldowns = commands.CooldownMapping.from_cooldown(1, 3600, commands.BucketType.user)

    async def cog_load(self):
        self._hide_non_help_commands()
        if not self._expiry_task or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(self.close_expired_applications())
        if not self._roles_sync_task or self._roles_sync_task.done():
            self._roles_sync_task = asyncio.create_task(self._auto_sync_roles_loop())
        try:
            self.bot.add_view(ChampionsApplyView(self))
        except Exception as exc:
            self.logger.error("Failed to register Champions Circle persistent view: %s", exc)

    def cog_unload(self):
        if self._expiry_task:
            self._expiry_task.cancel()
        if self._roles_sync_task:
            self._roles_sync_task.cancel()

    def _hide_non_help_commands(self) -> None:
        for command in self.walk_commands():
            if command.name != "cchelp":
                command.hidden = True

    @commands.Cog.listener()
    async def on_ready(self):
        self.logger.info(f"ChampionsCircle is ready!")

    def _normalize_application_entry(self, entry: Any) -> Optional[Dict[str, Any]]:
        if isinstance(entry, dict) and "user_id" in entry:
            return entry
        if isinstance(entry, int):
            return {"user_id": entry, "timestamp": 0, "answers": {}}
        return None

    async def _load_application_list(self, guild: discord.Guild, list_name: str) -> List[Dict[str, Any]]:
        try:
            entries = await self.config.guild(guild).get_raw(list_name, default=[])
        except Exception:
            entries = await getattr(self.config.guild(guild), list_name)()
        normalized: List[Dict[str, Any]] = []
        changed = False
        for entry in entries or []:
            normalized_entry = self._normalize_application_entry(entry)
            if normalized_entry:
                normalized.append(normalized_entry)
                if normalized_entry is not entry:
                    changed = True
            else:
                changed = True
        if changed:
            await getattr(self.config.guild(guild), list_name).set(normalized)
        return normalized

    async def _save_application_list(
        self, guild: discord.Guild, list_name: str, entries: List[Dict[str, Any]]
    ) -> None:
        await getattr(self.config.guild(guild), list_name).set(entries)

    async def _find_application(
        self, guild: discord.Guild, user_id: int
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        for list_name in self._application_lists:
            entries = await self._load_application_list(guild, list_name)
            for entry in entries:
                if entry.get("user_id") == user_id:
                    return list_name, entry, entries
        return None, None, None

    async def _remove_application(self, guild: discord.Guild, user_id: int) -> Optional[Dict[str, Any]]:
        list_name, entry, entries = await self._find_application(guild, user_id)
        if not list_name or not entry or entries is None:
            return None
        entries = [item for item in entries if item.get("user_id") != user_id]
        await self._save_application_list(guild, list_name, entries)
        return entry

    async def _remove_application_from_all(
        self, guild: discord.Guild, user_id: int
    ) -> Optional[Dict[str, Any]]:
        best_entry: Optional[Dict[str, Any]] = None
        best_timestamp = -1
        for list_name in self._application_lists:
            entries = await self._load_application_list(guild, list_name)
            removed_entries = [item for item in entries if item.get("user_id") == user_id]
            if removed_entries:
                entries = [item for item in entries if item.get("user_id") != user_id]
                await self._save_application_list(guild, list_name, entries)
                for entry in removed_entries:
                    timestamp = int(entry.get("timestamp") or 0)
                    if timestamp > best_timestamp:
                        best_timestamp = timestamp
                        best_entry = entry
        return best_entry

    def _merge_application_entries(
        self, base: Dict[str, Any], other: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if not other:
            return base
        merged = dict(base)
        if not merged.get("timestamp") and other.get("timestamp"):
            merged["timestamp"] = other.get("timestamp")
        if not merged.get("answers") and other.get("answers"):
            merged["answers"] = other.get("answers")
        if not merged.get("thread_id") and other.get("thread_id"):
            merged["thread_id"] = other.get("thread_id")
        return merged

    async def _move_application(
        self,
        guild: discord.Guild,
        user_id: int,
        target_list: str,
        *,
        entry: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        removed_entry = await self._remove_application_from_all(guild, user_id)
        if entry:
            entry = self._merge_application_entries(entry, removed_entry)
        else:
            entry = removed_entry
        if not entry:
            return None
        entries = await self._load_application_list(guild, target_list)
        entries = [item for item in entries if item.get("user_id") != user_id]
        entries.append(entry)
        await self._save_application_list(guild, target_list, entries)
        return entry

    @commands.command(name="ccsetup")
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def ccsetup(self, ctx):
        """Initialize and setup the tournament system"""

        guild = ctx.guild
        category = None
        category_name = "Champions Circle"
        forum_channel = None
        forum_id = await self.config.guild(guild).champions_forum()
        if forum_id:
            forum_channel = guild.get_channel(forum_id)
        if forum_channel is None:
            category = next(
                (c for c in guild.categories if c.name.lower() == category_name.lower()), None
            )
            if category is None:
                category = await guild.create_category(
                    name=category_name,
                    reason="Champions Circle setup",
                )
            forum_channel = await guild.create_forum(
                name="tournament-applications",
                topic="Tournament Applications",
                reason="Tournament setup",
                category=category,
            )

        tags = [
            discord.ForumTag(name="Pending", emoji="📝"),
            discord.ForumTag(name="Approved", emoji="✅"),
            discord.ForumTag(name="Denied", emoji="❌"),
            discord.ForumTag(name="Cancelled", emoji="⚠️"),
        ]
        await forum_channel.edit(available_tags=tags)

        announcement_channel = None
        announcement_id = await self.config.guild(guild).champions_channel()
        if announcement_id:
            announcement_channel = guild.get_channel(announcement_id)
        if announcement_channel is None:
            if category is None:
                category = next(
                    (c for c in guild.categories if c.name.lower() == category_name.lower()), None
                )
            if category is None:
                category = await guild.create_category(
                    name=category_name,
                    reason="Champions Circle setup",
                )
            announcement_channel = await guild.create_text_channel(
                name="tournament-announcements",
                topic="Tournament Announcements",
                category=category,
            )

        await self.config.guild(guild).champions_forum.set(forum_channel.id)
        await self.config.guild(guild).champions_channel.set(announcement_channel.id)

        role = next((r for r in guild.roles if r.name.lower() == "champions"), None)
        if role is None:
            role = await guild.create_role(
                name="Champions",
                reason="Champions Circle setup",
                mentionable=True,
            )

        await self.config.guild(guild).champions_role_id.set(role.id)
        await self.config.guild(guild).applications_open.set(False)
        await self.config.guild(guild).champions_message_id.set(None)

        class SetupButton(discord.ui.Button):
            def __init__(self, cog):
                super().__init__(label="Continue setup", style=discord.ButtonStyle.green)
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                await interaction.response.send_modal(SetupModal(self.cog))

        class SetupView(discord.ui.View):
            def __init__(self, cog):
                super().__init__(timeout=600)
                self.add_item(SetupButton(cog))

        await ctx.send(
            "Tournament setup ready. Click to continue:",
            view=SetupView(self),
        )

    async def process_setup(self, interaction: discord.Interaction, modal: "SetupModal"):
        """Process the setup modal submission"""
        try:
            await self.config.guild(interaction.guild).tourney_title.set(modal.tourney_title.value)
            await self.config.guild(interaction.guild).tourney_description.set(modal.description.value)

            time_value = (modal.time.value or "").strip()
            if time_value:
                try:
                    tourney_time = datetime.fromisoformat(time_value).replace(tzinfo=timezone.utc)
                    timestamp = int(tourney_time.timestamp())
                    await self.config.guild(interaction.guild).tourney_time.set(timestamp)
                except ValueError:
                    if interaction.response.is_done():
                        await interaction.followup.send(
                            "Invalid time format. Please use YYYY-MM-DD HH:MM:SS", ephemeral=True
                        )
                    else:
                        await interaction.response.send_message(
                            "Invalid time format. Please use YYYY-MM-DD HH:MM:SS", ephemeral=True
                        )
                    return
            else:
                await self.config.guild(interaction.guild).tourney_time.set(None)

            try:
                days = int(modal.duration.value)
                await self.config.guild(interaction.guild).application_duration.set(days)
            except ValueError:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "Invalid duration. Please enter a number", ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "Invalid duration. Please enter a number", ephemeral=True
                    )
                return

            raw_mode = None
            if hasattr(modal, "game_mode_select"):
                raw_mode = getattr(modal.game_mode_select, "values", None)
                if isinstance(raw_mode, list):
                    raw_mode = raw_mode[0] if raw_mode else None
            if not raw_mode:
                raw_mode = self._extract_component_value(interaction.data, "ccsetup_game_mode")
            game_mode_value = self._normalize_game_mode(raw_mode)
            if raw_mode and not game_mode_value:
                message = "Invalid game mode. Use 1v1, 2v2, 3v3, or 4v4."
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
                return
            if game_mode_value:
                await self.config.guild(interaction.guild).game_mode.set(game_mode_value)

            view = SetupContinueView(self, interaction.guild.id, interaction.user.id)
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Setup step 2: click Continue to configure team channels.",
                    view=view,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Setup step 2: click Continue to configure team channels.",
                    view=view,
                    ephemeral=True,
                )

        except Exception as e:
            self.logger.error(f"Error in setup process: {str(e)}")
            if interaction.response.is_done():
                await interaction.followup.send(
                    "An error occurred during setup. Please try again or contact support.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "An error occurred during setup. Please try again or contact support.",
                    ephemeral=True,
                )

    async def process_advanced_setup(
        self, interaction: discord.Interaction, modal: "SetupAdvancedModal"
    ) -> None:
        try:
            guild = interaction.guild
            team_count = self._parse_int(modal.team_count.value) or 0
            if team_count < 0:
                team_count = 0

            prefix = "Team"
            category_name = (
                f"Champions Circle - {team_count} Teams" if team_count > 0 else "Champions Circle - Teams"
            )

            await self.config.guild(guild).team_count.set(team_count)
            await self.config.guild(guild).team_voice_channel_prefix.set(prefix)

            role = guild.get_role(await self.config.guild(guild).team_captain_role_id())
            if role is None:
                role = next(
                    (r for r in guild.roles if r.name.lower() == "team captain"), None
                )
            if role is None:
                role = await guild.create_role(
                    name="Team Captain",
                    reason="Champions Circle setup",
                    mentionable=True,
                    color=self._random_role_color(),
                )
            await self.config.guild(guild).team_captain_role_id.set(role.id)

            await self.config.guild(guild).team_voice_category_id.set(None)
            await self.config.guild(guild).team_voice_channel_ids.set([])

            await self.update_embed(guild)

            embed = discord.Embed(
                title="Tournament Setup Complete",
                color=discord.Color.green(),
                description="Your tournament has been configured with the following settings:",
            )

            tourney_title = await self.config.guild(guild).tourney_title()
            tourney_description = await self.config.guild(guild).tourney_description()
            tourney_time = await self.config.guild(guild).tourney_time()
            duration = await self.config.guild(guild).application_duration()
            game_mode = await self.config.guild(guild).game_mode()

            embed.add_field(name="Title", value=tourney_title)
            embed.add_field(name="Description", value=tourney_description)
            embed.add_field(name="Time", value=self._format_time_display(tourney_time))
            embed.add_field(name="Application Duration", value=f"{duration} days")
            embed.add_field(name="Game mode", value=game_mode)
            embed.add_field(
                name="Team size",
                value=str(self._team_size_from_mode(game_mode)),
            )

            forum_id = await self.config.guild(guild).champions_forum()
            channel_id = await self.config.guild(guild).champions_channel()
            forum_channel = guild.get_channel(forum_id) if forum_id else None
            announcement_channel = guild.get_channel(channel_id) if channel_id else None
            embed.add_field(
                name="Channels",
                value=(
                    f"Forum: {forum_channel.mention if forum_channel else 'Not found'}\n"
                    f"Announcements: {announcement_channel.mention if announcement_channel else 'Not found'}"
                ),
                inline=False,
            )

            role = guild.get_role(await self.config.guild(guild).champions_role_id())
            embed.add_field(name="Champions Role", value=role.mention if role else "Not found")

            team_voice_category_id = await self.config.guild(guild).team_voice_category_id()
            team_voice_category = (
                guild.get_channel(team_voice_category_id) if team_voice_category_id else None
            )
            embed.add_field(
                name="Team voice category",
                value=team_voice_category.mention if team_voice_category else "Not set",
                inline=True,
            )
            embed.add_field(
                name="Team count",
                value=str(await self.config.guild(guild).team_count()),
                inline=True,
            )
            embed.add_field(
                name="Team channels",
                value=str(len(await self.config.guild(guild).team_voice_channel_ids())),
                inline=True,
            )
            captain_role_id = await self.config.guild(guild).team_captain_role_id()
            captain_role = guild.get_role(captain_role_id) if captain_role_id else None
            embed.add_field(
                name="Captain role",
                value=captain_role.mention if captain_role else "Not set",
                inline=True,
            )

            if interaction.response.is_done():
                await interaction.followup.send(embed=embed)
            else:
                await interaction.response.send_message(embed=embed)
        except Exception as exc:
            self.logger.error("Error in advanced setup: %s", exc)
            if interaction.response.is_done():
                await interaction.followup.send(
                    "An error occurred during advanced setup. Please try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "An error occurred during advanced setup. Please try again.",
                    ephemeral=True,
                )
    @commands.command(name="ccstart")
    @commands.admin_or_permissions(administrator=True)
    async def ccstart(self, ctx):
        """Start the tournament and open applications"""
        target_channel_id = await self.config.guild(ctx.guild).champions_channel()
        if not target_channel_id:
            await ctx.send("No Champions Circle channel is configured. Run `ccsetup` first.")
            return
        if ctx.channel.id != target_channel_id:
            await ctx.send("This command can only be used in the Champions Circle channel.")
            return

        await self.config.guild(ctx.guild).applications_open.set(True)
        await self.config.guild(ctx.guild).applications_opened_at.set(
            int(datetime.now(timezone.utc).timestamp())
        )
        await self._create_team_voice_assets(ctx.guild)
        await self.update_embed(ctx.guild)

    @commands.command(name="ccend")
    @commands.admin_or_permissions(administrator=True)
    async def ccend(self, ctx):
        """End the tournament and clean up"""
        target_channel_id = await self.config.guild(ctx.guild).champions_channel()
        if not target_channel_id:
            await ctx.send("No Champions Circle channel is configured. Run `ccsetup` first.")
            return
        if ctx.channel.id != target_channel_id:
            await ctx.send("This command can only be used in the Champions Circle channel.")
            return

        # Ask for confirmation
        await ctx.send(
            "Are you sure you want to end the tournament? This will clear all messages and reset the application lists. "
            "Reply with `yes` to confirm."
        )

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == "yes"

        try:
            await self.bot.wait_for("message", check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.send("Tournament end cancelled.")
            return

        # Archive/lock (and delete) application threads
        forum_id = await self.config.guild(ctx.guild).champions_forum()
        forum = ctx.guild.get_channel(forum_id) if forum_id else None
        thread_ids: set[int] = set()
        for list_name in self._application_lists:
            for entry in await self._load_application_list(ctx.guild, list_name):
                thread_id = entry.get("thread_id")
                if thread_id:
                    thread_ids.add(thread_id)
        try:
            threads_map = await self.config.guild(ctx.guild).application_threads()
            for thread_id in threads_map.values():
                thread_ids.add(int(thread_id))
        except Exception:
            pass

        for thread_id in thread_ids:
            thread = ctx.guild.get_thread(thread_id)
            if not thread and forum:
                try:
                    thread = await ctx.guild.fetch_channel(thread_id)
                except discord.HTTPException:
                    thread = None
            if isinstance(thread, discord.Thread):
                try:
                    await thread.delete()
                except discord.HTTPException:
                    try:
                        await thread.edit(archived=True, locked=True)
                    except discord.HTTPException:
                        self.logger.error("Failed to archive/delete thread %s", thread_id)

        roster_thread_id = await self.config.guild(ctx.guild).roster_thread_id()
        if roster_thread_id:
            roster_thread = ctx.guild.get_thread(roster_thread_id)
            if not roster_thread:
                try:
                    roster_thread = await ctx.guild.fetch_channel(roster_thread_id)
                except discord.HTTPException:
                    roster_thread = None
            if isinstance(roster_thread, discord.Thread):
                try:
                    await roster_thread.delete()
                except discord.HTTPException:
                    try:
                        await roster_thread.edit(archived=True, locked=True)
                    except discord.HTTPException:
                        self.logger.error("Failed to archive/delete roster thread %s", roster_thread_id)

        await self._cleanup_team_voice_assets(ctx.guild)

        # Clear messages
        channel = ctx.channel
        await ctx.send("Ending tournament and clearing channel...")

        purge_failed = False
        try:
            await channel.purge(limit=None)
        except discord.Forbidden:
            purge_failed = True
            await ctx.send("I don't have permission to delete messages in this channel. Continuing reset.")
        except discord.HTTPException:
            purge_failed = True
            await ctx.send("An error occurred while trying to delete messages. Continuing reset.")

        # Reset cog state
        await self.config.guild(ctx.guild).active_applications.set([])
        await self.config.guild(ctx.guild).cancelled_applications.set([])
        await self.config.guild(ctx.guild).approved_applications.set([])
        await self.config.guild(ctx.guild).denied_applications.set([])
        await self.config.guild(ctx.guild).application_threads.set({})
        await self.config.guild(ctx.guild).champions_message_id.set(None)
        await self.config.guild(ctx.guild).applications_open.set(False)
        await self.config.guild(ctx.guild).applications_opened_at.set(None)
        await self.config.guild(ctx.guild).roster_thread_id.set(None)
        await self.config.guild(ctx.guild).roster_anchor_message_id.set(None)
        await self.config.guild(ctx.guild).roster_message_ids.set({})
        await self.config.guild(ctx.guild).team_voice_category_id.set(None)
        await self.config.guild(ctx.guild).team_voice_channel_ids.set([])
        await self.config.guild(ctx.guild).team_text_channel_ids.set([])
        await self.config.guild(ctx.guild).team_role_ids.set([])
        await self.config.guild(ctx.guild).challonge_links.set({})
        await self.config.guild(ctx.guild).challonge_link_last_reminder.set({})
        await self.config.guild(ctx.guild).challonge_team_map.set({})
        await self.config.guild(ctx.guild).score_panel_message_ids.set({})
        # Reset draft system
        await self.config.guild(ctx.guild).draft_assignments.set({})
        await self.config.guild(ctx.guild).draft_captains.set({})
        await self.config.guild(ctx.guild).draft_order.set([])
        await self.config.guild(ctx.guild).draft_current_pick.set(0)
        await self.config.guild(ctx.guild).draft_pick_log.set([])
        await self.config.guild(ctx.guild).draft_started.set(False)
        await self.config.guild(ctx.guild).draft_locked.set(False)
        await self.config.guild(ctx.guild).teams_created_on_challonge.set(False)

        # Reset cooldowns
        self.reset_cooldowns()

        # Remove Champions role from all members
        guild = ctx.guild
        champions_role = guild.get_role(await self.config.guild(ctx.guild).champions_role_id())
        if champions_role:
            for member in champions_role.members:
                try:
                    await member.remove_roles(champions_role)
                except discord.HTTPException:
                    self.logger.error(f"Failed to remove Champions role from {member.name}")
        else:
            self.logger.error(f"Champions role with ID {await self.config.guild(ctx.guild).champions_role_id()} not found.")

        # Send a temporary message that will be deleted after 10 seconds
        if purge_failed:
            await channel.send(
                "Tournament ended. Channel messages were not cleared, but cog state and applications were reset.",
                delete_after=10,
            )
        else:
            await channel.send(
                "Tournament ended. Channel cleared, cog state reset, and application cooldowns reset. You can now use the tourney start command for a new tournament.",
                delete_after=10,
            )

    @commands.group(name="ccscore")
    @commands.guild_only()
    async def ccscore(self, ctx):
        """Report match scores for your team."""
        if ctx.invoked_subcommand is None:
            await ctx.send("Use `ccscore report <match_id> <score>` or `ccscore panel`.")

    @ccscore.command(name="panel")
    async def ccscore_panel(self, ctx):
        """Post a score report button in the current team channel."""
        team_number = await self._get_team_number_for_channel(ctx.guild, ctx.channel)
        if not team_number:
            await ctx.send("This is not a team text channel.")
            return
        team_role = self._get_team_role_for_number(ctx.guild, team_number)
        captain_role_id = await self.config.guild(ctx.guild).team_captain_role_id()
        if not self._member_can_report_score(ctx.author, team_role, captain_role_id):
            await ctx.send("Only team captains or mods can post the score panel.")
            return
        await ctx.send("Use the button below to report your match score:", view=ScoreReportView(self))

    @ccscore.command(name="report")
    async def ccscore_report(self, ctx, match_id: int, score: str):
        """Report a match score (format: X-Y)."""
        team_number = await self._get_team_number_for_channel(ctx.guild, ctx.channel)
        if not team_number:
            await ctx.send("This is not a team text channel.")
            return
        team_role = self._get_team_role_for_number(ctx.guild, team_number)
        captain_role_id = await self.config.guild(ctx.guild).team_captain_role_id()
        if not self._member_can_report_score(ctx.author, team_role, captain_role_id):
            await ctx.send("Only team captains or mods can report scores.")
            return
        team_map = await self.config.guild(ctx.guild).challonge_team_map()
        if not isinstance(team_map, dict):
            team_map = {}
        participant_id = team_map.get(str(team_number))
        if not participant_id:
            await ctx.send("No Challonge team mapping for this channel.")
            return
        ok, message = await self._submit_match_score(
            ctx.guild,
            match_id=match_id,
            participant_id=int(participant_id),
            score_input=score,
        )
        await ctx.send(message)

    @commands.group(name="ccquestions")
    @commands.admin_or_permissions(administrator=True)
    async def ccquestions(self, ctx):
        """Manage tournament application questions"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @ccquestions.command(name="add")
    async def questions_add(self, ctx, *, question: str):
        """Add a tournament application question"""
        async with self.config.guild(ctx.guild).custom_questions() as questions:
            questions.append(question)
        await ctx.send(f"Added question: {question}")

    @ccquestions.command(name="remove")
    async def questions_remove(self, ctx, index: int):
        """Remove a tournament application question by its index"""
        async with self.config.guild(ctx.guild).custom_questions() as questions:
            if 1 <= index <= len(questions):
                removed = questions.pop(index - 1)
                await ctx.send(f"Removed question: {removed}")
            else:
                await ctx.send("Invalid question index!")

    @ccquestions.command(name="list")
    async def questions_list(self, ctx):
        """List all tournament application questions"""
        questions = await self.config.guild(ctx.guild).custom_questions()
        if not questions:
            await ctx.send("No custom questions set.")
            return

        question_list = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
        note = "\n\nNote: If there are more than 5 questions, extras are grouped into an \"Additional info\" field in the modal."
        await ctx.send(f"Current questions:\n{question_list}{note}")

    def _format_time_display(self, tourney_time: Optional[int]) -> str:
        if not tourney_time:
            return "Not set"
        return f"<t:{tourney_time}:F> (<t:{tourney_time}:R>)"

    def _normalize_url(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.startswith(("http://", "https://")):
            return cleaned
        if cleaned.startswith("www."):
            return f"https://{cleaned}"
        if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(/|$)", cleaned, re.IGNORECASE):
            return f"https://{cleaned}"
        return cleaned

    def _extract_component_value(self, data: Any, custom_id: str) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        for row in data.get("components", []):
            for component in row.get("components", []):
                if component.get("custom_id") != custom_id:
                    continue
                values = component.get("values")
                if isinstance(values, list) and values:
                    return str(values[0])
                value = component.get("value")
                if value is not None:
                    return str(value)
        return None

    async def _build_tournament_info(self, guild: discord.Guild) -> str:
        title = await self.config.guild(guild).tourney_title()
        tourney_time = await self.config.guild(guild).tourney_time()
        bracket_url = self._normalize_url(
            await self.config.guild(guild).tourney_challonge_bracket_url()
        )

        lines = [f"**{title}**"]
        if tourney_time:
            lines.append(f"Time: <t:{tourney_time}:F> (<t:{tourney_time}:R>)")
        if bracket_url:
            lines.append(f"Bracket: {bracket_url}")
        return "\n".join(lines)

    async def _get_preferred_prefix(self, guild: discord.Guild) -> str:
        try:
            prefixes = await self.bot.get_valid_prefixes(guild)
        except Exception:
            return "!"
        for prefix in prefixes:
            if not prefix.startswith("<@"):
                return prefix
        return prefixes[0] if prefixes else "!"

    async def _send_status_dm(
        self,
        *,
        member: discord.abc.User,
        guild: discord.Guild,
        status_line: str,
        extra: Optional[str] = None,
        application_url: Optional[str] = None,
        command_hint: Optional[str] = None,
    ) -> None:
        info = await self._build_tournament_info(guild)
        parts = [status_line, "", info]
        if application_url:
            parts.append("")
            parts.append(f"Application: {application_url}")
        if extra:
            parts.append("")
            parts.append(extra)
        if command_hint:
            parts.append("")
            parts.append(f"Command: `{command_hint}`")
        message = "\n".join(parts)
        try:
            await member.send(message)
        except discord.HTTPException:
            self.logger.error("Failed to DM status update to user %s", member.id)

    def _extract_answer(self, answers: Dict[str, Any], tokens: List[str]) -> Optional[str]:
        for key, value in answers.items():
            key_text = str(key).lower()
            if any(token in key_text for token in tokens):
                return str(value).strip()
        return None

    def _format_user_entry(self, guild: discord.Guild, application: Dict[str, Any]) -> str:
        user_id = application.get("user_id")
        user = guild.get_member(user_id) if user_id else None
        display = user.mention if user else f"<@{user_id}>"

        answers = application.get("answers") or {}
        rank = self._extract_answer(answers, ["rank"]) or "Unranked"

        parts = [display, f"**{rank}**"]
        thread_id = application.get("thread_id")
        if thread_id:
            parts.append(f"[Application](https://discord.com/channels/{guild.id}/{thread_id})")
        return " | ".join(parts)

    def _chunk_lines(self, lines: List[str], limit: int = 900) -> List[List[str]]:
        chunks: List[List[str]] = []
        current: List[str] = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > limit:
                chunks.append(current)
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append(current)
        return chunks

    async def _ensure_roster_thread(self, guild: discord.Guild) -> Optional[discord.Thread]:
        channel_id = await self.config.guild(guild).champions_channel()
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return None

        roster_thread_id = await self.config.guild(guild).roster_thread_id()
        if roster_thread_id:
            thread = guild.get_thread(roster_thread_id)
            if not thread:
                try:
                    thread = await guild.fetch_channel(roster_thread_id)
                except discord.HTTPException:
                    thread = None
            if isinstance(thread, discord.Thread):
                return thread

            await self.config.guild(guild).roster_thread_id.set(None)
            await self.config.guild(guild).roster_message_ids.set({})
            await self.config.guild(guild).roster_anchor_message_id.set(None)

        try:
            anchor = await channel.send(
                "Champions Circle roster board. This thread contains the live applicant lists."
            )
        except discord.HTTPException:
            return None

        try:
            thread = await channel.create_thread(
                name="Champions Circle Roster",
                message=anchor,
                auto_archive_duration=10080,
            )
        except discord.HTTPException:
            return None

        await self.config.guild(guild).roster_thread_id.set(thread.id)
        await self.config.guild(guild).roster_anchor_message_id.set(anchor.id)
        return thread

    def _build_roster_embed(
        self,
        *,
        guild: discord.Guild,
        label: str,
        icon: str,
        color: discord.Color,
        entries: List[Dict[str, Any]],
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{icon} {label}",
            description=f"Total: {len(entries)}",
            color=color,
        )
        if not entries:
            embed.add_field(name="Applicants", value="None yet.", inline=False)
            return embed

        lines = [self._format_user_entry(guild, entry) for entry in entries]
        for idx, chunk in enumerate(self._chunk_lines(lines), start=1):
            name = "Applicants" if idx == 1 else f"Applicants ({idx})"
            embed.add_field(name=name, value="\n".join(chunk), inline=False)
        return embed

    async def _update_roster_board(self, guild: discord.Guild) -> Optional[str]:
        thread = await self._ensure_roster_thread(guild)
        if not thread:
            return None

        active = await self._load_application_list(guild, "active_applications")
        approved = await self._load_application_list(guild, "approved_applications")
        denied = await self._load_application_list(guild, "denied_applications")
        cancelled = await self._load_application_list(guild, "cancelled_applications")

        boards = [
            ("active", "Active Applicants", "🟡", discord.Color.gold(), active),
            ("approved", "Approved Champions", "🟢", discord.Color.green(), approved),
            ("denied", "Denied Applications", "🔴", discord.Color.red(), denied),
            ("cancelled", "Cancelled Applications", "⚪", discord.Color.light_grey(), cancelled),
        ]

        stored_ids = await self.config.guild(guild).roster_message_ids()
        if not isinstance(stored_ids, dict):
            stored_ids = {}

        for key, label, icon, color, entries in boards:
            embed = self._build_roster_embed(
                guild=guild,
                label=label,
                icon=icon,
                color=color,
                entries=entries,
            )
            msg_id = stored_ids.get(key)
            message = None
            if msg_id:
                try:
                    message = await thread.fetch_message(int(msg_id))
                except discord.HTTPException:
                    message = None
            if message:
                try:
                    await message.edit(embed=embed)
                except discord.HTTPException:
                    message = None
            if not message:
                try:
                    message = await thread.send(embed=embed)
                except discord.HTTPException:
                    continue
                stored_ids[key] = message.id

        await self.config.guild(guild).roster_message_ids.set(stored_ids)
        return thread.jump_url

    def _truncate_text(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(limit - 3, 0)].rstrip() + "..."

    def _normalize_game_mode(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            return None
        aliases = {
            "1v1": "1v1",
            "2v2": "2v2",
            "3v3": "3v3",
            "4v4": "4v4",
            "1s": "1v1",
            "2s": "2v2",
            "3s": "3v3",
            "4s": "4v4",
            "solo": "1v1",
            "duo": "2v2",
            "duos": "2v2",
            "doubles": "2v2",
            "trio": "3v3",
            "trios": "3v3",
            "standard": "3v3",
            "squad": "4v4",
            "squads": "4v4",
        }
        if cleaned in aliases:
            return aliases[cleaned]
        if cleaned.isdigit():
            return f"{cleaned}v{cleaned}"
        match = re.search(r"(\d)\s*v\s*(\d)", cleaned)
        if match and match.group(1) == match.group(2):
            return f"{match.group(1)}v{match.group(2)}"
        return None

    def _team_size_from_mode(self, mode: Optional[str]) -> int:
        normalized = self._normalize_game_mode(mode or "")
        if not normalized:
            return 3
        try:
            return int(normalized.split("v")[0])
        except (ValueError, IndexError):
            return 3

    def _parse_int(self, value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.lower() in {"none", "unset", "clear"}:
            return None
        try:
            return int(cleaned)
        except ValueError:
            return None

    def _resolve_role_from_text(self, guild: discord.Guild, value: str) -> Optional[discord.Role]:
        if not value:
            return None
        cleaned = value.strip()
        match = re.match(r"<@&(\d+)>", cleaned)
        if match:
            role_id = int(match.group(1))
            return guild.get_role(role_id)
        if cleaned.isdigit():
            role = guild.get_role(int(cleaned))
            if role:
                return role
        return next((role for role in guild.roles if role.name.lower() == cleaned.lower()), None)

    def _slugify_channel_name(self, value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return cleaned or "team"

    def _extract_team_number(self, name: str) -> Optional[int]:
        match = re.search(r"(\\d+)(?!.*\\d)", name)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _team_emoji(self, index: int) -> str:
        emojis = ["🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "⚫", "⚪"]
        return emojis[(index - 1) % len(emojis)]

    def _random_role_color(self) -> discord.Color:
        palette = [
            discord.Color.red(),
            discord.Color.orange(),
            discord.Color.gold(),
            discord.Color.green(),
            discord.Color.blue(),
            discord.Color.purple(),
            discord.Color.magenta(),
            discord.Color.teal(),
        ]
        return random.choice(palette)

    async def _ensure_voice_category(
        self, guild: discord.Guild, name: str
    ) -> Optional[discord.CategoryChannel]:
        category = next((c for c in guild.categories if c.name.lower() == name.lower()), None)
        if category:
            bot_member = guild.me or guild.get_member(self.bot.user.id)
            if bot_member:
                perms = category.permissions_for(bot_member)
                if not (perms.view_channel and perms.manage_channels):
                    fallback_name = name
                    for suffix in range(2, 6):
                        candidate = f"{name} ({suffix})"
                        if not any(c.name.lower() == candidate.lower() for c in guild.categories):
                            fallback_name = candidate
                            break
                    await self._notify_team_asset_issue(
                        guild,
                        message=(
                            f"Existing category `{category.name}` blocks bot permissions. "
                            f"Creating new category `{fallback_name}`."
                        ),
                    )
                    try:
                        return await guild.create_category(
                            name=fallback_name, reason="Champions Circle setup"
                        )
                    except discord.HTTPException:
                        return None
            return category
        try:
            return await guild.create_category(name=name, reason="Champions Circle setup")
        except discord.HTTPException:
            return None

    async def _ensure_team_roles(
        self,
        guild: discord.Guild,
        *,
        team_count: int,
        prefix: str,
    ) -> Dict[int, discord.Role]:
        roles_by_num: Dict[int, discord.Role] = {}
        role_ids: List[int] = []
        for index in range(1, team_count + 1):
            role_name = f"{prefix} {index}"
            role = next(
                (r for r in guild.roles if r.name.lower() == role_name.lower()), None
            )
            if role is None:
                try:
                    role = await guild.create_role(
                        name=role_name,
                        reason="Champions Circle team role",
                        mentionable=True,
                        color=self._random_role_color(),
                    )
                except discord.HTTPException:
                    continue
            roles_by_num[index] = role
            role_ids.append(role.id)
        await self.config.guild(guild).team_role_ids.set(role_ids)
        return roles_by_num

    def _build_team_overwrites(
        self,
        guild: discord.Guild,
        *,
        team_role: discord.Role,
        is_voice: bool,
    ) -> Dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            team_role: discord.PermissionOverwrite(
                view_channel=True,
                connect=True if is_voice else None,
                speak=True if is_voice else None,
                send_messages=None if is_voice else True,
                read_message_history=None if is_voice else True,
            ),
        }
        bot_member = guild.me or guild.get_member(self.bot.user.id)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                manage_channels=True,
                connect=True,
                speak=True,
                send_messages=True,
                read_message_history=True,
            )
        return overwrites

    async def _get_team_number_for_channel(
        self, guild: discord.Guild, channel: discord.abc.GuildChannel
    ) -> Optional[int]:
        team_channels = await self.config.guild(guild).team_text_channel_ids()
        if channel.id not in (team_channels or []):
            return None
        return self._extract_team_number(channel.name or "")

    async def _notify_team_asset_issue(self, guild: discord.Guild, *, message: str) -> None:
        channel_id = await self.config.guild(guild).champions_channel()
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass
        self.logger.error(message)

    def _get_team_role_for_number(
        self, guild: discord.Guild, team_number: int
    ) -> Optional[discord.Role]:
        if team_number <= 0:
            return None
        for role in guild.roles:
            if role.name.lower() == f"team {team_number}":
                return role
        for role in guild.roles:
            if self._extract_team_number(role.name or "") == team_number:
                return role
        return None

    def _member_can_report_score(
        self, member: discord.Member, team_role: Optional[discord.Role], captain_role_id: Optional[int]
    ) -> bool:
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True
        if captain_role_id:
            captain_role = member.guild.get_role(captain_role_id)
            if captain_role and captain_role in member.roles:
                return True
        if team_role and team_role in member.roles:
            return True
        return False

    def _parse_score_input(self, value: str) -> Optional[Tuple[int, int]]:
        cleaned = value.replace(" ", "")
        if "-" not in cleaned:
            return None
        parts = cleaned.split("-", 1)
        if len(parts) != 2:
            return None
        try:
            left = int(parts[0])
            right = int(parts[1])
        except ValueError:
            return None
        return left, right

    async def _post_score_panels(self, guild: discord.Guild) -> None:
        text_channel_ids = await self.config.guild(guild).team_text_channel_ids()
        if not text_channel_ids:
            return
        panel_map = await self.config.guild(guild).score_panel_message_ids()
        if not isinstance(panel_map, dict):
            panel_map = {}

        updated_map: Dict[str, int] = {}
        for channel_id in text_channel_ids:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            existing_id = panel_map.get(str(channel_id))
            message = None
            if existing_id:
                try:
                    message = await channel.fetch_message(existing_id)
                except discord.HTTPException:
                    message = None
            if message:
                try:
                    await message.edit(
                        content="Use the button below to report your match score:",
                        view=ScoreReportView(self),
                    )
                    updated_map[str(channel_id)] = message.id
                    continue
                except discord.HTTPException:
                    message = None
            try:
                message = await channel.send(
                    "Use the button below to report your match score:",
                    view=ScoreReportView(self),
                )
                updated_map[str(channel_id)] = message.id
            except discord.HTTPException:
                continue
        await self.config.guild(guild).score_panel_message_ids.set(updated_map)

    async def _get_participant_name_map(self, guild: discord.Guild) -> Dict[int, str]:
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key or not slug:
            return {}
        ok, payload = await self._challonge_request(
            guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        if not ok:
            return {}
        participants = payload if isinstance(payload, list) else []
        name_map: Dict[int, str] = {}
        for entry in participants:
            participant = entry.get("participant", {})
            pid = participant.get("id")
            name = participant.get("name")
            if pid is not None and name:
                name_map[int(pid)] = name
        return name_map

    async def _get_open_matches_for_participant(
        self, guild: discord.Guild, participant_id: int
    ) -> List[Dict[str, Any]]:
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key or not slug:
            return []
        ok, payload = await self._challonge_request(
            guild, "GET", f"/tournaments/{slug}/matches.json"
        )
        if not ok:
            return []
        matches = payload if isinstance(payload, list) else []
        filtered: List[Dict[str, Any]] = []
        for entry in matches:
            match = entry.get("match", {})
            state = match.get("state")
            if state not in {"open", "pending"}:
                continue
            p1 = match.get("player1_id") or match.get("participant1_id")
            p2 = match.get("player2_id") or match.get("participant2_id")
            if participant_id in {p1, p2}:
                filtered.append(match)
        return filtered

    async def _submit_match_score(
        self,
        guild: discord.Guild,
        *,
        match_id: int,
        participant_id: int,
        score_input: str,
    ) -> Tuple[bool, str]:
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key or not slug:
            return False, "Challonge API key or tournament not set."

        ok, payload = await self._challonge_request(
            guild, "GET", f"/tournaments/{slug}/matches/{match_id}.json"
        )
        if not ok:
            return False, f"Challonge match fetch failed: {payload}"
        match = payload.get("match") if isinstance(payload, dict) else None
        if not match:
            return False, "Match not found."

        p1 = match.get("player1_id") or match.get("participant1_id")
        p2 = match.get("player2_id") or match.get("participant2_id")
        if participant_id not in {p1, p2}:
            return False, "Your team is not part of that match."

        scores = self._parse_score_input(score_input)
        if not scores:
            return False, "Score must be in the form `X-Y`."
        our_score, opp_score = scores
        if our_score == opp_score:
            return False, "Scores cannot be tied."

        opponent_id = p2 if participant_id == p1 else p1
        winner_id = participant_id if our_score > opp_score else opponent_id
        scores_csv = f"{our_score}-{opp_score}"

        ok, result = await self._challonge_request(
            guild,
            "PUT",
            f"/tournaments/{slug}/matches/{match_id}.json",
            data={
                "match[scores_csv]": scores_csv,
                "match[winner_id]": winner_id,
            },
        )
        if not ok:
            return False, f"Challonge update failed: {result}"
        return True, "Score submitted."

    async def _start_score_report(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if not guild or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Score reporting must be used in a team text channel.", ephemeral=True
            )
            return

        team_number = await self._get_team_number_for_channel(guild, channel)
        if not team_number:
            await interaction.response.send_message(
                "This is not a team channel.", ephemeral=True
            )
            return

        team_role = self._get_team_role_for_number(guild, team_number)
        captain_role_id = await self.config.guild(guild).team_captain_role_id()
        if not self._member_can_report_score(interaction.user, team_role, captain_role_id):
            await interaction.response.send_message(
                "Only team captains or mods can report scores.", ephemeral=True
            )
            return

        team_map = await self.config.guild(guild).challonge_team_map()
        if not isinstance(team_map, dict):
            team_map = {}
        participant_id = team_map.get(str(team_number))
        if not participant_id:
            await interaction.response.send_message(
                "No Challonge team mapping for this channel. Use `ccchallonge teammap set`.",
                ephemeral=True,
            )
            return

        matches = await self._get_open_matches_for_participant(guild, int(participant_id))
        if not matches:
            await interaction.response.send_message(
                "No open matches found for your team.", ephemeral=True
            )
            return

        name_map = await self._get_participant_name_map(guild)
        if len(matches) == 1:
            match = matches[0]
            p1 = match.get("player1_id") or match.get("participant1_id")
            p2 = match.get("player2_id") or match.get("participant2_id")
            opponent_id = p2 if int(participant_id) == p1 else p1
            opponent_name = name_map.get(opponent_id)
            await interaction.response.send_modal(
                ScoreReportModal(
                    self,
                    guild.id,
                    int(match.get("id")),
                    int(participant_id),
                    opponent_name=opponent_name,
                )
            )
            return

        view = ScoreMatchSelectView(
            self, guild.id, int(participant_id), matches, name_map
        )
        await interaction.response.send_message(
            "Select the match you want to report:",
            view=view,
            ephemeral=True,
        )

    async def _ensure_team_voice_channels(
        self,
        guild: discord.Guild,
        *,
        category: discord.CategoryChannel,
        team_count: int,
        prefix: str,
        team_roles: Optional[Dict[int, discord.Role]] = None,
    ) -> List[int]:
        existing_channels: Dict[int, discord.VoiceChannel] = {}
        for channel in category.voice_channels:
            team_number = self._extract_team_number(channel.name or "")
            if team_number:
                existing_channels[team_number] = channel

        created_ids: List[int] = []
        missing_roles: List[int] = []
        failed: List[int] = []
        failed_details: List[str] = []
        for index in range(1, team_count + 1):
            emoji = self._team_emoji(index)
            name = f"{emoji} {prefix} {index}"
            channel = existing_channels.get(index)
            team_role = team_roles.get(index) if team_roles else None
            if team_role is None:
                missing_roles.append(index)
                continue
            if channel is None:
                try:
                    overwrites = self._build_team_overwrites(
                        guild, team_role=team_role, is_voice=True
                    )
                    channel = await guild.create_voice_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites,
                        reason="Champions Circle setup",
                    )
                except discord.HTTPException as exc:
                    fallback_name = f"{prefix} {index}"
                    if getattr(exc, "code", None) == 50035 and fallback_name:
                        try:
                            overwrites = self._build_team_overwrites(
                                guild, team_role=team_role, is_voice=True
                            )
                            channel = await guild.create_voice_channel(
                                name=fallback_name,
                                category=category,
                                overwrites=overwrites,
                                reason="Champions Circle setup (fallback name)",
                            )
                            created_ids.append(channel.id)
                            failed_details.append(
                                f"Team {index}: voice channel name fallback used ({name} -> {fallback_name})"
                            )
                            continue
                        except discord.HTTPException as retry_exc:
                            failed_details.append(
                                f"Team {index}: HTTP {getattr(retry_exc, 'status', '??')} "
                                f"(code {getattr(retry_exc, 'code', 'n/a')})"
                            )
                    else:
                        failed_details.append(
                            f"Team {index}: HTTP {getattr(exc, 'status', '??')} "
                            f"(code {getattr(exc, 'code', 'n/a')})"
                        )
                    failed.append(index)
                    continue
            else:
                if channel.name != name:
                    try:
                        await channel.edit(name=name)
                    except discord.HTTPException:
                        pass
                overwrites = dict(channel.overwrites)
                overwrites.update(
                    self._build_team_overwrites(
                        guild, team_role=team_role, is_voice=True
                    )
                )
                try:
                    await channel.edit(overwrites=overwrites)
                except discord.HTTPException:
                    pass
            created_ids.append(channel.id)
        await self.config.guild(guild).team_voice_channel_ids.set(created_ids)
        await self.config.guild(guild).team_voice_category_id.set(category.id)
        if missing_roles:
            self.logger.error(
                "Missing team roles for voice channels: %s",
                ", ".join(str(team) for team in sorted(set(missing_roles))),
            )
        if failed:
            self.logger.error(
                "Failed to create voice channels for teams: %s",
                ", ".join(str(team) for team in sorted(set(failed))),
            )
            if failed_details:
                self.logger.error("Voice channel create errors: %s", " | ".join(failed_details))
                await self._notify_team_asset_issue(
                    guild,
                    message="Voice channel errors: " + " | ".join(failed_details),
                )
        return created_ids

    async def _ensure_team_text_channels(
        self,
        guild: discord.Guild,
        *,
        category: discord.CategoryChannel,
        team_count: int,
        prefix: str,
        team_roles: Dict[int, discord.Role],
    ) -> List[int]:
        existing_channels: Dict[int, discord.TextChannel] = {}
        for channel in category.text_channels:
            team_number = self._extract_team_number(channel.name or "")
            if team_number:
                existing_channels[team_number] = channel

        created_ids: List[int] = []
        missing_roles: List[int] = []
        failed: List[int] = []
        failed_details: List[str] = []
        for index in range(1, team_count + 1):
            name = self._slugify_channel_name(f"{prefix} {index}")
            channel = existing_channels.get(index)
            team_role = team_roles.get(index)
            if team_role is None:
                missing_roles.append(index)
                continue
            if channel is None:
                try:
                    overwrites = self._build_team_overwrites(
                        guild, team_role=team_role, is_voice=False
                    )
                    channel = await guild.create_text_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites,
                        reason="Champions Circle setup",
                    )
                except discord.HTTPException as exc:
                    failed_details.append(
                        f"Team {index}: HTTP {getattr(exc, 'status', '??')} (code {getattr(exc, 'code', 'n/a')})"
                    )
                    failed.append(index)
                    continue
            else:
                if channel.name != name:
                    try:
                        await channel.edit(name=name)
                    except discord.HTTPException:
                        pass
                overwrites = dict(channel.overwrites)
                overwrites.update(
                    self._build_team_overwrites(
                        guild, team_role=team_role, is_voice=False
                    )
                )
                try:
                    await channel.edit(overwrites=overwrites)
                except discord.HTTPException:
                    pass
            created_ids.append(channel.id)
        await self.config.guild(guild).team_text_channel_ids.set(created_ids)
        if missing_roles:
            self.logger.error(
                "Missing team roles for text channels: %s",
                ", ".join(str(team) for team in sorted(set(missing_roles))),
            )
        if failed:
            self.logger.error(
                "Failed to create text channels for teams: %s",
                ", ".join(str(team) for team in sorted(set(failed))),
            )
            if failed_details:
                self.logger.error("Text channel create errors: %s", " | ".join(failed_details))
                await self._notify_team_asset_issue(
                    guild,
                    message="Text channel errors: " + " | ".join(failed_details),
                )
        return created_ids

    async def _create_team_voice_assets(self, guild: discord.Guild) -> None:
        team_count = await self.config.guild(guild).team_count()
        if not team_count or team_count <= 0:
            return
        prefix = await self.config.guild(guild).team_voice_channel_prefix() or "Team"
        category_name = f"Champions Circle - {team_count} Teams"
        category = await self._ensure_voice_category(guild, category_name)
        if not category:
            return
        bot_member = guild.me or guild.get_member(self.bot.user.id)
        if bot_member:
            perms = bot_member.guild_permissions
            if not perms.manage_roles or not perms.manage_channels:
                await self._notify_team_asset_issue(
                    guild,
                    message=(
                        "Bot lacks required permissions to create team assets. "
                        "manage_roles={manage_roles}, manage_channels={manage_channels}"
                    ).format(
                        manage_roles=perms.manage_roles,
                        manage_channels=perms.manage_channels,
                    ),
                )
                return
        overwrites = dict(category.overwrites)
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                manage_channels=True,
                connect=True,
                speak=True,
                send_messages=True,
                read_message_history=True,
            )
        try:
            await category.edit(overwrites=overwrites)
        except discord.HTTPException:
            pass
        team_roles = await self._ensure_team_roles(guild, team_count=team_count, prefix=prefix)
        if len(team_roles) < team_count:
            await self._notify_team_asset_issue(
                guild,
                message=(
                    "Team roles could not be created. "
                    "Please ensure the bot has **Manage Roles** and that its top role "
                    "is above the team roles."
                ),
            )
            return
        voice_ids = await self._ensure_team_voice_channels(
            guild,
            category=category,
            team_count=team_count,
            prefix=prefix,
            team_roles=team_roles,
        )
        text_ids = await self._ensure_team_text_channels(
            guild,
            category=category,
            team_count=team_count,
            prefix=prefix,
            team_roles=team_roles,
        )
        if len(voice_ids) < team_count:
            await self._notify_team_asset_issue(
                guild,
                message=(
                    "Some team voice channels failed to create. "
                    "Check Manage Channels/Permissions and bot role hierarchy."
                ),
            )
        if len(text_ids) < team_count:
            await self._notify_team_asset_issue(
                guild,
                message=(
                    "Some team channels failed to create. "
                    "Please ensure the bot has **Manage Channels** and **Manage Permissions**."
                ),
            )
        await self._post_score_panels(guild)

    async def _cleanup_team_voice_assets(self, guild: discord.Guild) -> None:
        text_channel_ids = await self.config.guild(guild).team_text_channel_ids()
        if text_channel_ids:
            for channel_id in text_channel_ids:
                channel = guild.get_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    try:
                        await channel.delete(reason="Champions Circle cleanup")
                    except discord.HTTPException:
                        continue

        channel_ids = await self.config.guild(guild).team_voice_channel_ids()
        if channel_ids:
            for channel_id in channel_ids:
                channel = guild.get_channel(channel_id)
                if isinstance(channel, discord.VoiceChannel):
                    try:
                        await channel.delete(reason="Champions Circle cleanup")
                    except discord.HTTPException:
                        continue

        category_id = await self.config.guild(guild).team_voice_category_id()
        category = guild.get_channel(category_id) if category_id else None
        if isinstance(category, discord.CategoryChannel):
            try:
                await category.delete(reason="Champions Circle cleanup")
            except discord.HTTPException:
                pass

        role_ids = await self.config.guild(guild).team_role_ids()
        if role_ids:
            for role_id in role_ids:
                role = guild.get_role(role_id)
                if role:
                    try:
                        await role.delete(reason="Champions Circle cleanup")
                    except discord.HTTPException:
                        continue
        captain_role_id = await self.config.guild(guild).team_captain_role_id()
        captain_role = guild.get_role(captain_role_id) if captain_role_id else None
        if captain_role:
            bot_member = guild.me or guild.get_member(self.bot.user.id)
            if not bot_member:
                self.logger.error("Cannot delete Team Captain role: bot member not found.")
            elif not bot_member.guild_permissions.manage_roles:
                await self._notify_team_asset_issue(
                    guild,
                    message="Cannot delete Team Captain role: bot lacks Manage Roles.",
                )
            elif captain_role >= bot_member.top_role:
                await self._notify_team_asset_issue(
                    guild,
                    message=(
                        "Cannot delete Team Captain role: role is above the bot. "
                        "Move the bot role above Team Captain."
                    ),
                )
            else:
                try:
                    await captain_role.delete(reason="Champions Circle cleanup")
                    await self.config.guild(guild).team_captain_role_id.set(None)
                except discord.HTTPException as exc:
                    await self._notify_team_asset_issue(
                        guild,
                        message=(
                            "Failed to delete Team Captain role "
                            f"(HTTP {getattr(exc, 'status', '??')}, code {getattr(exc, 'code', 'n/a')})."
                        ),
                    )
        else:
            await self.config.guild(guild).team_captain_role_id.set(None)
        await self.config.guild(guild).score_panel_message_ids.set({})

    def _parse_challonge_slug(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        cleaned = value.strip()
        if cleaned.lower() in {"clear", "none", "unset"}:
            return None
        if cleaned.startswith("http"):
            parsed = urlparse(cleaned)
            path = parsed.path.strip("/")
            if not path:
                return None
            parts = [part for part in path.split("/") if part]
            if "tournaments" in parts and "signup" in parts:
                try:
                    idx = parts.index("signup")
                    if idx + 1 < len(parts):
                        return parts[idx + 1]
                except ValueError:
                    pass
            return parts[-1]
        return cleaned

    async def _get_challonge_credentials(self, guild: discord.Guild) -> Tuple[Optional[str], Optional[str]]:
        api_key = await self.config.guild(guild).tourney_challonge_api_key()
        slug = await self.config.guild(guild).tourney_challonge_slug()
        return api_key, slug

    async def _sync_challonge_participant(
        self, guild: discord.Guild, user_id: int, *, add: bool
    ) -> None:
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key or not slug:
            return

        stored_map = await self.config.guild(guild).tourney_challonge_participant_map()
        if not isinstance(stored_map, dict):
            stored_map = {}

        user_id_str = str(user_id)
        member = guild.get_member(user_id)
        display_name = member.display_name if member else f"User {user_id}"

        if add:
            ok, participants_payload = await self._challonge_request(
                guild, "GET", f"/tournaments/{slug}/participants.json"
            )
            if not ok:
                self.logger.error("Challonge sync failed for %s: %s", user_id, participants_payload)
                return

            participants = participants_payload if isinstance(participants_payload, list) else []
            existing_by_name: Dict[str, int] = {}
            existing_ids = set()
            for entry in participants:
                participant = entry.get("participant", {})
                pid = participant.get("id")
                name = participant.get("name")
                if pid is not None:
                    existing_ids.add(int(pid))
                if name:
                    existing_by_name[name.lower()] = int(pid) if pid is not None else None

            existing_id = stored_map.get(user_id_str)
            if existing_id and int(existing_id) in existing_ids:
                return

            name_key = display_name.lower()
            if name_key in existing_by_name and existing_by_name[name_key]:
                stored_map[user_id_str] = existing_by_name[name_key]
                await self.config.guild(guild).tourney_challonge_participant_map.set(stored_map)
                return

            candidate_name = display_name
            if name_key in existing_by_name:
                candidate_name = f"{display_name} ({str(user_id)[-4:]})"

            ok, create_payload = await self._challonge_request(
                guild,
                "POST",
                f"/tournaments/{slug}/participants.json",
                data={"participant[name]": candidate_name},
            )
            if not ok:
                self.logger.error("Challonge add failed for %s: %s", user_id, create_payload)
                return
            participant = (
                create_payload.get("participant")
                if isinstance(create_payload, dict)
                else None
            )
            if participant and participant.get("id"):
                stored_map[user_id_str] = participant.get("id")
                await self.config.guild(guild).tourney_challonge_participant_map.set(stored_map)
            return

        participant_id = stored_map.get(user_id_str)
        link_entry = await self._get_challonge_link(guild, user_id)
        if link_entry and link_entry.get("participant_id"):
            participant_id = link_entry.get("participant_id")
        if not participant_id:
            return
        links = await self._get_challonge_links(guild)
        if links and link_entry:
            for other_user, other_link in links.items():
                if other_user == user_id_str:
                    continue
                if other_link.get("participant_id") == participant_id:
                    return
        for other_user, other_pid in stored_map.items():
            if other_user == user_id_str:
                continue
            try:
                if int(other_pid) == int(participant_id):
                    return
            except (TypeError, ValueError):
                continue
        ok, payload = await self._challonge_request(
            guild,
            "DELETE",
            f"/tournaments/{slug}/participants/{participant_id}.json",
        )
        if ok or "not found" in str(payload).lower():
            stored_map.pop(user_id_str, None)
            await self.config.guild(guild).tourney_challonge_participant_map.set(stored_map)

    async def _get_challonge_links(self, guild: discord.Guild) -> Dict[str, Any]:
        links = await self.config.guild(guild).challonge_links()
        if not isinstance(links, dict):
            links = {}
        return links

    async def _get_challonge_link(self, guild: discord.Guild, user_id: int) -> Optional[Dict[str, Any]]:
        links = await self._get_challonge_links(guild)
        return links.get(str(user_id))

    async def _set_challonge_link(
        self,
        guild: discord.Guild,
        *,
        user_id: int,
        participant_id: int,
        participant_name: str,
    ) -> None:
        links = await self._get_challonge_links(guild)
        links[str(user_id)] = {
            "participant_id": int(participant_id),
            "participant_name": participant_name,
            "linked_at": int(datetime.now(timezone.utc).timestamp()),
        }
        await self.config.guild(guild).challonge_links.set(links)
        stored_map = await self.config.guild(guild).tourney_challonge_participant_map()
        if not isinstance(stored_map, dict):
            stored_map = {}
        stored_map[str(user_id)] = int(participant_id)
        await self.config.guild(guild).tourney_challonge_participant_map.set(stored_map)

    async def _remove_challonge_link(self, guild: discord.Guild, user_id: int) -> None:
        links = await self._get_challonge_links(guild)
        links.pop(str(user_id), None)
        await self.config.guild(guild).challonge_links.set(links)

    async def _resolve_challonge_participant(
        self, guild: discord.Guild, value: str
    ) -> Optional[Dict[str, Any]]:
        if not value:
            return None
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key or not slug:
            return None
        ok, payload = await self._challonge_request(
            guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        if not ok:
            return None
        participants = payload if isinstance(payload, list) else []
        cleaned = value.strip()
        participant_id = int(cleaned) if cleaned.isdigit() else None
        name_matches: List[Dict[str, Any]] = []
        for entry in participants:
            participant = entry.get("participant", {})
            if participant_id and int(participant.get("id", 0)) == participant_id:
                return participant
            name = participant.get("name")
            if name and name.lower() == cleaned.lower():
                name_matches.append(participant)
        if participant_id:
            return None
        if len(name_matches) == 1:
            return name_matches[0]
        return None

    async def _sync_roles_from_challonge(
        self, guild: discord.Guild
    ) -> Tuple[int, int, Optional[str]]:
        """Sync team roles from draft assignments (primary) or legacy challonge links."""
        role_ids = await self.config.guild(guild).team_role_ids()
        team_roles = [guild.get_role(role_id) for role_id in role_ids or []]
        team_roles = [role for role in team_roles if role]
        if not team_roles:
            return 0, 0, "No team roles found."

        # Try draft assignments first (new system)
        draft_assignments = await self.config.guild(guild).draft_assignments()
        if isinstance(draft_assignments, dict) and draft_assignments:
            assigned = 0
            missing = 0
            for team_str, user_ids in draft_assignments.items():
                try:
                    team_index = int(team_str)
                except ValueError:
                    continue
                if team_index <= 0 or team_index > len(team_roles):
                    continue
                if not isinstance(user_ids, list):
                    continue

                target_role = team_roles[team_index - 1]
                for user_id in user_ids:
                    member = guild.get_member(user_id)
                    if not member:
                        missing += 1
                        continue
                    # Remove other team roles
                    remove_roles = [
                        role for role in team_roles if role != target_role and role in member.roles
                    ]
                    try:
                        if remove_roles:
                            await member.remove_roles(*remove_roles)
                        if target_role not in member.roles:
                            await member.add_roles(target_role)
                        assigned += 1
                    except discord.HTTPException:
                        missing += 1

            return assigned, missing, None

        # Fall back to legacy challonge links system
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key:
            return 0, 0, "Challonge API key is not set."
        if not slug:
            return 0, 0, "Challonge tournament is not set."

        ok, participants_payload = await self._challonge_request(
            guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        if not ok:
            return 0, 0, f"Challonge API error: {participants_payload}"

        participant_to_team: Dict[int, int] = {}
        team_map = await self.config.guild(guild).challonge_team_map()
        if not isinstance(team_map, dict):
            team_map = {}

        participants = participants_payload if isinstance(participants_payload, list) else []
        if team_map:
            for team_str, participant_id in team_map.items():
                try:
                    team_index = int(team_str)
                except ValueError:
                    continue
                if team_index <= 0 or team_index > len(team_roles):
                    continue
                try:
                    participant_to_team[int(participant_id)] = team_index
                except (TypeError, ValueError):
                    continue
        else:
            ordered = []
            for entry in participants:
                participant = entry.get("participant", {})
                seed = participant.get("seed") or 0
                pid = participant.get("id") or 0
                ordered.append((seed, pid, participant))
            ordered.sort(key=lambda item: (item[0] or 0, item[1] or 0))

            for idx, (_, _, participant) in enumerate(ordered, start=1):
                if idx > len(team_roles):
                    break
                pid = participant.get("id")
                if pid is not None:
                    participant_to_team[int(pid)] = idx

        links = await self._get_challonge_links(guild)
        assigned = 0
        missing = 0
        for user_id_str, link in links.items():
            participant_id = link.get("participant_id")
            team_index = participant_to_team.get(participant_id)
            member = guild.get_member(int(user_id_str))
            if not member or not team_index:
                missing += 1
                continue
            target_role = team_roles[team_index - 1]
            remove_roles = [
                role for role in team_roles if role != target_role and role in member.roles
            ]
            try:
                if remove_roles:
                    await member.remove_roles(*remove_roles)
                if target_role not in member.roles:
                    await member.add_roles(target_role)
                assigned += 1
            except discord.HTTPException:
                missing += 1

        return assigned, missing, None

    async def _try_autolink_challonge_member(
        self, guild: discord.Guild, member: discord.Member
    ) -> Optional[Dict[str, Any]]:
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key or not slug:
            return None
        ok, payload = await self._challonge_request(
            guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        if not ok:
            return None
        participants = payload if isinstance(payload, list) else []
        candidates = []
        names = {member.display_name.lower(), member.name.lower()}
        for entry in participants:
            participant = entry.get("participant", {})
            name = participant.get("name") or ""
            if name.lower() in names:
                candidates.append(participant)
        if len(candidates) == 1:
            participant = candidates[0]
            pid = participant.get("id")
            pname = participant.get("name") or str(pid)
            if pid:
                await self._set_challonge_link(
                    guild,
                    user_id=member.id,
                    participant_id=int(pid),
                    participant_name=pname,
                )
                return participant
        return None

    async def _challonge_request(
        self,
        guild: discord.Guild,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Any]:
        api_key, _ = await self._get_challonge_credentials(guild)
        if not api_key:
            return False, "Challonge API key not set."
        merged_params = dict(params or {})
        merged_params["api_key"] = api_key
        url = f"https://api.challonge.com/v1{endpoint}"
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method.upper(),
                    url,
                    params=merged_params,
                    data=data,
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        return False, f"{resp.status}: {text}"
                    try:
                        return True, await resp.json()
                    except Exception:
                        return True, text
        except Exception as exc:
            return False, str(exc)

    def _format_challonge_time(self, value: Optional[str]) -> str:
        if not value:
            return "Not set"
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return f"<t:{int(dt.timestamp())}:F>"
        except Exception:
            return str(value)

    def _build_panel_embed(
        self,
        *,
        guild: discord.Guild,
        title: str,
        description: str,
        status: str,
        time_display: str,
        close_display: str,
        duration: int,
        counts: Dict[str, int],
        forum: Optional[discord.abc.GuildChannel],
        roster_url: Optional[str],
        bracket_url: Optional[str],
        updated_ts: int,
    ) -> discord.Embed:
        trimmed_description = self._truncate_text(description or "No description set.", 1200)
        embed = discord.Embed(
            title=title,
            description=trimmed_description,
            color=0x00ff00,
        )
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Time", value=time_display, inline=True)
        embed.add_field(name="Applications close", value=close_display, inline=True)
        embed.add_field(name="Duration", value=f"{duration} days", inline=True)
        embed.add_field(
            name="Applications",
            value=(
                f"🟡 Active: {counts['active']}\n"
                f"🟢 Approved: {counts['approved']}\n"
                f"🔴 Denied: {counts['denied']}\n"
                f"⚪ Cancelled: {counts['cancelled']}"
            ),
            inline=True,
        )
        if forum:
            embed.add_field(name="Review", value=f"Applications live in {forum.mention}.", inline=False)
        if roster_url:
            embed.add_field(name="Roster", value=f"[Open roster]({roster_url})", inline=False)
        if bracket_url:
            embed.add_field(name="Challonge", value=f"[Bracket]({bracket_url})", inline=False)
        embed.add_field(
            name="How to apply",
            value="Use the buttons below to submit or cancel your application.",
            inline=False,
        )
        embed.timestamp = datetime.now(timezone.utc)
        embed.set_footer(text="Champions Circle - Last updated")
        return embed

    def _build_panel_view(
        self,
        *,
        guild: discord.Guild,
        title: str,
        description: str,
        status: str,
        time_display: str,
        close_display: str,
        duration: int,
        counts: Dict[str, int],
        forum: Optional[discord.abc.GuildChannel],
        roster_url: Optional[str],
        bracket_url: Optional[str],
        updated_ts: int,
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Color.green())

        trimmed_description = self._truncate_text(description or "No description set.", 1200)
        header_text = f"**{title}**\n{trimmed_description}"
        container.add_item(discord.ui.TextDisplay(header_text))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        overview_lines = [
            f"**Status:** {status}",
            f"**Time:** {time_display}",
            f"**Applications close:** {close_display}",
            f"**Duration:** {duration} days",
        ]
        counts_lines = [
            f"🟡 Active: {counts['active']}",
            f"🟢 Approved: {counts['approved']}",
            f"🔴 Denied: {counts['denied']}",
            f"⚪ Cancelled: {counts['cancelled']}",
        ]

        thumbnail_url = None
        if guild.icon:
            thumbnail_url = guild.icon.url
        elif self.bot.user:
            thumbnail_url = self.bot.user.display_avatar.url
        if thumbnail_url is None:
            thumbnail_url = "https://cdn.discordapp.com/embed/avatars/0.png"

        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay("\n".join(overview_lines)),
                discord.ui.TextDisplay("\n".join(counts_lines)),
                accessory=discord.ui.Thumbnail(thumbnail_url),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        review_line = f"Review applications in {forum.mention}." if forum else "Applications forum not configured."
        instructions = (
            "_Use the buttons below to apply or cancel._\n"
            f"{review_line}\n"
            f"_Last updated <t:{updated_ts}:R>_"
        )
        container.add_item(discord.ui.TextDisplay(instructions))

        view.add_item(container)

        row = discord.ui.ActionRow()
        row.add_item(JoinButton(self))
        row.add_item(CancelApplicationButton(self))
        if roster_url:
            row.add_item(discord.ui.Button(label="Roster", style=discord.ButtonStyle.link, url=roster_url))
        if bracket_url:
            row.add_item(discord.ui.Button(label="Challonge Bracket", style=discord.ButtonStyle.link, url=bracket_url))
        view.add_item(row)

        return view

    async def update_embed(self, guild):
        tourney_title = await self.config.guild(guild).tourney_title()
        tourney_description = await self.config.guild(guild).tourney_description()
        tourney_time = await self.config.guild(guild).tourney_time()
        duration = await self.config.guild(guild).application_duration()

        active_applications = await self._load_application_list(guild, "active_applications")
        approved_applications = await self._load_application_list(guild, "approved_applications")
        denied_applications = await self._load_application_list(guild, "denied_applications")
        cancelled_applications = await self._load_application_list(guild, "cancelled_applications")

        counts = {
            "active": len(active_applications),
            "approved": len(approved_applications),
            "denied": len(denied_applications),
            "cancelled": len(cancelled_applications),
        }

        applications_open = await self.config.guild(guild).applications_open()
        opened_at = await self.config.guild(guild).applications_opened_at()
        if applications_open and not opened_at:
            opened_at = int(datetime.now(timezone.utc).timestamp())
            await self.config.guild(guild).applications_opened_at.set(opened_at)
        status = "Open" if applications_open else "Not started"
        time_display = self._format_time_display(tourney_time)
        close_display = "Not set"
        if opened_at:
            close_ts = int(opened_at + (duration * 86400))
            close_display = f"<t:{close_ts}:F> (<t:{close_ts}:R>)"
        updated_ts = int(datetime.now(timezone.utc).timestamp())

        forum_id = await self.config.guild(guild).champions_forum()
        forum = guild.get_channel(forum_id) if forum_id else None
        bracket_url = self._normalize_url(
            await self.config.guild(guild).tourney_challonge_bracket_url()
        )
        roster_url = None
        if applications_open:
            roster_url = await self._update_roster_board(guild)

        panel_view = self._build_panel_view(
            guild=guild,
            title=tourney_title,
            description=tourney_description,
            status=status,
            time_display=time_display,
            close_display=close_display,
            duration=duration,
            counts=counts,
            forum=forum,
            roster_url=roster_url,
            bracket_url=bracket_url,
            updated_ts=updated_ts,
        )
        fallback_embed = self._build_panel_embed(
            guild=guild,
            title=tourney_title,
            description=tourney_description,
            status=status,
            time_display=time_display,
            close_display=close_display,
            duration=duration,
            counts=counts,
            forum=forum,
            roster_url=roster_url,
            bracket_url=bracket_url,
            updated_ts=updated_ts,
        )

        message_id = await self.config.guild(guild).champions_message_id()
        if not applications_open and not message_id:
            return

        channel_id = await self.config.guild(guild).champions_channel()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            self.logger.error(f"Error: Channel with ID {channel_id} not found.")
            return

        try:
            if message_id:
                message = await channel.fetch_message(message_id)
                await message.edit(content=None, embed=None, view=panel_view)
            else:
                message = await channel.send(view=panel_view)
                await self.config.guild(guild).champions_message_id.set(message.id)
        except discord.HTTPException as e:
            self.logger.error(f"Error updating panel view: {str(e)}")
            try:
                fallback_view = ChampionsApplyView(self)
                message_id = await self.config.guild(guild).champions_message_id()
                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                        await message.edit(embed=fallback_embed, view=fallback_view)
                        return
                    except discord.HTTPException:
                        await self.config.guild(guild).champions_message_id.set(None)
                message = await channel.send(embed=fallback_embed, view=fallback_view)
                await self.config.guild(guild).champions_message_id.set(message.id)
            except discord.HTTPException as exc:
                self.logger.error(f"Error updating embed fallback: {str(exc)}")

    @commands.command()
    @guild_only()
    async def cancel_application(self, ctx):
        """Cancel your Champions Circle application."""
        list_name, entry, _ = await self._find_application(ctx.guild, ctx.author.id)
        if not entry:
            await ctx.send("You don't have an active Champions Circle application.")
            return
        if list_name == "cancelled_applications":
            await ctx.send("Your application is already cancelled.")
            return

        if list_name == "approved_applications":
            role_id = await self.config.guild(ctx.guild).champions_role_id()
            role = ctx.guild.get_role(role_id) if role_id else None
            if role and role in ctx.author.roles:
                try:
                    await ctx.author.remove_roles(role)
                except discord.HTTPException:
                    self.logger.error("Failed to remove Champions role from %s", ctx.author.name)

        thread_id = entry.get("thread_id")
        if thread_id:
            thread = ctx.guild.get_thread(thread_id)
            if isinstance(thread, discord.Thread) and thread.parent:
                cancelled_tag = next(
                    (tag for tag in thread.parent.available_tags if tag.name.lower() == "cancelled"),
                    None,
                )
                if cancelled_tag:
                    try:
                        await thread.edit(applied_tags=[cancelled_tag])
                    except discord.HTTPException:
                        self.logger.error("Failed to tag cancelled application thread %s", thread_id)
                embed = discord.Embed(
                    title="Application Cancelled",
                    description=f"{ctx.author.mention} cancelled their application.",
                    color=discord.Color.orange(),
                )
                await thread.send(embed=embed)

        await self._move_application(ctx.guild, ctx.author.id, "cancelled_applications", entry=entry)
        await self._sync_challonge_participant(ctx.guild, ctx.author.id, add=False)
        await self._remove_challonge_link(ctx.guild, ctx.author.id)
        await self.update_embed(ctx.guild)
        await ctx.send("Your Champions Circle application has been cancelled.")

    @commands.command()
    @commands.admin_or_permissions(administrator=True)
    @guild_only()
    async def setchampionschannel(self, ctx, channel: discord.TextChannel):
        """Set the Champions Circle channel."""
        await self.config.guild(ctx.guild).champions_channel.set(channel.id)
        await ctx.send(f"Champions Circle channel set to {channel.mention}")

    @commands.command()
    @commands.admin_or_permissions(administrator=True)
    @guild_only()
    async def setapplicationduration(self, ctx, days: int):
        """Set the duration for which applications remain open."""
        await self.config.guild(ctx.guild).application_duration.set(days)
        await ctx.send(f"Application duration set to {days} days.")

    @commands.command()
    @commands.admin_or_permissions(administrator=True)
    @guild_only()
    async def ccsettime(self, ctx, *, time_value: Optional[str] = None):
        """Set or clear the tournament time (YYYY-MM-DD HH:MM:SS)."""
        if not time_value or time_value.strip().lower() in {"clear", "none", "unset"}:
            await self.config.guild(ctx.guild).tourney_time.set(None)
            await self.update_embed(ctx.guild)
            await ctx.send("Tournament time cleared.")
            return

        try:
            tourney_time = datetime.fromisoformat(time_value.strip()).replace(tzinfo=timezone.utc)
        except ValueError:
            await ctx.send("Invalid time format. Use YYYY-MM-DD HH:MM:SS or `ccsettime clear`.")
            return

        timestamp = int(tourney_time.timestamp())
        await self.config.guild(ctx.guild).tourney_time.set(timestamp)
        await self.update_embed(ctx.guild)
        await ctx.send(f"Tournament time updated: <t:{timestamp}:F>")

    @commands.group(name="ccchallonge")
    @commands.admin_or_permissions(administrator=True)
    @guild_only()
    async def ccchallonge(self, ctx):
        """Manage Challonge settings for the tournament."""
        if ctx.invoked_subcommand is None:
            bracket_url = await self.config.guild(ctx.guild).tourney_challonge_bracket_url()
            api_key = await self.config.guild(ctx.guild).tourney_challonge_api_key()
            slug = await self.config.guild(ctx.guild).tourney_challonge_slug()
            team_map = await self.config.guild(ctx.guild).challonge_team_map()
            lines = []
            if slug:
                lines.append(f"Tournament: `{slug}`")
            if bracket_url:
                lines.append(f"Bracket: {bracket_url}")
            if api_key:
                masked = f"{api_key[:4]}***{api_key[-4:]}" if len(api_key) > 8 else "***"
                lines.append(f"API key: set ({masked})")
            if team_map:
                lines.append(f"Teams mapped: {len(team_map)}")
            if not lines:
                await ctx.send("No Challonge configuration set.")
            else:
                await ctx.send("\n".join(lines))

    @ccchallonge.command(name="set")
    async def ccchallonge_set(
        self,
        ctx,
        bracket_url: str,
    ):
        """Set the Challonge bracket URL."""
        bracket_value = self._normalize_url(bracket_url)
        await self.config.guild(ctx.guild).tourney_challonge_bracket_url.set(bracket_value)
        await self.update_embed(ctx.guild)
        await ctx.send(f"Bracket URL set: {bracket_value}")

    @ccchallonge.command(name="tournament", aliases=["slug"])
    async def ccchallonge_tournament(self, ctx, *, slug_or_url: Optional[str] = None):
        """Set or clear the Challonge tournament slug. Auto-creates team participants."""
        if not slug_or_url:
            current = await self.config.guild(ctx.guild).tourney_challonge_slug()
            if current:
                await ctx.send(f"Challonge tournament: {current}")
            else:
                await ctx.send("No Challonge tournament set.")
            return

        slug = self._parse_challonge_slug(slug_or_url)
        if not slug:
            await self.config.guild(ctx.guild).tourney_challonge_slug.set(None)
            await self.config.guild(ctx.guild).teams_created_on_challonge.set(False)
            await self.config.guild(ctx.guild).challonge_team_map.set({})
            await ctx.send("Challonge tournament cleared.")
            return

        await self.config.guild(ctx.guild).tourney_challonge_slug.set(slug)

        # Check if API key is set
        api_key = await self.config.guild(ctx.guild).tourney_challonge_api_key()
        team_count = await self.config.guild(ctx.guild).team_count()

        if api_key and team_count > 0:
            await ctx.send(f"Challonge tournament set to `{slug}`. Creating team participants...")
            created, errors = await self._create_challonge_team_participants(ctx.guild)
            if errors:
                await ctx.send(f"Created {created} teams on Challonge ({errors} errors).")
            elif created > 0:
                await ctx.send(f"Created {created} teams on Challonge. Ready for draft!")
            else:
                await ctx.send("Teams already exist on Challonge. Mapped to local teams.")
        else:
            await ctx.send(f"Challonge tournament set to `{slug}`.")
            if not api_key:
                await ctx.send("Set API key with `ccchallonge key <token>` to auto-create teams.")
            if team_count <= 0:
                await ctx.send("Configure teams with `ccsetup` first.")

    @ccchallonge.command(name="refresh")
    async def ccchallonge_refresh(self, ctx):
        """Pull bracket link from Challonge API."""
        api_key, slug = await self._get_challonge_credentials(ctx.guild)
        if not api_key:
            await ctx.send("Challonge API key is not set. Use `ccchallonge key <token>`.")
            return
        if not slug:
            await ctx.send("Challonge tournament is not set. Use `ccchallonge tournament <slug>`.")
            return

        ok, payload = await self._challonge_request(
            ctx.guild, "GET", f"/tournaments/{slug}.json"
        )
        if not ok:
            await ctx.send(f"Challonge API error: {payload}")
            return

        tournament = payload.get("tournament") if isinstance(payload, dict) else None
        if not tournament:
            await ctx.send("Unexpected Challonge response.")
            return

        bracket_url = tournament.get("full_challonge_url")
        if bracket_url:
            await self.config.guild(ctx.guild).tourney_challonge_bracket_url.set(bracket_url)
            await self.update_embed(ctx.guild)
            await ctx.send(f"Bracket URL refreshed: {bracket_url}")
        else:
            await ctx.send("No bracket URL found in Challonge response.")

    @ccchallonge.command(name="clear")
    async def ccchallonge_clear(self, ctx):
        """Clear Challonge bracket URL."""
        await self.config.guild(ctx.guild).tourney_challonge_bracket_url.set(None)
        await self.update_embed(ctx.guild)
        await ctx.send("Bracket URL cleared.")

    @ccchallonge.command(name="createteams")
    @commands.admin_or_permissions(administrator=True)
    async def ccchallonge_createteams(self, ctx):
        """Manually create team participants on Challonge."""
        api_key, slug = await self._get_challonge_credentials(ctx.guild)
        if not api_key:
            await ctx.send("Challonge API key is not set. Use `ccchallonge key <token>`.")
            return
        if not slug:
            await ctx.send("Challonge tournament is not set. Use `ccchallonge tournament <slug>`.")
            return

        team_count = await self.config.guild(ctx.guild).team_count()
        if team_count <= 0:
            await ctx.send("No teams configured. Use `ccsetup` first.")
            return

        await ctx.send("Creating team participants on Challonge...")
        created, errors = await self._create_challonge_team_participants(ctx.guild)

        if errors:
            await ctx.send(f"Created {created} teams ({errors} errors).")
        elif created > 0:
            await ctx.send(f"Created {created} teams on Challonge. Ready for draft!")
        else:
            await ctx.send("Teams already exist on Challonge. Mapped to local teams.")

    @ccchallonge.command(name="key")
    async def ccchallonge_key(self, ctx, *, api_key: Optional[str] = None):
        """Set or clear the Challonge API key."""
        if not api_key:
            current = await self.config.guild(ctx.guild).tourney_challonge_api_key()
            if current:
                masked = f"{current[:4]}***{current[-4:]}" if len(current) > 8 else "***"
                await ctx.send(f"Challonge API key is set ({masked}).")
            else:
                await ctx.send("Challonge API key is not set.")
            return

        if api_key.strip().lower() in {"clear", "none", "unset"}:
            await self.config.guild(ctx.guild).tourney_challonge_api_key.set(None)
            await ctx.send("Challonge API key cleared.")
            return

        await self.config.guild(ctx.guild).tourney_challonge_api_key.set(api_key.strip())
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            await ctx.send(
                "Challonge API key saved. I couldn't delete your message (missing permissions)."
            )
            return
        except discord.HTTPException:
            pass
        await ctx.send("Challonge API key saved.")

    @ccchallonge.command(name="info")
    async def ccchallonge_info(self, ctx):
        """Show Challonge tournament info (v1)."""
        api_key, slug = await self._get_challonge_credentials(ctx.guild)
        if not api_key:
            await ctx.send("Challonge API key is not set. Use `ccchallonge key <token>`.")
            return
        if not slug:
            await ctx.send("Challonge tournament is not set. Use `ccchallonge tournament <slug>`.")
            return

        ok, payload = await self._challonge_request(
            ctx.guild, "GET", f"/tournaments/{slug}.json"
        )
        if not ok:
            await ctx.send(f"Challonge API error: {payload}")
            return

        tournament = payload.get("tournament") if isinstance(payload, dict) else None
        if not tournament:
            await ctx.send("Unexpected Challonge response.")
            return

        embed = discord.Embed(
            title=tournament.get("name") or slug,
            color=discord.Color.orange(),
        )
        embed.add_field(name="State", value=tournament.get("state") or "Unknown", inline=True)
        embed.add_field(
            name="Type", value=tournament.get("tournament_type") or "Unknown", inline=True
        )
        embed.add_field(
            name="Participants",
            value=str(tournament.get("participants_count") or 0),
            inline=True,
        )
        embed.add_field(
            name="Matches",
            value=str(tournament.get("matches_count") or 0),
            inline=True,
        )
        embed.add_field(
            name="Start time",
            value=self._format_challonge_time(tournament.get("started_at")),
            inline=True,
        )
        embed.add_field(
            name="Completed",
            value=self._format_challonge_time(tournament.get("completed_at")),
            inline=True,
        )
        if tournament.get("full_challonge_url"):
            embed.add_field(
                name="Bracket",
                value=f"[Open]({tournament.get('full_challonge_url')})",
                inline=False,
            )
        await ctx.send(embed=embed)

    @ccchallonge.command(name="participants", aliases=["players"])
    async def ccchallonge_participants(self, ctx, limit: int = 40):
        """List Challonge participants (v1)."""
        api_key, slug = await self._get_challonge_credentials(ctx.guild)
        if not api_key:
            await ctx.send("Challonge API key is not set. Use `ccchallonge key <token>`.")
            return
        if not slug:
            await ctx.send("Challonge tournament is not set. Use `ccchallonge tournament <slug>`.")
            return

        ok, payload = await self._challonge_request(
            ctx.guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        if not ok:
            await ctx.send(f"Challonge API error: {payload}")
            return

        participants = payload if isinstance(payload, list) else []
        names = []
        for entry in participants:
            participant = entry.get("participant", {})
            name = participant.get("name") or "Unknown"
            seed = participant.get("seed")
            if seed:
                names.append(f"{seed}. {name}")
            else:
                names.append(name)

        if not names:
            await ctx.send("No participants found.")
            return

        embed = discord.Embed(
            title="Challonge participants",
            color=discord.Color.orange(),
        )
        for idx, chunk in enumerate(self._chunk_lines(names, limit=900), start=1):
            label = "Participants" if idx == 1 else f"Participants ({idx})"
            embed.add_field(name=label, value="\n".join(chunk), inline=False)
            if idx * 30 >= limit:
                break
        await ctx.send(embed=embed)

    @ccchallonge.command(name="matches")
    async def ccchallonge_matches(self, ctx, state: Optional[str] = None, limit: int = 10):
        """Show Challonge matches (v1)."""
        api_key, slug = await self._get_challonge_credentials(ctx.guild)
        if not api_key:
            await ctx.send("Challonge API key is not set. Use `ccchallonge key <token>`.")
            return
        if not slug:
            await ctx.send("Challonge tournament is not set. Use `ccchallonge tournament <slug>`.")
            return

        ok, participants_payload = await self._challonge_request(
            ctx.guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        if not ok:
            await ctx.send(f"Challonge API error: {participants_payload}")
            return

        id_to_name: Dict[int, str] = {}
        for entry in participants_payload if isinstance(participants_payload, list) else []:
            participant = entry.get("participant", {})
            pid = participant.get("id")
            if pid is not None:
                id_to_name[int(pid)] = participant.get("name") or f"Participant {pid}"

        params: Dict[str, Any] = {}
        if state:
            params["state"] = state
        ok, matches_payload = await self._challonge_request(
            ctx.guild, "GET", f"/tournaments/{slug}/matches.json", params=params
        )
        if not ok:
            await ctx.send(f"Challonge API error: {matches_payload}")
            return

        matches = matches_payload if isinstance(matches_payload, list) else []
        if not matches:
            await ctx.send("No matches found.")
            return

        lines: List[str] = []
        for entry in matches[: max(1, min(limit, 25))]:
            match = entry.get("match", {})
            p1 = id_to_name.get(match.get("player1_id"), "TBD")
            p2 = id_to_name.get(match.get("player2_id"), "TBD")
            score = match.get("scores_csv") or "–"
            match_state = match.get("state") or "unknown"
            lines.append(f"{p1} vs {p2} | {score} | {match_state}")

        embed = discord.Embed(
            title="Challonge matches",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    @ccchallonge.command(name="sync")
    async def ccchallonge_sync(self, ctx, purge: Optional[str] = None):
        """Sync approved applicants to Challonge participants (v1)."""
        api_key, slug = await self._get_challonge_credentials(ctx.guild)
        if not api_key:
            await ctx.send("Challonge API key is not set. Use `ccchallonge key <token>`.")
            return
        if not slug:
            await ctx.send("Challonge tournament is not set. Use `ccchallonge tournament <slug>`.")
            return

        approved = await self._load_application_list(ctx.guild, "approved_applications")
        approved_ids = [entry.get("user_id") for entry in approved if entry.get("user_id")]
        if not approved_ids:
            await ctx.send("No approved applicants to sync.")
            return

        ok, participants_payload = await self._challonge_request(
            ctx.guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        if not ok:
            await ctx.send(f"Challonge API error: {participants_payload}")
            return

        participants = participants_payload if isinstance(participants_payload, list) else []
        existing_by_name = {}
        existing_ids = set()
        for entry in participants:
            participant = entry.get("participant", {})
            pid = participant.get("id")
            name = participant.get("name")
            if pid is not None:
                existing_ids.add(int(pid))
            if name:
                existing_by_name[name.lower()] = int(pid) if pid is not None else None

        stored_map = await self.config.guild(ctx.guild).tourney_challonge_participant_map()
        if not isinstance(stored_map, dict):
            stored_map = {}
        links = await self._get_challonge_links(ctx.guild)

        added = 0
        linked = 0
        errors = 0

        for user_id in approved_ids:
            user_id_str = str(user_id)
            link_entry = links.get(user_id_str) if links else None
            member = ctx.guild.get_member(user_id)
            display_name = member.display_name if member else f"User {user_id}"

            if link_entry and link_entry.get("participant_id"):
                participant_id = int(link_entry.get("participant_id"))
                if participant_id in existing_ids:
                    stored_map[user_id_str] = participant_id
                    linked += 1
                    continue

            existing_id = stored_map.get(user_id_str)
            if existing_id and int(existing_id) in existing_ids:
                linked += 1
                continue

            name_key = display_name.lower()
            if name_key in existing_by_name and existing_by_name[name_key]:
                stored_map[user_id_str] = existing_by_name[name_key]
                linked += 1
                continue

            candidate_name = display_name
            if name_key in existing_by_name:
                candidate_name = f"{display_name} ({str(user_id)[-4:]})"

            ok, create_payload = await self._challonge_request(
                ctx.guild,
                "POST",
                f"/tournaments/{slug}/participants.json",
                data={"participant[name]": candidate_name},
            )
            if not ok:
                errors += 1
                continue
            participant = (
                create_payload.get("participant")
                if isinstance(create_payload, dict)
                else None
            )
            if participant and participant.get("id"):
                stored_map[user_id_str] = participant.get("id")
                added += 1
            else:
                errors += 1

        purge_flag = purge is not None and purge.lower() in {"purge", "remove", "delete"}
        if purge_flag:
            approved_set = {str(uid) for uid in approved_ids}
            to_remove = [
                (uid, pid)
                for uid, pid in stored_map.items()
                if uid not in approved_set
            ]
            for uid, pid in to_remove:
                ok, _ = await self._challonge_request(
                    ctx.guild,
                    "DELETE",
                    f"/tournaments/{slug}/participants/{pid}.json",
                )
                if ok:
                    stored_map.pop(uid, None)

        await self.config.guild(ctx.guild).tourney_challonge_participant_map.set(stored_map)
        await ctx.send(
            f"Challonge sync complete. Added {added}, linked {linked}, errors {errors}."
        )

    @ccchallonge.command(name="link")
    async def ccchallonge_link(
        self, ctx, member: Optional[discord.Member] = None, *, participant: Optional[str] = None
    ):
        """Link a Discord user to a Challonge participant."""
        if member is None:
            member = ctx.author
        elif member != ctx.author and not (
            ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator
        ):
            await ctx.send("You can only link yourself.")
            return
        if participant and participant.strip().lower() in {"me", "self"}:
            participant = None
        if not participant:
            api_key, slug = await self._get_challonge_credentials(ctx.guild)
            if not api_key or not slug:
                await ctx.send("Challonge API key or tournament is not set.")
                return
            ok, payload = await self._challonge_request(
                ctx.guild, "GET", f"/tournaments/{slug}/participants.json"
            )
            if not ok:
                await ctx.send(f"Challonge API error: {payload}")
                return
            participants = payload if isinstance(payload, list) else []
            if not participants:
                await ctx.send("No participants found in Challonge.")
                return
            if len(participants) <= 25:
                await ctx.send(
                    "Select your Challonge participant:",
                    view=ChallongeLinkSelectView(
                        self, ctx.guild.id, member.id, participants
                    ),
                )
                return
            prefix = await self._get_preferred_prefix(ctx.guild)
            await ctx.send(
                "Too many participants to list. "
                f"Use `{prefix}ccchallonge link [@user] <participant id>`."
            )
            return

        participant_data = await self._resolve_challonge_participant(ctx.guild, participant)
        if not participant_data:
            prefix = await self._get_preferred_prefix(ctx.guild)
            await ctx.send(
                f"Participant not found. Use `{prefix}ccchallonge participants` to list names/ids."
            )
            return

        participant_id = participant_data.get("id")
        participant_name = participant_data.get("name") or str(participant_id)
        if not participant_id:
            await ctx.send("Participant data missing an ID.")
            return

        await self._set_challonge_link(
            ctx.guild,
            user_id=member.id,
            participant_id=int(participant_id),
            participant_name=participant_name,
        )
        await ctx.send(
            f"Linked {member.mention} to Challonge participant `{participant_name}`."
        )
        await self._sync_roles_from_challonge(ctx.guild)

    @ccchallonge.command(name="unlink")
    async def ccchallonge_unlink(self, ctx, member: Optional[discord.Member] = None):
        """Remove a Challonge link for a Discord user."""
        if member is None:
            member = ctx.author
        elif member != ctx.author and not (
            ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator
        ):
            await ctx.send("You can only unlink yourself.")
            return
        await self._remove_challonge_link(ctx.guild, member.id)
        await ctx.send(f"Removed Challonge link for {member.mention}.")

    @ccchallonge.group(name="teammap")
    @commands.admin_or_permissions(administrator=True)
    async def ccchallonge_teammap(self, ctx):
        """Map team numbers to Challonge participants."""
        if ctx.invoked_subcommand is None:
            team_map = await self.config.guild(ctx.guild).challonge_team_map()
            if not team_map:
                await ctx.send("No team mappings set.")
                return
            lines = [f"Team {team}: {pid}" for team, pid in sorted(team_map.items())]
            await ctx.send("Team map:\n" + "\n".join(lines))

    @ccchallonge_teammap.command(name="set")
    async def ccchallonge_teammap_set(
        self, ctx, team_number: int, *, participant: str
    ):
        """Set a team mapping to a Challonge participant."""
        if team_number <= 0:
            await ctx.send("Team number must be 1 or higher.")
            return
        participant_data = await self._resolve_challonge_participant(ctx.guild, participant)
        if not participant_data:
            await ctx.send("Participant not found. Use `ccchallonge participants` to list names/ids.")
            return
        participant_id = participant_data.get("id")
        participant_name = participant_data.get("name") or str(participant_id)
        if not participant_id:
            await ctx.send("Participant data missing an ID.")
            return
        team_map = await self.config.guild(ctx.guild).challonge_team_map()
        if not isinstance(team_map, dict):
            team_map = {}
        team_map[str(team_number)] = int(participant_id)
        await self.config.guild(ctx.guild).challonge_team_map.set(team_map)
        await ctx.send(f"Mapped Team {team_number} -> `{participant_name}`.")

    @ccchallonge_teammap.command(name="clear")
    async def ccchallonge_teammap_clear(self, ctx, team_number: Optional[int] = None):
        """Clear team mappings."""
        team_map = await self.config.guild(ctx.guild).challonge_team_map()
        if not isinstance(team_map, dict):
            team_map = {}
        if team_number is None:
            team_map = {}
            await self.config.guild(ctx.guild).challonge_team_map.set(team_map)
            await ctx.send("Cleared all team mappings.")
            return
        removed = team_map.pop(str(team_number), None)
        await self.config.guild(ctx.guild).challonge_team_map.set(team_map)
        if removed is None:
            await ctx.send(f"No mapping found for Team {team_number}.")
        else:
            await ctx.send(f"Cleared mapping for Team {team_number}.")

    @ccchallonge.command(name="syncroles")
    @commands.admin_or_permissions(administrator=True)
    async def ccchallonge_syncroles(self, ctx):
        """Assign team roles from Challonge participant order."""
        assigned, missing, error = await self._sync_roles_from_challonge(ctx.guild)
        if error:
            await ctx.send(error)
            return
        await ctx.send(
            f"Team sync complete. Assigned {assigned} members. {missing} missing links/participants."
        )

    @ccchallonge.command(name="purgeall")
    @commands.admin_or_permissions(administrator=True)
    async def ccchallonge_purgeall(self, ctx):
        """Remove all Challonge participants and clear local links."""
        api_key, slug = await self._get_challonge_credentials(ctx.guild)
        if not api_key:
            await ctx.send("Challonge API key is not set. Use `ccchallonge key <token>`.")
            return
        if not slug:
            await ctx.send("Challonge tournament is not set. Use `ccchallonge tournament <slug>`.")
            return

        ok, participants_payload = await self._challonge_request(
            ctx.guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        if not ok:
            await ctx.send(f"Challonge API error: {participants_payload}")
            return

        participants = participants_payload if isinstance(participants_payload, list) else []
        removed = 0
        failed = 0
        for entry in participants:
            participant = entry.get("participant", {})
            pid = participant.get("id")
            if not pid:
                continue
            ok, _ = await self._challonge_request(
                ctx.guild,
                "DELETE",
                f"/tournaments/{slug}/participants/{pid}.json",
            )
            if ok:
                removed += 1
            else:
                failed += 1

        await self.config.guild(ctx.guild).tourney_challonge_participant_map.set({})
        await self.config.guild(ctx.guild).challonge_links.set({})
        await self.config.guild(ctx.guild).challonge_link_last_reminder.set({})
        await self.config.guild(ctx.guild).challonge_team_map.set({})
        await ctx.send(f"Purge complete. Removed {removed}, failed {failed}.")

    @ccchallonge.command(name="syncrolesauto")
    @commands.admin_or_permissions(administrator=True)
    async def ccchallonge_syncrolesauto(
        self, ctx, enabled: Optional[str] = None, interval: Optional[int] = None
    ):
        """Configure automatic Challonge role sync."""
        if enabled is None:
            autosync = await self.config.guild(ctx.guild).challonge_sync_roles_enabled()
            interval = await self.config.guild(ctx.guild).challonge_sync_roles_interval()
            await ctx.send(f"Auto sync: {autosync}\nInterval: {interval}m")
            return
        flag = enabled.lower() in {"on", "true", "yes", "1"}
        await self.config.guild(ctx.guild).challonge_sync_roles_enabled.set(flag)
        if interval is not None:
            await self.config.guild(ctx.guild).challonge_sync_roles_interval.set(interval)
        await ctx.send("Auto sync updated.")

    @ccchallonge.command(name="linksettings")
    @commands.admin_or_permissions(administrator=True)
    async def ccchallonge_linksettings(
        self, ctx, setting: Optional[str] = None, *, value: Optional[str] = None
    ):
        """Configure Challonge link enforcement."""
        if not setting:
            required = await self.config.guild(ctx.guild).challonge_link_required()
            grace = await self.config.guild(ctx.guild).challonge_link_grace_hours()
            reminder = await self.config.guild(ctx.guild).challonge_link_reminder_hours()
            autosync = await self.config.guild(ctx.guild).challonge_sync_roles_enabled()
            interval = await self.config.guild(ctx.guild).challonge_sync_roles_interval()
            await ctx.send(
                f"Link required: {required}\nGrace hours: {grace}\n"
                f"Reminder hours: {reminder}\nAuto sync: {autosync}\nInterval: {interval}m"
            )
            return
        setting = setting.lower()
        if setting in {"require", "required"}:
            flag = (value or "").lower() in {"on", "true", "yes", "1"}
            await self.config.guild(ctx.guild).challonge_link_required.set(flag)
            await ctx.send(f"Link required set to {flag}.")
            return
        if setting == "grace":
            hours = self._parse_int(value) if value else None
            if hours is None:
                await ctx.send("Provide hours: `ccchallonge linksettings grace 48`")
                return
            await self.config.guild(ctx.guild).challonge_link_grace_hours.set(hours)
            await ctx.send(f"Grace period set to {hours} hours.")
            return
        if setting == "reminder":
            hours = self._parse_int(value) if value else None
            if hours is None:
                await ctx.send("Provide hours: `ccchallonge linksettings reminder 12`")
                return
            await self.config.guild(ctx.guild).challonge_link_reminder_hours.set(hours)
            await ctx.send(f"Reminder interval set to {hours} hours.")
            return
        if setting == "autosync":
            flag = (value or "").lower() in {"on", "true", "yes", "1"}
            await self.config.guild(ctx.guild).challonge_sync_roles_enabled.set(flag)
            await ctx.send(f"Auto role sync set to {flag}.")
            return
        if setting == "interval":
            minutes = self._parse_int(value) if value else None
            if minutes is None:
                await ctx.send("Provide minutes: `ccchallonge linksettings interval 15`")
                return
            await self.config.guild(ctx.guild).challonge_sync_roles_interval.set(minutes)
            await ctx.send(f"Auto role sync interval set to {minutes} minutes.")
            return
        await ctx.send("Unknown setting. Use `require`, `grace`, `reminder`, `autosync`, or `interval`.")

    @commands.command()
    @commands.admin_or_permissions(administrator=True)
    @guild_only()
    async def setchampionsrole(self, ctx, role: discord.Role):
        """Set the Champions Circle role."""
        await self.config.guild(ctx.guild).champions_role_id.set(role.id)
        await ctx.send(f"Champions Circle role set to {role.name}")

    # ─────────────────────────────────────────────────────────────────────────
    # DRAFT SYSTEM COMMANDS
    # ─────────────────────────────────────────────────────────────────────────

    @commands.group(name="ccdraft")
    @commands.guild_only()
    async def ccdraft(self, ctx):
        """Manage the tournament draft system."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @ccdraft.command(name="pool")
    async def ccdraft_pool(self, ctx):
        """Show all approved players available for drafting."""
        approved = await self._load_application_list(ctx.guild, "approved_applications")
        if not approved:
            await ctx.send("No approved players in the pool.")
            return

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        # Get all drafted user IDs
        drafted_ids = set()
        for team_users in draft_assignments.values():
            if isinstance(team_users, list):
                drafted_ids.update(team_users)

        # Filter to undrafted players
        undrafted = []
        for app in approved:
            user_id = app.get("user_id")
            if user_id and user_id not in drafted_ids:
                undrafted.append(app)

        if not undrafted:
            await ctx.send("All approved players have been drafted.")
            return

        game_mode = await self.config.guild(ctx.guild).game_mode()
        team_size = self._team_size_from_mode(game_mode)

        embed = discord.Embed(
            title="Draft Pool",
            description=f"**{len(undrafted)}** players available for drafting\nTeam size: **{team_size}** ({game_mode})",
            color=discord.Color.blue(),
        )

        lines = []
        for idx, app in enumerate(undrafted, start=1):
            user_id = app.get("user_id")
            member = ctx.guild.get_member(user_id)
            display = member.mention if member else f"<@{user_id}>"
            answers = app.get("answers") or {}
            rank = self._extract_answer(answers, ["rank"]) or "Unranked"
            lines.append(f"{idx}. {display} - **{rank}**")

        # Chunk if needed
        for idx, chunk in enumerate(self._chunk_lines(lines, limit=1000), start=1):
            name = "Available Players" if idx == 1 else f"Available Players ({idx})"
            embed.add_field(name=name, value="\n".join(chunk), inline=False)

        await ctx.send(embed=embed)

    @ccdraft.command(name="captains")
    async def ccdraft_captains(self, ctx, *members: discord.Member):
        """Assign captains for all teams in order."""
        if not await self._require_draft_admin(ctx):
            return

        draft_started = await self.config.guild(ctx.guild).draft_started()
        if draft_started:
            await ctx.send("Draft is in progress. Use `ccdraft reset` before changing captains.")
            return

        team_count = await self.config.guild(ctx.guild).team_count()
        if team_count <= 0:
            await ctx.send("Team count is not set. Run `ccsetup` first.")
            return

        if len(members) != team_count:
            await ctx.send(f"Provide exactly {team_count} captains (Team 1 -> Team {team_count}).")
            return

        if len({m.id for m in members}) != len(members):
            await ctx.send("Each captain must be unique.")
            return

        approved = await self._load_application_list(ctx.guild, "approved_applications")
        approved_ids = {app.get("user_id") for app in approved}
        not_approved = [m for m in members if m.id not in approved_ids]
        if not_approved:
            mentions = ", ".join(m.mention for m in not_approved)
            await ctx.send(f"These captains are not approved: {mentions}")
            return

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        captain_ids = [m.id for m in members]
        for team_key, team_users in list(draft_assignments.items()):
            if not isinstance(team_users, list):
                draft_assignments[team_key] = []
                continue
            draft_assignments[team_key] = [uid for uid in team_users if uid not in captain_ids]

        draft_captains = {}
        for idx, member in enumerate(members, start=1):
            team_key = str(idx)
            draft_captains[team_key] = member.id
            if team_key not in draft_assignments:
                draft_assignments[team_key] = []
            if member.id not in draft_assignments[team_key]:
                draft_assignments[team_key].insert(0, member.id)

        await self.config.guild(ctx.guild).draft_assignments.set(draft_assignments)
        await self.config.guild(ctx.guild).draft_captains.set(draft_captains)
        await self._set_captain_roles(ctx.guild, captain_ids)

        for idx, member in enumerate(members, start=1):
            await self._apply_draft_roles(ctx.guild, idx, [member.id])
            await self._update_challonge_team_roster(ctx.guild, idx)

        await ctx.send("Captains set for all teams. Use `ccdraft start` to begin the draft.")

    @ccdraft.command(name="start")
    async def ccdraft_start(self, ctx):
        """Start a snake draft with the current captains."""
        if not await self._require_draft_admin(ctx):
            return

        draft_started = await self.config.guild(ctx.guild).draft_started()
        if draft_started:
            await ctx.send("Draft already started. Use `ccdraft reset` to restart.")
            return

        team_count = await self.config.guild(ctx.guild).team_count()
        if team_count <= 0:
            await ctx.send("Team count is not set. Run `ccsetup` first.")
            return

        game_mode = await self.config.guild(ctx.guild).game_mode()
        team_size = self._team_size_from_mode(game_mode)

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        draft_captains = await self.config.guild(ctx.guild).draft_captains()
        if not isinstance(draft_captains, dict):
            draft_captains = {}

        missing_captains = [i for i in range(1, team_count + 1) if str(i) not in draft_captains]
        if missing_captains:
            await ctx.send("Set captains first using `ccdraft captains` or `ccdraft captain`.")
            return

        await self._set_captain_roles(ctx.guild, list(draft_captains.values()))

        team_slots: Dict[int, int] = {}
        for i in range(1, team_count + 1):
            team_key = str(i)
            roster = draft_assignments.get(team_key, [])
            if not isinstance(roster, list):
                roster = []
                draft_assignments[team_key] = roster
            captain_id = draft_captains.get(team_key)
            if captain_id and captain_id not in roster:
                roster.insert(0, captain_id)
            if len(roster) > team_size:
                await ctx.send(f"Team {i} exceeds the team size of {team_size}.")
                return
            team_slots[i] = max(team_size - len(roster), 0)

        total_slots = sum(team_slots.values())
        if total_slots == 0:
            await ctx.send("All teams are already full.")
            return

        order = self._build_snake_draft_order(team_slots)
        if not order:
            await ctx.send("Unable to build a draft order. Check team sizes.")
            return

        await self.config.guild(ctx.guild).draft_assignments.set(draft_assignments)
        await self.config.guild(ctx.guild).draft_order.set(order)
        await self.config.guild(ctx.guild).draft_current_pick.set(0)
        await self.config.guild(ctx.guild).draft_pick_log.set([])
        await self.config.guild(ctx.guild).draft_started.set(True)
        await self.config.guild(ctx.guild).draft_locked.set(False)

        next_team = order[0]
        captain_id = draft_captains.get(str(next_team))
        captain = ctx.guild.get_member(captain_id) if captain_id else None
        captain_label = captain.mention if captain else "No captain set"

        preview = []
        for idx, team in enumerate(order[:5], start=1):
            cap_id = draft_captains.get(str(team))
            cap_member = ctx.guild.get_member(cap_id) if cap_id else None
            cap_label = cap_member.display_name if cap_member else "No captain"
            preview.append(f"{idx}. Team {team} ({cap_label})")

        embed = discord.Embed(
            title="Draft Started",
            description=f"Mode: **{game_mode}** | Team size: **{team_size}**",
            color=discord.Color.green(),
        )
        embed.add_field(name="On the clock", value=f"Team {next_team} — {captain_label}", inline=False)
        embed.add_field(name="Next picks", value="\n".join(preview), inline=False)

        await ctx.send(embed=embed)

    @ccdraft.command(name="status")
    async def ccdraft_status(self, ctx):
        """Show the current draft status."""
        game_mode = await self.config.guild(ctx.guild).game_mode()
        team_size = self._team_size_from_mode(game_mode)
        draft_started = await self.config.guild(ctx.guild).draft_started()
        draft_locked = await self.config.guild(ctx.guild).draft_locked()
        draft_order = await self.config.guild(ctx.guild).draft_order()
        draft_current_pick = await self.config.guild(ctx.guild).draft_current_pick()
        draft_captains = await self.config.guild(ctx.guild).draft_captains()
        draft_pick_log = await self.config.guild(ctx.guild).draft_pick_log()

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        embed = discord.Embed(
            title="Draft Status",
            description=f"Mode: **{game_mode}** | Team size: **{team_size}**",
            color=discord.Color.gold(),
        )

        state = "Not started"
        if draft_started:
            state = "In progress"
        if draft_locked:
            state = "Locked"
        embed.add_field(name="State", value=state, inline=True)

        if not isinstance(draft_order, list) or not draft_order:
            embed.add_field(name="On the clock", value="No draft order", inline=False)
            await ctx.send(embed=embed)
            return

        next_index = self._find_next_pick_index(
            draft_order, draft_current_pick, draft_assignments, team_size
        )
        if next_index is None:
            embed.add_field(name="On the clock", value="Draft complete", inline=False)
        else:
            team_number = draft_order[next_index]
            captain_id = draft_captains.get(str(team_number))
            captain = ctx.guild.get_member(captain_id) if captain_id else None
            captain_label = captain.mention if captain else "No captain set"
            embed.add_field(
                name="On the clock",
                value=f"Team {team_number} — {captain_label}",
                inline=False,
            )

            upcoming = []
            for idx in range(next_index, min(next_index + 5, len(draft_order))):
                team = draft_order[idx]
                cap_id = draft_captains.get(str(team))
                cap_member = ctx.guild.get_member(cap_id) if cap_id else None
                cap_label = cap_member.display_name if cap_member else "No captain"
                upcoming.append(f"{idx + 1}. Team {team} ({cap_label})")
            if upcoming:
                embed.add_field(name="Upcoming", value="\n".join(upcoming), inline=False)

        if isinstance(draft_pick_log, list) and draft_pick_log:
            last_pick = draft_pick_log[-1]
            pick_team = last_pick.get("team")
            pick_user_id = last_pick.get("user_id")
            pick_member = ctx.guild.get_member(pick_user_id) if pick_user_id else None
            pick_label = pick_member.mention if pick_member else f"<@{pick_user_id}>"
            embed.add_field(
                name="Last pick",
                value=f"Team {pick_team}: {pick_label}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @ccdraft.command(name="pick")
    async def ccdraft_pick(self, ctx, member: discord.Member):
        """Pick one player for the team currently on the clock."""
        draft_started = await self.config.guild(ctx.guild).draft_started()
        if not draft_started:
            await ctx.send("Draft has not started. Use `ccdraft start`.")
            return

        draft_locked = await self.config.guild(ctx.guild).draft_locked()
        if draft_locked:
            await ctx.send("Draft is locked. Use `ccdraft unlock` to make changes.")
            return

        draft_order = await self.config.guild(ctx.guild).draft_order()
        if not isinstance(draft_order, list) or not draft_order:
            await ctx.send("Draft order is not set. Use `ccdraft start`.")
            return

        game_mode = await self.config.guild(ctx.guild).game_mode()
        team_size = self._team_size_from_mode(game_mode)

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        draft_current_pick = await self.config.guild(ctx.guild).draft_current_pick()
        next_index = self._find_next_pick_index(
            draft_order, draft_current_pick, draft_assignments, team_size
        )
        if next_index is None:
            await self.config.guild(ctx.guild).draft_started.set(False)
            await self.config.guild(ctx.guild).draft_locked.set(True)
            await ctx.send("Draft is complete.")
            return

        team_number = draft_order[next_index]
        draft_captains = await self.config.guild(ctx.guild).draft_captains()
        captain_id = draft_captains.get(str(team_number)) if isinstance(draft_captains, dict) else None

        if not await self._is_draft_admin(ctx):
            if ctx.author.id != captain_id:
                captain = ctx.guild.get_member(captain_id) if captain_id else None
                captain_label = captain.mention if captain else "No captain set"
                await ctx.send(f"Team {team_number} is on the clock. Captain: {captain_label}")
                return

        approved = await self._load_application_list(ctx.guild, "approved_applications")
        approved_ids = {app.get("user_id") for app in approved}
        if member.id not in approved_ids:
            await ctx.send(f"{member.mention} is not approved for the draft.")
            return

        assigned_team = None
        for team_str, team_users in draft_assignments.items():
            if isinstance(team_users, list) and member.id in team_users:
                assigned_team = team_str
                break
        if assigned_team:
            await ctx.send(f"{member.mention} is already on Team {assigned_team}.")
            return

        team_key = str(team_number)
        roster = draft_assignments.get(team_key, [])
        if not isinstance(roster, list):
            roster = []
        if len(roster) >= team_size:
            await ctx.send(f"Team {team_number} is already full.")
            return

        roster.append(member.id)
        draft_assignments[team_key] = roster
        await self.config.guild(ctx.guild).draft_assignments.set(draft_assignments)

        await self._apply_draft_roles(ctx.guild, team_number, [member.id])
        await self._update_challonge_team_roster(ctx.guild, team_number)

        pick_log = await self.config.guild(ctx.guild).draft_pick_log()
        if not isinstance(pick_log, list):
            pick_log = []
        pick_log.append(
            {
                "team": team_number,
                "user_id": member.id,
                "pick_index": next_index,
                "picked_at": int(datetime.now(timezone.utc).timestamp()),
            }
        )
        await self.config.guild(ctx.guild).draft_pick_log.set(pick_log)

        next_pick = self._find_next_pick_index(
            draft_order, next_index + 1, draft_assignments, team_size
        )
        if next_pick is None:
            await self.config.guild(ctx.guild).draft_current_pick.set(len(draft_order))
            await self.config.guild(ctx.guild).draft_started.set(False)
            await self.config.guild(ctx.guild).draft_locked.set(True)
            await ctx.send(f"Team {team_number} selected {member.mention}. Draft complete.")
            return

        await self.config.guild(ctx.guild).draft_current_pick.set(next_pick)

        next_team = draft_order[next_pick]
        next_captain_id = draft_captains.get(str(next_team)) if isinstance(draft_captains, dict) else None
        next_captain = ctx.guild.get_member(next_captain_id) if next_captain_id else None
        next_captain_label = next_captain.mention if next_captain else "No captain set"

        await ctx.send(
            f"Team {team_number} selected {member.mention}. "
            f"Next up: Team {next_team} — {next_captain_label}"
        )

    @ccdraft.command(name="undo")
    async def ccdraft_undo(self, ctx):
        """Undo the last draft pick."""
        if not await self._require_draft_admin(ctx):
            return

        draft_pick_log = await self.config.guild(ctx.guild).draft_pick_log()
        if not isinstance(draft_pick_log, list) or not draft_pick_log:
            await ctx.send("No draft picks to undo.")
            return

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        last_pick = draft_pick_log.pop()
        team_number = last_pick.get("team")
        user_id = last_pick.get("user_id")
        pick_index = last_pick.get("pick_index")

        if team_number is None or user_id is None:
            await ctx.send("Last pick data is invalid.")
            return

        team_key = str(team_number)
        roster = draft_assignments.get(team_key, [])
        if isinstance(roster, list) and user_id in roster:
            roster.remove(user_id)
            draft_assignments[team_key] = roster
            await self._remove_draft_role(ctx.guild, team_number, user_id)
            await self._update_challonge_team_roster(ctx.guild, team_number)

        await self.config.guild(ctx.guild).draft_assignments.set(draft_assignments)
        await self.config.guild(ctx.guild).draft_pick_log.set(draft_pick_log)

        if isinstance(pick_index, int) and pick_index >= 0:
            await self.config.guild(ctx.guild).draft_current_pick.set(pick_index)
        else:
            current_pick = await self.config.guild(ctx.guild).draft_current_pick()
            await self.config.guild(ctx.guild).draft_current_pick.set(max(current_pick - 1, 0))

        await self.config.guild(ctx.guild).draft_started.set(True)
        await self.config.guild(ctx.guild).draft_locked.set(False)

        member = ctx.guild.get_member(user_id)
        label = member.mention if member else f"<@{user_id}>"
        await ctx.send(f"Undo complete. Removed {label} from Team {team_number}.")

    @ccdraft.command(name="reset")
    async def ccdraft_reset(self, ctx):
        """Reset the draft order and pick log (keeps assignments)."""
        if not await self._require_draft_admin(ctx):
            return

        await self.config.guild(ctx.guild).draft_order.set([])
        await self.config.guild(ctx.guild).draft_current_pick.set(0)
        await self.config.guild(ctx.guild).draft_pick_log.set([])
        await self.config.guild(ctx.guild).draft_started.set(False)
        await self.config.guild(ctx.guild).draft_locked.set(False)
        await ctx.send("Draft order has been reset. Use `ccdraft start` to begin again.")

    @ccdraft.command(name="assign")
    async def ccdraft_assign(self, ctx, team_number: int, *members: discord.Member):
        """Assign players to a team (admin override). Usage: ccdraft assign 1 @Player1 @Player2"""
        if not await self._require_draft_admin(ctx):
            return

        draft_started = await self.config.guild(ctx.guild).draft_started()
        if draft_started:
            await ctx.send("Draft is in progress. Use `ccdraft pick` or `ccdraft reset` before assigning.")
            return

        if not members:
            await ctx.send("Please mention at least one player to assign.")
            return

        draft_locked = await self.config.guild(ctx.guild).draft_locked()
        if draft_locked:
            await ctx.send("The draft is locked. Use `ccdraft unlock` to make changes.")
            return

        team_count = await self.config.guild(ctx.guild).team_count()
        if team_number < 1 or team_number > team_count:
            await ctx.send(f"Invalid team number. Must be between 1 and {team_count}.")
            return

        # Verify all members are approved
        approved = await self._load_application_list(ctx.guild, "approved_applications")
        approved_ids = {app.get("user_id") for app in approved}

        not_approved = [m for m in members if m.id not in approved_ids]
        if not_approved:
            mentions = ", ".join(m.mention for m in not_approved)
            await ctx.send(f"These players are not approved: {mentions}")
            return

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        draft_captains = await self.config.guild(ctx.guild).draft_captains()
        if not isinstance(draft_captains, dict):
            draft_captains = {}

        member_ids = [m.id for m in members]
        for team_str, captain_id in draft_captains.items():
            if int(team_str) != team_number and captain_id in member_ids:
                await ctx.send(
                    f"A captain is already assigned to Team {team_str}. "
                    "Use `ccdraft captain` to move captains."
                )
                return

        # Check if any member is already on another team
        for team_str, team_users in draft_assignments.items():
            if not isinstance(team_users, list):
                continue
            if int(team_str) == team_number:
                continue
            for member in members:
                if member.id in team_users:
                    await ctx.send(
                        f"{member.mention} is already assigned to Team {team_str}. "
                        f"Use `ccdraft remove {team_str} @{member.name}` first."
                    )
                    return

        # Get or create team list
        team_key = str(team_number)
        if team_key not in draft_assignments:
            draft_assignments[team_key] = []

        # Check team size limit
        game_mode = await self.config.guild(ctx.guild).game_mode()
        team_size = self._team_size_from_mode(game_mode)
        current_size = len(draft_assignments[team_key])
        new_members = [m for m in members if m.id not in draft_assignments[team_key]]

        if current_size + len(new_members) > team_size:
            await ctx.send(
                f"Team {team_number} would exceed the team size limit of {team_size}. "
                f"Current: {current_size}, trying to add: {len(new_members)}."
            )
            return

        # Assign members
        for member in new_members:
            draft_assignments[team_key].append(member.id)

        await self.config.guild(ctx.guild).draft_assignments.set(draft_assignments)

        # Assign team roles and update channel access
        await self._apply_draft_roles(ctx.guild, team_number, [m.id for m in new_members])

        # Update Challonge misc field
        await self._update_challonge_team_roster(ctx.guild, team_number)

        mentions = ", ".join(m.mention for m in new_members)
        await ctx.send(f"Assigned to Team {team_number}: {mentions}")

    @ccdraft.command(name="remove")
    async def ccdraft_remove(self, ctx, team_number: int, member: discord.Member):
        """Remove a player from a team."""
        if not await self._require_draft_admin(ctx):
            return

        draft_started = await self.config.guild(ctx.guild).draft_started()
        if draft_started:
            await ctx.send("Draft is in progress. Use `ccdraft undo` or `ccdraft reset` first.")
            return

        draft_locked = await self.config.guild(ctx.guild).draft_locked()
        if draft_locked:
            await ctx.send("The draft is locked. Use `ccdraft unlock` to make changes.")
            return

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        draft_captains = await self.config.guild(ctx.guild).draft_captains()
        if not isinstance(draft_captains, dict):
            draft_captains = {}

        team_key = str(team_number)
        if team_key not in draft_assignments or member.id not in draft_assignments[team_key]:
            await ctx.send(f"{member.mention} is not on Team {team_number}.")
            return

        draft_assignments[team_key].remove(member.id)
        await self.config.guild(ctx.guild).draft_assignments.set(draft_assignments)

        if draft_captains.get(team_key) == member.id:
            draft_captains.pop(team_key, None)
            await self.config.guild(ctx.guild).draft_captains.set(draft_captains)
            await self._set_captain_roles(ctx.guild, list(draft_captains.values()))

        # Remove team role
        await self._remove_draft_role(ctx.guild, team_number, member.id)

        # Update Challonge misc field
        await self._update_challonge_team_roster(ctx.guild, team_number)

        await ctx.send(f"Removed {member.mention} from Team {team_number}. They're back in the draft pool.")

    @ccdraft.command(name="show")
    async def ccdraft_show(self, ctx, team_number: Optional[int] = None):
        """Show team rosters. Optionally specify a team number."""
        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        draft_captains = await self.config.guild(ctx.guild).draft_captains()
        if not isinstance(draft_captains, dict):
            draft_captains = {}

        team_count = await self.config.guild(ctx.guild).team_count()
        game_mode = await self.config.guild(ctx.guild).game_mode()
        team_size = self._team_size_from_mode(game_mode)
        draft_locked = await self.config.guild(ctx.guild).draft_locked()
        draft_started = await self.config.guild(ctx.guild).draft_started()

        if team_number is not None:
            # Show specific team
            if team_number < 1 or team_number > team_count:
                await ctx.send(f"Invalid team number. Must be between 1 and {team_count}.")
                return

            team_key = str(team_number)
            user_ids = draft_assignments.get(team_key, [])
            captain_id = draft_captains.get(team_key)

            embed = discord.Embed(
                title=f"{self._team_emoji(team_number)} Team {team_number}",
                description=f"**{len(user_ids)}/{team_size}** players",
                color=self._random_role_color(),
            )

            captain_member = ctx.guild.get_member(captain_id) if captain_id else None
            captain_label = captain_member.mention if captain_member else "Not set"
            embed.add_field(name="Captain", value=captain_label, inline=False)

            if user_ids:
                lines = []
                for uid in user_ids:
                    member = ctx.guild.get_member(uid)
                    display = member.mention if member else f"<@{uid}>"
                    if captain_id and uid == captain_id:
                        display = f"{display} (C)"
                    lines.append(display)
                embed.add_field(name="Roster", value="\n".join(lines), inline=False)
            else:
                embed.add_field(name="Roster", value="No players assigned", inline=False)

            await ctx.send(embed=embed)
            return

        # Show all teams
        embed = discord.Embed(
            title="Draft Status",
            description=(
                f"Game mode: **{game_mode}** | Team size: **{team_size}**\n"
                f"Draft started: **{'Yes' if draft_started else 'No'}** | "
                f"Draft locked: **{'Yes' if draft_locked else 'No'}**"
            ),
            color=discord.Color.gold(),
        )

        for i in range(1, team_count + 1):
            team_key = str(i)
            user_ids = draft_assignments.get(team_key, [])
            captain_id = draft_captains.get(team_key)
            emoji = self._team_emoji(i)

            captain_member = ctx.guild.get_member(captain_id) if captain_id else None
            captain_label = captain_member.mention if captain_member else "Not set"

            if user_ids:
                lines = []
                for uid in user_ids:
                    member = ctx.guild.get_member(uid)
                    display = member.display_name if member else f"User {uid}"
                    if captain_id and uid == captain_id:
                        display = f"{display} (C)"
                    lines.append(display)
                roster = "\n".join(lines)
            else:
                roster = "*Empty*"

            embed.add_field(
                name=f"{emoji} Team {i} ({len(user_ids)}/{team_size})",
                value=f"Captain: {captain_label}\n{roster}",
                inline=True,
            )

        # Add pool count
        approved = await self._load_application_list(ctx.guild, "approved_applications")
        drafted_ids = set()
        for team_users in draft_assignments.values():
            if isinstance(team_users, list):
                drafted_ids.update(team_users)
        undrafted_count = sum(1 for app in approved if app.get("user_id") not in drafted_ids)
        embed.set_footer(text=f"{undrafted_count} players remaining in draft pool")

        await ctx.send(embed=embed)

    @ccdraft.command(name="lock")
    async def ccdraft_lock(self, ctx):
        """Lock the draft to prevent further changes."""
        if not await self._require_draft_admin(ctx):
            return

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not draft_assignments:
            await ctx.send("No draft assignments yet. Assign players before locking.")
            return

        await self.config.guild(ctx.guild).draft_locked.set(True)
        await ctx.send("Draft is now **locked**. Use `ccdraft unlock` to make changes.")

    @ccdraft.command(name="unlock")
    async def ccdraft_unlock(self, ctx):
        """Unlock the draft to allow changes."""
        if not await self._require_draft_admin(ctx):
            return

        await self.config.guild(ctx.guild).draft_locked.set(False)
        await ctx.send("Draft is now **unlocked**. You can make changes.")

    @ccdraft.command(name="clear")
    async def ccdraft_clear(self, ctx, team_number: Optional[int] = None):
        """Clear draft assignments. Specify team number or leave blank for all."""
        if not await self._require_draft_admin(ctx):
            return

        draft_started = await self.config.guild(ctx.guild).draft_started()
        if draft_started:
            await ctx.send("Draft is in progress. Use `ccdraft reset` before clearing.")
            return

        draft_locked = await self.config.guild(ctx.guild).draft_locked()
        if draft_locked:
            await ctx.send("The draft is locked. Use `ccdraft unlock` first.")
            return

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        draft_captains = await self.config.guild(ctx.guild).draft_captains()
        if not isinstance(draft_captains, dict):
            draft_captains = {}

        if team_number is not None:
            team_key = str(team_number)
            if team_key in draft_assignments:
                # Remove roles from all members
                for user_id in draft_assignments[team_key]:
                    await self._remove_draft_role(ctx.guild, team_number, user_id)
                draft_assignments[team_key] = []
                if team_key in draft_captains:
                    draft_captains.pop(team_key, None)
                await self.config.guild(ctx.guild).draft_assignments.set(draft_assignments)
                await self.config.guild(ctx.guild).draft_captains.set(draft_captains)
                await self._set_captain_roles(ctx.guild, list(draft_captains.values()))
                await self._update_challonge_team_roster(ctx.guild, team_number)
                await ctx.send(f"Cleared Team {team_number}.")
            else:
                await ctx.send(f"Team {team_number} has no assignments.")
            return

        # Clear all
        for team_str, user_ids in draft_assignments.items():
            if isinstance(user_ids, list):
                for user_id in user_ids:
                    await self._remove_draft_role(ctx.guild, int(team_str), user_id)

        await self.config.guild(ctx.guild).draft_assignments.set({})
        await self.config.guild(ctx.guild).draft_captains.set({})
        await self._set_captain_roles(ctx.guild, [])

        # Update all Challonge teams
        team_count = await self.config.guild(ctx.guild).team_count()
        for i in range(1, team_count + 1):
            await self._update_challonge_team_roster(ctx.guild, i)

        await ctx.send("Cleared all draft assignments.")

    @ccdraft.command(name="captain")
    async def ccdraft_captain(self, ctx, team_number: int, member: discord.Member):
        """Assign a team captain."""
        if not await self._require_draft_admin(ctx):
            return

        draft_started = await self.config.guild(ctx.guild).draft_started()
        if draft_started:
            await ctx.send("Draft is in progress. Use `ccdraft reset` before changing captains.")
            return

        team_count = await self.config.guild(ctx.guild).team_count()
        if team_number < 1 or team_number > team_count:
            await ctx.send(f"Invalid team number. Must be between 1 and {team_count}.")
            return

        approved = await self._load_application_list(ctx.guild, "approved_applications")
        approved_ids = {app.get("user_id") for app in approved}
        if member.id not in approved_ids:
            await ctx.send(f"{member.mention} is not approved.")
            return

        draft_assignments = await self.config.guild(ctx.guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}
        draft_captains = await self.config.guild(ctx.guild).draft_captains()
        if not isinstance(draft_captains, dict):
            draft_captains = {}

        team_key = str(team_number)
        for other_team, captain_id in draft_captains.items():
            if str(other_team) != team_key and captain_id == member.id:
                await ctx.send(f"{member.mention} is already captain of Team {other_team}.")
                return

        for team_str, team_users in draft_assignments.items():
            if str(team_str) == team_key:
                continue
            if isinstance(team_users, list) and member.id in team_users:
                await ctx.send(
                    f"{member.mention} is already assigned to Team {team_str}. "
                    f"Use `ccdraft remove {team_str} @{member.display_name}` first."
                )
                return

        if team_key not in draft_assignments:
            draft_assignments[team_key] = []
        if member.id not in draft_assignments[team_key]:
            draft_assignments[team_key].insert(0, member.id)

        draft_captains[team_key] = member.id
        await self.config.guild(ctx.guild).draft_assignments.set(draft_assignments)
        await self.config.guild(ctx.guild).draft_captains.set(draft_captains)
        await self._set_captain_roles(ctx.guild, list(draft_captains.values()))

        await self._apply_draft_roles(ctx.guild, team_number, [member.id])
        await self._update_challonge_team_roster(ctx.guild, team_number)

        await ctx.send(f"{member.mention} is now the captain of Team {team_number}.")

    # ─────────────────────────────────────────────────────────────────────────
    # DRAFT HELPER METHODS
    # ─────────────────────────────────────────────────────────────────────────

    async def _is_draft_admin(self, ctx: commands.Context) -> bool:
        if await self.bot.is_owner(ctx.author):
            return True
        perms = ctx.author.guild_permissions
        return perms.administrator or perms.manage_guild or perms.manage_roles

    async def _require_draft_admin(self, ctx: commands.Context) -> bool:
        if await self._is_draft_admin(ctx):
            return True
        await ctx.send("You don't have permission to manage the draft.")
        return False

    async def _set_captain_roles(self, guild: discord.Guild, captain_ids: List[int]) -> None:
        role_id = await self.config.guild(guild).team_captain_role_id()
        if not role_id:
            return
        role = guild.get_role(role_id)
        if not role:
            return

        captain_set = {int(uid) for uid in captain_ids}
        for member in list(role.members):
            if member.id not in captain_set:
                try:
                    await member.remove_roles(role)
                except discord.HTTPException:
                    self.logger.error("Failed to remove captain role from %s", member.id)

        for user_id in captain_set:
            member = guild.get_member(user_id)
            if member and role not in member.roles:
                try:
                    await member.add_roles(role)
                except discord.HTTPException:
                    self.logger.error("Failed to add captain role to %s", user_id)

    def _build_snake_draft_order(self, team_slots: Dict[int, int]) -> List[int]:
        order: List[int] = []
        remaining = {team: max(slots, 0) for team, slots in team_slots.items()}
        round_number = 0

        while True:
            available = [team for team, slots in remaining.items() if slots > 0]
            if not available:
                break
            available.sort()
            if round_number % 2 == 1:
                available.reverse()
            for team in available:
                if remaining.get(team, 0) <= 0:
                    continue
                order.append(team)
                remaining[team] -= 1
            round_number += 1

        return order

    def _find_next_pick_index(
        self,
        order: List[int],
        start_index: int,
        assignments: Dict[str, List[int]],
        team_size: int,
    ) -> Optional[int]:
        for idx in range(start_index, len(order)):
            team = order[idx]
            roster = assignments.get(str(team), [])
            if not isinstance(roster, list):
                roster = []
            if len(roster) < team_size:
                return idx
        return None

    async def _apply_draft_roles(
        self, guild: discord.Guild, team_number: int, user_ids: List[int]
    ) -> None:
        """Apply team role to drafted players and grant channel access."""
        team_role = self._get_team_role_for_number(guild, team_number)
        if not team_role:
            return

        for user_id in user_ids:
            member = guild.get_member(user_id)
            if member and team_role not in member.roles:
                try:
                    await member.add_roles(team_role)
                except discord.HTTPException:
                    self.logger.error(f"Failed to add team role to {user_id}")

    async def _remove_draft_role(
        self, guild: discord.Guild, team_number: int, user_id: int
    ) -> None:
        """Remove team role from a player."""
        team_role = self._get_team_role_for_number(guild, team_number)
        if not team_role:
            return

        member = guild.get_member(user_id)
        if member and team_role in member.roles:
            try:
                await member.remove_roles(team_role)
            except discord.HTTPException:
                self.logger.error(f"Failed to remove team role from {user_id}")

    async def _update_challonge_team_roster(
        self, guild: discord.Guild, team_number: int
    ) -> None:
        """Update the Challonge participant misc field with team roster."""
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key or not slug:
            return

        team_map = await self.config.guild(guild).challonge_team_map()
        if not isinstance(team_map, dict):
            return

        participant_id = team_map.get(str(team_number))
        if not participant_id:
            return

        draft_assignments = await self.config.guild(guild).draft_assignments()
        if not isinstance(draft_assignments, dict):
            draft_assignments = {}

        team_key = str(team_number)
        user_ids = draft_assignments.get(team_key, [])

        # Build misc string with Discord user IDs
        misc_value = ",".join(str(uid) for uid in user_ids) if user_ids else ""

        # Update Challonge participant
        await self._challonge_request(
            guild,
            "PUT",
            f"/tournaments/{slug}/participants/{participant_id}.json",
            data={"participant[misc]": misc_value},
        )

    async def _create_challonge_team_participants(self, guild: discord.Guild) -> Tuple[int, int]:
        """Create team participants on Challonge. Returns (created, errors)."""
        api_key, slug = await self._get_challonge_credentials(guild)
        if not api_key or not slug:
            return 0, 0

        team_count = await self.config.guild(guild).team_count()
        if not team_count or team_count <= 0:
            return 0, 0

        # Check existing participants
        ok, payload = await self._challonge_request(
            guild, "GET", f"/tournaments/{slug}/participants.json"
        )
        existing_names = set()
        if ok and isinstance(payload, list):
            for entry in payload:
                participant = entry.get("participant", {})
                name = participant.get("name", "")
                existing_names.add(name.lower())

        team_map = await self.config.guild(guild).challonge_team_map()
        if not isinstance(team_map, dict):
            team_map = {}

        created = 0
        errors = 0
        prefix = await self.config.guild(guild).team_voice_channel_prefix() or "Team"

        for i in range(1, team_count + 1):
            team_name = f"{prefix} {i}"

            # Skip if already exists
            if team_name.lower() in existing_names:
                # Try to find and map the existing participant
                if ok and isinstance(payload, list):
                    for entry in payload:
                        participant = entry.get("participant", {})
                        if participant.get("name", "").lower() == team_name.lower():
                            team_map[str(i)] = participant.get("id")
                            break
                continue

            # Create participant
            result_ok, result_payload = await self._challonge_request(
                guild,
                "POST",
                f"/tournaments/{slug}/participants.json",
                data={
                    "participant[name]": team_name,
                    "participant[misc]": "",  # Will be filled during draft
                },
            )

            if result_ok:
                participant = (
                    result_payload.get("participant")
                    if isinstance(result_payload, dict)
                    else None
                )
                if participant and participant.get("id"):
                    team_map[str(i)] = participant.get("id")
                    created += 1
            else:
                errors += 1

        await self.config.guild(guild).challonge_team_map.set(team_map)
        await self.config.guild(guild).teams_created_on_challonge.set(True)

        return created, errors

    async def close_expired_applications(self):
        """Close applications that have expired."""
        while self == self.bot.get_cog("ChampionsCircle"):
            try:
                all_guilds = await self.config.all_guilds()
                now_ts = int(datetime.now(timezone.utc).timestamp())
                for guild_id, guild_data in all_guilds.items():
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        continue

                    duration_days = int(guild_data.get("application_duration", 7))
                    active_apps = await self._load_application_list(guild, "active_applications")
                    cancelled_apps = await self._load_application_list(guild, "cancelled_applications")
                    approved_apps = await self._load_application_list(guild, "approved_applications")

                    remaining: List[Dict[str, Any]] = []
                    updated = False
                    for app in active_apps:
                        timestamp = int(app.get("timestamp") or 0)
                        if timestamp and now_ts - timestamp > duration_days * 86400:
                            cancelled_apps.append(app)
                            updated = True
                            user = guild.get_member(app.get("user_id"))
                            if user:
                                await self._send_status_dm(
                                    member=user,
                                    guild=guild,
                                    status_line="Your Champions Circle application has expired.",
                                )
                        else:
                            remaining.append(app)

                    if updated:
                        await self._save_application_list(guild, "active_applications", remaining)
                        await self._save_application_list(guild, "cancelled_applications", cancelled_apps)
                        await self.update_embed(guild)

                    # Legacy linking enforcement disabled - now using draft system
                    # Players are assigned to teams via ccdraft, no individual linking needed
            except Exception as e:
                self.logger.error(f"Error in close_expired_applications: {str(e)}")

            await asyncio.sleep(3600)

    async def _auto_sync_roles_loop(self):
        """Periodically sync team roles from Challonge."""
        while self == self.bot.get_cog("ChampionsCircle"):
            try:
                all_guilds = await self.config.all_guilds()
                for guild_id, guild_data in all_guilds.items():
                    if not guild_data.get("challonge_sync_roles_enabled", True):
                        continue
                    if not guild_data.get("applications_open", False):
                        continue
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        continue
                    await self._sync_roles_from_challonge(guild)
            except Exception as exc:
                self.logger.error("Error in auto role sync: %s", exc)

            interval = 15
            try:
                interval = int(
                    min(
                        max(
                            (data.get("challonge_sync_roles_interval", 15) or 15),
                            5,
                        )
                        for data in (await self.config.all_guilds()).values()
                    )
                )
            except Exception:
                interval = 15
            await asyncio.sleep(interval * 60)

    @commands.command()
    async def cchelp(self, ctx):
        """Display help for Champions Circle commands."""
        embed = discord.Embed(title="Champions Circle Help", color=0x00ff00)

        embed.add_field(
            name="Setup",
            value=(
                "`ccsetup` - create forum + announcement channels and configure the tournament\n"
                "`ccstart` - post the application panel\n"
                "`ccend` - close the tournament and reset applications"
            ),
            inline=False,
        )
        embed.add_field(
            name="Applications",
            value=(
                "Use the **Apply** button in the Champions Circle channel to apply.\n"
                "`cancel_application` - cancel your application\n"
                "`ccapplicants` - view applicant lists"
            ),
            inline=False,
        )
        embed.add_field(
            name="Draft System",
            value=(
                "`ccdraft pool` - show undrafted approved players\n"
                "`ccdraft captains @p1 ...` - set captains for all teams\n"
                "`ccdraft captain <team#> @player` - set a team captain\n"
                "`ccdraft start` - start the snake draft\n"
                "`ccdraft pick @player` - pick when on the clock\n"
                "`ccdraft status` - show current pick/order\n"
                "`ccdraft undo` - undo last pick\n"
                "`ccdraft assign/remove/clear` - admin overrides\n"
                "`ccdraft lock/unlock` - lock/unlock draft changes"
            ),
            inline=False,
        )
        embed.add_field(
            name="Score Reporting",
            value=(
                "`ccscore report <match_id> <score>` - submit a score from team chat\n"
                "`ccscore panel` - post a score report button"
            ),
            inline=False,
        )
        embed.add_field(
            name="Challonge",
            value=(
                "`ccchallonge key <token>` - set API key\n"
                "`ccchallonge tournament <slug|url>` - set tournament slug\n"
                "`ccchallonge set <bracket_url>` - set bracket URL\n"
                "`ccchallonge refresh` - refresh bracket URL\n"
                "`ccchallonge createteams` - create team participants\n"
                "`ccchallonge info/participants/matches` - view Challonge data\n"
                "`ccchallonge sync` - sync team rosters to Challonge\n"
                "`ccchallonge syncroles` - sync team roles from draft\n"
                "`ccchallonge purgeall` - remove all Challonge participants"
            ),
            inline=False,
        )
        embed.add_field(
            name="Admin",
            value=(
                "`cclist` - list current champions\n"
                "`ccsettings` - view current configuration\n"
                "`ccsettime` - set/clear tournament time\n"
                "`ccmode <1v1|2v2|3v3|4v4>` - set game mode\n"
                "`ccquestions add/remove/list` - manage application questions"
            ),
            inline=False,
        )

        await ctx.send(embed=embed)

    @commands.command(name="cclist")
    async def cclist(self, ctx):
        """List current champions."""
        role_id = await self.config.guild(ctx.guild).champions_role_id()
        role = ctx.guild.get_role(role_id) if role_id else None
        if not role:
            await ctx.send("Champions role is not configured. Run `ccsetup` or `setchampionsrole` first.")
            return

        members = list(role.members)
        embed = discord.Embed(
            title="Champions Circle members",
            description=f"Total champions: {len(members)}",
            color=0x00ff00,
        )
        if not members:
            embed.add_field(name="Members", value="No champions yet.", inline=False)
        else:
            lines = [member.mention for member in members]
            chunk: List[str] = []
            chunk_len = 0
            idx = 1
            for line in lines:
                if chunk_len + len(line) + 1 > 900:
                    embed.add_field(name=f"Members ({idx})", value="\n".join(chunk), inline=False)
                    idx += 1
                    chunk = []
                    chunk_len = 0
                chunk.append(line)
                chunk_len += len(line) + 1
            if chunk:
                embed.add_field(name=f"Members ({idx})", value="\n".join(chunk), inline=False)

        await ctx.send(embed=embed)

    @commands.command(name="ccapplicants", aliases=["ccapps", "ccroster"])
    @commands.admin_or_permissions(manage_guild=True)
    async def ccapplicants(self, ctx):
        """View applicant lists by status."""
        guild = ctx.guild
        active = await self._load_application_list(guild, "active_applications")
        approved = await self._load_application_list(guild, "approved_applications")
        denied = await self._load_application_list(guild, "denied_applications")
        cancelled = await self._load_application_list(guild, "cancelled_applications")

        embed = discord.Embed(
            title="Champions Circle applicants",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Totals",
            value=(
                f"🟡 Active: {len(active)}\n"
                f"🟢 Approved: {len(approved)}\n"
                f"🔴 Denied: {len(denied)}\n"
                f"⚪ Cancelled: {len(cancelled)}"
            ),
            inline=False,
        )

        any_entries = any([active, approved, denied, cancelled])
        if not any_entries:
            embed.add_field(name="Applicants", value="No applications yet.", inline=False)
            await ctx.send(embed=embed)
            return

        for label, apps in (
            ("Active Applicants", active),
            ("Approved Champions", approved),
            ("Denied Applications", denied),
            ("Cancelled Applications", cancelled),
        ):
            if not apps:
                continue
            lines = [self._format_user_entry(guild, app) for app in apps]
            for idx, chunk in enumerate(self._chunk_lines(lines), start=1):
                name = label if idx == 1 else f"{label} ({idx})"
                embed.add_field(name=name, value="\n".join(chunk), inline=False)

        await ctx.send(embed=embed)

    @commands.command(name="ccsettings")
    @commands.admin_or_permissions(administrator=True)
    async def ccsettings(self, ctx):
        """Display current tournament settings."""
        guild = ctx.guild
        forum_id = await self.config.guild(guild).champions_forum()
        channel_id = await self.config.guild(guild).champions_channel()
        role_id = await self.config.guild(guild).champions_role_id()
        duration = await self.config.guild(guild).application_duration()
        game_mode = await self.config.guild(guild).game_mode()
        team_count = await self.config.guild(guild).team_count()
        team_roles = await self.config.guild(guild).team_role_ids()
        team_text_channels = await self.config.guild(guild).team_text_channel_ids()
        questions = await self.config.guild(guild).custom_questions()
        applications_open = await self.config.guild(guild).applications_open()
        opened_at = await self.config.guild(guild).applications_opened_at()
        bracket_url = await self.config.guild(guild).tourney_challonge_bracket_url()
        challonge_slug = await self.config.guild(guild).tourney_challonge_slug()
        challonge_key = await self.config.guild(guild).tourney_challonge_api_key()
        team_map = await self.config.guild(guild).challonge_team_map()

        active = await self._load_application_list(guild, "active_applications")
        approved = await self._load_application_list(guild, "approved_applications")
        denied = await self._load_application_list(guild, "denied_applications")
        cancelled = await self._load_application_list(guild, "cancelled_applications")

        forum = guild.get_channel(forum_id) if forum_id else None
        channel = guild.get_channel(channel_id) if channel_id else None
        role = guild.get_role(role_id) if role_id else None

        embed = discord.Embed(title="Champions Circle settings", color=0x00ff00)
        embed.add_field(name="Status", value="Open" if applications_open else "Not started", inline=True)
        if opened_at:
            close_ts = int(opened_at + (duration * 86400))
            embed.add_field(
                name="Applications close",
                value=f"<t:{close_ts}:F> (<t:{close_ts}:R>)",
                inline=True,
            )
        embed.add_field(name="Forum", value=forum.mention if forum else "Not set", inline=True)
        embed.add_field(name="Announcements", value=channel.mention if channel else "Not set", inline=True)
        embed.add_field(name="Champions role", value=role.mention if role else "Not set", inline=True)
        embed.add_field(name="Application duration", value=f"{duration} days", inline=True)
        embed.add_field(name="Game mode", value=game_mode or "Not set", inline=True)
        embed.add_field(
            name="Team size",
            value=str(self._team_size_from_mode(game_mode)),
            inline=True,
        )
        embed.add_field(
            name="Teams",
            value=str(team_count or 0),
            inline=True,
        )
        if team_roles:
            role_mentions = [
                role.mention
                for role_id in team_roles
                if (role := guild.get_role(role_id))
            ]
            if role_mentions:
                embed.add_field(
                    name="Team roles",
                    value=", ".join(role_mentions),
                    inline=False,
                )
        if team_text_channels:
            embed.add_field(
                name="Team text channels",
                value=str(len(team_text_channels)),
                inline=True,
            )
        if bracket_url or challonge_slug:
            lines = []
            if challonge_slug:
                lines.append(f"Tournament: `{challonge_slug}`")
            if bracket_url:
                lines.append(f"Bracket: {bracket_url}")
            if team_map:
                lines.append(f"Teams mapped: {len(team_map)}")
            embed.add_field(name="Challonge", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Challonge API",
            value="Set" if challonge_key else "Not set",
            inline=True,
        )
        # Draft system info
        draft_assignments = await self.config.guild(guild).draft_assignments()
        draft_locked = await self.config.guild(guild).draft_locked()
        draft_started = await self.config.guild(guild).draft_started()
        draft_captains = await self.config.guild(guild).draft_captains()
        if draft_assignments or draft_captains:
            total_drafted = sum(len(v) for v in draft_assignments.values() if isinstance(v, list))
            captain_count = len(draft_captains) if isinstance(draft_captains, dict) else 0
            embed.add_field(
                name="Draft",
                value=(
                    f"Captains: {captain_count}\n"
                    f"Players drafted: {total_drafted}\n"
                    f"Started: {'Yes' if draft_started else 'No'}\n"
                    f"Locked: {'Yes' if draft_locked else 'No'}"
                ),
                inline=True,
            )
        embed.add_field(
            name="Applications",
            value=(
                f"Active: {len(active)}\n"
                f"Approved: {len(approved)}\n"
                f"Denied: {len(denied)}\n"
                f"Cancelled: {len(cancelled)}"
            ),
            inline=True,
        )
        if questions:
            question_list = "\n".join(f"{idx + 1}. {q}" for idx, q in enumerate(questions))
            embed.add_field(name="Questions", value=question_list, inline=False)

        await ctx.send(embed=embed)

    @commands.command(name="ccmode")
    @commands.admin_or_permissions(administrator=True)
    async def ccmode(self, ctx, *, mode: str):
        """Set the tournament game mode (1v1, 2v2, 3v3, 4v4)."""
        normalized = self._normalize_game_mode(mode)
        if not normalized:
            await ctx.send("Invalid game mode. Use 1v1, 2v2, 3v3, or 4v4.")
            return
        await self.config.guild(ctx.guild).game_mode.set(normalized)
        await self.update_embed(ctx.guild)
        await ctx.send(f"Game mode set to {normalized}.")

class ApplicationModal(discord.ui.Modal):
    def __init__(self, cog, questions: List[str]):
        trimmed = [str(q).strip() for q in (questions or []) if str(q).strip()]
        if not trimmed:
            trimmed = [
                "Epic Account ID:",
                "Rank:",
                "Primary Platform (PC, Xbox, PlayStation, Switch):",
                "Preferred Region for Matches (NA East, NA West, EU):",
                "RL Tracker Link:",
                "Age:",
                "Timezone:",
                "Twitch Channel (optional):",
            ]

        super().__init__(title="Tournament Application")
        self.cog = cog
        self.all_questions = trimmed
        self.compound_label = "Additional info"
        self.compound_questions: List[str] = []
        if len(trimmed) > 5:
            self.compound_questions = trimmed[4:]
            self.questions = trimmed[:4] + [self.compound_label]
        else:
            self.questions = trimmed[:5]
        self.extra_questions = []

        self.text_inputs: List[Tuple[str, discord.ui.TextInput]] = []
        self.select_inputs: List[Tuple[str, discord.ui.Select]] = []

        for question in self.questions:
            question_text = question.strip()
            question_lower = question_text.lower()
            is_optional = "optional" in question_lower

            select = self._build_select(question_text, required=not is_optional)
            if select is not None:
                label_text = question_text[:45] if question_text else "Question"
                description = None
                if len(question_text) > 45:
                    description = question_text[:100]
                if is_optional:
                    optional_note = "Optional."
                    if description:
                        description = f"{optional_note} {description}"[:100]
                    else:
                        description = optional_note
                label = discord.ui.Label(text=label_text, description=description, component=select)
                self.select_inputs.append((question_text, select))
                self.add_item(label)
                continue

            label = question_text[:45] if question_text else "Question"
            placeholder = question_text[:100] if question_text else "Answer"
            style = discord.TextStyle.paragraph if "note" in question_lower else discord.TextStyle.short

            if self.compound_questions and question_text == self.compound_label:
                prompt_lines = [f"{idx + 1}. {q}" for idx, q in enumerate(self.compound_questions)]
                prompt_text = "\n".join(prompt_lines)
                if len(prompt_text) > 3800:
                    prompt_text = prompt_text[:3800].rstrip() + "..."
                compound_required = any("optional" not in q.lower() for q in self.compound_questions)
                field = discord.ui.TextInput(
                    label=label,
                    placeholder="Answer each item on its own line.",
                    default=prompt_text,
                    required=compound_required,
                    style=discord.TextStyle.paragraph,
                )
            else:
                field = discord.ui.TextInput(
                    label=label,
                    placeholder=placeholder,
                    required=not is_optional,
                    style=style,
                )
            self.text_inputs.append((question_text, field))
            self.add_item(field)

    def _normalize_link_value(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return cleaned
        if cleaned.startswith(("http://", "https://")):
            return cleaned
        if cleaned.startswith("www."):
            return f"https://{cleaned}"
        if re.match(r"^[a-z0-9.-]+\\.[a-z]{2,}(/|$)", cleaned, re.IGNORECASE):
            return f"https://{cleaned}"
        return cleaned

    def _format_answer(self, question: str, value: str) -> str:
        if not value:
            return value
        question_lower = question.lower()
        if "link" in question_lower or "tracker" in question_lower:
            return self._normalize_link_value(value)
        if re.match(r"^https?://", value.strip(), re.IGNORECASE):
            return value.strip()
        return value

    def _build_select(self, question: str, *, required: bool) -> Optional[discord.ui.Select]:
        question_lower = question.lower()
        select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        if "rank" in question_lower:
            options = [
                "Unranked",
                "Bronze",
                "Silver",
                "Gold",
                "Platinum",
                "Diamond",
                "Champion",
                "Grand Champion",
                "Supersonic Legend",
            ]
            min_values = 1 if required else 0
            return select_cls(
                placeholder="Select your rank",
                options=[discord.SelectOption(label=opt, value=opt) for opt in options],
                min_values=min_values,
                max_values=1,
            )
        if "region" in question_lower:
            options = ["NA East", "NA West", "EU", "OCE", "SAM", "ME", "Asia"]
            min_values = 1 if required else 0
            return select_cls(
                placeholder="Select your region",
                options=[discord.SelectOption(label=opt, value=opt) for opt in options],
                min_values=min_values,
                max_values=1,
            )
        if "platform" in question_lower:
            options = ["PC", "Xbox", "PlayStation", "Switch"]
            min_values = 1 if required else 0
            return select_cls(
                placeholder="Select your platform",
                options=[discord.SelectOption(label=opt, value=opt) for opt in options],
                min_values=min_values,
                max_values=1,
            )
        return None

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        answers: Dict[str, Any] = {}
        for question, field in self.text_inputs:
            if self.compound_questions and question == self.compound_label:
                parsed = self._parse_compound_answers(field.value)
                for q_text, value in zip(self.compound_questions, parsed):
                    answers[q_text] = self._format_answer(q_text, value)
                continue
            answers[question] = self._format_answer(question, field.value)
        for question, select in self.select_inputs:
            selected = select.values[0] if select.values else None
            answers[question] = selected or "-"

        forum_id = await self.cog.config.guild(guild).champions_forum()
        forum = guild.get_channel(forum_id) if forum_id else None
        if forum is None:
            await interaction.response.send_message(
                "Applications forum channel is not configured. Ask an admin to run `ccsetup`.",
                ephemeral=True,
            )
            return

        pending_tag = next(
            (tag for tag in forum.available_tags if tag.name.lower() == "pending"), None
        )
        applied_tags = [pending_tag] if pending_tag else None

        thread_result = await forum.create_thread(
            name=f"Application - {interaction.user.name}",
            content=f"New application from {interaction.user.mention}",
            applied_tags=applied_tags,
        )
        thread = thread_result.thread if hasattr(thread_result, "thread") else thread_result

        def extract_answer(tokens: List[str]) -> Optional[str]:
            for key, value in answers.items():
                key_text = str(key).lower()
                if any(token in key_text for token in tokens):
                    return str(value).strip()
            return None

        embed = discord.Embed(title="Tournament Application", color=discord.Color.blue())
        for question in self.all_questions:
            answer = answers.get(question, "-")
            value_text = str(answer).strip() if answer is not None else ""
            if not value_text:
                value_text = "-"
            if re.match(r"^https?://", value_text, re.IGNORECASE):
                value_text = f"[Open link]({value_text})"
            embed.add_field(name=question, value=value_text, inline=False)
        embed.set_author(name=interaction.user.name, icon_url=interaction.user.display_avatar.url)

        view = ApplicationReviewView(self.cog, interaction.user.id)
        await thread.send(embed=embed, view=view)

        await self.cog._remove_application_from_all(guild, interaction.user.id)
        entry = {
            "user_id": interaction.user.id,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "answers": answers,
            "thread_id": thread.id,
        }
        active_apps = await self.cog._load_application_list(guild, "active_applications")
        active_apps = [app for app in active_apps if app.get("user_id") != interaction.user.id]
        active_apps.append(entry)
        await self.cog._save_application_list(guild, "active_applications", active_apps)

        threads = await self.cog.config.guild(guild).application_threads()
        threads[str(interaction.user.id)] = thread.id
        await self.cog.config.guild(guild).application_threads.set(threads)

        await self.cog.update_embed(guild)
        response_message = "Your application has been submitted!"
        await interaction.response.send_message(response_message, ephemeral=True)

    def _parse_compound_answers(self, raw: str) -> List[str]:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        answers: List[str] = []
        for idx, question in enumerate(self.compound_questions, start=1):
            line = lines[idx - 1] if idx - 1 < len(lines) else ""
            line = re.sub(r"^(?:\\d+[.)]|[-•])\\s*", "", line).strip()
            question_clean = question.rstrip(":").strip()
            line_clean = line.rstrip(":").strip()
            if question_clean and line_clean.lower() == question_clean.lower():
                line = ""
            elif question_clean and line.lower().startswith(question_clean.lower()):
                line = line[len(question_clean):].lstrip(" :-").strip()
            answers.append(line or "-")
        return answers


class ApplicationReviewView(discord.ui.View):
    def __init__(self, cog, applicant_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.applicant_id = applicant_id

    async def _send_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.NotFound:
            return

    async def _ensure_reviewer(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True
        await self._send_ephemeral(interaction, "You don't have permission to review applications.")
        return False

    async def _post_action_embed(
        self, interaction: discord.Interaction, title: str, description: str, color: discord.Color
    ) -> None:
        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            embed = discord.Embed(title=title, description=description, color=color)
            embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url,
            )
            await thread.send(embed=embed)

    @discord.ui.select(
        placeholder="Select review action",
        custom_id="cc_review_select",
        options=[
            discord.SelectOption(
                label="Approve",
                description="Accept the application",
                emoji="✅",
                value="approve",
            ),
            discord.SelectOption(
                label="Deny",
                description="Reject the application",
                emoji="❌",
                value="deny",
            ),
            discord.SelectOption(
                label="Request More Info",
                description="Ask for additional information",
                emoji="ℹ️",
                value="more_info",
            ),
        ],
    )
    async def review_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await self._ensure_reviewer(interaction):
            return
        action = select.values[0]
        if action == "deny":
            await interaction.response.send_modal(DenialReasonModal(self.cog, self.applicant_id))
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if action == "approve":
            await self.approve_application(interaction)
        elif action == "more_info":
            await self.request_more_info(interaction)

    async def approve_application(self, interaction: discord.Interaction):
        entry = await self.cog._move_application(
            interaction.guild, self.applicant_id, "approved_applications"
        )
        if not entry:
            await self._send_ephemeral(interaction, "No pending application found for that user.")
            return
        now_ts = int(datetime.now(timezone.utc).timestamp())
        entry["approved_at"] = now_ts
        approved_apps = await self.cog._load_application_list(
            interaction.guild, "approved_applications"
        )
        for idx, app in enumerate(approved_apps):
            if app.get("user_id") == self.applicant_id:
                approved_apps[idx] = entry
                break
        else:
            approved_apps.append(entry)
        await self.cog._save_application_list(
            interaction.guild, "approved_applications", approved_apps
        )

        thread = interaction.channel
        if isinstance(thread, discord.Thread) and thread.parent:
            approved_tag = next(
                (tag for tag in thread.parent.available_tags if tag.name.lower() == "approved"),
                None,
            )
            if approved_tag:
                await thread.edit(applied_tags=[approved_tag])

        member = interaction.guild.get_member(self.applicant_id)
        role = interaction.guild.get_role(await self.cog.config.guild(interaction.guild).champions_role_id())
        if member and role:
            await member.add_roles(role)

        await self._post_action_embed(
            interaction,
            "Application Approved",
            f"{interaction.user.mention} approved <@{self.applicant_id}>.",
            discord.Color.green(),
        )

        if member:
            thread_url = None
            thread = interaction.guild.get_thread(entry.get("thread_id")) if entry else None
            if isinstance(thread, discord.Thread):
                thread_url = thread.jump_url

            await self.cog._send_status_dm(
                member=member,
                guild=interaction.guild,
                status_line="Your tournament application has been approved!",
                extra="You're now in the draft pool. Watch for the draft to see which team you're assigned to!",
                application_url=thread_url,
            )

        # Note: We no longer add individuals to Challonge here.
        # Teams are created on Challonge, players assigned via ccdraft
        await self.cog.update_embed(interaction.guild)
        await self._send_ephemeral(interaction, "Application approved.")

    async def request_more_info(self, interaction: discord.Interaction):
        thread = interaction.channel
        member = interaction.guild.get_member(self.applicant_id)
        await self._post_action_embed(
            interaction,
            "More Info Requested",
            f"{interaction.user.mention} requested more info from <@{self.applicant_id}>.",
            discord.Color.orange(),
        )
        if member:
            await self.cog._send_status_dm(
                member=member,
                guild=interaction.guild,
                status_line="More information is needed for your application.",
                extra=f"Please reply in the application thread: {thread.jump_url}",
            )
        await self._send_ephemeral(interaction, "Request sent to applicant.")


class JoinButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(
            style=discord.ButtonStyle.green,
            label="Apply for Champions Circle",
            custom_id="join_champions",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        if not await self.cog.config.guild(interaction.guild).applications_open():
            await interaction.response.send_message(
                "Applications are currently closed.",
                ephemeral=True,
            )
            return
        class DummyMessage:
            def __init__(self, author):
                self.author = author

        dummy_message = DummyMessage(interaction.user)
        bucket = self.cog.application_cooldowns.get_bucket(dummy_message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            minutes, seconds = divmod(int(retry_after), 60)
            await interaction.response.send_message(
                f"You can apply again in {minutes} minutes and {seconds} seconds.",
                ephemeral=True,
            )
            return

        list_name, _, _ = await self.cog._find_application(interaction.guild, interaction.user.id)
        if list_name == "active_applications":
            await interaction.response.send_message(
                "You already have an active application for the Champions Circle.",
                ephemeral=True,
            )
            return
        if list_name == "approved_applications":
            await interaction.response.send_message(
                "You're already approved for this tournament.",
                ephemeral=True,
            )
            return

        questions = await self.cog.config.guild(interaction.guild).custom_questions()
        modal = ApplicationModal(self.cog, questions)
        await interaction.response.send_modal(modal)


class ChampionsApplyView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.add_item(JoinButton(cog))
        self.add_item(CancelApplicationButton(cog))



class CancelApplicationButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(style=discord.ButtonStyle.red, label="Cancel Application", custom_id="cancel_application")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        guild = interaction.guild

        list_name, entry, _ = await self.cog._find_application(guild, user_id)
        if not entry:
            await interaction.response.send_message(
                "You don't have an active Champions Circle application to cancel.",
                ephemeral=True,
            )
            return
        if list_name == "cancelled_applications":
            await interaction.response.send_message("Your application is already cancelled.", ephemeral=True)
            return

        approved_entries = await self.cog._load_application_list(guild, "approved_applications")
        was_approved = any(app.get("user_id") == user_id for app in approved_entries)
        if was_approved:
            role_id = await self.cog.config.guild(guild).champions_role_id()
            role = guild.get_role(role_id) if role_id else None
            if role and role in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(role)
                except discord.HTTPException:
                    self.cog.logger.error(
                        "Failed to remove Champions role from %s", interaction.user.name
                    )

        thread_id = entry.get("thread_id")
        if thread_id:
            thread = guild.get_thread(thread_id)
            if isinstance(thread, discord.Thread) and thread.parent:
                cancelled_tag = next(
                    (tag for tag in thread.parent.available_tags if tag.name.lower() == "cancelled"),
                    None,
                )
                if cancelled_tag:
                    try:
                        await thread.edit(applied_tags=[cancelled_tag])
                    except discord.HTTPException:
                        self.cog.logger.error(
                            "Failed to tag cancelled application thread %s", thread_id
                        )
                embed = discord.Embed(
                    title="Application Cancelled",
                    description=f"<@{user_id}> cancelled their application.",
                    color=discord.Color.orange(),
                )
                await thread.send(embed=embed)

        await self.cog._move_application(guild, user_id, "cancelled_applications", entry=entry)
        await self.cog._sync_challonge_participant(guild, user_id, add=False)
        await self.cog._remove_challonge_link(guild, user_id)
        await self.cog.update_embed(guild)
        await interaction.response.send_message("Your application has been cancelled.", ephemeral=True)


class TournamentScheduleView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        
    @discord.ui.select(
        placeholder="Select tournament phase",
        options=[
            discord.SelectOption(label="Registration", value="registration"),
            discord.SelectOption(label="Group Stage", value="groups"),
            discord.SelectOption(label="Playoffs", value="playoffs"),
            discord.SelectOption(label="Finals", value="finals")
        ]
    )
    async def phase_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        # Show date/time picker modal for selected phase
        await interaction.response.send_modal(
            TournamentPhaseScheduleModal(self.cog, select.values[0])
        )

class SetupContinueView(discord.ui.View):
    def __init__(self, cog: ChampionsCircle, guild_id: int, author_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id

    @discord.ui.button(label="Continue setup", style=discord.ButtonStyle.green)
    async def continue_setup(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the setup creator can continue.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(SetupAdvancedModal(self.cog, self.guild_id))

class SetupModal(discord.ui.Modal, title="Tournament Setup"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.game_mode_select: Optional[discord.ui.Select] = None

        self.tourney_title = discord.ui.TextInput(
            label="Tournament Title",
            placeholder="Enter tournament title",
            required=True,
        )
        self.add_item(self.tourney_title)

        self.description = discord.ui.TextInput(
            label="Tournament Description",
            placeholder="Enter tournament description",
            style=discord.TextStyle.paragraph,
            required=True,
        )
        self.add_item(self.description)

        self.time = discord.ui.TextInput(
            label="Tournament Time",
            placeholder="YYYY-MM-DD HH:MM:SS (optional)",
            required=False,
        )
        self.add_item(self.time)

        self.duration = discord.ui.TextInput(
            label="Application Duration (days)",
            placeholder="Enter number of days",
            required=True,
        )
        self.add_item(self.duration)

        select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        game_mode = select_cls(
            placeholder="Game mode",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="1v1", value="1v1"),
                discord.SelectOption(label="2v2", value="2v2"),
                discord.SelectOption(label="3v3", value="3v3"),
                discord.SelectOption(label="4v4", value="4v4"),
            ],
            custom_id="ccsetup_game_mode",
        )
        self.game_mode_select = game_mode
        label = discord.ui.Label(
            text="Game mode",
            description="Choose 1v1, 2v2, 3v3, or 4v4.",
            component=game_mode,
        )
        self.add_item(label)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.process_setup(interaction, self)


class SetupAdvancedModal(discord.ui.Modal, title="Tournament Setup (Advanced)"):
    def __init__(self, cog: ChampionsCircle, guild_id: int):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id

        self.team_count = discord.ui.TextInput(
            label="Team count (optional)",
            placeholder="Leave blank to skip voice channels",
            required=False,
        )
        self.add_item(self.team_count)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.process_advanced_setup(interaction, self)

class DenialReasonModal(discord.ui.Modal, title="Application Denial"):
    def __init__(self, cog, applicant_id):
        super().__init__()
        self.cog = cog
        self.applicant_id = applicant_id

        self.reason = discord.ui.TextInput(
            label="Denial Reason",
            placeholder="Enter the reason for denying this application",
            style=discord.TextStyle.paragraph,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        thread = interaction.channel
        if isinstance(thread, discord.Thread) and thread.parent:
            denied_tag = next(
                (tag for tag in thread.parent.available_tags if tag.name.lower() == "denied"),
                None,
            )
            if denied_tag:
                await thread.edit(applied_tags=[denied_tag])

        entry = await self.cog._move_application(
            interaction.guild, self.applicant_id, "denied_applications"
        )
        if not entry:
            await interaction.response.send_message(
                "No pending application found for that user.", ephemeral=True
            )
            return

        if isinstance(thread, discord.Thread):
            embed = discord.Embed(
                title="Application Denied",
                description=(
                    f"{interaction.user.mention} denied <@{self.applicant_id}>.\n"
                    f"Reason: {self.reason.value}"
                ),
                color=discord.Color.red(),
            )
            embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url,
            )
            await thread.send(embed=embed)

        member = interaction.guild.get_member(self.applicant_id)
        if member:
            await self.cog._send_status_dm(
                member=member,
                guild=interaction.guild,
                status_line="Your tournament application has been denied.",
                extra=f"Reason: {self.reason.value}",
            )

        await self.cog._sync_challonge_participant(
            interaction.guild, self.applicant_id, add=False
        )
        await self.cog._remove_challonge_link(interaction.guild, self.applicant_id)
        await self.cog.update_embed(interaction.guild)
        await interaction.response.send_message("Application denied.", ephemeral=True)


class TournamentPhaseScheduleModal(discord.ui.Modal):
    def __init__(self, cog, phase):
        super().__init__(title=f"Schedule {phase.title()} Phase")
        self.cog = cog
        self.phase = phase
        
        self.start_time = discord.ui.TextInput(
            label="Start Time",
            placeholder="YYYY-MM-DD HH:MM:SS",
            required=True
        )
        self.add_item(self.start_time)
        
        self.duration = discord.ui.TextInput(
            label="Duration (hours)",
            placeholder="Enter duration in hours",
            required=True
        )
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            start = datetime.fromisoformat(self.start_time.value).replace(tzinfo=timezone.utc)
            duration = float(self.duration.value)
            
            embed = discord.Embed(
                title=f"Tournament {self.phase.title()} Phase Scheduled",
                color=discord.Color.blue()
            )
            embed.add_field(name="Start Time", value=f"<t:{int(start.timestamp())}:F>")
            embed.add_field(name="Duration", value=f"{duration} hours")
            
            await interaction.response.send_message(embed=embed)
            
        except ValueError:
            await interaction.response.send_message(
                "Invalid time format or duration. Please use YYYY-MM-DD HH:MM:SS for time and a number for duration.",
                ephemeral=True
            )

class ScoreReportModal(discord.ui.Modal, title="Report Match Score"):
    def __init__(
        self,
        cog: ChampionsCircle,
        guild_id: int,
        match_id: int,
        participant_id: int,
        opponent_name: Optional[str] = None,
    ):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.match_id = match_id
        self.participant_id = participant_id
        label = "Score (your-opponent)"
        if opponent_name:
            label = f"Score vs {opponent_name}"[:45]
        self.score = discord.ui.TextInput(
            label=label,
            placeholder="e.g. 3-1",
            required=True,
        )
        self.add_item(self.score)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild or guild.id != self.guild_id:
            await interaction.response.send_message("Guild mismatch.", ephemeral=True)
            return
        ok, message = await self.cog._submit_match_score(
            guild,
            match_id=self.match_id,
            participant_id=self.participant_id,
            score_input=self.score.value,
        )
        await interaction.response.send_message(message, ephemeral=True)

class ScoreMatchSelectView(discord.ui.View):
    def __init__(
        self,
        cog: ChampionsCircle,
        guild_id: int,
        participant_id: int,
        matches: List[Dict[str, Any]],
        name_map: Dict[int, str],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.participant_id = participant_id

        options = []
        for match in matches:
            mid = match.get("id")
            p1 = match.get("player1_id") or match.get("participant1_id")
            p2 = match.get("player2_id") or match.get("participant2_id")
            opponent_id = p2 if participant_id == p1 else p1
            opponent_name = name_map.get(opponent_id, f"Participant {opponent_id}")
            round_num = match.get("round")
            label = f"Match {mid}: vs {opponent_name}"
            if round_num is not None:
                label = f"R{round_num} · {label}"
            options.append(
                discord.SelectOption(label=label[:100], value=str(mid))
            )
        self.select = discord.ui.Select(
            placeholder="Select match",
            min_values=1,
            max_values=1,
            options=options[:25],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("Guild mismatch.", ephemeral=True)
            return
        match_id = int(self.select.values[0])
        name_map = await self.cog._get_participant_name_map(interaction.guild)
        opponent_name = None
        matches = await self.cog._get_open_matches_for_participant(
            interaction.guild, self.participant_id
        )
        for match in matches:
            if int(match.get("id", 0)) == match_id:
                p1 = match.get("player1_id") or match.get("participant1_id")
                p2 = match.get("player2_id") or match.get("participant2_id")
                opponent_id = p2 if self.participant_id == p1 else p1
                opponent_name = name_map.get(opponent_id)
                break
        await interaction.response.send_modal(
            ScoreReportModal(
                self.cog,
                interaction.guild.id,
                match_id,
                self.participant_id,
                opponent_name=opponent_name,
            )
        )

class ScoreReportView(discord.ui.View):
    def __init__(self, cog: ChampionsCircle):
        super().__init__(timeout=3600)
        self.cog = cog

    @discord.ui.button(label="Report Score", style=discord.ButtonStyle.primary)
    async def report_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._start_score_report(interaction)


class ChallongeLinkSelectView(discord.ui.View):
    def __init__(
        self,
        cog: ChampionsCircle,
        guild_id: int,
        member_id: int,
        participants: List[Dict[str, Any]],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.member_id = member_id

        options = []
        for entry in participants[:25]:
            participant = entry.get("participant", {})
            pid = participant.get("id")
            name = participant.get("name") or f"Participant {pid}"
            label = f"{name}"
            description = f"ID: {pid}" if pid else None
            if pid is None:
                continue
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(pid),
                    description=description[:100] if description else None,
                )
            )

        select = discord.ui.Select(
            placeholder="Select your Challonge participant",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("Guild mismatch.", ephemeral=True)
            return
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("You can only link yourself.", ephemeral=True)
            return
        participant_id = int(self.children[0].values[0])
        participant_data = await self.cog._resolve_challonge_participant(
            interaction.guild, str(participant_id)
        )
        participant_name = (
            participant_data.get("name") if participant_data else str(participant_id)
        )
        await self.cog._set_challonge_link(
            interaction.guild,
            user_id=interaction.user.id,
            participant_id=participant_id,
            participant_name=participant_name,
        )
        await self.cog._sync_roles_from_challonge(interaction.guild)
        await interaction.response.send_message(
            f"Linked to `{participant_name}`.",
            ephemeral=True,
        )

async def setup(bot):
    cog = ChampionsCircle(bot)
    await bot.add_cog(cog)
