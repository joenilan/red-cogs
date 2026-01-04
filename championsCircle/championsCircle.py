import discord
import aiohttp
from redbot.core import commands, Config
from discord.ext.commands import guild_only
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import re
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
        }
        self.config.register_guild(**default_guild)
        self.logger = logging.getLogger("red.championsCircle")
        self.application_cooldowns = commands.CooldownMapping.from_cooldown(1, 3600, commands.BucketType.user)
        self._expiry_task: Optional[asyncio.Task] = None
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
        try:
            self.bot.add_view(ChampionsApplyView(self))
        except Exception as exc:
            self.logger.error("Failed to register Champions Circle persistent view: %s", exc)

    def cog_unload(self):
        if self._expiry_task:
            self._expiry_task.cancel()

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

        # Archive/lock application threads
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
                    await thread.edit(archived=True, locked=True)
                except discord.HTTPException:
                    self.logger.error("Failed to archive thread %s", thread_id)

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
                    await roster_thread.edit(archived=True, locked=True)
                except discord.HTTPException:
                    self.logger.error("Failed to archive roster thread %s", roster_thread_id)

        await self._cleanup_team_voice_assets(ctx.guild)

        # Clear messages
        channel = ctx.channel
        await ctx.send("Ending tournament and clearing channel...")

        try:
            await channel.purge(limit=None)
        except discord.Forbidden:
            await ctx.send("I don't have permission to delete messages in this channel.")
            return
        except discord.HTTPException:
            await ctx.send("An error occurred while trying to delete messages.")
            return

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
        temp_msg = await channel.send("Tournament ended. Channel cleared, cog state reset, and application cooldowns reset. You can now use the tourney start command for a new tournament.", delete_after=10)

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
        signup_url = self._normalize_url(
            await self.config.guild(guild).tourney_challonge_signup_url()
        )
        bracket_url = self._normalize_url(
            await self.config.guild(guild).tourney_challonge_bracket_url()
        )

        lines = [f"**{title}**"]
        if tourney_time:
            lines.append(f"Time: <t:{tourney_time}:F> (<t:{tourney_time}:R>)")
        if signup_url:
            lines.append(f"Signup: {signup_url}")
        if bracket_url:
            lines.append(f"Bracket: {bracket_url}")
        return "\n".join(lines)

    async def _send_status_dm(
        self,
        *,
        member: discord.abc.User,
        guild: discord.Guild,
        status_line: str,
        extra: Optional[str] = None,
    ) -> None:
        info = await self._build_tournament_info(guild)
        message = f"{status_line}\n\n{info}"
        if extra:
            message = f"{message}\n\n{extra}"
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

    async def _ensure_voice_category(
        self, guild: discord.Guild, name: str
    ) -> Optional[discord.CategoryChannel]:
        category = next((c for c in guild.categories if c.name.lower() == name.lower()), None)
        if category:
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
                manage_permissions=True,
                connect=True,
                speak=True,
                send_messages=True,
                read_message_history=True,
            )
        return overwrites

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
        for index in range(1, team_count + 1):
            name = f"{prefix} {index}"
            channel = existing_channels.get(index)
            if channel is None:
                try:
                    overwrites = None
                    team_role = team_roles.get(index) if team_roles else None
                    if team_role:
                        overwrites = self._build_team_overwrites(
                            guild, team_role=team_role, is_voice=True
                        )
                    channel = await guild.create_voice_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites,
                        reason="Champions Circle setup",
                    )
                except discord.HTTPException:
                    continue
            else:
                team_role = team_roles.get(index) if team_roles else None
                if team_role:
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
        for index in range(1, team_count + 1):
            name = self._slugify_channel_name(f"{prefix} {index}")
            channel = existing_channels.get(index)
            team_role = team_roles.get(index)
            if team_role is None:
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
                except discord.HTTPException:
                    continue
            else:
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
        overwrites = dict(category.overwrites)
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        bot_member = guild.me or guild.get_member(self.bot.user.id)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                manage_channels=True,
                manage_permissions=True,
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
        await self._ensure_team_voice_channels(
            guild,
            category=category,
            team_count=team_count,
            prefix=prefix,
            team_roles=team_roles,
        )
        await self._ensure_team_text_channels(
            guild,
            category=category,
            team_count=team_count,
            prefix=prefix,
            team_roles=team_roles,
        )

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
        if not participant_id:
            return
        ok, payload = await self._challonge_request(
            guild,
            "DELETE",
            f"/tournaments/{slug}/participants/{participant_id}.json",
        )
        if ok or "not found" in str(payload).lower():
            stored_map.pop(user_id_str, None)
            await self.config.guild(guild).tourney_challonge_participant_map.set(stored_map)

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
        signup_url: Optional[str],
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
        signup_url: Optional[str],
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
        signup_url = self._normalize_url(
            await self.config.guild(guild).tourney_challonge_signup_url()
        )
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
            signup_url=signup_url,
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
            signup_url=signup_url,
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
        """Manage Challonge links for the tournament."""
        if ctx.invoked_subcommand is None:
            signup_url = await self.config.guild(ctx.guild).tourney_challonge_signup_url()
            bracket_url = await self.config.guild(ctx.guild).tourney_challonge_bracket_url()
            api_key = await self.config.guild(ctx.guild).tourney_challonge_api_key()
            slug = await self.config.guild(ctx.guild).tourney_challonge_slug()
            lines = []
            if signup_url:
                lines.append(f"Signup: {signup_url}")
            if bracket_url:
                lines.append(f"Bracket: {bracket_url}")
            if slug:
                lines.append(f"Tournament: {slug}")
            if api_key:
                masked = f"{api_key[:4]}***{api_key[-4:]}" if len(api_key) > 8 else "***"
                lines.append(f"API key: set ({masked})")
            if not lines:
                await ctx.send("No Challonge links set.")
            else:
                await ctx.send("\n".join(lines))

    @ccchallonge.command(name="set")
    async def ccchallonge_set(
        self,
        ctx,
        signup_url: str,
        bracket_url: Optional[str] = None,
    ):
        """Set Challonge signup and optional bracket links."""
        signup_value = self._normalize_url(signup_url)
        bracket_value = self._normalize_url(bracket_url) if bracket_url else None
        await self.config.guild(ctx.guild).tourney_challonge_signup_url.set(signup_value)
        await self.config.guild(ctx.guild).tourney_challonge_bracket_url.set(bracket_value)
        await self.update_embed(ctx.guild)
        await ctx.send("Challonge links updated.")

    @ccchallonge.command(name="tournament", aliases=["slug"])
    async def ccchallonge_tournament(self, ctx, *, slug_or_url: Optional[str] = None):
        """Set or clear the Challonge tournament slug."""
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
            await ctx.send("Challonge tournament cleared.")
            return

        await self.config.guild(ctx.guild).tourney_challonge_slug.set(slug)
        await ctx.send(f"Challonge tournament set to `{slug}`.")

    @ccchallonge.command(name="refresh")
    async def ccchallonge_refresh(self, ctx):
        """Pull bracket/signup links from Challonge (v1) if available."""
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
        signup_url = tournament.get("sign_up_url") or tournament.get("signup_url")

        if bracket_url:
            await self.config.guild(ctx.guild).tourney_challonge_bracket_url.set(bracket_url)
        if signup_url:
            await self.config.guild(ctx.guild).tourney_challonge_signup_url.set(signup_url)

        await self.update_embed(ctx.guild)

        if signup_url:
            await ctx.send("Challonge links refreshed from API.")
        else:
            await ctx.send(
                "Bracket link refreshed. Signup link was not provided by the API; set it manually if needed."
            )

    @ccchallonge.command(name="clear")
    async def ccchallonge_clear(self, ctx):
        """Clear Challonge links."""
        await self.config.guild(ctx.guild).tourney_challonge_signup_url.set(None)
        await self.config.guild(ctx.guild).tourney_challonge_bracket_url.set(None)
        await self.update_embed(ctx.guild)
        await ctx.send("Challonge links cleared.")

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

        added = 0
        linked = 0
        errors = 0

        for user_id in approved_ids:
            user_id_str = str(user_id)
            member = ctx.guild.get_member(user_id)
            display_name = member.display_name if member else f"User {user_id}"

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

    @commands.command()
    @commands.admin_or_permissions(administrator=True)
    @guild_only()
    async def setchampionsrole(self, ctx, role: discord.Role):
        """Set the Champions Circle role."""
        await self.config.guild(ctx.guild).champions_role_id.set(role.id)
        await ctx.send(f"Champions Circle role set to {role.name}")

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
            except Exception as e:
                self.logger.error(f"Error in close_expired_applications: {str(e)}")

            await asyncio.sleep(3600)

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
            name="Questions",
            value="`ccquestions add/remove/list` - manage questions (extras grouped into Additional info)",
            inline=False,
        )
        embed.add_field(
            name="Admin",
            value=(
                "`cclist` - list current champions\n"
                "`ccsettings` - view current configuration\n"
                "`ccsettime` - set/clear tournament time\n"
                "`ccchallonge set/clear` - manage Challonge links\n"
                "`ccchallonge tournament <slug|url>` - set tournament slug\n"
                "`ccchallonge key <token|clear>` - manage Challonge API key\n"
                "`ccchallonge refresh` - pull links from Challonge\n"
                "`ccchallonge info/participants/matches` - view Challonge data\n"
                "`ccchallonge sync [purge]` - sync approved applicants\n"
                "`ccmode <1v1|2v2|3v3|4v4>` - set game mode\n"
                "`ccsetup` now includes game mode + advanced team/voice options\n"
                "`setchampionschannel` / `setchampionsrole` / `setapplicationduration`"
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
        signup_url = await self.config.guild(guild).tourney_challonge_signup_url()
        bracket_url = await self.config.guild(guild).tourney_challonge_bracket_url()
        challonge_slug = await self.config.guild(guild).tourney_challonge_slug()
        challonge_key = await self.config.guild(guild).tourney_challonge_api_key()
        challonge_slug = await self.config.guild(guild).tourney_challonge_slug()
        challonge_key = await self.config.guild(guild).tourney_challonge_api_key()

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
        if signup_url or bracket_url or challonge_slug:
            lines = []
            if signup_url:
                lines.append(f"Signup: {signup_url}")
            if bracket_url:
                lines.append(f"Bracket: {bracket_url}")
            if challonge_slug:
                lines.append(f"Tournament: {challonge_slug}")
            embed.add_field(name="Challonge", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Challonge API key",
            value="Set" if challonge_key else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Challonge API key",
            value="Set" if challonge_key else "Not set",
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
            await self.cog._send_status_dm(
                member=member,
                guild=interaction.guild,
                status_line="Your tournament application has been approved!",
            )

        await self.cog._sync_challonge_participant(
            interaction.guild, self.applicant_id, add=True
        )
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

async def setup(bot):
    cog = ChampionsCircle(bot)
    await bot.add_cog(cog)

