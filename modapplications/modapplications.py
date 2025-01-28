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
            "questions": [
                {
                    "id": "username",
                    "question": "What is your {platform} Username?",
                    "required": True
                },
                {
                    "id": "age",
                    "question": "Are you over the age of 16? (Yes/No)",
                    "required": True,
                    "valid_responses": ["yes", "no"]
                },
                {
                    "id": "experience",
                    "question": "Are you a Moderator on any other {platform} channels? If yes, please list which ones.",
                    "required": False
                },
                {
                    "id": "motivation",
                    "question": "Why do you want to be a {platform} moderator?",
                    "required": True
                },
                {
                    "id": "activity",
                    "question": "How active are you on our {platform}? (Rate 1-5)",
                    "required": True,
                    "valid_responses": ["1", "2", "3", "4", "5"]
                },
                {
                    "id": "tos",
                    "question": "Do you agree to follow {platform}'s Terms of Service? (Yes/No)",
                    "required": True,
                    "valid_responses": ["yes", "no"]
                }
            ],
            "app_channel": None,
            "notify_role": None,
            "applications_open": False,
            "mod_role": None,  # New: Role for moderator reviewers
            "category_id": None  # New: Category for application channels
        }
        
        self.config.register_guild(**default_guild)

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
    @commands.is_owner()  # Changed to owner-only since it creates channels
    async def modapp_open(self, ctx):
        """Open applications and create necessary channels/roles."""
        try:
            # Create the category
            category = await ctx.guild.create_category(
                "Mod Applications",
                reason="Automated setup for moderator applications"
            )
            
            # Create the reviewer role if it doesn't exist
            reviewer_role = await ctx.guild.create_role(
                name="Application Reviewer",
                color=discord.Color.blue(),
                reason="Role for reviewing moderator applications"
            )
            
            # Create the applications channel
            apps_channel = await ctx.guild.create_text_channel(
                "mod-applications",
                category=category,
                overwrites={
                    ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    reviewer_role: discord.PermissionOverwrite(read_messages=True),
                    ctx.guild.me: discord.PermissionOverwrite(read_messages=True)
                }
            )
            
            # Save the IDs to config
            await self.config.guild(ctx.guild).app_channel.set(apps_channel.id)
            await self.config.guild(ctx.guild).mod_role.set(reviewer_role.id)
            await self.config.guild(ctx.guild).category_id.set(category.id)
            await self.config.guild(ctx.guild).applications_open.set(True)
            
            # Add the role to the command user
            await ctx.author.add_roles(reviewer_role)
            
            await ctx.send(
                "📝 Moderator applications are now open!\n"
                f"Category: {category.name}\n"
                f"Applications Channel: {apps_channel.mention}\n"
                f"Reviewer Role: {reviewer_role.mention}\n"
                f"You have been given the reviewer role.\n"
                "Users can apply using the `!apply` command."
            )
            
        except discord.Forbidden:
            await ctx.send("I don't have the required permissions to create channels and roles.")
        except Exception as e:
            await ctx.send(f"An error occurred during setup: {str(e)}")

    @modapp.command(name="close")
    @commands.is_owner()  # Changed to owner-only since it deletes channels
    async def modapp_close(self, ctx):
        """Close applications and remove channels/roles."""
        try:
            # Get saved IDs
            category_id = await self.config.guild(ctx.guild).category_id()
            mod_role_id = await self.config.guild(ctx.guild).mod_role()
            
            # Delete category (this will delete all channels in it)
            if category_id:
                category = ctx.guild.get_channel(category_id)
                if category:
                    await category.delete(reason="Closing mod applications")
            
            # Delete reviewer role
            if mod_role_id:
                role = ctx.guild.get_role(mod_role_id)
                if role:
                    await role.delete(reason="Closing mod applications")
            
            # Clear config
            await self.config.guild(ctx.guild).app_channel.set(None)
            await self.config.guild(ctx.guild).mod_role.set(None)
            await self.config.guild(ctx.guild).category_id.set(None)
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

