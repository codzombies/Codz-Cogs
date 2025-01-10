import asyncio
from contextlib import suppress
from typing import ClassVar

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import bold, error, info, italics, success


class MuteSync(commands.Cog):
    """Automatically sync timeouts across servers.

    This cog allows server admins to synchronize timeouts for members across multiple servers.
    """

    __author__ = "YourName"
    __version__ = "1.0.0"

    default_guild_settings: ClassVar[dict[str, list[int]]] = {
        "timeout_sources": [],
    }

    def __init__(self, bot: Red) -> None:
        """Initialize the cog."""
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=9876543210, force_registration=True
        )
        self.config.register_guild(**self.default_guild_settings)

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Show version in help."""
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    async def red_delete_data_for_user(self, *, _requester: str, _user_id: int) -> None:
        """No user data to delete."""
        return

    @commands.group()
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def MuteSync(self, ctx: commands.Context) -> None:
        """Configure MuteSync for this server."""

    @MuteSync.command(aliases=["add", "enable"])
    async def enable(
        self, ctx: commands.Context, *, server: discord.Guild | str
    ) -> None:
        """Enable pulling timeouts from a server."""
        if not ctx.guild:
            return
        if not ctx.guild.me.guild_permissions.moderate_members:
            await ctx.send(
                error(
                    "I do not have the Moderate Members permission in this server! Syncing timeouts will not work!"
                )
            )
            return
        if isinstance(server, str):
            await ctx.send(
                error(
                    "I could not find that server. I can only pull timeouts from other servers that I am in."
                )
            )
            return
        if server == ctx.guild:
            await ctx.send(
                error("You can only pull timeouts in from other servers, not this one.")
            )
            return
        timeout_sources = await self.config.guild(ctx.guild).timeout_sources()
        if server.id in timeout_sources:
            await ctx.send(
                success(f"We are already pulling timeouts from {server.name}.")
            )
            return

        timeout_sources.append(server.id)
        await self.config.guild(ctx.guild).timeout_sources.set(timeout_sources)
        await ctx.send(success(f'Now pulling timeouts from "{server.name}".'))

    @MuteSync.command(aliases=["remove", "del", "disable"])
    async def disable(
        self, ctx: commands.Context, *, server: discord.Guild | str
    ) -> None:
        """Disable pulling timeouts from a server."""
        if not ctx.guild:
            return
        timeout_sources = await self.config.guild(ctx.guild).timeout_sources()

        server_id = None
        if isinstance(server, discord.Guild):
            server_id = server.id
        elif isinstance(server, int) or server.isdigit():
            server_id = int(server)
        else:
            await ctx.send(error("I could not find that server."))
            return

        if server_id in timeout_sources:
            timeout_sources.remove(server_id)
            await self.config.guild(ctx.guild).timeout_sources.set(timeout_sources)
            await ctx.send(success(f"Timeouts will no longer be pulled from that server."))
        else:
            await ctx.send(
                info("We were not pulling timeouts from that server in the first place.")
            )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """When a member's timeout changes, propagate it to other servers."""
        if before.communication_disabled_until == after.communication_disabled_until:
            return

        source_guild = after.guild
        timeout_until = after.communication_disabled_until
        all_guild_dict = await self.config.all_guilds()

        for dest_guild_id, dest_guild_settings in all_guild_dict.items():
            if dest_guild_id == source_guild.id:
                continue  # Skip self
            if source_guild.id in dest_guild_settings.get("timeout_sources", []):
                dest_guild = self.bot.get_guild(dest_guild_id)
                if not dest_guild or dest_guild.unavailable:
                    continue
                if not dest_guild.me.guild_permissions.moderate_members:
                    continue

                dest_member = dest_guild.get_member(after.id)
                if not dest_member:
                    continue

                with suppress(
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    if timeout_until:  # Timeout applied
                        await dest_member.edit(
                            communication_disabled_until=timeout_until,
                            reason=f'MuteSync from server "{source_guild.name}"',
                        )
                    else:  # Timeout lifted
                        await dest_member.edit(
                            communication_disabled_until=None,
                            reason=f'MuteSync from server "{source_guild.name}"',
                        )
