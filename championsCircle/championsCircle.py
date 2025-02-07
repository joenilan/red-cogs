import discord
from redbot.core import commands, Config
from discord.ext.commands import guild_only
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from discord import app_commands
from typing import Optional

class ChampionsCircle(commands.Cog):
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

    @app_commands.command(name="starttourney")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def starttourney(self, interaction: discord.Interaction):
        """Start a new tournament and set up the join button for Champions Circle applications."""
        if interaction.channel_id != await self.config.guild(interaction.guild).champions_channel():
            await interaction.response.send_message("This command can only be used in the Champions Circle channel.", ephemeral=True)
            return

        view = discord.ui.View(timeout=None)
        view.add_item(JoinButton(self))
        view.add_item(CancelApplicationButton(self))
        
        embed = discord.Embed(title="Champions Circle Applications", description="Current applicants and their status.", color=0x00ff00)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        await self.config.guild(interaction.guild).champions_message_id.set(message.id)
        await self.update_embed(interaction.guild)

    @commands.command()
    @commands.has_permissions(administrator=True)
    @guild_only()
    async def test_role_assign(self, ctx, member: discord.Member):
        role = ctx.guild.get_role(await self.config.guild(ctx.guild).champions_role_id())
        if role is None:
            await ctx.send("Error: Champions role not found.")
            return
        try:
            await member.add_roles(role)
            await ctx.send(f"Successfully assigned {role.name} to {member.name}")
        except discord.Forbidden:
            await ctx.send("Error: I don't have permission to assign roles.")
        except discord.HTTPException as e:
            await ctx.send(f"An error occurred: {str(e)}")

    @commands.command()
    @guild_only()
    async def list_champions(self, ctx):
        approved_applications = await self.config.guild(ctx.guild).approved_applications()
        if not approved_applications:
            await ctx.send("There are no champions yet!")
            return

        embed = discord.Embed(title="Champions Circle", description="Our esteemed champions:", color=0x00ff00)
        for application in approved_applications:
            champion_id = application['user_id']
            champion = ctx.guild.get_member(champion_id)
            if champion:
                rank = application['answers'].get('Rank:', 'Unranked')
                tracker_link = application['answers'].get('RL Tracker Link:', 'Not provided')
                value = f"Rank: [{rank}]({tracker_link})" if tracker_link != 'Not provided' else f"Rank: {rank}"
                embed.add_field(name=champion.name, value=value, inline=False)

        await ctx.send(embed=embed)

    @commands.command()
    @commands.has_permissions(administrator=True)
    @guild_only()
    async def clearall(self, ctx):
        """Clear all messages in the Champions Circle channel."""
        if ctx.channel.id != await self.config.guild(ctx.guild).champions_channel():
            await ctx.send("This command can only be used in the Champions Circle channel.")
            return

        # Ask for confirmation
        confirm_msg = await ctx.send("Are you sure you want to clear all messages in this channel? This action cannot be undone. Reply with 'yes' to confirm.")

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == 'yes'

        try:
            await self.bot.wait_for('message', check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.send("Clearall command cancelled.")
            return

        # Clear messages
        channel = ctx.channel
        await ctx.send("Clearing all messages...")

        try:
            async for message in channel.history(limit=None):
                await message.delete()
        except discord.Forbidden:
            await ctx.send("I don't have permission to delete messages in this channel.")
        except discord.HTTPException:
            await ctx.send("An error occurred while trying to delete messages.")
        else:
            await ctx.send("All messages have been cleared from the Champions Circle channel.", delete_after=10)

    @commands.command()
    @commands.has_permissions(administrator=True)
    @guild_only()
    async def endtourney(self, ctx):
        """End the current tournament, clear the channel, and reset the cog's state."""
        if ctx.channel.id != await self.config.guild(ctx.guild).champions_channel():
            await ctx.send("This command can only be used in the Champions Circle channel.")
            return

        # Ask for confirmation
        confirm_msg = await ctx.send("Are you sure you want to end the tournament? This will clear all messages and reset the application lists. Reply with 'yes' to confirm.")

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == 'yes'

        try:
            await self.bot.wait_for('message', check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.send("Tournament end cancelled.")
            return

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
        temp_msg = await channel.send("Tournament ended. Channel cleared, cog state reset, and application cooldowns reset. You can now use the starttourney command for a new tournament.", delete_after=10)

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
        embed.add_field(name="list_champions", value="List current champions", inline=False)

        # Admin commands
        embed.add_field(name="Admin Commands", value="\u200b", inline=False)
        embed.add_field(name="starttourney", value="Start a new tournament and set up the join button for Champions Circle applications", inline=False)
        embed.add_field(name="setchampionschannel", value="Set the Champions Circle channel", inline=False)
        embed.add_field(name="setapplicationduration", value="Set the duration for which applications remain open", inline=False)
        embed.add_field(name="setchampionsrole", value="Set the Champions Circle role", inline=False)
        embed.add_field(name="endtourney", value="End the current tournament and reset the cog", inline=False)
        embed.add_field(name="clearall", value="Clear all messages in the Champions Circle channel", inline=False)
        embed.add_field(name="test_role_assign", value="Test role assignment", inline=False)
        embed.add_field(name="championssettings", value="Display current settings for the Champions Circle cog", inline=False)

        # Tournament management commands
        embed.add_field(name="Tournament Management", value="\u200b", inline=False)
        embed.add_field(name="tourney settitle", value="Set the tournament title", inline=False)
        embed.add_field(name="tourney setdescription", value="Set the tournament description", inline=False)
        embed.add_field(name="tourney settime", value="Set the tournament time (format: YYYY-MM-DD HH:MM)", inline=False)

        # Question management commands
        embed.add_field(name="Question Management", value="\u200b", inline=False)
        embed.add_field(name="questions add", value="Add a custom question to the Champions Circle application", inline=False)
        embed.add_field(name="questions remove", value="Remove a custom question from the Champions Circle application", inline=False)
        embed.add_field(name="questions list", value="List all custom questions for the Champions Circle application", inline=False)

        await ctx.send(embed=embed)

    @commands.command()
    @commands.has_permissions(administrator=True)
    @guild_only()
    async def championssettings(self, ctx):
        """Display current settings for the Champions Circle cog."""
        guild = ctx.guild
        settings = await self.config.guild(guild).all()

        embed = discord.Embed(title="Champions Circle Settings", color=0x00ff00)
        
        champions_channel = self.bot.get_channel(settings['champions_channel'])
        champions_role = guild.get_role(settings['champions_role_id'])
        
        embed.add_field(name="Champions Channel", value=champions_channel.mention if champions_channel else "Not set", inline=False)
        embed.add_field(name="Champions Role", value=champions_role.mention if champions_role else "Not set", inline=False)
        embed.add_field(name="Application Duration", value=f"{settings['application_duration']} days", inline=False)
        embed.add_field(name="Tournament Title", value=settings['tourney_title'], inline=False)
        embed.add_field(name="Tournament Description", value=settings['tourney_description'], inline=False)
        
        if settings['tourney_time']:
            embed.add_field(name="Tournament Time", value=f"<t:{settings['tourney_time']}:F>", inline=False)
        else:
            embed.add_field(name="Tournament Time", value="Not set", inline=False)
        
        embed.add_field(name="Active Applications", value=len(settings['active_applications']), inline=True)
        embed.add_field(name="Approved Applications", value=len(settings['approved_applications']), inline=True)
        embed.add_field(name="Denied Applications", value=len(settings['denied_applications']), inline=True)
        embed.add_field(name="Cancelled Applications", value=len(settings['cancelled_applications']), inline=True)
        
        cooldown = self.application_cooldowns._cooldown
        embed.add_field(name="Application Cooldown", value=f"{cooldown.per} seconds", inline=False)

        await ctx.send(embed=embed)

    @commands.group()
    @commands.admin_or_permissions(administrator=True)
    @guild_only()
    async def questions(self, ctx):
        """Manage custom questions for the Champions Circle application."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @questions.command(name="add")
    @guild_only()
    async def add_question(self, ctx, *, question: str):
        """Add a custom question to the Champions Circle application."""
        async with self.config.guild(ctx.guild).custom_questions() as questions:
            questions.append(question)
        await ctx.send(f"Question added: {question}")

    @questions.command(name="remove")
    @guild_only()
    async def remove_question(self, ctx, index: int):
        """Remove a custom question from the Champions Circle application."""
        async with self.config.guild(ctx.guild).custom_questions() as questions:
            if 1 <= index <= len(questions):
                removed_question = questions.pop(index - 1)
                await ctx.send(f"Question removed: {removed_question}")
            else:
                await ctx.send("Invalid question index.")

    @questions.command(name="list")
    @guild_only()
    async def list_questions(self, ctx):
        """List all custom questions for the Champions Circle application."""
        try:
            questions = await self.config.guild(ctx.guild).custom_questions()
            if questions:
                question_list = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
                await ctx.send(f"Current custom questions:\n{question_list}")
            else:
                await ctx.send("No custom questions set.")
        except AttributeError:
            await ctx.send("This command can only be used in a server.")

    @app_commands.command(name="tourney")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        action="The action to perform",
        value="The value to set"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Set Title", value="title"),
        app_commands.Choice(name="Set Description", value="description"),
        app_commands.Choice(name="Set Time", value="time")
    ])
    async def tourney_command(self, interaction: discord.Interaction, action: str, value: str):
        """Manage tournament settings"""
        if action == "title":
            await self.config.guild(interaction.guild).tourney_title.set(value)
            await interaction.response.send_message(f"Tournament title set to: {value}", ephemeral=True)
        elif action == "description":
            await self.config.guild(interaction.guild).tourney_description.set(value)
            await interaction.response.send_message(f"Tournament description set to: {value}", ephemeral=True)
        elif action == "time":
            try:
                tourney_time = datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc)
                timestamp = int(tourney_time.timestamp())
                await self.config.guild(interaction.guild).tourney_time.set(timestamp)
                await interaction.response.send_message(f"Tournament time set to: <t:{timestamp}:F>", ephemeral=True)
            except ValueError:
                await interaction.response.send_message("Invalid time format. Please use ISO format (YYYY-MM-DD HH:MM:SS)", ephemeral=True)
                return
        
        await self.update_embed(interaction.guild)

    @app_commands.command(name="setup")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setup_tournament(self, interaction: discord.Interaction):
        """Set up the tournament infrastructure"""
        
        # Create forum channel if it doesn't exist
        forum_channel = await interaction.guild.create_forum(
            name="tournament-applications",
            topic="Champions Circle Tournament Applications",
            reason="Tournament application system setup"
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
        announcement_channel = await interaction.guild.create_text_channel(
            name="tournament-announcements",
            topic="Champions Circle Tournament Announcements"
        )
        
        # Save channels to config
        await self.config.guild(interaction.guild).champions_forum.set(forum_channel.id)
        await self.config.guild(interaction.guild).champions_channel.set(announcement_channel.id)
        
        await interaction.response.send_message("Tournament system has been set up!", ephemeral=True)

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
        
        self.platform = discord.ui.Select(
            placeholder="Select your platform",
            options=[
                discord.SelectOption(label="PC", value="pc"),
                discord.SelectOption(label="PlayStation", value="ps"),
                discord.SelectOption(label="Xbox", value="xbox"),
                discord.SelectOption(label="Switch", value="switch")
            ]
        )
        self.add_item(self.platform)
        
        self.region = discord.ui.Select(
            placeholder="Select your region",
            options=[
                discord.SelectOption(label="NA East", value="nae"),
                discord.SelectOption(label="NA West", value="naw"),
                discord.SelectOption(label="EU", value="eu"),
                discord.SelectOption(label="Other", value="other")
            ]
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
        embed.add_field(name="Platform", value=self.platform.values[0])
        embed.add_field(name="Region", value=self.region.values[0])
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

async def setup(bot):
    cog = ChampionsCircle(bot)
    await bot.add_cog(cog)
    bot.loop.create_task(cog.close_expired_applications())