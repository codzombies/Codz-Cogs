from .bannedcount import BannedCount

async def setup(bot):
    await bot.add_cog(BannedCount(bot))