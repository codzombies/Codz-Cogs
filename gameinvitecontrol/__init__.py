from .gameinvitecontrol import GameInviteControl

async def setup(bot):
    await bot.add_cog(GameInviteControl(bot))
