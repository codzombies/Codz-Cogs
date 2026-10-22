import math
from typing import List, Optional

import discord
from redbot.core import checks, commands
from redbot.core.bot import Red

PER_PAGE = 20


class TagPaginator(discord.ui.View):
    """Button-based paginator for a list of embeds."""

    def __init__(self, author_id: int, pages: List[discord.Embed], timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.pages = pages
        self.index = 0
        self.message: Optional[discord.Message] = None
        self._sync_buttons()

    def _sync_buttons(self):
        last = len(self.pages) - 1
        self.first_page.disabled = self.prev_page.disabled = self.index == 0
        self.next_page.disabled = self.last_page.disabled = self.index == last
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran the command can use these buttons.", ephemeral=True
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction):
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        await self._show(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = len(self.pages) - 1
        await self._show(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ServerTag(commands.Cog):
    """Look up a user's Discord server tag, or list everyone here wearing a given tag."""

    def __init__(self, bot: Red):
        self.bot = bot

    # ------------------------------------------------------------------
    # Single user lookup (one REST call per invocation, works for any user ID)
    # ------------------------------------------------------------------
    @commands.command(name="getservertag")
    async def getservertag(self, ctx: commands.Context, user_id: Optional[int] = None):
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

    # ------------------------------------------------------------------
    # List all members of this server wearing a tag (cache only, no REST calls)
    # ------------------------------------------------------------------
    @commands.command(name="servertag", aliases=["tagmembers"])
    @commands.guild_only()
    @checks.mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(embed_links=True)
    async def servertag(self, ctx: commands.Context, *, tag: str):
        """List members in this server wearing the given server tag.

        Example: `[p]servertag FISH`
        """
        if not hasattr(discord.Member, "primary_guild"):
            return await ctx.send(
                "This needs discord.py 2.6 or newer, which this Red install doesn't have."
            )

        query = tag.strip().casefold()
        guild = ctx.guild

        async with ctx.typing():
            # Normally already cached; chunk is one rate-limited gateway request if not.
            if not guild.chunked:
                await guild.chunk()

            matches = []
            tag_guild_ids = set()
            for member in guild.members:
                pg = member.primary_guild
                if pg is None or not pg.identity_enabled or not pg.tag:
                    continue
                if pg.tag.casefold() == query:
                    matches.append(member)
                    tag_guild_ids.add(pg.id)

        if not matches:
            return await ctx.send(f"No members here are displaying the tag `{tag.strip()}`.")

        matches.sort(key=lambda m: m.display_name.casefold())
        total_pages = math.ceil(len(matches) / PER_PAGE)
        display_tag = matches[0].primary_guild.tag

        pages = []
        for p in range(total_pages):
            chunk = matches[p * PER_PAGE : (p + 1) * PER_PAGE]
            lines = [
                f"`{p * PER_PAGE + i + 1}.` {m.mention} • {discord.utils.escape_markdown(str(m))}"
                for i, m in enumerate(chunk)
            ]
            embed = discord.Embed(
                title=f"Members with tag [{display_tag}]",
                description="\n".join(lines),
                colour=await ctx.embed_colour(),
            )
            footer = f"{len(matches)} member(s) • Page {p + 1}/{total_pages}"
            if len(tag_guild_ids) > 1:
                footer += f" • Note: {len(tag_guild_ids)} different servers use this tag"
            embed.set_footer(text=footer)
            pages.append(embed)

        view = TagPaginator(ctx.author.id, pages)
        view.message = await ctx.send(embed=pages[0], view=view)