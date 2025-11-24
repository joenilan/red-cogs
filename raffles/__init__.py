from .core import Raffles


async def setup(bot):
    await bot.add_cog(Raffles(bot))
