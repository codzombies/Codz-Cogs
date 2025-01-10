import asyncio
from contextlib import suppress
from typing import ClassVar

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import (
    bold,
    error,
    info,
    italics,
    question,
    success,
)
from redbot.core.utils.predicates import MessagePredicate

from .pcx_lib import SettingDisplay


class MuteSync(commands.Cog):
    """Automatically sync mutes across servers.

    This cog allows server admins to timeout users on their server
    when they are timed out on another server that the bot is in.
    """

    __author__ = "PhasecoreX"
    __version__ = "1.0.0"

    default_guild_settings: ClassVar[dict[str, str | list[int] | dict[str, int]]] = {
        "cached_guild_name": "Unknown Server Name",
        "mute_sources": [],
        "mute_count": {},
    }

    def __init__(self, bot: Red) -> None:
        """Set up the cog."""
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=1224364860, force_registration=True
        )
        self.config.register_guild(**self.default_guild_settings)
        self.mute_cache = {}

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Show version in help."""
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    async def red_delete_data_for_user(self, *, _requester: str, _user_id: int) -> None:
        """Nothing to delete."""
        return

    async def initialize(self) -> None:
        """Perform setup actions before loading cog."""
        await self._migrate_config()

    async def _migrate_config(self) -> None:
        """Perform some configuration migrations."""
        # schema_version = await self.config.schema_version()

    @commands.group()
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def mutesync(self, ctx: commands.Context) -> None:
        """Configure MuteSync for this server."""

    @mutesync.command(aliases=["info"])
    async def settings(self, ctx: commands.Context) -> None:
        """Display current settings."""
        if not ctx.guild:
            return
        info_text = ""

        if not ctx.guild.me.guild_permissions.moderate_members:
            info_text += error(
                "I do not have the Timeout Members permission in this server!\nSyncing mutes from other servers into this one will not work!\n\n"
            )

        pull_servers = SettingDisplay()
        sync_servers = SettingDisplay()
        unknown_servers = []
        mute_count_dict = await self.config.guild(ctx.guild).mute_count()
        for source_guild_id in await self.config.guild(ctx.guild).mute_sources():
            mute_count = 0
            if str(source_guild_id) in mute_count_dict:
                mute_count = mute_count_dict[str(source_guild_id)]
            mute_count_info = f"{mute_count} mute{'' if mute_count == 1 else 's'}"

            guild_mute_source = self.bot.get_guild(source_guild_id)
            if guild_mute_source:
                # Update their cached guild name
                await self.config.guild_from_id(source_guild_id).cached_guild_name.set(
                    guild_mute_source.name
                )
                # Check if they are pulling from us (sync) or not (pull)
                if (
                    ctx.guild.id
                    in await self.config.guild_from_id(source_guild_id).mute_sources()
                ):
                    sync_servers.add(guild_mute_source.name, mute_count_info)
                else:
                    pull_servers.add(guild_mute_source.name, mute_count_info)
            else:
                unknown_servers.append(
                    f'`{source_guild_id}` - Last known as "{await self.config.guild_from_id(source_guild_id).cached_guild_name()}", {mute_count_info}'
                )

        if not sync_servers and not pull_servers:
            info_text += (
                info(bold("No servers are enabled for pulling!\n"))
                + "Use `[p]mutesync enable` to add some.\n\n"
            )
        else:
            if sync_servers:
                info_text += (
                    "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} "
                    + bold("Syncing mutes with these servers:")
                    + "\n"
                    + italics("(They are also pulling our mutes)")
                    + "\n"
                    + sync_servers.display()
                    + "\n"
                )
            if pull_servers:
                info_text += (
                    ":leftwards_arrow_with_hook: "
                    + bold("Pulling mutes from these servers:")
                    + "\n"
                    + italics("(They are not pulling our mutes)")
                    + "\n"
                    + pull_servers.display()
                    + "\n"
                )

        if unknown_servers:
            info_text += (
                error(bold("These servers are no longer available:"))
                + "\n"
                + italics("(I am not in them anymore)")
                + "\n"
                + "\n".join(unknown_servers)
                + "\n\n"
            )

        mute_count = await self.config.guild(ctx.guild).mute_count()
        total_mutes = 0
        for count in mute_count.values():
            total_mutes += count

        info_text += italics(
            f"Pulled a total of {total_mutes} mute{'' if total_mutes == 1 else 's'} from {len(mute_count)} server{'' if len(mute_count) == 1 else 's'} into this server."
        )

        await ctx.send(info_text)

    @mutesync.command(aliases=["add", "pull"])
    async def enable(
        self, ctx: commands.Context, *, server: discord.Guild | str
    ) -> None:
        """Enable pulling mutes from a server."""
        if not ctx.guild:
            return
        if not ctx.guild.me.guild_permissions.moderate_members:
            await ctx.send(
                error(
                    "I do not have the Timeout Members permission in this server! Syncing mutes from other servers into this one will not work!"
                )
            )
            return
        if isinstance(server, str):
            await ctx.send(
                error(
                    "I could not find that server. I can only pull mutes from other servers that I am in."
                )
            )
            return
        if server == ctx.guild:
            await ctx.send(
                error("You can only pull mutes in from other servers, not this one.")
            )
            return
        mute_sources = await self.config.guild(ctx.guild).mute_sources()
        if server.id in mute_sources:
            # Update our and their cached guild name
            await self.config.guild(ctx.guild).cached_guild_name.set(ctx.guild.name)
            await self.config.guild_from_id(server.id).cached_guild_name.set(
                server.name
            )
            await ctx.send(
                success(
                    f"We are already pulling mutes from {server.name} into this server."
                )
            )
            return

        # You really want to do this?
        pred = MessagePredicate.yes_or_no(ctx)
        await ctx.send(
            question(
                f'Are you **sure** you want to pull new mutes and unmutes from the server "{server.name}" into this server? (yes/no)\n\n'
                "Be sure to only do this for servers that you trust, as all mutes and unmutes that occur there will be mirrored into this server."
            )
        )
        with suppress(asyncio.TimeoutError):
            await ctx.bot.wait_for("message", check=pred, timeout=30)
        if pred.result:
            pass
        else:
            await ctx.send(info("Cancelled adding server as a mute source."))
            return

        # Update our and their cached guild name
        await self.config.guild(ctx.guild).cached_guild_name.set(ctx.guild.name)
        await self.config.guild_from_id(server.id).cached_guild_name.set(server.name)
        # Add their server to our pull list and save
        mute_sources.append(server.id)
        await self.config.guild(ctx.guild).mute_sources.set(mute_sources)
        await ctx.send(
            success(
                f'New mutes from "{server.name}" will now be pulled into this server.'
            )
        )

    @mutesync.command(aliases=["remove", "del", "delete"])
    async def disable(
        self, ctx: commands.Context, *, server: discord.Guild | int | str
    ) -> None:
        """Disable pulling mutes from a server."""
        if not ctx.guild:
            return
        server_id = None
        mute_sources = await self.config.guild(ctx.guild).mute_sources()

        if isinstance(server, discord.Guild):
            # Given arg was converted to a guild, nice!
            server_id = server.id
        elif server not in mute_sources:
            # Given arg was the name of a guild (str), or an ID not in the mute source list (int)
            # (could be a guild with a name of just numbers?)
            all_guild_dict = await self.config.all_guilds()
            for dest_guild_id, dest_guild_settings in all_guild_dict.items():
                if dest_guild_settings.get("cached_guild_name") == str(server):
                    server_id = int(dest_guild_id)
                    break
        elif isinstance(server, int):
            # If not a guild or a string above, it should be an int
            server_id = server

        if not server_id:
            await ctx.send(error("I could not find that server."))
        elif server_id in mute_sources:
            mute_sources.remove(server_id)
            await self.config.guild(ctx.guild).mute_sources.set(mute_sources)
            await ctx.send(success("Mutes will no longer be pulled from that server."))
        else:
            await ctx.send(
                info(
                    "It doesn't seem like we were pulling mutes from that server in the first place."
                )
            )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """When a user is muted or unmuted, propagate that mute to other servers that are subscribed."""
        if before.timed_out_until == after.timed_out_until:
            return  # No change in mute status

        mute = after.timed_out_until is not None
        source_guild = after.guild
        await self._handle_mute_unmute(source_guild, after, mute=mute)

    async def _handle_mute_unmute(
        self,
        source_guild: discord.Guild,
        user: discord.Member,
        *,
        mute: bool,
    ) -> None:
        if source_guild.id not in self.mute_cache:
            self.mute_cache[source_guild.id] = {}
        self.mute_cache[source_guild.id][user.id] = mute

        # Update our cached guild name
        await self.config.guild(source_guild).cached_guild_name.set(source_guild.name)

        all_guild_dict = await self.config.all_guilds()
        for dest_guild_id, dest_guild_settings in all_guild_dict.items():
            if dest_guild_id == source_guild.id:
                continue  # Skip self
            if source_guild.id in dest_guild_settings.get("mute_sources", []):
                dest_guild = self.bot.get_guild(dest_guild_id)
                if not dest_guild or dest_guild.unavailable:
                    continue
                if not dest_guild.me.guild_permissions.moderate_members:
                    continue
                if (
                    dest_guild.id in self.mute_cache
                    and user.id in self.mute_cache[dest_guild.id]
                    and self.mute_cache[dest_guild.id][user.id] == mute
                ):
                    continue  # We already (un)muted them, prevent loop
                with suppress(
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    reason = f'MuteSync from server "{source_guild.name}"'
                    if mute:
                        timeout_until = discord.utils.utcnow() + discord.timedelta(minutes=10)  # Example: 10 minutes
                        await user.edit(timeout=timeout_until, reason=reason)
                        async with self.config.guild(
                            dest_guild
                        ).mute_count() as mute_count:
                            if source_guild.id not in mute_count:
                                mute_count[source_guild.id] = 0
                            mute_count[source_guild.id] += 1
                    else:
                        await user.edit(timeout=None, reason=reason)
