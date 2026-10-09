from .identifyactivity import IdentifyActivity


async def setup(bot):
    await bot.add_cog(IdentifyActivity(bot))
