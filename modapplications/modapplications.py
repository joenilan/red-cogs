import discord
from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import box, pagify
from datetime import datetime
from typing import Optional, Dict, List
import asyncio

class ModApplications(commands.Cog):
    """A cog for handling Twitch/Discord moderator applications."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=8675309, force_registration=True
        )
        
        default_guild = {
            "applications": {},
            "questions": [],  # Remove this as we're not using it anymore
            "base_questions": [  # Add this to match what we're using
                {
                    "id": "age",
                    "question": "Are you over the age of 16? (Yes/No)",
                    "required": True,
                    "valid_responses": ["yes", "no"]
                },
                {
                    "id": "timezone",
                    "question": "What timezone are you in? (e.g. EST, PST, GMT)",
                    "required": True
                },
                {
                    "id": "experience",
                    "question": "Do you have any previous moderation experience? If yes, please describe.",
                    "required": False
                },
                {
                    "id": "motivation",
                    "question": "Why do you want to be a moderator?",
                    "required": True
                },
                {
                    "id": "activity",
                    "question": "How active are you in our community? (Rate 1-5)",
                    "required": True,
                    "valid_responses": ["1", "2", "3", "4", "5"]
                },
                {
                    "id": "tos",
                    "question": "Do you agree to follow our Terms of Service? (Yes/No)",
                    "required": True,
                    "valid_responses": ["yes", "no"]
                }
            ],
            "platform_questions": {  # Add this to match what we're using
                "discord": [
                    {
                        "id": "discord_username",
                        "question": "What is your Discord Username?",
                        "required": True
                    },
                    {
                        "id": "discord_experience",
                        "question": "How long have you been using Discord?",
                        "required": True
                    }
                ],
                "twitch": [
                    {
                        "id": "twitch_username",
                        "question": "What is your Twitch username?",
                        "required": True
                    },
                    {
                        "id": "twitch_following",
                        "question": "How long have you been following our Twitch channel?",
                        "required": True
                    }
                ],
                "youtube": [
                    {
                        "id": "youtube_username",
                        "question": "What is your YouTube username?",
                        "required": True
                    },
                    {
                        "id": "youtube_experience",
                        "question": "How familiar are you with YouTube's community guidelines?",
                        "required": True
                    }
                ],
                "tiktok": [
                    {
                        "id": "tiktok_username",
                        "question": "What is your TikTok username?",
                        "required": True
                    },
                    {
                        "id": "tiktok_experience",
                        "question": "How familiar are you with TikTok's community guidelines?",
                        "required": True
                    }
                ],
                "kick": [
                    {
                        "id": "kick_username",
                        "question": "What is your Kick username?",
                        "required": True
                    },
                    {
                        "id": "kick_experience",
                        "question": "How long have you been using Kick?",
                        "required": True
                    }
                ]
            },
            "app_channel": None,
            "notify_role": None,
            "applications_open": False,
            "mod_role": None,  # New: Role for moderator reviewers
            "category_id": None,  # New: Category for application channels
            "info_channel": None  # New: Info channel for application button
        }
        
        self.config.register_guild(**default_guild)

        # Add persistent views when the cog loads
        self.persistent_views_added = False

    @commands.group()
    @commands.admin_or_permissions(administrator=True)
    async def modapp(self, ctx):
        """Moderator application management commands."""
        pass

    @modapp.command(name="setup")
    async def modapp_setup(self, ctx, channel: discord.TextChannel, notify_role: Optional[discord.Role] = None):
        """Set up the application system."""
        await self.config.guild(ctx.guild).app_channel.set(channel.id)
        if notify_role:
            await self.config.guild(ctx.guild).notify_role.set(notify_role.id)
        await ctx.send(f"Applications will be sent to {channel.mention}" + 
                      (f" and will notify {notify_role.mention}" if notify_role else ""))

    @modapp.command(name="toggle")
    async def modapp_toggle(self, ctx):
        """Toggle applications open/closed."""
        current = await self.config.guild(ctx.guild).applications_open()
        await self.config.guild(ctx.guild).applications_open.set(not current)
        status = "opened" if not current else "closed"
        await ctx.send(f"Applications are now {status}.")

    @modapp.command(name="open")
    @commands.is_owner()
    async def modapp_open(self, ctx):
        """Open applications and create necessary channels/roles."""
        try:
            # Create the category
            category = await ctx.guild.create_category(
                "Mod Applications",
                reason="Automated setup for moderator applications"
            )
            
            # Use existing reviewer roles
            reviewer_roles = [
                ctx.guild.get_role(734775241707880518),  # The One
                ctx.guild.get_role(735167487854903377)   # Agent
            ]
            
            # Create the applications forum
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                ctx.guild.me: discord.PermissionOverwrite(read_messages=True)
            }
            
            # Add permissions for reviewer roles
            for role in reviewer_roles:
                if role:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True)
            
            # Create forum channel with proper type
            apps_forum = await category.create_forum(
                "mod-applications",
                overwrites=overwrites,
                topic="Moderator Applications",
                reason="Forum for moderator applications"
            )
            
            # Set up forum tags
            tags = [
                discord.ForumTag(name="Pending", emoji="📝"),
                discord.ForumTag(name="Approved", emoji="✅"),
                discord.ForumTag(name="Denied", emoji="❌")
            ]
            
            await apps_forum.edit(available_tags=tags)
            
            # Create the public info channel
            info_channel = await ctx.guild.create_text_channel(
                "apply-here",
                category=category,
                overwrites={
                    ctx.guild.default_role: discord.PermissionOverwrite(send_messages=False)
                }
            )
            
            # Send the application button message
            embed = discord.Embed(
                title="Moderator Applications",
                description="Click the button below to start your moderator application!",
                color=discord.Color.blue()
            )
            
            view = ApplicationStartView(self)
            await info_channel.send(embed=embed, view=view)
            
            # Save the IDs to config
            await self.config.guild(ctx.guild).app_channel.set(apps_forum.id)
            await self.config.guild(ctx.guild).info_channel.set(info_channel.id)
            await self.config.guild(ctx.guild).category_id.set(category.id)
            await self.config.guild(ctx.guild).applications_open.set(True)
            
            # Create initial status embed
            status_embed = discord.Embed(
                title="Application Status Overview",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            status_embed.add_field(
                name="📝 Pending Applications (0)",
                value="No pending applications",
                inline=False
            )
            
            status_embed.add_field(
                name="✅ Approved Applications (0)",
                value="No approved applications",
                inline=False
            )
            
            status_embed.add_field(
                name="❌ Denied Applications (0)",
                value="No denied applications",
                inline=False
            )
            
            # Create a thread for the status overview
            thread_with_message = await apps_forum.create_thread(
                name="📊 Application Status Overview",
                content="Status overview for all applications:",
                embed=status_embed,
                applied_tags=[discord.utils.get(apps_forum.available_tags, name="Pending")]
            )
            
            # The thread and starter message are separate now
            status_thread = thread_with_message.thread
            starter_message = thread_with_message.message
            
            # Pin the status message
            await starter_message.pin()
            
            await ctx.send(
                "📝 Moderator applications are now open!\n"
                f"Category: {category.name}\n"
                f"Applications Forum: {apps_forum.mention}\n"
                f"Info Channel: {info_channel.mention}\n"
                "Reviewer Roles: The One and Agent\n"
            )
            
        except discord.Forbidden:
            await ctx.send("I don't have the required permissions to create channels and roles.")
        except Exception as e:
            await ctx.send(f"An error occurred during setup: {str(e)}")

    @modapp.command(name="close")
    @commands.is_owner()
    async def modapp_close(self, ctx):
        """Close applications and remove channels/roles."""
        try:
            # Get saved IDs
            category_id = await self.config.guild(ctx.guild).category_id()
            mod_role_id = await self.config.guild(ctx.guild).mod_role()
            
            if category_id:
                category = ctx.guild.get_channel(category_id)
                if category:
                    # Delete all channels in the category first
                    for channel in category.channels:
                        try:
                            await channel.delete(reason="Closing mod applications")
                        except discord.HTTPException:
                            continue
                    
                    # Then delete the category
                    await category.delete(reason="Closing mod applications")
            
            # Clear config
            await self.config.guild(ctx.guild).app_channel.set(None)
            await self.config.guild(ctx.guild).mod_role.set(None)
            await self.config.guild(ctx.guild).category_id.set(None)
            await self.config.guild(ctx.guild).info_channel.set(None)
            await self.config.guild(ctx.guild).applications_open.set(False)
            
            await ctx.send("❌ Moderator applications are now closed and all related channels/roles have been removed.")
            
        except discord.Forbidden:
            await ctx.send("I don't have the required permissions to delete channels and roles.")
        except Exception as e:
            await ctx.send(f"An error occurred while closing applications: {str(e)}")

    @commands.command()
    @commands.guild_only()
    async def apply(self, ctx):
        """Start a moderator application."""
        # Delete the command message to keep channels clean
        await ctx.message.delete()
        
        # Check if the application system is set up
        if not await self.config.guild(ctx.guild).app_channel():
            await ctx.send("The application system is not set up yet.", delete_after=10)
            return
        
        # Check if applications are open
        if not await self.config.guild(ctx.guild).applications_open():
            await ctx.author.send("Sorry, moderator applications are currently closed.")
            return

        # Start application process in DMs
        try:
            await ctx.author.send("Welcome to the moderator application process! Please choose what you'd like to apply for:")
            view = ApplicationTypeView(self, ctx.author, ctx.guild)
            await ctx.author.send("What would you like to apply for?", view=view)
        except discord.Forbidden:
            await ctx.send("I couldn't DM you! Please enable DMs from server members and try again.", delete_after=10)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if not interaction.type == discord.InteractionType.component:
            return

        if interaction.data["custom_id"] == "start_application":
            await self.start_application_process(interaction)
        elif interaction.data["custom_id"] == "submit_application":
            view = interaction.message.view
            if isinstance(view, ApplicationTypeView):
                await view.submit_platforms(interaction, None)

    async def start_application_process(self, interaction: discord.Interaction):
        """Handle the initial application button press."""
        try:
            # Check if applications are open
            if not await self.config.guild(interaction.guild).applications_open():
                await interaction.response.send_message("Sorry, moderator applications are currently closed.", ephemeral=True)
                return

            # Try to DM the user
            try:
                # Acknowledge the interaction first
                await interaction.response.send_message("I've sent you a DM to start the application process!", ephemeral=True)
                
                # Send only one DM with the view
                view = ApplicationTypeView(self, interaction.user, interaction.guild)
                await interaction.user.send(
                    "Welcome to the moderator application process!\nWhat would you like to apply for?",
                    view=view
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "I couldn't DM you! Please enable DMs from server members and try again.",
                    ephemeral=True
                )
                
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"An error occurred while starting the application: {str(e)}",
                    ephemeral=True
                )

    async def cog_load(self):
        """This is called when the cog is loaded."""
        if not self.persistent_views_added:
            self.bot.add_view(ApplicationStartView(self))
            self.bot.add_view(ApplicationTypeView(self, None, None))
            self.bot.add_view(ApplicationResponseView(self, None))
            self.persistent_views_added = True

    async def update_status_embed(self, guild):
        """Updates the status tracking embed in the reviewer channel."""
        try:
            channel_id = await self.config.guild(guild).app_channel()
            if not channel_id:
                return
                
            channel = guild.get_channel(channel_id)
            if not channel:
                return

            # Get all applications
            applications = await self.config.guild(guild).applications()
            
            # Track the latest status for each user
            user_latest_status = {}
            
            # First pass: determine the most recent status for each user
            for msg_id, app in applications.items():
                user_id = app['user_id']
                timestamp = app.get('timestamp', '0')
                
                if user_id not in user_latest_status or timestamp > user_latest_status[user_id]['timestamp']:
                    user_latest_status[user_id] = {
                        'status': app['status'],
                        'timestamp': timestamp,
                        'msg_id': msg_id,
                        'platforms': app.get('platforms', 'Unknown')
                    }
            
            # Now organize by status using only the latest status for each user
            pending = []
            approved = []
            denied = []
            
            for user_id, data in user_latest_status.items():
                user = guild.get_member(user_id)
                if not user:
                    continue
                    
                jump_url = f"https://discord.com/channels/{guild.id}/{channel_id}/{data['msg_id']}"
                entry = f"[{user.name}]({jump_url}) - {data['platforms']}"
                
                if data['status'] == 'pending':
                    pending.append((entry, data['timestamp']))
                elif data['status'] == 'approved':
                    approved.append((entry, data['timestamp']))
                elif data['status'] == 'denied':
                    denied.append((entry, data['timestamp']))
            
            # Sort each list by timestamp (most recent first)
            pending.sort(key=lambda x: x[1], reverse=True)
            approved.sort(key=lambda x: x[1], reverse=True)
            denied.sort(key=lambda x: x[1], reverse=True)
            
            # Create the status embed
            embed = discord.Embed(
                title="Application Status Overview",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            embed.add_field(
                name=f"📝 Pending Applications ({len(pending)})",
                value="\n".join(entry for entry, _ in pending) if pending else "No pending applications",
                inline=False
            )
            
            embed.add_field(
                name=f"✅ Approved Applications ({len(approved)})",
                value="\n".join(entry for entry, _ in approved) if approved else "No approved applications",
                inline=False
            )
            
            embed.add_field(
                name=f"❌ Denied Applications ({len(denied)})",
                value="\n".join(entry for entry, _ in denied) if denied else "No denied applications",
                inline=False
            )

            # Find and update existing status embed
            async for message in channel.history(limit=10):
                if message.author == guild.me and message.embeds:
                    if message.embeds[0].title == "Application Status Overview":
                        await message.edit(embed=embed)
                        return

            # If no existing embed found, send new one and pin it
            status_msg = await channel.send(embed=embed)
            await status_msg.pin()

        except Exception as e:
            print(f"Error in update_status_embed: {str(e)}")

    async def submit_application(self, guild, user, answers: Dict[str, str]):
        """Submit the completed application."""
        try:
            channel_id = await self.config.guild(guild).app_channel()
            if not channel_id:
                await user.send("Error: Application channel not configured. Please contact an administrator.")
                return

            forum = guild.get_channel(channel_id)
            if not forum:
                await user.send("Error: Could not find application channel. Please contact an administrator.")
                return

            # Create the embed
            embed = discord.Embed(
                title="New Moderator Application",
                description=f"Application from {user.mention} ({user.id})",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )

            # Add platforms field separately
            if 'platforms' in answers:
                embed.add_field(
                    name="Platforms",
                    value=answers['platforms'],
                    inline=False
                )

            # Add all other answers to the embed
            for question_id, answer in answers.items():
                if question_id != 'platforms':  # Skip platforms as we already added it
                    # Format the question ID to be more readable
                    field_name = question_id.replace("_", " ").title()
                    embed.add_field(name=field_name, value=answer, inline=False)

            # Create a new forum post
            thread = await forum.create_thread(
                name=f"Application - {user.name}",
                embed=embed,
                applied_tags=[discord.utils.get(forum.available_tags, name="Pending")],
                view=ApplicationResponseView(self, user.id)
            )

            # Store the application in the config
            async with self.config.guild(guild).applications() as apps:
                apps[str(thread.id)] = {
                    "user_id": user.id,
                    "platforms": answers.get('platforms', 'Unknown'),
                    "answers": answers,
                    "status": "pending",
                    "timestamp": datetime.now().isoformat()
                }

            await user.send("Your application has been submitted! You will be notified when it has been reviewed.")

        except Exception as e:
            print(f"Error in submit_application: {str(e)}")
            await user.send(f"An error occurred while submitting your application: {str(e)}")
            raise

class ApplicationTypeView(discord.ui.View):
    def __init__(self, cog, user, guild):
        super().__init__(timeout=None)
        self.cog = cog
        self.user = user
        self.guild = guild
        self.platforms = {
            "discord": "Discord",
            "twitch": "Twitch",
            "youtube": "YouTube",
            "tiktok": "TikTok",
            "kick": "Kick"
        }
        self.selected_platforms = []

    def disable_all_items(self):
        """Disable all items in the view."""
        for item in self.children:
            item.disabled = True

    @discord.ui.select(
        placeholder="Select platforms to moderate (multiple allowed)",
        options=[
            discord.SelectOption(label="Discord", value="discord"),
            discord.SelectOption(label="Twitch", value="twitch"),
            discord.SelectOption(label="YouTube", value="youtube"),
            discord.SelectOption(label="TikTok", value="tiktok"),
            discord.SelectOption(label="Kick", value="kick")
        ],
        min_values=1,
        max_values=5,
        custom_id="platform_select"
    )
    async def platform_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not your application!", ephemeral=True)
            return

        self.selected_platforms = select.values
        # Just acknowledge the selection without sending a new message
        await interaction.response.defer()

    @discord.ui.button(
        label="Submit",
        style=discord.ButtonStyle.green,
        custom_id="submit_platforms"  # Add custom_id for persistence
    )
    async def submit_platforms(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not your application!", ephemeral=True)
            return

        if not self.selected_platforms:
            await interaction.response.send_message("Please select at least one platform first!", ephemeral=True)
            return

        try:
            # Acknowledge the interaction first
            await interaction.response.defer(ephemeral=True)
            
            # Disable the view
            self.disable_all_items()
            await interaction.message.edit(view=self)
            
            # Send confirmation
            await interaction.followup.send("Starting your application process...", ephemeral=True)
            
            # Start single application process
            if await self.start_application(interaction):
                await interaction.user.send("Application completed! Thank you for applying.")
            
        except Exception as e:
            await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

    async def start_application(self, interaction: discord.Interaction) -> bool:
        try:
            answers = {}
            
            # Set platforms FIRST before asking any questions
            platform_names = [self.platforms[p] for p in self.selected_platforms]
            answers["platforms"] = ", ".join(platform_names)
            
            # Define base questions directly
            base_questions = [
                {
                    "id": "age",
                    "question": "Are you over the age of 16? (Yes/No)",
                    "required": True,
                    "valid_responses": ["yes", "no"]
                },
                {
                    "id": "timezone",
                    "question": "What timezone are you in? (e.g. EST, PST, GMT)",
                    "required": True
                },
                {
                    "id": "experience",
                    "question": "Do you have any previous moderation experience? If yes, please describe.",
                    "required": False
                },
                {
                    "id": "motivation",
                    "question": "Why do you want to be a moderator?",
                    "required": True
                },
                {
                    "id": "activity",
                    "question": "How active are you in our community? (Rate 1-5)",
                    "required": True,
                    "valid_responses": ["1", "2", "3", "4", "5"]
                },
                {
                    "id": "tos",
                    "question": "Do you agree to follow our Terms of Service? (Yes/No)",
                    "required": True,
                    "valid_responses": ["yes", "no"]
                }
            ]
            
            # Define platform questions directly
            platform_questions = {
                "discord": [
                    {
                        "id": "discord_username",
                        "question": "What is your Discord Username?",
                        "required": True
                    },
                    {
                        "id": "discord_experience",
                        "question": "How long have you been using Discord?",
                        "required": True
                    }
                ],
                "twitch": [
                    {
                        "id": "twitch_username",
                        "question": "What is your Twitch username?",
                        "required": True
                    },
                    {
                        "id": "twitch_following",
                        "question": "How long have you been following our Twitch channel?",
                        "required": True
                    }
                ]
            }
            
            # Ask base questions first
            for question in base_questions:
                answer = await self.ask_question(interaction, question["question"], question.get("valid_responses"))
                if not self.validate_answer(question, answer):
                    return False
                answers[question["id"]] = answer

            # Ask platform-specific questions for each selected platform
            for platform in self.selected_platforms:
                if platform in platform_questions:
                    for question in platform_questions[platform]:
                        answer = await self.ask_question(
                            interaction,
                            f"[{self.platforms[platform]}] {question['question']}", 
                            question.get("valid_responses")
                        )
                        if not self.validate_answer(question, answer):
                            return False
                        answers[f"{platform}_{question['id']}"] = answer

            # Submit the application through the cog
            await self.cog.submit_application(self.guild, self.user, answers)
            await interaction.followup.send("Application completed! Thank you for applying.", ephemeral=True)
            return True
            
        except Exception as e:
            print(f"Debug - Error occurred: {str(e)}")
            await interaction.followup.send(f"An error occurred during the application: {str(e)}", ephemeral=True)
            return False

    def validate_answer(self, question: dict, answer: Optional[str]) -> bool:
        """Validate an answer against question requirements."""
        if not answer and question["required"]:
            return False
            
        if answer and question.get("valid_responses"):
            if answer.lower() not in [r.lower() for r in question["valid_responses"]]:
                return False
                
        # Auto-reject conditions
        if question["id"] == "age" and answer.lower() == "no":
            return False
            
        if question["id"] == "tos" and answer.lower() == "no":
            return False
            
        return True

    async def ask_question(self, interaction: discord.Interaction, question: str, valid_responses: Optional[List[str]] = None) -> Optional[str]:
        while True:  # Keep asking until we get a valid response or cancellation
            await interaction.followup.send(
                f"**{question}**\n" + 
                (f"Please answer with one of: {', '.join(valid_responses)}\n" if valid_responses else "") +
                "You have 5 minutes to respond. Type 'cancel' to cancel."
            )
            
            def check(m):
                return m.author == self.user and isinstance(m.channel, discord.DMChannel)
                
            try:
                response_message = await self.cog.bot.wait_for(
                    "message",
                    check=check,
                    timeout=300
                )
                
                if response_message.content.lower() == "cancel":
                    await interaction.followup.send("Application cancelled.")
                    return None
                    
                if valid_responses:
                    if response_message.content.lower() not in [r.lower() for r in valid_responses]:
                        await interaction.followup.send(
                            f"Invalid response. Please try again with one of these options: {', '.join(valid_responses)}"
                        )
                        continue  # Ask the question again
                    
                return response_message.content
                
            except asyncio.TimeoutError:
                await interaction.followup.send("Application timed out. Please start over.")
                return None

class ApplicationResponseView(discord.ui.View):
    def __init__(self, cog, applicant_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.applicant_id = applicant_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="approve_app")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_response(interaction, "approved", "Your moderator application has been approved! Staff will contact you soon with next steps.")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, custom_id="deny_app")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_response(interaction, "denied", "Your moderator application has been denied. Feel free to apply again in the future.")

    async def process_response(self, interaction: discord.Interaction, status: str, message: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)
            return

        await interaction.response.defer()

        # Update application status
        async with self.cog.config.guild(interaction.guild).applications() as apps:
            if str(interaction.message.id) in apps:
                apps[str(interaction.message.id)]["status"] = status

        # Update forum post tags
        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            # Remove old status tags
            current_tags = [tag for tag in thread.applied_tags if not any(s in tag.name for s in ["Pending", "Approved", "Denied"])]
            # Add new status tag
            new_tag = discord.utils.get(thread.parent.available_tags, name=status.title())
            if new_tag:
                current_tags.append(new_tag)
                await thread.edit(applied_tags=current_tags)

            # Auto-archive if denied
            if status == "denied":
                await thread.edit(archived=True, locked=True)

        # Notify the applicant
        try:
            user = interaction.guild.get_member(self.applicant_id)
            if user:
                await user.send(message)
        except discord.HTTPException:
            await interaction.followup.send("Could not DM the applicant, but the application has been processed.", ephemeral=True)

        # Update the status embed first
        await self.cog.update_status_embed(interaction.guild)

        # Update the current application embed
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green() if status == "approved" else discord.Color.red()
        embed.set_footer(text=f"{status.title()} by {interaction.user}")
        
        self.disable_all_items()
        await interaction.message.edit(embed=embed, view=self)

class ApplicationStartView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Apply Now", style=discord.ButtonStyle.primary, custom_id="start_application")
    async def start_application(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_application_process(interaction) 