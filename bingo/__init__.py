from .bingo import Bingo


async def setup(bot):
    await bot.add_cog(Bingo(bot))
