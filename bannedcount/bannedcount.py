from typing import Tuple
from redbot.core import Config, commands, checks

class BannedCount(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    async def get_ban_limit(ctx: commands.Context, limit: int) -> Tuple[int, list]:
        async with ctx.typing():
            limit = min(limit, 50000)  # Set the limit to 10000
            bans = [entry async for entry in ctx.guild.bans(limit=limit)]
            ban_count = len(bans)
            if not ban_count:
                raise commands.UserFeedbackCheckFailure("This server has no bans.")
            limit = min(ban_count, limit)
            return limit, bans

    @commands.command()
    @commands.guild_only()
    @commands.mod()
    async def bannedcount(self, ctx, limit: int = 50000):
        """Get the number of banned members in the server"""
        try:
            limit, bans = await self.get_ban_limit(ctx, limit)
            await ctx.send(f"Number of banned members: {limit}")
        except commands.UserFeedbackCheckFailure as e:
            await ctx.send(f"{e}")
        except Exception as e:
            await ctx.send(f"An error occurred: {e}")