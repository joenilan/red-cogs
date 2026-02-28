from .mmidlealpha import MMIdleAlpha


async def setup(bot):
    await bot.add_cog(MMIdleAlpha(bot))
