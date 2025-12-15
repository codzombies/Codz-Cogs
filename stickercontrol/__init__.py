from .stickercontrol import StickerControl

async def setup(bot):
    await bot.add_cog(StickerControl(bot))