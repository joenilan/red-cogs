import discord
from redbot.core import commands, Config
from discord.ext.commands import guild_only
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from discord import app_commands
from typing import Optional

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
            "application_duration": 7,  # days
            "custom_questions": [
                "Epic Account ID:",
                "Rank:",
                "Primary Platform (PC, Xbox, PlayStation, Switch):",
                "Preferred Region for Matches (NA East, NA West, EU, Other - please specify if Other):",
                "RL Tracker Link:",
                "Have you read and understood the tournament rules? (Yes/No)",
                "Do you agree to follow the tournament code of conduct? (Yes/No)",
                "Any special requests or additional notes? (e.g., match scheduling preferences, etc)"
            ],
            "tourney_title": "Champions Circle Tournament",
            "tourney_description": "Join our exciting tournament!",
            "tourney_time": None,  # We'll store this as a UTC timestamp
        }
        self.config.register_guild(**default_guild)
        self.logger = logging.getLogger("red.championsCircle")
        self.admin_user_id = 131881984690487296  # Replace with the actual admin user ID
        self.application_cooldowns = commands.CooldownMapping.from_cooldown(1, 3600, commands.BucketType.user)

    def reset_cooldowns(self):
        self.application_cooldowns = commands.CooldownMapping.from_cooldown(1, 3600, commands.BucketType.user)

    @commands.Cog.listener()
    async def on_ready(self):
        self.logger.info(f"ChampionsCircle is ready!")
        self.bot.loop.create_task(self.close_expired_applications())

    @commands.command(name="ccsetup")
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def ccsetup(self, ctx):
        """Initialize and setup the tournament system"""
        
        # Create forum channel
        forum_channel = await ctx.guild.create_forum(
            name="tournament-applications",
            topic="Tournament Applications",
            reason="Tournament setup"
        )
        
        # Set up forum tags
        tags = [
            discord.ForumTag(name="Pending", emoji="🔵"),
            discord.ForumTag(name="Approved", emoji="✅"),
            discord.ForumTag(name="Denied", emoji="❌"),
            discord.ForumTag(name="Cancelled", emoji="⚠️")
        ]
        await forum_channel.edit(available_tags=tags)
        
        # Create announcement channel
        announcement_channel = await ctx.guild.create_text_channel(
            name="tournament-announcements",
            topic="Tournament Announcements"
        )
        
        # Save channels to config
        await self.config.guild(ctx.guild).champions_forum.set(forum_channel.id)
        await self.config.guild(ctx.guild).champions_channel.set(announcement_channel.id)
        
        # Create role selection view
        class RoleSelect(discord.ui.RoleSelect):
            def __init__(self):
                super().__init__(placeholder="Select Champions Role", min_values=1, max_values=1)
            
            async def callback(self, interaction: discord.Interaction):
                role = self.values[0]
                await self.view.cog.config.guild(interaction.guild).champions_role_id.set(role.id)
                await interaction.response.send_modal(SetupModal())

        class RoleView(discord.ui.View):
            def __init__(self, cog):
                super().__init__()
                self.cog = cog
                self.add_item(RoleSelect())

        # Start setup process
        await ctx.send(
            "Let's set up your tournament! First, select the Champions role:",
            view=RoleView(self)
        )

    async def process_setup(self, interaction: discord.Interaction, modal: SetupModal):
        """Process the setup modal submission"""
        try:
            # Set tournament title and description
            await self.config.guild(interaction.guild).tourney_title.set(modal.title.value)
            await self.config.guild(interaction.guild).tourney_description.set(modal.description.value)
            
            # Set tournament time
            try:
                tourney_time = datetime.fromisoformat(modal.time.value).replace(tzinfo=timezone.utc)
                timestamp = int(tourney_time.timestamp())
                await self.config.guild(interaction.guild).tourney_time.set(timestamp)
            except ValueError:
                await interaction.followup.send("Invalid time format. Please use YYYY-MM-DD HH:MM:SS", ephemeral=True)
                return
            
            # Set application duration
            try:
                days = int(modal.duration.value)
                await self.config.guild(interaction.guild).application_duration.set(days)
            except ValueError:
                await interaction.followup.send("Invalid duration. Please enter a number", ephemeral=True)
                return
            
            # Send success message
            embed = discord.Embed(
                title="Tournament Setup Complete",
                color=discord.Color.green(),
                description="Your tournament has been configured with the following settings:"
            )
            embed.add_field(name="Title", value=modal.title.value)
            embed.add_field(name="Description", value=modal.description.value)
            embed.add_field(name="Time", value=f"<t:{timestamp}:F>")
            embed.add_field(name="Application Duration", value=f"{days} days")
            embed.add_field(name="Channels Created", value=f"✅ Forum Channel\n✅ Announcement Channel")
            
            role = interaction.guild.get_role(await self.config.guild(interaction.guild).champions_role_id())
            embed.add_field(name="Champions Role", value=role.mention if role else "Not found")
            
            await interaction.response.send_message(embed=embed)
            
            # Add start tournament button
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Start Tournament",
                style=discord.ButtonStyle.green,
                custom_id="start_tournament"
            ))
            await interaction.followup.send("Ready to begin? Click below to start the tournament:", view=view)
            
        except Exception as e:
            self.logger.error(f"Error in setup process: {str(e)}")
            await interaction.followup.send(
                "An error occurred during setup. Please try again or contact support.",
                ephemeral=True
            )

    @commands.command(name="ccstart")
    @commands.admin_or_permissions(administrator=True)
    async def ccstart(self, ctx):
        """Start the tournament and open applications"""
        if ctx.channel.id != await self.config.guild(ctx.guild).champions_channel():
            await ctx.send("This command can only be used in the Champions Circle channel.")
            return

        view = discord.ui.View(timeout=None)
        view.add_item(JoinButton(self))
        view.add_item(CancelApplicationButton(self))
        
        embed = discord.Embed(title="Tournament Applications", description="Current applicants and their status.", color=0x00ff00)
        message = await ctx.send(embed=embed, view=view)
        await self.config.guild(ctx.guild).champions_message_id.set(message.id)
        await self.update_embed(ctx.guild)

    @commands.command(name="ccend")
    @commands.admin_or_permissions(administrator=True)
    async def ccend(self, ctx):
        """End the tournament and clean up"""
        if ctx.channel.id != await self.config.guild(ctx.guild).champions_channel():
            await ctx.send("This command can only be used in the Champions Circle channel.")
            return

        # Ask for confirmation
        confirm_msg = await ctx.response.send_message("Are you sure you want to end the tournament? This will clear all messages and reset the application lists. Reply with 'yes' to confirm.")

        def check(m):
            return m.author == ctx.user and m.channel == ctx.channel and m.content.lower() == 'yes'

        try:
            await self.bot.wait_for('message', check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.response.send_message("Tournament end cancelled.")
            return

        # Clear messages
        channel = ctx.channel
        await ctx.response.send_message("Ending tournament and clearing channel...")

        try:
            await channel.purge(limit=None)
        except discord.Forbidden:
            await ctx.response.send_message("I don't have permission to delete messages in this channel.")
            return
        except discord.HTTPException:
            await ctx.response.send_message("An error occurred while trying to delete messages.")
            return

        # Reset cog state
        await self.config.guild(ctx.guild).active_applications.set([])
        await self.config.guild(ctx.guild).cancelled_applications.set([])
        await self.config.guild(ctx.guild).approved_applications.set([])
        await self.config.guild(ctx.guild).denied_applications.set([])
        await self.config.guild(ctx.guild).champions_message_id.set(None)

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
        await ctx.send(f"Current questions:\n{question_list}")

    async def update_embed(self, guild):
        tourney_title = await self.config.guild(guild).tourney_title()
        embed = discord.Embed(title=tourney_title, color=0x00ff00)
        
        # Add tournament details
        tourney_description = await self.config.guild(guild).tourney_description()
        tourney_time = await self.config.guild(guild).tourney_time()
        
        embed.add_field(name="Description", value=tourney_description, inline=False)
        
        if tourney_time:
            embed.add_field(name="Time", value=f"<t:{tourney_time}:F>", inline=False)
        else:
            embed.add_field(name="Time", value="Not set", inline=False)
        
        async def format_user_entry(application):
            user_id = application['user_id']
            user = guild.get_member(user_id)
            if not user:
                return f"<@{user_id}> (User left server)"
            
            rank = "Unranked"
            tracker_link = ""
            if 'answers' in application:
                questions = await self.config.guild(guild).custom_questions()
                rank_question = next((q for q in questions if q.lower().startswith("rank")), None)
                tracker_question = next((q for q in questions if "tracker" in q.lower()), None)
                
                if rank_question and rank_question in application['answers']:
                    rank = application['answers'][rank_question]
                if tracker_question and tracker_question in application['answers']:
                    tracker_link = application['answers'][tracker_question]
            
            if tracker_link:
                return f"<@{user_id}> - [{rank}]({tracker_link})"
            else:
                return f"<@{user_id}> - {rank}"

        active_applications = await self.config.guild(guild).active_applications()
        active_list = "\n".join([await format_user_entry(app) for app in active_applications]) or "No active applications"
        
        approved_applications = await self.config.guild(guild).approved_applications()
        approved_list = "\n".join([await format_user_entry(app) for app in approved_applications]) or "No approved applications"
        
        denied_applications = await self.config.guild(guild).denied_applications()
        denied_list = "\n".join([await format_user_entry(app) for app in denied_applications]) or "No denied applications"
        
        cancelled_applications = await self.config.guild(guild).cancelled_applications()
        cancelled_list = "\n".join([await format_user_entry(app) for app in cancelled_applications]) or "No cancelled applications"
        
        embed.add_field(name="🟦 Active Applications", value=active_list, inline=False)
        embed.add_field(name="🟩 Approved Applications", value=approved_list, inline=False)
        embed.add_field(name="🟥 Denied Applications", value=denied_list, inline=False)
        embed.add_field(name="🟨 Cancelled Applications", value=cancelled_list, inline=False)

        channel = self.bot.get_channel(await self.config.guild(guild).champions_channel())
        if not channel:
            self.logger.error(f"Error: Channel with ID {await self.config.guild(guild).champions_channel()} not found.")
            return

        try:
            if await self.config.guild(guild).champions_message_id():
                message = await channel.fetch_message(await self.config.guild(guild).champions_message_id())
                await message.edit(embed=embed)
            else:
                message = await channel.send(embed=embed)
                await self.config.guild(guild).champions_message_id.set(message.id)
        except discord.HTTPException as e:
            self.logger.error(f"Error updating embed: {str(e)}")

    async def send_answers_to_admin(self, user, answers):
        admin_user = self.bot.get_user(self.admin_user_id)
        if not admin_user:
            self.logger.error(f"Error: Admin user with ID {self.admin_user_id} not found.")
            return

        embed = discord.Embed(title=f"New Champion Application: {user.name}", color=0x00ff00)
        for question, answer in answers.items():
            embed.add_field(name=question, value=answer, inline=False)

        view = AdminResponseView(self, user.id, user.guild.id)
        await admin_user.send(embed=embed, view=view)

    @commands.command()
    @guild_only()
    async def cancel_application(self, ctx):
        """Cancel your Champions Circle application."""
        if ctx.author.id in await self.config.guild(ctx.guild).active_applications():
            active_applications = await self.config.guild(ctx.guild).active_applications()
            active_applications.remove(ctx.author.id)
            await self.config.guild(ctx.guild).active_applications.set(active_applications)
            cancelled_applications = await self.config.guild(ctx.guild).cancelled_applications()
            cancelled_applications.append(ctx.author.id)
            await self.config.guild(ctx.guild).cancelled_applications.set(cancelled_applications)
            await self.update_embed(ctx.guild)
            await ctx.send("Your Champions Circle application has been cancelled.", ephemeral=True)
        else:
            await ctx.send("You don't have an active Champions Circle application.", ephemeral=True)

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
    async def setchampionsrole(self, ctx, role: discord.Role):
        """Set the Champions Circle role."""
        await self.config.guild(ctx.guild).champions_role_id.set(role.id)
        await ctx.send(f"Champions Circle role set to {role.name}")

    async def close_expired_applications(self):
        """Close applications that have expired."""
        while self == self.bot.get_cog("ChampionsCircle"):
            try:
                all_guilds = await self.config.all_guilds()
                for guild_id, guild_data in all_guilds.items():
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        continue
                    
                    active_apps = guild_data["active_applications"]
                    for app in active_apps[:]:  # Create a copy of the list to iterate over
                        if "timestamp" in app and datetime.now() - datetime.fromtimestamp(app["timestamp"]) > timedelta(days=guild_data["application_duration"]):
                            active_apps.remove(app)
                            guild_data["cancelled_applications"].append(app)
                            user = guild.get_member(app["user_id"])
                            if user:
                                try:
                                    await user.send("Your Champions Circle application has expired.")
                                except discord.HTTPException:
                                    self.logger.error(f"Failed to send expiration message to user {user.id}")
                    
                    await self.config.guild(guild).active_applications.set(active_apps)
                    await self.config.guild(guild).cancelled_applications.set(guild_data["cancelled_applications"])
                    await self.update_embed(guild)
            except Exception as e:
                self.logger.error(f"Error in close_expired_applications: {str(e)}")
            
            await asyncio.sleep(3600)  # Check every hour

    @commands.command()
    async def cchelp(self, ctx):
        """Display help for Champions Circle commands."""
        embed = discord.Embed(title="Champions Circle Help", color=0x00ff00)
        
        # General commands
        embed.add_field(name="General Commands", value="\u200b", inline=False)
        embed.add_field(name="cchelp", value="Display this help message", inline=False)
        embed.add_field(name="cclist", value="List current champions", inline=False)

        # Setup and Configuration
        embed.add_field(name="Setup Commands", value="\u200b", inline=False)
        embed.add_field(name="ccsetup", value="Initialize and setup the tournament system", inline=False)
        embed.add_field(name="ccsetup title", value="Set tournament title", inline=False)
        embed.add_field(name="ccsetup description", value="Set tournament description", inline=False)
        embed.add_field(name="ccsetup time", value="Set tournament time (YYYY-MM-DD HH:MM:SS)", inline=False)
        embed.add_field(name="ccsetup role", value="Set champions role", inline=False)
        embed.add_field(name="ccsetup duration", value="Set application duration in days", inline=False)

        # Tournament Management
        embed.add_field(name="Tournament Commands", value="\u200b", inline=False)
        embed.add_field(name="ccstart", value="Start tournament and open applications", inline=False)
        embed.add_field(name="ccend", value="End tournament and cleanup", inline=False)
        embed.add_field(name="ccclear", value="Clear all messages in tournament channel", inline=False)

        # Question Management
        embed.add_field(name="Question Commands", value="\u200b", inline=False)
        embed.add_field(name="ccquestions add", value="Add application question", inline=False)
        embed.add_field(name="ccquestions remove", value="Remove question by index", inline=False)
        embed.add_field(name="ccquestions list", value="List all questions", inline=False)

        # Utility Commands
        embed.add_field(name="Utility Commands", value="\u200b", inline=False)
        embed.add_field(name="ccsettings", value="Display current settings", inline=False)
        embed.add_field(name="cctest", value="Test role assignment", inline=False)

        await ctx.send(embed=embed)

    @commands.command(name="cclist")
    async def cclist(self, ctx):
        """List current champions"""
        # ... existing list_champions logic ...

    @commands.command(name="ccsettings")
    @commands.admin_or_permissions(administrator=True)
    async def ccsettings(self, ctx):
        """Display current tournament settings"""
        # ... existing championssettings logic ...

class ApplicationModal(discord.ui.Modal, title="Tournament Application"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        
        self.epic_id = discord.ui.TextInput(
            label="Epic Account ID",
            placeholder="Your Epic Games Account ID",
            required=True
        )
        self.add_item(self.epic_id)
        
        self.rank = discord.ui.TextInput(
            label="Current Rank",
            placeholder="e.g., Diamond 2 Division 4",
            required=True
        )
        self.add_item(self.rank)
        
        self.platform = discord.ui.TextInput(
            label="Platform",
            placeholder="PC, PlayStation, Xbox, or Switch",
            required=True
        )
        self.add_item(self.platform)
        
        self.region = discord.ui.TextInput(
            label="Region",
            placeholder="NA East, NA West, EU, or Other",
            required=True
        )
        self.add_item(self.region)

    async def on_submit(self, interaction: discord.Interaction):
        # Create application thread in forum
        forum = interaction.guild.get_channel(await self.cog.config.guild(interaction.guild).champions_forum())
        
        thread = await forum.create_thread(
            name=f"Application - {interaction.user.name}",
            content=f"New application from {interaction.user.mention}",
            applied_tags=[tag for tag in forum.available_tags if tag.name == "Pending"]
        )
        
        # Create embed with application details
        embed = discord.Embed(
            title="Tournament Application",
            color=discord.Color.blue()
        )
        embed.add_field(name="Epic ID", value=self.epic_id.value)
        embed.add_field(name="Rank", value=self.rank.value)
        embed.add_field(name="Platform", value=self.platform.value)
        embed.add_field(name="Region", value=self.region.value)
        embed.set_author(name=interaction.user.name, icon_url=interaction.user.avatar.url)
        
        # Add review buttons
        view = ApplicationReviewView(self.cog, interaction.user.id)
        
        await thread.send(embed=embed, view=view)
        await interaction.response.send_message("Your application has been submitted!", ephemeral=True)

class ApplicationReviewView(discord.ui.View):
    def __init__(self, cog, applicant_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.applicant_id = applicant_id
        
    @discord.ui.select(
        placeholder="Select review action",
        options=[
            discord.SelectOption(
                label="Approve",
                description="Accept the application",
                emoji="✅",
                value="approve"
            ),
            discord.SelectOption(
                label="Deny",
                description="Reject the application",
                emoji="❌",
                value="deny"
            ),
            discord.SelectOption(
                label="Request More Info",
                description="Ask for additional information",
                emoji="❓",
                value="more_info"
            )
        ]
    )
    async def review_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if select.values[0] == "approve":
            await self.approve_application(interaction)
        elif select.values[0] == "deny":
            # Show denial reason modal
            await interaction.response.send_modal(DenialReasonModal(self.cog, self.applicant_id))
        elif select.values[0] == "more_info":
            await self.request_more_info(interaction)

    async def approve_application(self, interaction: discord.Interaction):
        thread = interaction.channel
        # Update thread tags
        await thread.edit(applied_tags=[tag for tag in thread.parent.available_tags if tag.name == "Approved"])
        
        # Assign role
        member = interaction.guild.get_member(self.applicant_id)
        role = interaction.guild.get_role(await self.cog.config.guild(interaction.guild).champions_role_id())
        await member.add_roles(role)
        
        # Send notifications
        await thread.send(f"Application approved by {interaction.user.mention}")
        try:
            await member.send("Congratulations! Your tournament application has been approved!")
        except discord.HTTPException:
            pass

class JoinButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(style=discord.ButtonStyle.green, label="Apply for Champions Circle", custom_id="join_champions")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        # Create a dummy message object for cooldown purposes
        class DummyMessage:
            def __init__(self, author):
                self.author = author

        dummy_message = DummyMessage(interaction.user)
        bucket = self.cog.application_cooldowns.get_bucket(dummy_message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            minutes, seconds = divmod(int(retry_after), 60)
            await interaction.response.send_message(f"You can apply again in {minutes} minutes and {seconds} seconds.", ephemeral=True)
            return

        if interaction.user.id in [app['user_id'] for app in await self.cog.config.guild(interaction.guild).active_applications()]:
            await interaction.response.send_message("You already have an active application for the Champions Circle.", ephemeral=True)
            return

        view = ApplicationModal(self.cog)
        await interaction.response.send_message("Great! Let's start your application process. Click the button below to begin the questionnaire:", view=view, ephemeral=True)

class CancelApplicationButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(style=discord.ButtonStyle.red, label="Cancel Application", custom_id="cancel_application")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        guild = interaction.guild
        application = None

        for list_name in ['active_applications', 'approved_applications', 'denied_applications']:
            current_list = await self.cog.config.guild(guild).get_raw(list_name)
            application = next((app for app in current_list if app['user_id'] == user_id), None)
            if application:
                current_list.remove(application)
                await self.cog.config.guild(guild).set_raw(list_name, value=current_list)
                break

        if application:
            cancelled_applications = await self.cog.config.guild(guild).cancelled_applications()
            cancelled_applications.append(application)
            await self.cog.config.guild(guild).cancelled_applications.set(cancelled_applications)

            if list_name == 'approved_applications':
                role = guild.get_role(await self.cog.config.guild(guild).champions_role_id())
                if role and role in interaction.user.roles:
                    await interaction.user.remove_roles(role)
                await interaction.response.send_message("Your approved Champions Circle application has been cancelled. The Champions role has been removed if it was assigned.", ephemeral=True)
            else:
                await interaction.response.send_message("Your Champions Circle application has been cancelled.", ephemeral=True)
        else:
            await interaction.response.send_message("You don't have an active Champions Circle application to cancel.", ephemeral=True)
        
        await self.cog.update_embed(guild)

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
    def __init__(self):
        super().__init__()
        
        self.title = discord.ui.TextInput(
            label="Tournament Title",
            placeholder="Enter tournament title",
            required=True
        )
        self.add_item(self.title)
        
        self.description = discord.ui.TextInput(
            label="Tournament Description",
            placeholder="Enter tournament description",
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.add_item(self.description)
        
        self.time = discord.ui.TextInput(
            label="Tournament Time",
            placeholder="YYYY-MM-DD HH:MM:SS",
            required=True
        )
        self.add_item(self.time)
        
        self.duration = discord.ui.TextInput(
            label="Application Duration (days)",
            placeholder="Enter number of days",
            required=True
        )
        self.add_item(self.duration)

class DenialReasonModal(discord.ui.Modal, title="Application Denial"):
    def __init__(self, cog, applicant_id):
        super().__init__()
        self.cog = cog
        self.applicant_id = applicant_id
        
        self.reason = discord.ui.TextInput(
            label="Denial Reason",
            placeholder="Enter the reason for denying this application",
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        thread = interaction.channel
        # Update thread tags
        await thread.edit(applied_tags=[tag for tag in thread.parent.available_tags if tag.name == "Denied"])
        
        # Send notifications
        await thread.send(f"Application denied by {interaction.user.mention}\nReason: {self.reason.value}")
        
        member = interaction.guild.get_member(self.applicant_id)
        if member:
            try:
                await member.send(f"Your tournament application has been denied.\nReason: {self.reason.value}")
            except discord.HTTPException:
                pass

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
    bot.loop.create_task(cog.close_expired_applications())