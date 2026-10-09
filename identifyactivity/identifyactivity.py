from __future__ import annotations

from typing import List, Optional, Tuple

import discord
from redbot.core import commands
from redbot.core.utils.chat_formatting import escape

MAX_DESCRIPTION_LENGTH = 4000  # Embed descriptions cap at 4096; leave headroom
MAX_LINES_PER_PAGE = 20
VIEW_TIMEOUT = 120  # seconds of inactivity before buttons are removed


class ActivityPaginator(discord.ui.View):
    """Embed paginator with back / close / next buttons."""

    def __init__(
        self,
        author: discord.abc.User,
        query: str,
        pages: List[str],
        total: int,
    ):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.author = author
        self.query = query
        self.pages = pages
        self.total = total
        self.index = 0
        self.message: Optional[discord.Message] = None

        # No need for arrows when everything fits on one page.
        if len(self.pages) == 1:
            self.remove_item(self.previous)
            self.remove_item(self.next)
        else:
            self._update_buttons()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def build_embed(self) -> discord.Embed:
        title = f"Members playing {self.query}"
        if len(title) > 256:
            title = title[:253] + "..."

        embed = discord.Embed(
            title=title,
            description=self.pages[self.index],
            color=discord.Color.green(),
        )
        embed.set_footer(
            text=(
                f"Page {self.index + 1}/{len(self.pages)} • "
                f"{self.total} member{'s' if self.total != 1 else ''} found"
            )
        )
        return embed

    def _update_buttons(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        # Strip the buttons but leave the embed readable.
        if self.message:
            try:
                await self.message.edit(view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    # ------------------------------------------------------------------ #
    # Buttons (declaration order = left to right)
    # ------------------------------------------------------------------ #
    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.index = max(self.index - 1, 0)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Can't delete? At least remove the buttons.
            await interaction.message.edit(view=None)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(self.index + 1, len(self.pages) - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class IdentifyActivity(commands.Cog):
    """Find server members by what they're currently doing."""

    def __init__(self, bot):
        self.bot = bot

    async def red_delete_data_for_user(self, **kwargs):
        """This cog stores no user data."""
        return

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _matching_activity(member: discord.Member, needle: str) -> Optional[str]:
        """Return the name of the first activity containing `needle`, else None."""
        for activity in member.activities:
            # Custom statuses are free text, not "activities" - skip them.
            if isinstance(activity, discord.CustomActivity):
                continue
            name = getattr(activity, "name", None)
            if name and needle in name.lower():
                return name
        return None

    @staticmethod
    def _paginate(lines: List[str]) -> List[str]:
        """Group lines into pages that respect embed length and line limits."""
        pages: List[str] = []
        current: List[str] = []
        length = 0

        for line in lines:
            added = len(line) + 1  # +1 for newline
            if current and (
                length + added > MAX_DESCRIPTION_LENGTH
                or len(current) >= MAX_LINES_PER_PAGE
            ):
                pages.append("\n".join(current))
                current, length = [], 0
            current.append(line)
            length += added

        if current:
            pages.append("\n".join(current))
        return pages

    # ------------------------------------------------------------------ #
    # Command
    # ------------------------------------------------------------------ #
    @commands.command(name="identifyactivity", aliases=["whoplays"])
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def identifyactivity(self, ctx: commands.Context, *, query: str):
        """List members whose current activity matches the given text.

        The search is case-insensitive and matches partial names.

        **Examples:**
        - `[p]identifyactivity Call of Duty`
        - `[p]identifyactivity Spotify`
        """
        query = query.strip()
        needle = query.lower()

        async with ctx.typing():
            guild = ctx.guild
            if not guild.chunked:
                await guild.chunk()

            matches: List[Tuple[discord.Member, str]] = []
            for member in guild.members:
                activity_name = self._matching_activity(member, needle)
                if activity_name:
                    matches.append((member, activity_name))

        if not matches:
            return await ctx.send(
                f"No members found with an activity matching **{escape(query, mass_mentions=True, formatting=True)}**."
            )

        matches.sort(key=lambda pair: pair[0].display_name.lower())
        lines = [
            f"{member.mention} — {escape(name, mass_mentions=True, formatting=True)}"
            for member, name in matches
        ]

        pages = self._paginate(lines)
        view = ActivityPaginator(ctx.author, query, pages, total=len(matches))
        view.message = await ctx.send(embed=view.build_embed(), view=view)
