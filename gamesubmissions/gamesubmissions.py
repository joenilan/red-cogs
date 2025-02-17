import discord
from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify
from datetime import datetime, timedelta
from typing import Dict, List
import asyncio

class GameSubmissions(commands.Cog):
    """A cog for managing game submissions and creating weekly polls."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=867530999, force_registration=True
        )
        
        default_guild = {
            "submissions": {},  # Dict of game submissions
            "last_poll_time": None,
            "current_poll_message_id": None,
            "winners": [],
            "channels": {
                "game_forum": None,  # Single forum channel for everything
            }
        }
        
        self.config.register_guild(**default_guild)

    async def initialize_channels(self, ctx: commands.Context, category_id: int):
        """Initialize the forum channel"""
        category = ctx.guild.get_channel(category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            await ctx.send("❌ Invalid category ID or category not found!")
            return None

        channels = await self.config.guild(ctx.guild).channels()
        
        # Create or get game forum
        game_forum = None
        if channels["game_forum"]:
            game_forum = ctx.guild.get_channel(channels["game_forum"])
        if not game_forum:
            try:
                # Create forum tags
                forum_tags = [
                    discord.ForumTag(name="Submitted", emoji="📥"),
                    discord.ForumTag(name="In Poll", emoji="🗳️"),
                    discord.ForumTag(name="Winner", emoji="🏆"),
                    discord.ForumTag(name="Active Poll", emoji="🎮"),
                    discord.ForumTag(name="Past Winners", emoji="📜")
                ]

                game_forum = await ctx.guild.create_forum(
                    name="game-submissions",
                    category=category,
                    topic="Game submissions, polls, and winners",
                    reason="Game submissions forum setup",
                    default_auto_archive_duration=10080,  # 7 days
                    default_thread_slowmode_delay=0,
                    available_tags=forum_tags
                )
                channels["game_forum"] = game_forum.id

                # Create pinned posts for winners and active poll
                winners_embed = discord.Embed(
                    title="🏆 Past Winners",
                    description="Games chosen by the community",
                    color=discord.Color.gold()
                )
                winners_thread = await game_forum.create_thread(
                    name="Past Winners",
                    embed=winners_embed,
                    applied_tags=[discord.utils.get(game_forum.available_tags, name="Past Winners")]
                )
                await winners_thread.thread.pin()

            except discord.Forbidden:
                await ctx.send("⚠️ Could not create forum channel - missing permissions")
                return None
            except Exception as e:
                await ctx.send(f"⚠️ Could not create forum channel: {str(e)}")
                return None

        await self.config.guild(ctx.guild).channels.set(channels)
        return {"game_forum": game_forum}

    async def update_winners(self, guild: discord.Guild):
        """Update the winners thread in the forum"""
        channels = await self.config.guild(guild).channels()
        forum = guild.get_channel(channels["game_forum"])
        if not forum:
            return

        winners = await self.config.guild(guild).winners()
        
        embed = discord.Embed(
            title="🏆 Past Winners",
            description="Games chosen by the community",
            color=discord.Color.gold()
        )
        
        for winner in reversed(winners[-10:]):
            embed.add_field(
                name=f"{winner['game_name']} ({winner['date']})",
                value=f"Votes: {winner['votes']}",
                inline=False
            )

        # Find and update the winners thread
        async for thread in forum.archived_threads(limit=None):
            if thread.name == "Past Winners":
                await thread.edit(archived=False)
                async for message in thread.history(limit=1):
                    await message.edit(embed=embed)
                return

        # Create new winners thread if not found
        winners_thread = await forum.create_thread(
            name="Past Winners",
            embed=embed,
            applied_tags=[discord.utils.get(forum.available_tags, name="Past Winners")]
        )
        await winners_thread.thread.pin()

    @commands.group(name="gamesubmit", aliases=["gs"])
    @commands.guild_only()
    async def gamesubmit(self, ctx: commands.Context):
        """Commands for managing game submissions"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @gamesubmit.command(name="add")
    async def add_game(self, ctx: commands.Context, game_name: str, game_url: str):
        """Submit a game to the list"""
        channels = await self.config.guild(ctx.guild).channels()
        forum = ctx.guild.get_channel(channels["game_forum"])
        
        # Check if game already exists
        async with self.config.guild(ctx.guild).submissions() as submissions:
            if game_name.lower() in submissions:
                await ctx.send(f"❌ {game_name} is already in the submissions list!")
                return

            try:
                # Create forum post for the game
                embed = discord.Embed(
                    title=game_name,
                    url=game_url,
                    description=f"Submitted by: {ctx.author.mention}\n\nDiscuss this game submission in this thread!",
                    color=discord.Color.green(),
                    timestamp=datetime.now()
                )

                if forum and hasattr(forum, 'create_thread'):
                    # Try to create forum thread if supported
                    submitted_tag = discord.utils.get(forum.available_tags, name="Submitted")
                    thread = await forum.create_thread(
                        name=game_name,
                        embed=embed,
                        applied_tags=[submitted_tag] if submitted_tag else []
                    )
                    thread_id = thread.thread.id
                    thread_url = thread.thread.jump_url
                else:
                    thread_id = None
                    thread_url = None

                # Store submission data
                submissions[game_name.lower()] = {
                    "name": game_name,
                    "url": game_url,
                    "submitted_by": ctx.author.id,
                    "timestamp": datetime.now().isoformat(),
                    "thread_id": thread_id
                }

                if thread_url:
                    await ctx.send(f"✅ Game submitted! Discuss it here: {thread_url}")
                else:
                    await ctx.send(f"✅ Game submitted!")
                    
                # Update the game list channel
                await self.update_game_list_channel(ctx.guild)

            except Exception as e:
                await ctx.send(f"✅ Game submitted, but couldn't create forum thread: {str(e)}")
                # Still store the submission without thread info
                submissions[game_name.lower()] = {
                    "name": game_name,
                    "url": game_url,
                    "submitted_by": ctx.author.id,
                    "timestamp": datetime.now().isoformat()
                }

    @gamesubmit.command(name="remove")
    async def remove_game(self, ctx: commands.Context, *, game_name: str):
        """Remove a game from the submissions list
        
        Parameters:
        -----------
        game_name: str
            The name of the game to remove
        """
        async with self.config.guild(ctx.guild).submissions() as submissions:
            if game_name.lower() not in submissions:
                await ctx.send(f"❌ {game_name} is not in the submissions list!")
                return
            
            # Only allow removal by the submitter or administrators
            submission = submissions[game_name.lower()]
            if submission["submitted_by"] != ctx.author.id and not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ You can only remove games that you submitted!")
                return
            
            del submissions[game_name.lower()]
        
        await ctx.send(f"✅ Removed {game_name} from the submissions list!")
        await self.update_game_list_channel(ctx.guild)

    @gamesubmit.command(name="list")
    async def list_games(self, ctx: commands.Context):
        """List all submitted games"""
        submissions = await self.config.guild(ctx.guild).submissions()
        
        if not submissions:
            await ctx.send("No games have been submitted yet!")
            return
        
        embed = discord.Embed(
            title="Submitted Games",
            color=discord.Color.blue()
        )
        
        for game_key in sorted(submissions.keys()):
            game = submissions[game_key]
            submitter = ctx.guild.get_member(game["submitted_by"])
            submitter_name = submitter.display_name if submitter else "Unknown User"
            
            embed.add_field(
                name=game["name"],
                value=f"[Link]({game['url']})\nSubmitted by: {submitter_name}",
                inline=False
            )
        
        await ctx.send(embed=embed)

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="setup")
    async def setup_channels(self, ctx: commands.Context, category_id: int = None):
        """Set up or update the channels for polls and submissions
        
        Parameters:
        -----------
        category_id: int
            The ID of the category to create/use channels in. If not provided, will use the current category.
        """
        category_id = category_id or 1334552562942087198  # Use provided category ID or default
        channels = await self.initialize_channels(ctx, category_id)
        
        if channels:
            await ctx.send("✅ Channels have been set up successfully!")
            await self.update_winners(ctx.guild)
            await self.update_game_list_channel(ctx.guild)
        else:
            await ctx.send("❌ Failed to set up channels!")

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="createpoll")
    async def create_poll(self, ctx: commands.Context):
        """Create a poll with the current game submissions"""
        submissions = await self.config.guild(ctx.guild).submissions()
        channels = await self.config.guild(ctx.guild).channels()
        
        forum = ctx.guild.get_channel(channels["game_forum"])
        
        if not forum:
            await ctx.send("❌ Channels not set up! Please run `[p]gamesubmit setup` first.")
            return

        # Update forum tags for games in poll
        submitted_tag = discord.utils.get(forum.available_tags, name="Submitted")
        poll_tag = discord.utils.get(forum.available_tags, name="In Poll")
        
        for game_key, game in submissions.items():
            if thread_id := game.get("thread_id"):
                thread = await forum.fetch_thread(thread_id)
                if thread:
                    # Update tags to show game is in poll
                    await thread.edit(applied_tags=[poll_tag] if poll_tag else [])

        # Create the poll embed
        embed = discord.Embed(
            title="🎮 Game Poll",
            description="Vote for which game you'd like to see played next!\nPoll closes in 7 days.",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        # Add games to the poll with number emojis
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        
        for i, (game_key, game) in enumerate(submissions.items()):
            if i >= len(number_emojis):  # Limit to 10 games per poll
                break
            embed.add_field(
                name=f"{number_emojis[i]} {game['name']}",
                value=f"[Link]({game['url']})",
                inline=False
            )
        
        # Clear the poll channel and send new poll
        await forum.purge()
        poll_message = await forum.send(embed=embed)
        
        # Add reaction options
        for i in range(min(len(submissions), len(number_emojis))):
            await poll_message.add_reaction(number_emojis[i])
        
        # Update last poll time and message ID
        await self.config.guild(ctx.guild).last_poll_time.set(datetime.now().isoformat())
        await self.config.guild(ctx.guild).current_poll_message_id.set(poll_message.id)
        
        if forum != ctx.channel:
            await ctx.send(f"✅ Poll created in {forum.mention}!")

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="endpoll")
    async def end_poll(self, ctx: commands.Context):
        """End the current poll and announce the winner"""
        poll_message_id = await self.config.guild(ctx.guild).current_poll_message_id()
        channels = await self.config.guild(ctx.guild).channels()
        forum = ctx.guild.get_channel(channels["game_forum"])
        
        if not forum or not poll_message_id:
            await ctx.send("❌ No active poll found!")
            return
        
        try:
            poll_message = await forum.fetch_message(poll_message_id)
        except discord.NotFound:
            await ctx.send("❌ Poll message not found!")
            return
        
        # Count reactions
        submissions = await self.config.guild(ctx.guild).submissions()
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        
        votes = []
        for i, (game_key, game) in enumerate(submissions.items()):
            if i >= len(number_emojis):
                break
            reaction = discord.utils.get(poll_message.reactions, emoji=number_emojis[i])
            count = reaction.count - 1 if reaction else 0  # Subtract 1 to exclude bot's reaction
            votes.append((game["name"], count))
        
        # Sort by votes
        votes.sort(key=lambda x: x[1], reverse=True)
        winner = votes[0]
        
        # Add to winners list
        async with self.config.guild(ctx.guild).winners() as winners:
            winners.append({
                "game_name": winner[0],
                "votes": winner[1],
                "date": datetime.now().strftime("%Y-%m-%d")
            })
        
        # Update winners thread
        await self.update_winners(ctx.guild)
        
        # Create winner announcement
        embed = discord.Embed(
            title="🏆 Poll Results",
            description=f"**Winner: {winner[0]}**\nVotes: {winner[1]}",
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )
        
        for game, vote_count in votes[1:]:
            embed.add_field(
                name=game,
                value=f"Votes: {vote_count}",
                inline=False
            )
        
        await forum.send(embed=embed)
        await ctx.send("✅ Poll ended and winner announced!")

    async def update_game_list_channel(self, guild: discord.Guild):
        """Update the game list channel"""
        channels = await self.config.guild(guild).channels()
        forum = guild.get_channel(channels["game_forum"])
        if not forum:
            return

        submissions = await self.config.guild(guild).submissions()
        
        embed = discord.Embed(
            title="📋 Submitted Games",
            description="All games currently in the submission pool",
            color=discord.Color.blue()
        )
        
        for game_key in sorted(submissions.keys()):
            game = submissions[game_key]
            submitter = guild.get_member(game["submitted_by"])
            submitter_name = submitter.display_name if submitter else "Unknown User"
            
            embed.add_field(
                name=game["name"],
                value=f"[Link]({game['url']})\nSubmitted by: {submitter_name}",
                inline=False
            )

        # Clear channel and send new embed
        await forum.purge()
        await forum.send(embed=embed)

def setup(bot: Red):
    bot.add_cog(GameSubmissions(bot)) 