class ApplicationTypeView(discord.ui.View):
    def __init__(self, cog, user, guild):
        super().__init__(timeout=300)
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
        max_values=5  # Allow selecting all platforms
    )
    async def platform_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_platforms = select.values
        await interaction.response.send_message("Click 'Submit' when you've selected all platforms you want to apply for.", ephemeral=True)

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.green)
    async def submit_platforms(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_platforms:
            await interaction.response.send_message("Please select at least one platform first!", ephemeral=True)
            return

        await interaction.response.defer()
        self.disable_all_items()
        await interaction.message.edit(view=self)

        # Start applications for all selected platforms
        for platform in self.selected_platforms:
            await interaction.followup.send(f"\n\n**Starting {self.platforms[platform]} Moderator Application**")
            if not await self.start_application(interaction, platform):
                return  # Stop if any application fails

        await interaction.followup.send("All applications completed! Thank you for applying.")

    async def start_application(self, interaction: discord.Interaction, platform: str) -> bool:
        questions = await self.cog.config.guild(self.guild).questions()
        answers = {}
        platform_name = self.platforms[platform]

        for question in questions:
            formatted_question = question["question"].format(platform=platform_name)
            answer = await self.ask_question(interaction, formatted_question, question.get("valid_responses"))
            
            if question["required"] and not answer:
                await interaction.followup.send("Required question was not answered. Application cancelled.")
                return False
                
            if answer and question.get("valid_responses"):
                if answer.lower() not in [r.lower() for r in question["valid_responses"]]:
                    await interaction.followup.send(
                        f"Invalid response. Please answer with one of: {', '.join(question['valid_responses'])}. Application cancelled."
                    )
                    return False
                    
            # Auto-reject conditions
            if question["id"] == "age" and answer.lower() == "no":
                await interaction.followup.send("Sorry, you must be 16 or older to apply for moderator positions. Application cancelled.")
                return False
                
            if question["id"] == "tos" and answer.lower() == "no":
                await interaction.followup.send("You must agree to the Terms of Service to apply. Application cancelled.")
                return False
                
            answers[question["id"]] = answer

        # Add platform to answers
        answers["platform"] = platform_name
        
        # Create and send the application
        await self.submit_application(platform_name, answers)
        return True

    async def ask_question(self, interaction: discord.Interaction, question: str, valid_responses: Optional[List[str]] = None) -> Optional[str]:
        await interaction.followup.send(f"**{question}**\nYou have 5 minutes to respond. Type 'cancel' to cancel.")
        
        try:
            response_message = await self.cog.bot.wait_for(
                "message",
                check=lambda m: m.author == self.user and m.channel == interaction.channel,
                timeout=300
            )
            
            if response_message.content.lower() == "cancel":
                await interaction.followup.send("Application cancelled.")
                return None
                
            if valid_responses and response_message.content.lower() not in [r.lower() for r in valid_responses]:
                await interaction.followup.send(
                    f"Invalid response. Please answer with one of: {', '.join(valid_responses)}. Application cancelled."
                )
                return None
                
            return response_message.content
            
        except asyncio.TimeoutError:
            await interaction.followup.send("Application timed out. Please start over.")
            return None

    async def submit_application(self, platform: str, answers: Dict[str, str]):
        channel_id = await self.cog.config.guild(self.guild).app_channel()
        if not channel_id:
            await self.user.send("Error: Application channel not configured. Please contact an administrator.")
            return

        channel = self.guild.get_channel(channel_id)
        if not channel:
            await self.user.send("Error: Could not find application channel. Please contact an administrator.")
            return

        embed = discord.Embed(
            title=f"New Moderator Application - {platform.title()}",
            description=f"Application from {self.user.mention} ({self.user.id})",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )

        for question, answer in answers.items():
            embed.add_field(name=question.title(), value=answer, inline=False)

        view = ApplicationResponseView(self.cog, self.user.id)
        msg = await channel.send(embed=embed, view=view)

        # Store the application in the config
        async with self.cog.config.guild(self.guild).applications() as apps:
            apps[str(msg.id)] = {
                "user_id": self.user.id,
                "type": platform,
                "answers": answers,
                "status": "pending",
                "timestamp": datetime.now().isoformat()
            }

        await self.user.send("Your application has been submitted! You will be notified when it has been reviewed.")

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

        # Notify the applicant
        try:
            user = interaction.guild.get_member(self.applicant_id)
            if user:
                await user.send(message)
        except discord.HTTPException:
            await interaction.followup.send("Could not DM the applicant, but the application has been processed.", ephemeral=True)

        # Update the embed
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green() if status == "approved" else discord.Color.red()
        embed.set_footer(text=f"{status.title()} by {interaction.user}")
        
        self.disable_all_items()
        await interaction.message.edit(embed=embed, view=self) 