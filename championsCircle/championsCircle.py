import discord
from redbot.core import commands

class ChampionsCircle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.champions_channel = 1276624088848404490  # Replace with the actual channel ID
        self.champions_role_id = 1276625441779613863  # Replace with the actual role ID
        self.champions_list = []

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"ChampionsCircle is ready!")

    @commands.command()
    async def setup_join_button(self, ctx):
        if ctx.channel.id != self.champions_channel:
            await ctx.send("This command can only be used in the Champions Circle channel.")
            return

        view = discord.ui.View(timeout=None)
        view.add_item(JoinButton(self))
        await ctx.send("Click the button to join the Champions Circle!", view=view)

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def test_role_assign(self, ctx, member: discord.Member):
        role = ctx.guild.get_role(self.champions_role_id)
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
    async def list_champions(self, ctx):
        if not self.champions_list:
            await ctx.send("There are no champions yet!")
            return

        embed = discord.Embed(title="Champions Circle", description="Our esteemed champions:", color=0x00ff00)
        for champion_id in self.champions_list:
            champion = ctx.guild.get_member(champion_id)
            if champion:
                embed.add_field(name=champion.name, value=f"ID: {champion.id}", inline=False)

        await ctx.send(embed=embed)

    async def update_embed(self, guild):
        embed = discord.Embed(title="Champions Circle", description="A list of our esteemed champions.", color=0x00ff00)
        for champion_id in self.champions_list:
            champion = guild.get_member(champion_id)
            if champion:
                embed.add_field(name=f"{champion.name}", value=f"ID: {champion.id}", inline=False)

        channel = self.bot.get_channel(self.champions_channel)
        message = await channel.fetch_message(1234567890)  # Replace with the actual message ID
        await message.edit(embed=embed)

class JoinButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(style=discord.ButtonStyle.green, label="Join the Champions Circle", custom_id="join_champions")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id in self.cog.champions_list:
            await interaction.user.send("You are already part of the Champions Circle.")
            return

        champions_role = interaction.guild.get_role(self.cog.champions_role_id)
        if champions_role is None:
            await interaction.user.send("Error: Champions role not found.")
            return

        try:
            await interaction.user.add_roles(champions_role)
            self.cog.champions_list.append(interaction.user.id)
            await self.cog.update_embed(interaction.guild)
            await interaction.user.send(f"Welcome to the Champions Circle! You've been given the {champions_role.name} role.")
        except discord.Forbidden:
            await interaction.user.send("Error: The bot doesn't have permission to assign roles.")
        except discord.HTTPException:
            await interaction.user.send("An error occurred while assigning the role. Please try again later.")

        # Acknowledge the interaction to prevent "This interaction failed" error
        await interaction.response.defer()

async def setup(bot):
    await bot.add_cog(ChampionsCircle(bot))