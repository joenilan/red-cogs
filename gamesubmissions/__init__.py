from .gamesubmissions import GameSubmissions

async def setup(bot):
    await bot.add_cog(GameSubmissions(bot)) 