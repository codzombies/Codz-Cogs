from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import humanize_list
import discord
from typing import Literal, Optional
import datetime


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
    #  Presence listener                                                   #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_presence_update(
        self, before: discord.Member, after: discord.Member
    ):
        """
        Fires whenever a member's presence changes.

        We look for a Rich Presence activity that has **both** a party *and* a
        join secret — that is the exact configuration required for Discord's
        native game-invite buttons to appear (per the Managing Game Invites docs).
        """
        if after.bot:
            return
        if after.guild is None:
            return

        cfg = await self.config.guild(after.guild).all()
        if not cfg["enabled"]:
            return

        # Find the new invite-capable activity (must have party + secrets)
        invite_activity = self._find_invite_activity(after.activities)
        if invite_activity is None:
            return

        # Optional game filter
        if cfg["tracked_games"]:
            app_id = str(getattr(invite_activity, "application_id", "") or "")
            if app_id not in cfg["tracked_games"]:
                return

        # Build human-readable info
        game_name = invite_activity.name or "Unknown Game"
        party_info = self._format_party(invite_activity)
        timestamp = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        # Store in rolling log
        await self._append_log(
            after.guild,
            cfg,
            {
                "user_id": after.id,
                "game": game_name,
                "party": party_info,
                "timestamp": timestamp,
                "application_id": str(
                    getattr(invite_activity, "application_id", "") or ""
                ),
            },
        )

        # Post to log channel if configured
        log_channel_id = cfg["log_channel"]
        if log_channel_id:
            log_channel = after.guild.get_channel(log_channel_id)
            if log_channel:
                await self._post_log_embed(
                    log_channel, after, invite_activity, party_info, timestamp
                )

        # Handle channel-based restrictions
        # We scan text channels the member can see for any messages that
        # Discord auto-generated as a game invite (type == CALL or activity invite).
        # Since discord.py doesn't expose invite-embed messages directly via presence,
        # we check all visible channels against the blacklist/whitelist and,
        # if delete_invites is on, we scan recent messages for the activity invite type.
        if cfg["delete_invites"]:
            await self._maybe_delete_invite_messages(after, invite_activity, cfg)

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _find_invite_activity(self, activities):
        """
        Return the first Activity that qualifies as invite-capable.

        According to the Discord docs an activity enables game invites when it has:
          - a party (with party ID, current size, max size)
          - a join secret (ActivitySecrets.join)
          - supported platforms

        discord.py exposes these on `discord.Activity` objects.
        """
        for activity in activities:
            if not isinstance(activity, discord.Activity):
                continue
            # party must exist and have a join field
            if activity.party and activity.party.get("id"):
                # The join secret is exposed as activity.secrets (dict)
                secrets = getattr(activity, "secrets", {}) or {}
                if secrets.get("join"):
                    return activity
        return None

    def _format_party(self, activity: discord.Activity) -> str:
        """Return a 'current/max' party string, or empty string if unavailable."""
        party = activity.party or {}
        size = party.get("size")
        if size and len(size) == 2:
            return f"{size[0]}/{size[1]} players"
        return ""

    async def _append_log(self, guild: discord.Guild, cfg: dict, entry: dict):
        """Append an entry to the guild's rolling invite log."""
        async with self.config.guild(guild).invite_log() as log:
            log.insert(0, entry)
            max_size = cfg.get("max_log_size", 50)
            if len(log) > max_size:
                del log[max_size:]

    async def _post_log_embed(
        self,
        channel: discord.TextChannel,
        member: discord.Member,
        activity: discord.Activity,
        party_info: str,
        timestamp: str,
    ):
        """Post a Rich Presence invite-detected embed to the log channel."""
        embed = discord.Embed(
            title="🎮 Game Invite Available",
            description=(
                f"{member.mention} is now invitable to **{activity.name}**."
            ),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=str(member), icon_url=member.display_avatar.url
        )
        if party_info:
            embed.add_field(name="Party", value=party_info, inline=True)

        state = activity.state or ""
        details = activity.details or ""
        if state:
            embed.add_field(name="State", value=state, inline=True)
        if details:
            embed.add_field(name="Details", value=details, inline=True)

        app_id = getattr(activity, "application_id", None)
        if app_id:
            embed.set_footer(text=f"Application ID: {app_id}")

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _maybe_delete_invite_messages(
        self,
        member: discord.Member,
        activity: discord.Activity,
        cfg: dict,
    ):
        """
        Scan recent messages in channels where invites are *blocked* and delete
        any Discord-native game-invite messages sent by this member.

        Discord sends game invites as messages with type `MessageType.call`
        or as activity-invite messages. We look for messages from the member
        that reference the same application_id.
        """
        for channel in member.guild.text_channels:
            if not self._should_block_in_channel(channel, cfg):
                continue
            if not channel.permissions_for(member.guild.me).manage_messages:
                continue
            if not channel.permissions_for(member.guild.me).read_message_history:
                continue
            try:
                async for message in channel.history(limit=20):
                    if message.author.id != member.id:
                        continue
                    # Discord's activity invite messages have an Activity on them
                    if (
                        message.activity
                        and str(getattr(activity, "application_id", ""))
                        and message.activity.get("application_id")
                        == str(getattr(activity, "application_id", ""))
                    ):
                        await message.delete()
                        warning = await channel.send(
                            f"{member.mention}, game invites are not allowed in this channel.",
                            delete_after=5,
                        )
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
