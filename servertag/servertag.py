import discord
from redbot.core import commands


class ServerTag(commands.Cog):
    """Look up a user's Discord server tag."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="getservertag")
    async def getservertag(self, ctx: commands.Context, user_id: int = None):
        """Get the server tag a user is displaying.

        Usage: [p]getservertag <user_id>
        Leave the ID blank to check yourself.
        """
        user_id = user_id or ctx.author.id

        try:
            # Raw API call so this works regardless of discord.py version
            data = await ctx.bot.http.get_user(user_id)
        except discord.NotFound:
            return await ctx.send("❌ I couldn't find a user with that ID.")
        except discord.HTTPException:
            return await ctx.send("❌ Discord returned an error. Try again later.")

        username = data.get("global_name") or data.get("username", "Unknown")
        primary = data.get("primary_guild")

        if not primary or not primary.get("tag"):
            return await ctx.send(f"**{username}** doesn't have a server tag.")

        tag = primary["tag"]
        guild_id = primary.get("identity_guild_id")
        enabled = primary.get("identity_enabled")
        badge = primary.get("badge")

        embed = discord.Embed(title=f"Server Tag for {username}", color=discord.Color.blurple())
        embed.add_field(name="Tag", value=f"`{tag}`", inline=True)
        embed.add_field(name="Server ID", value=str(guild_id), inline=True)
        embed.add_field(
            name="Currently displayed",
            value="Yes" if enabled else "No",
            inline=True,
        )
        if badge and guild_id:
            embed.set_thumbnail(
                url=f"https://cdn.discordapp.com/guild-tag-badges/{guild_id}/{badge}.png"
            )

        await ctx.send(embed=embed)