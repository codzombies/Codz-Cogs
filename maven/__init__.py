from redbot.core.bot import Red

from .maven import Maven


async def setup(bot: Red) -> None:
    await bot.add_cog(Maven(bot))
