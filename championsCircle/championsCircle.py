import discord
from redbot.core import commands, Config
from discord.ext.commands import guild_only
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

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

    async def _move_application(
        self,
        guild: discord.Guild,
        user_id: int,
        target_list: str,
        *,
        entry: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not entry:
            entry = await self._remove_application(guild, user_id)
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
            "Champions role set automatically. Click to continue tournament setup:",
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

            embed = discord.Embed(
                title="Tournament Setup Complete",
                color=discord.Color.green(),
                description="Your tournament has been configured with the following settings:",
            )
            embed.add_field(name="Title", value=modal.tourney_title.value)
            embed.add_field(name="Description", value=modal.description.value)
            if time_value:
                embed.add_field(name="Time", value=f"<t:{timestamp}:F>")
            else:
                embed.add_field(name="Time", value="Not set")
            embed.add_field(name="Application Duration", value=f"{days} days")

            forum_id = await self.config.guild(interaction.guild).champions_forum()
            channel_id = await self.config.guild(interaction.guild).champions_channel()
            forum_channel = interaction.guild.get_channel(forum_id) if forum_id else None
            announcement_channel = interaction.guild.get_channel(channel_id) if channel_id else None
            embed.add_field(
                name="Channels",
                value=(
                    f"Forum: {forum_channel.mention if forum_channel else 'Not found'}\n"
                    f"Announcements: {announcement_channel.mention if announcement_channel else 'Not found'}"
                ),
                inline=False,
            )

            role = interaction.guild.get_role(
                await self.config.guild(interaction.guild).champions_role_id()
            )
            embed.add_field(name="Champions Role", value=role.mention if role else "Not found")

            if interaction.response.is_done():
                await interaction.followup.send(embed=embed)
            else:
                await interaction.response.send_message(embed=embed)

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
        note = "\n\nNote: The modal shows the first 5 questions. Extra questions are asked in the thread."
        await ctx.send(f"Current questions:\n{question_list}{note}")

    def _format_time_display(self, tourney_time: Optional[int]) -> str:
        if not tourney_time:
            return "Not set"
        return f"<t:{tourney_time}:F> (<t:{tourney_time}:R>)"

    def _truncate_text(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(limit - 3, 0)].rstrip() + "..."

    def _build_panel_embed(
        self,
        *,
        guild: discord.Guild,
        title: str,
        description: str,
        status: str,
        time_display: str,
        duration: int,
        counts: Dict[str, int],
        forum: Optional[discord.abc.GuildChannel],
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
        duration: int,
        counts: Dict[str, int],
        forum: Optional[discord.abc.GuildChannel],
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
        status = "Open" if applications_open else "Not started"
        time_display = self._format_time_display(tourney_time)
        updated_ts = int(datetime.now(timezone.utc).timestamp())

        forum_id = await self.config.guild(guild).champions_forum()
        forum = guild.get_channel(forum_id) if forum_id else None

        panel_view = self._build_panel_view(
            guild=guild,
            title=tourney_title,
            description=tourney_description,
            status=status,
            time_display=time_display,
            duration=duration,
            counts=counts,
            forum=forum,
            updated_ts=updated_ts,
        )
        fallback_embed = self._build_panel_embed(
            guild=guild,
            title=tourney_title,
            description=tourney_description,
            status=status,
            time_display=time_display,
            duration=duration,
            counts=counts,
            forum=forum,
            updated_ts=updated_ts,
        )

        channel = self.bot.get_channel(await self.config.guild(guild).champions_channel())
        if not channel:
            self.logger.error(
                f"Error: Channel with ID {await self.config.guild(guild).champions_channel()} not found."
            )
            return

        try:
            message_id = await self.config.guild(guild).champions_message_id()
            if message_id:
                message = await channel.fetch_message(message_id)
                await message.edit(content=None, embed=None, view=panel_view)
            else:
                message = await channel.send(view=panel_view)
                await self.config.guild(guild).champions_message_id.set(message.id)
        except discord.HTTPException as e:
            self.logger.error(f"Error updating panel view: {str(e)}")
            try:
                message_id = await self.config.guild(guild).champions_message_id()
                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                        await message.edit(embed=fallback_embed)
                        return
                    except discord.HTTPException:
                        await self.config.guild(guild).champions_message_id.set(None)
                message = await channel.send(embed=fallback_embed)
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
                                try:
                                    await user.send(
                                        "Your Champions Circle application has expired."
                                    )
                                except discord.HTTPException:
                                    self.logger.error(
                                        "Failed to send expiration message to user %s", user.id
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
                "`cancel_application` - cancel your application"
            ),
            inline=False,
        )
        embed.add_field(
            name="Questions",
            value="`ccquestions add/remove/list` - manage application questions (first 5 in modal, extras in thread)",
            inline=False,
        )
        embed.add_field(
            name="Admin",
            value=(
                "`cclist` - list current champions\n"
                "`ccsettings` - view current configuration\n"
                "`ccsettime` - set/clear tournament time\n"
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

    @commands.command(name="ccsettings")
    @commands.admin_or_permissions(administrator=True)
    async def ccsettings(self, ctx):
        """Display current tournament settings."""
        guild = ctx.guild
        forum_id = await self.config.guild(guild).champions_forum()
        channel_id = await self.config.guild(guild).champions_channel()
        role_id = await self.config.guild(guild).champions_role_id()
        duration = await self.config.guild(guild).application_duration()
        questions = await self.config.guild(guild).custom_questions()
        applications_open = await self.config.guild(guild).applications_open()

        active = await self._load_application_list(guild, "active_applications")
        approved = await self._load_application_list(guild, "approved_applications")
        denied = await self._load_application_list(guild, "denied_applications")
        cancelled = await self._load_application_list(guild, "cancelled_applications")

        forum = guild.get_channel(forum_id) if forum_id else None
        channel = guild.get_channel(channel_id) if channel_id else None
        role = guild.get_role(role_id) if role_id else None

        embed = discord.Embed(title="Champions Circle settings", color=0x00ff00)
        embed.add_field(name="Status", value="Open" if applications_open else "Not started", inline=True)
        embed.add_field(name="Forum", value=forum.mention if forum else "Not set", inline=True)
        embed.add_field(name="Announcements", value=channel.mention if channel else "Not set", inline=True)
        embed.add_field(name="Champions role", value=role.mention if role else "Not set", inline=True)
        embed.add_field(name="Application duration", value=f"{duration} days", inline=True)
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
        self.questions = trimmed[:5]
        self.extra_questions = trimmed[5:]

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
            field = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                required=not is_optional,
                style=style,
            )
            self.text_inputs.append((question_text, field))
            self.add_item(field)

    def _build_select(self, question: str, *, required: bool) -> Optional[discord.ui.Select]:
        question_lower = question.lower()
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
            return discord.ui.Select(
                placeholder="Select your rank",
                options=[discord.SelectOption(label=opt, value=opt) for opt in options],
                min_values=min_values,
                max_values=1,
                required=required,
            )
        if "region" in question_lower:
            options = ["NA East", "NA West", "EU", "OCE", "SAM", "ME", "Asia"]
            min_values = 1 if required else 0
            return discord.ui.Select(
                placeholder="Select your region",
                options=[discord.SelectOption(label=opt, value=opt) for opt in options],
                min_values=min_values,
                max_values=1,
                required=required,
            )
        if "platform" in question_lower:
            options = ["PC", "Xbox", "PlayStation", "Switch"]
            min_values = 1 if required else 0
            return discord.ui.Select(
                placeholder="Select your platform",
                options=[discord.SelectOption(label=opt, value=opt) for opt in options],
                min_values=min_values,
                max_values=1,
                required=required,
            )
        return None

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        answers: Dict[str, Any] = {}
        for question, field in self.text_inputs:
            answers[question] = field.value
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

        summary_parts = []
        rank = extract_answer(["rank"])
        region = extract_answer(["region"])
        platform = extract_answer(["platform"])
        tracker_link = extract_answer(["tracker", "rl tracker"])
        if rank:
            summary_parts.append(f"**{rank}**")
        if region:
            summary_parts.append(region)
        if platform:
            summary_parts.append(platform)
        if tracker_link:
            summary_parts.append(f"[Tracker]({tracker_link})")

        embed = discord.Embed(title="Tournament Application", color=discord.Color.blue())
        if summary_parts:
            embed.description = " | ".join(summary_parts)
        for question, answer in answers.items():
            embed.add_field(name=question, value=answer or "-", inline=False)
        embed.set_author(name=interaction.user.name, icon_url=interaction.user.display_avatar.url)

        view = ApplicationReviewView(self.cog, interaction.user.id)
        await thread.send(embed=embed, view=view)

        await self.cog._remove_application(guild, interaction.user.id)
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

        if self.extra_questions:
            follow_up = "\n".join(f"- {q}" for q in self.extra_questions)
            extra_embed = discord.Embed(
                title="Additional Questions",
                description=(
                    "Please reply in this thread with answers to the following:\n"
                    f"{follow_up}"
                ),
                color=discord.Color.blurple(),
            )
            await thread.send(embed=extra_embed)

        await self.cog.update_embed(guild)
        response_message = "Your application has been submitted!"
        if self.extra_questions:
            response_message += f" Please answer the additional questions in {thread.mention}."
        await interaction.response.send_message(response_message, ephemeral=True)


class ApplicationReviewView(discord.ui.View):
    def __init__(self, cog, applicant_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.applicant_id = applicant_id

    async def _send_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

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
        if select.values[0] == "approve":
            await self.approve_application(interaction)
        elif select.values[0] == "deny":
            await interaction.response.send_modal(DenialReasonModal(self.cog, self.applicant_id))
        elif select.values[0] == "more_info":
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
            try:
                await member.send("Congratulations! Your tournament application has been approved!")
            except discord.HTTPException:
                pass

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
            try:
                await member.send(
                    f"Your application needs more info. Please reply in the application thread: {thread.jump_url}"
                )
            except discord.HTTPException:
                pass
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

        if list_name == "approved_applications":
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

class SetupModal(discord.ui.Modal, title="Tournament Setup"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog

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

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.process_setup(interaction, self)


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
            try:
                await member.send(
                    f"Your tournament application has been denied.\n"
                    f"Reason: {self.reason.value}"
                )
            except discord.HTTPException:
                pass

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

