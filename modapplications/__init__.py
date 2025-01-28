from redbot.core.bot import Red
from .modapplications import ModApplications

async def setup(bot: Red):
    cog = ModApplications(bot)
    await bot.add_cog(cog) 