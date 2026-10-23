from .lokfishing import LokFishing


async def setup(bot):
    await bot.add_cog(LokFishing(bot))