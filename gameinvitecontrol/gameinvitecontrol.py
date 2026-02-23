from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import humanize_list
import discord
from typing import Literal, Optional


class GameInviteControl(commands.Cog):
    """
    Monitor and control Discord Rich Presence game invites.

    This cog listens for Discord's ACTIVITY_UPDATE gateway events to track
    when users publish Rich Presence with party/join-secret data (which enables
    the native Discord 'Ask to Join' / 'Join Game' invite buttons).

    Features
    --------
    • Blacklist or whitelist which channels receive invite-log messages.
    • Optionally auto-delete game-invite messages in restricted channels.
    • Keep a rolling invite log viewable by admins.
    • Per-game (application) filtering so you only track the titles you care about.
    """

    # ------------------------------------------------------------------ #
    #  Setup                                                               #
    # ------------------------------------------------------------------ #

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=9876543210, force_registration=True
        )

        default_guild = {
            # Feature toggle
            "enabled": False,

            # "blacklist" → log invites everywhere EXCEPT listed channels/categories
            # "whitelist" → ONLY log invites in listed channels/categories
            "mode": "blacklist",
            "channels": [],      # List of channel IDs
            "categories": [],    # List of category IDs

            # When True, bot will try to delete the original message that carried
            # the game-invite embed (requires Manage Messages permission).
            "delete_invites": False,

            # Channel where the bot posts its own invite-log embeds.
            # If None, logs are only stored internally.
            "log_channel": None,

            # Optional allow-list of Discord application IDs to track.
            # Empty list = track ALL games.
            "tracked_games": [],   # List of application_id strings

            # Rolling log (most recent first, capped at max_log_size).
            "invite_log": [],
            "max_log_size": 50,
        }

        self.config.register_guild(**default_guild)

    # ------------------------------------------------------------------ #
    #  Top-level command group                                            #
    # ------------------------------------------------------------------ #

    @commands.group(name="gameinvite")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def gameinvite(self, ctx: commands.Context):
        """Manage Rich Presence game invite monitoring and controls."""
        pass

    # ------------------------------------------------------------------ #
    #  Enable / Disable                                                    #
    # ------------------------------------------------------------------ #

    @gameinvite.command(name="enable")
    async def enable_control(self, ctx: commands.Context):
        """Enable game invite monitoring."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("✅ Game invite monitoring is now **enabled**.")

    @gameinvite.command(name="disable")
    async def disable_control(self, ctx: commands.Context):
        """Disable game invite monitoring."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("✅ Game invite monitoring is now **disabled**.")

    # ------------------------------------------------------------------ #
    #  Mode                                                                #
    # ------------------------------------------------------------------ #

    @gameinvite.command(name="mode")
    async def set_mode(
        self, ctx: commands.Context, mode: Literal["blacklist", "whitelist"]
    ):
        """
        Set the channel restriction mode.

        **blacklist** – Monitor invites everywhere *except* the listed channels/categories.
        **whitelist** – Monitor invites *only* in the listed channels/categories.
        """
        await self.config.guild(ctx.guild).mode.set(mode)
        await ctx.send(f"✅ Game invite channel mode set to **{mode}**.")

    # ------------------------------------------------------------------ #
    #  Channel management                                                  #
    # ------------------------------------------------------------------ #

    @gameinvite.command(name="addchannel")
    async def add_channel(
        self, ctx: commands.Context, *channels: discord.TextChannel
    ):
        """Add one or more channels to the blacklist/whitelist."""
        if not channels:
            return await ctx.send("❌ Please specify at least one channel.")

        async with self.config.guild(ctx.guild).channels() as ch_list:
            added = []
            for ch in channels:
                if ch.id not in ch_list:
                    ch_list.append(ch.id)
                    added.append(ch.mention)

        if added:
            mode = await self.config.guild(ctx.guild).mode()
            await ctx.send(
                f"✅ Added {humanize_list(added)} to the **{mode}**."
            )
        else:
            await ctx.send("ℹ️ All specified channels were already in the list.")

    @gameinvite.command(name="removechannel")
    async def remove_channel(
        self, ctx: commands.Context, *channels: discord.TextChannel
    ):
        """Remove one or more channels from the blacklist/whitelist."""
        if not channels:
            return await ctx.send("❌ Please specify at least one channel.")

        async with self.config.guild(ctx.guild).channels() as ch_list:
            removed = []
            for ch in channels:
                if ch.id in ch_list:
                    ch_list.remove(ch.id)
                    removed.append(ch.mention)

        if removed:
            mode = await self.config.guild(ctx.guild).mode()
            await ctx.send(
                f"✅ Removed {humanize_list(removed)} from the **{mode}**."
            )
        else:
            await ctx.send("ℹ️ None of the specified channels were in the list.")

    # ------------------------------------------------------------------ #
    #  Category management                                                 #
    # ------------------------------------------------------------------ #

    @gameinvite.command(name="addcategory")
    async def add_category(
        self, ctx: commands.Context, *categories: discord.CategoryChannel
    ):
        """Add one or more categories to the blacklist/whitelist."""
        if not categories:
            return await ctx.send("❌ Please specify at least one category.")

        async with self.config.guild(ctx.guild).categories() as cat_list:
            added = []
            for cat in categories:
                if cat.id not in cat_list:
                    cat_list.append(cat.id)
                    added.append(f"**{cat.name}**")

        if added:
            mode = await self.config.guild(ctx.guild).mode()
            await ctx.send(
                f"✅ Added {humanize_list(added)} to the **{mode}**."
            )
        else:
            await ctx.send("ℹ️ All specified categories were already in the list.")

    @gameinvite.command(name="removecategory")
    async def remove_category(
        self, ctx: commands.Context, *categories: discord.CategoryChannel
    ):
        """Remove one or more categories from the blacklist/whitelist."""
        if not categories:
            return await ctx.send("❌ Please specify at least one category.")

        async with self.config.guild(ctx.guild).categories() as cat_list:
            removed = []
            for cat in categories:
                if cat.id in cat_list:
                    cat_list.remove(cat.id)
                    removed.append(f"**{cat.name}**")

        if removed:
            mode = await self.config.guild(ctx.guild).mode()
            await ctx.send(
                f"✅ Removed {humanize_list(removed)} from the **{mode}**."
            )
        else:
            await ctx.send("ℹ️ None of the specified categories were in the list.")

    # ------------------------------------------------------------------ #
    #  Log channel                                                         #
    # ------------------------------------------------------------------ #

    @gameinvite.command(name="logchannel")
    async def set_log_channel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ):
        """
        Set (or clear) the channel where invite-log embeds are posted.

        Leave `channel` blank to disable posting logs to a channel.
        """
        if channel is None:
            await self.config.guild(ctx.guild).log_channel.set(None)
            await ctx.send("✅ Log channel cleared. Invite events will only be stored internally.")
        else:
            await self.config.guild(ctx.guild).log_channel.set(channel.id)
            await ctx.send(f"✅ Invite activity will be logged to {channel.mention}.")

    # ------------------------------------------------------------------ #
    #  Delete-invites toggle                                               #
    # ------------------------------------------------------------------ #

    @gameinvite.command(name="deleteinvites")
    async def toggle_delete_invites(
        self, ctx: commands.Context, enabled: bool
    ):
        """
        Toggle whether the bot deletes game-invite messages in restricted channels.

        Usage: `[p]gameinvite deleteinvites true|false`

        Requires the bot to have **Manage Messages** in those channels.
        Only takes effect when channel/category restrictions are active.
        """
        await self.config.guild(ctx.guild).delete_invites.set(enabled)
        state = "**enabled**" if enabled else "**disabled**"
        await ctx.send(f"✅ Auto-deletion of game-invite messages is now {state}.")

    # ------------------------------------------------------------------ #
    #  Tracked games                                                       #
    # ------------------------------------------------------------------ #

    @gameinvite.group(name="games")
    async def games_group(self, ctx: commands.Context):
        """Manage which games (by application ID) are tracked."""
        pass

    @games_group.command(name="add")
    async def add_game(self, ctx: commands.Context, application_id: str):
        """
        Add a game application ID to the tracked list.

        When the tracked list is non-empty, ONLY those games are monitored.
        Find a game's application ID by inspecting its Rich Presence activity.
        """
        async with self.config.guild(ctx.guild).tracked_games() as games:
            if application_id in games:
                return await ctx.send("ℹ️ That application ID is already being tracked.")
            games.append(application_id)
        await ctx.send(f"✅ Now tracking game with application ID `{application_id}`.")

    @games_group.command(name="remove")
    async def remove_game(self, ctx: commands.Context, application_id: str):
        """Remove a game application ID from the tracked list."""
        async with self.config.guild(ctx.guild).tracked_games() as games:
            if application_id not in games:
                return await ctx.send("ℹ️ That application ID is not in the tracked list.")
            games.remove(application_id)
        await ctx.send(f"✅ Removed application ID `{application_id}` from tracking.")

    @games_group.command(name="list")
    async def list_games(self, ctx: commands.Context):
        """Show all tracked game application IDs."""
        games = await self.config.guild(ctx.guild).tracked_games()
        if not games:
            return await ctx.send(
                "ℹ️ No specific games are tracked — **all** Rich Presence games are monitored."
            )
        await ctx.send(
            "**Tracked game application IDs:**\n"
            + "\n".join(f"• `{g}`" for g in games)
        )

    @games_group.command(name="clear")
    async def clear_games(self, ctx: commands.Context):
        """Clear the tracked games list (reverts to monitoring all games)."""
        await self.config.guild(ctx.guild).tracked_games.set([])
        await ctx.send("✅ Tracked games list cleared. All games will now be monitored.")

    # ------------------------------------------------------------------ #
    #  Invite log                                                          #
    # ------------------------------------------------------------------ #

    @gameinvite.command(name="log")
    async def view_log(self, ctx: commands.Context, entries: int = 10):
        """
        Display the most recent invite log entries (default: 10, max: 25).
        """
        entries = min(entries, 25)
        invite_log = await self.config.guild(ctx.guild).invite_log()

        if not invite_log:
            return await ctx.send("ℹ️ The invite log is empty.")

        recent = invite_log[:entries]
        embed = discord.Embed(
            title=f"Game Invite Log (last {len(recent)} entries)",
            color=await ctx.embed_color(),
        )

        for entry in recent:
            user_str = f"<@{entry['user_id']}>"
            game = entry.get("game", "Unknown Game")
            party = entry.get("party", "")
            ts = entry.get("timestamp", "")
            embed.add_field(
                name=f"{game}{' · ' + party if party else ''}",
                value=f"{user_str} — {ts}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @gameinvite.command(name="clearlog")
    async def clear_log(self, ctx: commands.Context):
        """Clear the stored invite log for this server."""
        await self.config.guild(ctx.guild).invite_log.set([])
        await ctx.send("✅ Invite log cleared.")

    # ------------------------------------------------------------------ #
    #  Settings overview                                                   #
    # ------------------------------------------------------------------ #

    @gameinvite.command(name="settings")
    async def show_settings(self, ctx: commands.Context):
        """Show the current game invite control configuration."""
        cfg = await self.config.guild(ctx.guild).all()

        enabled = "✅ Enabled" if cfg["enabled"] else "❌ Disabled"
        mode = cfg["mode"].capitalize()
        delete = "✅ Yes" if cfg["delete_invites"] else "❌ No"

        log_ch = ctx.guild.get_channel(cfg["log_channel"]) if cfg["log_channel"] else None
        log_ch_str = log_ch.mention if log_ch else "Not set"

        channel_mentions = []
        for cid in cfg["channels"]:
            ch = ctx.guild.get_channel(cid)
            channel_mentions.append(ch.mention if ch else f"Unknown ({cid})")

        category_names = []
        for catid in cfg["categories"]:
            cat = ctx.guild.get_channel(catid)
            category_names.append(f"**{cat.name}**" if cat else f"Unknown ({catid})")

        embed = discord.Embed(
            title="Game Invite Control — Settings",
            color=await ctx.embed_color(),
        )
        embed.add_field(name="Status", value=enabled, inline=True)
        embed.add_field(name="Mode", value=mode, inline=True)
        embed.add_field(name="Auto-delete Invites", value=delete, inline=True)
        embed.add_field(name="Log Channel", value=log_ch_str, inline=True)
        embed.add_field(
            name="Tracked Games",
            value=(
                humanize_list([f"`{g}`" for g in cfg["tracked_games"]])
                if cfg["tracked_games"]
                else "All games"
            ),
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        embed.add_field(
            name=f"Channels ({len(channel_mentions)})",
            value=humanize_list(channel_mentions) if channel_mentions else "None",
            inline=False,
        )
        embed.add_field(
            name=f"Categories ({len(category_names)})",
            value=humanize_list(category_names) if category_names else "None",
            inline=False,
        )
        embed.add_field(
            name=f"Log Entries",
            value=str(len(cfg["invite_log"])),
            inline=True,
        )
        embed.add_field(
            name="Max Log Size",
            value=str(cfg["max_log_size"]),
            inline=True,
        )

        await ctx.send(embed=embed)

    @gameinvite.command(name="clear")
    async def clear_lists(self, ctx: commands.Context):
        """Clear all channels and categories from the blacklist/whitelist."""
        await self.config.guild(ctx.guild).channels.set([])
        await self.config.guild(ctx.guild).categories.set([])
        await ctx.send("✅ Cleared all channels and categories from the list.")

    # ------------------------------------------------------------------ #
    #  Message listener — catches actual game invite messages              #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        Fires on every message. We filter down to Discord's native game-invite
        messages, which are regular messages that carry a ``message.activity``
        dict (with a ``type`` of 1=Join, 2=Spectate, 3=Listen, 5=JoinRequest)
        and a ``message.application`` object identifying the game.

        This is the same mechanism StickerControl uses for stickers — catch
        the message as it arrives, check the channel against the blacklist/
        whitelist, delete if blocked, and log either way.
        """
        # Ignore DMs, bots, and messages with no activity payload
        if not message.guild:
            return
        if message.author.bot:
            return
        if not message.activity:
            # message.activity is the dict Discord attaches to game-invite messages
            return

        cfg = await self.config.guild(message.guild).all()
        if not cfg["enabled"]:
            return

        # Bypass for users with Manage Messages (same pattern as StickerControl)
        if message.author.guild_permissions.manage_messages:
            return

        # Optional game filter — message.application is a PartialApplication-like
        # object; fall back to message.application.id if present
        if cfg["tracked_games"]:
            app = getattr(message, "application", None)
            app_id = str(app.id) if app and app.id else ""
            if app_id not in cfg["tracked_games"]:
                return

        # Gather info for logging regardless of block decision
        app = getattr(message, "application", None)
        game_name = (app.name if app and app.name else None) or "Unknown Game"
        activity_type = message.activity.get("type", 0)
        # Discord activity types: 1=Join, 2=Spectate, 3=Listen, 5=JoinRequest
        type_labels = {1: "Join", 2: "Spectate", 3: "Listen", 5: "Join Request"}
        invite_type = type_labels.get(activity_type, f"Type {activity_type}")
        timestamp = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        # Log the event
        app_id_str = str(app.id) if app and app.id else ""
        await self._append_log(
            message.guild,
            cfg,
            {
                "user_id": message.author.id,
                "game": game_name,
                "party": invite_type,
                "timestamp": timestamp,
                "application_id": app_id_str,
                "channel_id": message.channel.id,
            },
        )

        # Post to staff log channel if configured
        log_channel_id = cfg["log_channel"]
        if log_channel_id:
            log_channel = message.guild.get_channel(log_channel_id)
            if log_channel and log_channel.id != message.channel.id:
                await self._post_log_embed_from_message(
                    log_channel, message, game_name, invite_type, timestamp
                )

        # Check whether this channel is blocked
        should_block = self._should_block_in_channel(message.channel, cfg)

        if should_block and cfg["delete_invites"]:
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention}, game invites are not allowed in this channel.",
                    delete_after=5,
                )
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _append_log(self, guild: discord.Guild, cfg: dict, entry: dict):
        """Append an entry to the guild's rolling invite log."""
        async with self.config.guild(guild).invite_log() as log:
            log.insert(0, entry)
            max_size = cfg.get("max_log_size", 50)
            if len(log) > max_size:
                del log[max_size:]

    async def _post_log_embed_from_message(
        self,
        log_channel: discord.TextChannel,
        message: discord.Message,
        game_name: str,
        invite_type: str,
        timestamp: str,
    ):
        """Post a game-invite-detected embed to the staff log channel."""
        source_channel = message.channel
        embed = discord.Embed(
            title="🎮 Game Invite Detected",
            description=(
                f"{message.author.mention} sent a **{invite_type}** invite "
                f"for **{game_name}** in {source_channel.mention}."
            ),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=str(message.author),
            icon_url=message.author.display_avatar.url,
        )
        app = getattr(message, "application", None)
        if app and app.id:
            embed.set_footer(text=f"Application ID: {app.id}")

        try:
            await log_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _should_block_in_channel(
        self, channel: discord.TextChannel, cfg: dict
    ) -> bool:
        """Return True if invite activity should be blocked in this channel."""
        mode = cfg["mode"]
        channels = cfg["channels"]
        categories = cfg["categories"]

        channel_in_list = channel.id in channels
        category_in_list = (
            channel.category_id in categories
            if channel.category_id
            else False
        )
        in_list = channel_in_list or category_in_list

        if mode == "blacklist":
            return in_list        # block channels that are listed
        else:                     # whitelist
            return not in_list    # block channels that are NOT listed
