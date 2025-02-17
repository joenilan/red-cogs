import discord
from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify
from datetime import datetime, timedelta
from typing import Dict, List
import asyncio
from discord.ui import Button, View, Select
import uuid

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
                "game_forum": None,  # Forum for submissions and active polls
                "hall_of_fame": None  # Channel for winners and runner-ups
            },
            "messages": {
                "game_list_id": None,  # ID of the game list message
                "game_list_thread_id": None,  # ID of the game list thread
                "hall_of_fame_id": None  # ID of the hall of fame message
            }
        }
        
        self.config.register_guild(**default_guild)

    async def initialize_channels(self, ctx: commands.Context, category_id: int):
        """Initialize the forum and hall of fame channels"""
        category = ctx.guild.get_channel(category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            await ctx.send("❌ Invalid category ID or category not found!")
            return None

        channels = await self.config.guild(ctx.guild).channels()
        messages = await self.config.guild(ctx.guild).messages()
        
        try:
            # Set up read-only permissions
            everyone_role = ctx.guild.default_role
            read_only_permissions = {
                everyone_role: discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    read_message_history=True,
                    send_messages=False,
                    create_public_threads=False,
                    create_private_threads=False,
                    send_messages_in_threads=False,
                    add_reactions=True,
                    use_external_emojis=True,
                    use_external_stickers=True
                ),
                ctx.guild.me: discord.PermissionOverwrite(  # Bot needs full permissions
                    administrator=True
                )
            }

            # Create or get game forum
            game_forum = None
            if channels["game_forum"]:
                game_forum = ctx.guild.get_channel(channels["game_forum"])
            if not game_forum:
                # Create forum tags
                forum_tags = [
                    discord.ForumTag(name="Submitted", emoji="📥"),
                    discord.ForumTag(name="In Poll", emoji="🗳️"),
                    discord.ForumTag(name="Active Poll", emoji="🎮")
                ]

                game_forum = await ctx.guild.create_forum(
                    name="game-submissions",
                    category=category,
                    topic="Submit games and vote in active polls",
                    reason="Game submissions forum setup",
                    default_auto_archive_duration=10080,  # 7 days
                    default_thread_slowmode_delay=0,
                    available_tags=forum_tags,
                    overwrites=read_only_permissions
                )
                channels["game_forum"] = game_forum.id

                # Create initial Game List thread
                initial_embed = discord.Embed(
                    title="📋 Submitted Games",
                    description="All games currently in the submission pool",
                    color=discord.Color.blue()
                )
                
                thread = await game_forum.create_thread(
                    name="Game List",
                    content="Current list of submitted games",
                    embed=initial_embed,  # This creates the first message
                    applied_tags=[]
                )
                
                # Store the thread and message IDs
                messages["game_list_thread_id"] = thread.thread.id
                messages["game_list_id"] = thread.message.id  # Use the thread's initial message

            # Create or get game tracker channel
            game_tracker = None
            if channels["hall_of_fame"]:
                game_tracker = ctx.guild.get_channel(channels["hall_of_fame"])
            if not game_tracker:
                game_tracker = await ctx.guild.create_text_channel(
                    name="game-tracker",
                    category=category,
                    topic="Current and completed community-chosen games",
                    overwrites=read_only_permissions
                )
                channels["hall_of_fame"] = game_tracker.id

                # Create initial game tracker message
                await self.update_hall_of_fame(ctx.guild)

            # If channels exist but permissions need updating
            if game_forum:
                await game_forum.edit(overwrites=read_only_permissions)
            if game_tracker:
                await game_tracker.edit(overwrites=read_only_permissions)

        except discord.Forbidden:
            await ctx.send("⚠️ Could not create/update channels - missing permissions")
            return None
        except Exception as e:
            await ctx.send(f"⚠️ Could not create/update channels: {str(e)}")
            return None

        await self.config.guild(ctx.guild).channels.set(channels)
        await self.config.guild(ctx.guild).messages.set(messages)
        return {"game_forum": game_forum, "hall_of_fame": game_tracker}

    async def update_hall_of_fame(self, guild: discord.Guild):
        """Update the hall of fame message"""
        channels = await self.config.guild(guild).channels()
        messages = await self.config.guild(guild).messages()
        hall_of_fame = guild.get_channel(channels["hall_of_fame"])
        if not hall_of_fame:
            return

        winners = await self.config.guild(guild).winners()
        
        # Create main embed
        winners_embed = discord.Embed(
            title="🎮 Game Tracker",
            description="Community-chosen games",
            color=discord.Color.blue()
        )
        
        # Split winners into custom and regular
        custom_winners = [w for w in winners if w.get("custom_added")]
        regular_winners = [w for w in winners if not w.get("custom_added")]
        
        # Add custom winners section if any exist
        if custom_winners:
            custom_list = "\n".join([f"**{winner['game_name']}**" for winner in custom_winners])
            winners_embed.add_field(
                name="Currently Playing",
                value=custom_list,
                inline=False
            )
        
        # Add latest regular winner if exists
        if regular_winners:
            latest_winner = regular_winners[-1]
            winners_embed.add_field(
                name="Next Up",
                value=f"**{latest_winner['game_name']}**\n"
                      f"Votes: {latest_winner['votes']}\n"
                      f"Date: {latest_winner['date']}",
                inline=False
            )
        
        # Add past winners section
        if len(regular_winners) > 1:
            past_winners = ""
            for winner in reversed(regular_winners[:-1][-5:]):
                past_winners += f"**{winner['game_name']}** ({winner['date']}) - {winner['votes']} votes\n"
            
            winners_embed.add_field(
                name="Previously Played",
                value=past_winners or "No completed games yet",
                inline=False
            )

        try:
            if messages["hall_of_fame_id"]:
                try:
                    message = await hall_of_fame.fetch_message(messages["hall_of_fame_id"])
                    await message.edit(embed=winners_embed)
                    return
                except discord.NotFound:
                    pass
            
            # Create new message if needed
            message = await hall_of_fame.send(embed=winners_embed)
            
            # Store the new message ID
            async with self.config.guild(guild).messages() as messages:
                messages["hall_of_fame_id"] = message.id
            
        except Exception as e:
            print(f"Error updating hall of fame: {e}")

    @commands.group(name="gamesubmit", aliases=["gs"])
    @commands.guild_only()
    async def gamesubmit(self, ctx: commands.Context):
        """Commands for managing game submissions"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @gamesubmit.command(name="add")
    async def add_game(self, ctx: commands.Context, *, game_name: str = None):
        """Submit a game to the list
        
        Run the command without parameters to start an interactive submission process.
        """
        # Interactive submission process if no game_name provided
        if game_name is None:
            try:
                # Ask for game name
                await ctx.send("What's the name of the game you want to submit?")
                msg = await self.bot.wait_for(
                    "message",
                    timeout=30.0,
                    check=lambda m: m.author == ctx.author and m.channel == ctx.channel
                )
                game_name = msg.content

                # Check if game already exists
                async with self.config.guild(ctx.guild).submissions() as submissions:
                    if game_name.lower() in submissions:
                        await ctx.send(f"❌ {game_name} is already in the submissions list!")
                        return

                # Ask for game URL
                await ctx.send("Please provide the URL to the game (Steam, Epic, etc.):")
                msg = await self.bot.wait_for(
                    "message",
                    timeout=30.0,
                    check=lambda m: m.author == ctx.author and m.channel == ctx.channel
                )
                game_url = msg.content

                # Ask if game is free or paid
                await ctx.send("Is the game Free or Paid? (Type 'free' or 'paid')")
                msg = await self.bot.wait_for(
                    "message",
                    timeout=30.0,
                    check=lambda m: m.author == ctx.author and m.channel == ctx.channel
                )
                is_paid = msg.content.lower() == 'paid'

                game_price = None
                if is_paid:
                    await ctx.send("What's the current price of the game? (e.g., $19.99)")
                    msg = await self.bot.wait_for(
                        "message",
                        timeout=30.0,
                        check=lambda m: m.author == ctx.author and m.channel == ctx.channel
                    )
                    game_price = msg.content

                # Create confirmation embed
                confirm_embed = discord.Embed(
                    title="Confirm Game Submission",
                    description="Please review the details below. React with ✅ to submit or ❌ to cancel.",
                    color=discord.Color.blue()
                )
                confirm_embed.add_field(name="Game Name", value=game_name, inline=False)
                confirm_embed.add_field(name="Game URL", value=game_url, inline=False)
                confirm_embed.add_field(name="Type", value="Paid" if is_paid else "Free", inline=True)
                if game_price:
                    confirm_embed.add_field(name="Price", value=game_price, inline=True)

                view = ConfirmSubmissionView()
                confirm_msg = await ctx.send(embed=confirm_embed, view=view)
                
                # Wait for button interaction
                await view.wait()
                if view.value is None:
                    await ctx.send("Submission timed out. Please try again.")
                    return
                if not view.value:
                    await ctx.send("Game submission cancelled.")
                    return

            except asyncio.TimeoutError:
                await ctx.send("Submission timed out. Please try again.")
                return

        else:
            # If game_name was provided, ask for URL (maintaining backward compatibility)
            if "game_url" not in locals():
                await ctx.send("Please provide the URL to the game (Steam, Epic, etc.):")
                msg = await self.bot.wait_for(
                    "message",
                    timeout=30.0,
                    check=lambda m: m.author == ctx.author and m.channel == ctx.channel
                )
                game_url = msg.content

        # Store submission
        async with self.config.guild(ctx.guild).submissions() as submissions:
            # Debug print to see what's in submissions before adding
            await ctx.send(f"Current submissions before adding: {len(submissions)}")
            
            # Store submission data
            submission_data = {
                "name": game_name,
                "url": game_url,
                "submitted_by": ctx.author.id,
                "timestamp": datetime.now().isoformat()
            }
            
            # Add price information if available
            if 'is_paid' in locals():
                submission_data["is_paid"] = is_paid
                if game_price:
                    submission_data["price"] = game_price

            # Store using lowercase key for case-insensitive lookup
            submissions[game_name.lower()] = submission_data
            
            # Debug print to verify submission was added
            await ctx.send(f"✅ Game submitted! Current submissions: {len(submissions)}\nGame data: {submission_data}")
            
            # Force save the config
            await self.config.guild(ctx.guild).submissions.set(submissions)
            
            # Update the game list thread
            await self.update_game_list_channel(ctx.guild)

            # Debug: List all current submissions
            games_list = "\n".join([f"- {game['name']}" for game in submissions.values()])
            await ctx.send(f"Current games in list:\n{games_list}")

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
            
            # Create value string with price info
            value = f"[Link]({game['url']})\nSubmitted by: {submitter_name}"
            if game.get("is_paid") is not None:
                value += f"\nType: {'Paid' if game['is_paid'] else 'Free'}"
                if game.get("price"):
                    value += f"\nPrice: {game['price']}"
            
            embed.add_field(
                name=game["name"],
                value=value,
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
            await self.update_hall_of_fame(ctx.guild)
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

        if not submissions:
            await ctx.send("❌ No games to create a poll with!")
            return

        # Create the poll embed
        embed = discord.Embed(
            title="🎮 Game Poll",
            description="Select which game you'd like to see played next!\nPoll closes in 7 days.",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        # Add games to the poll
        games_list = []
        for game_key, game in submissions.items():
            games_list.append(game)
            price_info = f" - {game['price']}" if game.get('price') else ""
            type_info = "Paid" if game.get('is_paid') else "Free"
            embed.add_field(
                name=game['name'],
                value=f"[Link]({game['url']})\n{type_info}{price_info}",
                inline=False
            )

        # Create view with select menu
        view = PollView(games_list)
        
        # Create a new thread for the poll
        poll_thread = await forum.create_thread(
            name="🎮 Current Game Poll",
            embed=embed,
            applied_tags=[discord.utils.get(forum.available_tags, name="Active Poll")]
        )
        
        # Send poll message with view
        poll_message = await poll_thread.thread.send(embed=embed, view=view)
        
        # Store poll data
        await self.config.guild(ctx.guild).last_poll_time.set(datetime.now().isoformat())
        await self.config.guild(ctx.guild).current_poll_message_id.set(poll_message.id)
        
        await ctx.send(f"✅ Poll created! Vote here: {poll_thread.thread.jump_url}")

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
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "��"]
        
        # Count votes
        votes = []
        for i, (game_key, game) in enumerate(submissions.items()):
            if i >= len(number_emojis):
                break
            reaction = discord.utils.get(poll_message.reactions, emoji=number_emojis[i])
            count = reaction.count - 1 if reaction else 0
            votes.append((game["name"], count))
        
        # Sort by votes
        votes.sort(key=lambda x: x[1], reverse=True)
        
        # Check for ties
        highest_votes = votes[0][1]
        tied_winners = [(game, submissions[game.lower()]["is_paid"]) for game, vote_count in votes if vote_count == highest_votes]
        
        if len(tied_winners) > 1:
            # Check if we have a mix of free and paid games
            free_games = [game for game, is_paid in tied_winners if not is_paid]
            if free_games:
                # Free game wins automatically
                winner = (free_games[0], highest_votes)
            else:
                # All paid games, create tie-breaker
                tie_embed = discord.Embed(
                    title="🎮 Tie-Breaker Poll",
                    description="We have a tie! Vote again to determine the winner.\nPoll closes in 24 hours.",
                    color=discord.Color.orange(),
                    timestamp=datetime.now()
                )
                
                for game_name, _ in tied_winners:
                    game_data = next(game for game in submissions.values() if game["name"] == game_name)
                    price_info = f" - {game_data['price']}" if game_data.get('price') else ""
                    tie_embed.add_field(
                        name=game_name,
                        value=f"[Link]({game_data['url']})\nPaid{price_info}",
                        inline=False
                    )

                # Create tie-breaker only for paid games
                tied_games = [game for game in submissions.values() if game["name"] in [g[0] for g in tied_winners]]
                view = PollView(tied_games, timeout=86400)  # 24 hour timeout
                
                tiebreaker_thread = await forum.create_thread(
                    name="🎯 Tie-Breaker Poll",
                    embed=tie_embed,
                    applied_tags=[discord.utils.get(forum.available_tags, name="Active Poll")]
                )
                
                tiebreaker_msg = await tiebreaker_thread.thread.send(embed=tie_embed, view=view)
                await self.config.guild(ctx.guild).current_poll_message_id.set(tiebreaker_msg.id)
                
                await ctx.send(f"⚖️ We have a tie between paid games: {', '.join(g[0] for g in tied_winners)}\n"
                             f"A tie-breaker poll has been created here: {tiebreaker_thread.thread.jump_url}")
                return
        else:
            winner = votes[0]
        
        # Add to winners list
        async with self.config.guild(ctx.guild).winners() as winners:
            winners.append({
                "game_name": winner[0],
                "votes": winner[1],
                "date": datetime.now().strftime("%Y-%m-%d"),
                "custom_added": False  # Default to regular winner
            })
        
        # Keep non-winning games in submissions
        async with self.config.guild(ctx.guild).submissions() as submissions:
            # Remove only the winning game
            winner_key = next(
                (key for key, game in submissions.items() if game["name"] == winner[0]),
                None
            )
            if winner_key:
                del submissions[winner_key]
            
            # If this was a tie-breaker, ensure other tied games go back to submissions
            if len(tied_winners) > 1:
                await ctx.send("🔄 Non-winning tied games have been returned to the submission pool.")
        
        # Update hall of fame and game list
        await self.update_hall_of_fame(ctx.guild)
        await self.update_game_list_channel(ctx.guild)
        
        # Create winner announcement in forum
        winner_thread = await forum.create_thread(
            name=f"🏆 Winner: {winner[0]}",
            embed=discord.Embed(
                title="🏆 Poll Results",
                description=f"**Winner: {winner[0]}**\nVotes: {winner[1]}",
                color=discord.Color.gold(),
                timestamp=datetime.now()
            )
        )
        
        # Add runner-ups to the winner thread
        runners_embed = discord.Embed(
            title="Runner-ups",
            color=discord.Color.silver()
        )
        for game, vote_count in votes[1:]:
            runners_embed.add_field(
                name=game,
                value=f"Votes: {vote_count}",
                inline=False
            )
        
        await winner_thread.thread.send(embed=runners_embed)
        await ctx.send("✅ Poll ended and winner announced!")

    async def update_game_list_channel(self, guild: discord.Guild):
        """Update the game list message in the forum"""
        channels = await self.config.guild(guild).channels()
        messages = await self.config.guild(guild).messages()
        forum = guild.get_channel(channels["game_forum"])
        if not forum:
            return

        submissions = await self.config.guild(guild).submissions()
        
        embed = discord.Embed(
            title="📋 Submitted Games",
            description="All games currently in the submission pool",
            color=discord.Color.blue()
        )
        
        # Split games into free and paid
        free_games = []
        paid_games = []
        
        for game_key in sorted(submissions.keys()):
            game = submissions[game_key]
            submitter = guild.get_member(game["submitted_by"])
            submitter_name = submitter.display_name if submitter else "Unknown User"
            
            value = f"[Link]({game['url']})\nSubmitted by: {submitter_name}"
            if game.get("price"):
                value += f"\nPrice: {game['price']}"
            
            game_entry = {
                "name": game["name"],
                "value": value,
                "inline": False
            }
            
            if game.get("is_paid"):
                paid_games.append(game_entry)
            else:
                free_games.append(game_entry)
        
        # Add Free Games section
        if free_games:
            embed.add_field(name="🆓 Free Games", value="‾‾‾‾‾‾‾‾‾‾", inline=False)
            for game in free_games:
                embed.add_field(**game)
        
        # Add Paid Games section
        if paid_games:
            embed.add_field(name="💰 Paid Games", value="‾‾‾‾‾‾‾‾‾‾", inline=False)
            for game in paid_games:
                embed.add_field(**game)
        
        try:
            # Get the thread and message using stored IDs
            thread = None
            message = None
            
            if messages["game_list_thread_id"]:
                thread = forum.get_thread(messages["game_list_thread_id"])
                if thread and messages["game_list_id"]:
                    try:
                        message = await thread.fetch_message(messages["game_list_id"])
                    except discord.NotFound:
                        pass

            if message:
                # Update existing message
                await message.edit(embed=embed)
            else:
                # Create new thread and message if not found
                thread = await forum.create_thread(
                    name="Game List",
                    content="Current list of submitted games",
                    embed=embed,
                    applied_tags=[]
                )
                message = await thread.thread.send(embed=embed)
                await message.pin()
                
                # Store the new IDs
                async with self.config.guild(guild).messages() as messages:
                    messages["game_list_thread_id"] = thread.thread.id
                    messages["game_list_id"] = message.id
            
        except Exception as e:
            print(f"Error updating game list: {e}")

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="clear")
    async def clear_setup(self, ctx: commands.Context, confirm: bool = False):
        """Clear all channels and reset the configuration
        
        Use `[p]gamesubmit clear true` to confirm the action.
        This will delete all created channels and reset the configuration.
        """
        if not confirm:
            await ctx.send("⚠️ This will delete all game submission channels and reset the configuration.\n"
                         "Run `[p]gamesubmit clear true` to confirm.")
            return

        channels = await self.config.guild(ctx.guild).channels()
        
        # Delete forum channel if it exists
        if channels["game_forum"]:
            forum = ctx.guild.get_channel(channels["game_forum"])
            if forum:
                try:
                    await forum.delete(reason="Game submissions clear command")
                except discord.Forbidden:
                    await ctx.send("⚠️ Could not delete forum channel - missing permissions")
                except Exception as e:
                    await ctx.send(f"⚠️ Error deleting forum channel: {str(e)}")

        # Delete hall of fame channel if it exists
        if channels["hall_of_fame"]:
            hall_of_fame = ctx.guild.get_channel(channels["hall_of_fame"])
            if hall_of_fame:
                try:
                    await hall_of_fame.delete(reason="Game submissions clear command")
                except discord.Forbidden:
                    await ctx.send("⚠️ Could not delete hall of fame channel - missing permissions")
                except Exception as e:
                    await ctx.send(f"⚠️ Error deleting hall of fame channel: {str(e)}")

        # Reset all configuration
        await self.config.guild(ctx.guild).clear()
        
        await ctx.send("✅ All channels deleted and configuration reset. "
                      "You can now run `[p]gamesubmit setup` to start fresh.")

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="testhof")
    async def test_hall_of_fame(self, ctx: commands.Context):
        """Populate the hall of fame with test data to preview the layout"""
        
        # Sample winners data
        test_winners = [
            {
                "game_name": "Stardew Valley",
                "votes": 15,
                "date": "2024-01-15"
            },
            {
                "game_name": "Hollow Knight",
                "votes": 12,
                "date": "2024-01-22"
            },
            {
                "game_name": "Baldur's Gate 3",
                "votes": 20,
                "date": "2024-01-29"
            },
            {
                "game_name": "Hades",
                "votes": 18,
                "date": "2024-02-05"
            },
            {
                "game_name": "Lethal Company",
                "votes": 25,
                "date": "2024-02-12"
            }
        ]
        
        # Store test data
        async with self.config.guild(ctx.guild).winners() as winners:
            winners.clear()  # Clear existing winners
            winners.extend(test_winners)
        
        # Update hall of fame display
        await self.update_hall_of_fame(ctx.guild)
        await ctx.send("✅ Hall of Fame populated with test data!")

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="clearhof")
    async def clear_hall_of_fame(self, ctx: commands.Context):
        """Clear all entries from the hall of fame"""
        async with self.config.guild(ctx.guild).winners() as winners:
            winners.clear()
        
        await self.update_hall_of_fame(ctx.guild)
        await ctx.send("✅ Hall of Fame cleared!")

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="addwinner")
    async def add_winner(self, ctx: commands.Context, *, game_name: str):
        """Add a past winner to the hall of fame"""
        async with self.config.guild(ctx.guild).winners() as winners:
            winners.append({
                "game_name": game_name,
                "votes": 0,
                "date": "2024-02-01",
                "custom_added": True  # Flag to identify manually added winners
            })
        
        await self.update_hall_of_fame(ctx.guild)
        await ctx.send(f"✅ Added {game_name} to the Hall of Fame!")

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="removewinner")
    async def remove_winner(self, ctx: commands.Context, *, game_name: str):
        """Remove a game from the hall of fame
        
        Parameters:
        -----------
        game_name: str
            Name of the game to remove
        """
        async with self.config.guild(ctx.guild).winners() as winners:
            # Find and remove the game
            for i, winner in enumerate(winners):
                if winner["game_name"].lower() == game_name.lower():
                    del winners[i]
                    await self.update_hall_of_fame(ctx.guild)
                    await ctx.send(f"✅ Removed {game_name} from the Hall of Fame!")
                    return
            
            await ctx.send(f"❌ {game_name} not found in the Hall of Fame!")

    @commands.admin_or_permissions(administrator=True)
    @gamesubmit.command(name="completewinner")
    async def complete_winner(self, ctx: commands.Context, *, game_name: str):
        """Mark a custom winner as completed and move it to past winners
        
        Example:
        [p]gamesubmit completewinner "Game Name"
        """
        async with self.config.guild(ctx.guild).winners() as winners:
            # Find the custom winner
            for i, winner in enumerate(winners):
                if winner.get("custom_added") and winner["game_name"].lower() == game_name.lower():
                    # Update the winner entry
                    winner["custom_added"] = False  # No longer in "Currently Playing"
                    winner["date"] = datetime.now().strftime("%Y-%m-%d")  # Set completion date
                    winners[i] = winner
                    await self.update_hall_of_fame(ctx.guild)
                    await ctx.send(f"✅ Marked {game_name} as completed and moved to past winners!")
                    return
            
            await ctx.send(f"❌ {game_name} not found in currently playing games!")

class PollView(View):
    def __init__(self, games: list, timeout: int = 604800):  # 7 days default
        super().__init__(timeout=timeout)
        self.votes = {}
        
        # Create select menu for voting
        options = [
            discord.SelectOption(
                label=game["name"][:100],  # Discord has 100 char limit for labels
                description=f"{'Paid' if game.get('is_paid') else 'Free'}"[:100],
                value=str(i)
            ) for i, game in enumerate(games)
        ]
        
        select = Select(
            placeholder="Select a game to vote for...",
            options=options,
            custom_id=f"poll_select_{uuid.uuid4()}"
        )
        
        async def select_callback(interaction: discord.Interaction):
            user_id = interaction.user.id
            choice = int(select.values[0])
            self.votes[user_id] = choice
            await interaction.response.send_message(
                f"You voted for {games[choice]['name']}!",
                ephemeral=True
            )
            
        select.callback = select_callback
        self.add_item(select)

class ConfirmSubmissionView(View):
    def __init__(self, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.value = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: Button):
        self.value = False
        await interaction.response.defer()
        self.stop()

def setup(bot: Red):
    bot.add_cog(GameSubmissions(bot)) 