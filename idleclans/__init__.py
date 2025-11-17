from .idleclans import IdleClans

async def setup(bot):
    await bot.add_cog(IdleClans(bot))
