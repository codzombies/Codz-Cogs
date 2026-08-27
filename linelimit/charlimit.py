import logging
from typing import Optional

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

BaseCog = getattr(commands, "Cog", object)


class Linelimit(BaseCog): 
    """Limit the amount of lines per message, per channel or per category."""

    __author__ = ["Eternalll"]
    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=98757087587)

        def_channel = {
            "line_limit": None,
        }
        def_guild = {
            "mod_channel": None,
            "delete_delay": 10,
            "categories": {},
        }
        self.config.register_channel(**def_channel)
        self.config.register_guild(**def_guild)

        # cache["channels"][channel_id] = line_limit
        # cache["categories"][category_id] = line_limit
        self.cache = {"channels": {}, "categories": {}}
        self.loaded = False  # Whether the cache has been built at least once
        self.log = logging.getLogger("red.cogs.linelimit")

    def format_help_for_context(self, ctx: commands.Context) -> str:
        context = super().format_help_for_context(ctx)
        authors = ", ".join(self.__author__)
        return f"{context}\n\nAuthor: {authors}\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, **kwargs):
        """
        Nothing to delete
        """
        return

    # -------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------

    @commands.guild_only()
    @commands.group()
    @checks.mod_or_permissions(manage_messages=True)
    @checks.bot_has_permissions(embed_links=True, manage_messages=True)
    async def linelimit(self, ctx):
        """Manage line limits for channels and categories"""
        pass

    @linelimit.group(name="set")
    async def set_group(self, ctx):
        """
        Set the line limit for a channel or a category.
        """
        pass

    @set_group.command(name="channel")
    async def set_channel(self, ctx, channel: discord.TextChannel, lines: int):
        """
        Sets a channel's line limit.

        This overrides any category limit for this specific channel.
        Set `lines` to 0 to remove the channel-specific limit.

        `example: [p]linelimit set channel #lfg 2`
        """
        if lines <= 0:
            await self.config.channel(channel).line_limit.set(None)
            self.cache["channels"].pop(channel.id, None)
            await ctx.send(f"Done, cleared the line limit for {channel.mention}.")
        else:
            await self.config.channel(channel).line_limit.set(lines)
            self.cache["channels"][channel.id] = lines
            await ctx.send(f"Done, {channel.mention} is now limited to {lines} line(s) per message.")

    @set_group.command(name="category")
    async def set_category(self, ctx, category: discord.CategoryChannel, lines: int):
        """
        Sets a category's line limit.

        This applies to every channel inside the category, unless that channel
        has its own limit set via `[p]linelimit set channel`.
        Set `lines` to 0 to remove the category limit.

        `example: [p]linelimit set category "Looking For Group" 2`
        """
        async with self.config.guild(ctx.guild).categories() as categories:
            if lines <= 0:
                categories.pop(str(category.id), None)
                self.cache["categories"].pop(category.id, None)
                await ctx.send(f"Done, cleared the line limit for category **{category.name}**.")
            else:
                categories[str(category.id)] = lines
                self.cache["categories"][category.id] = lines
                await ctx.send(
                    f"Done, category **{category.name}** is now limited to {lines} line(s) per message."
                )

    @linelimit.command(name="modchannel")
    async def mod_channel(self, ctx, channel: Optional[discord.TextChannel] = None):
        """
        Sets the channel mods are notified in when a message is deleted.

        Leave `channel` empty to disable mod notifications.

        `example: [p]linelimit modchannel #mod-log`
        """
        if channel is None:
            await self.config.guild(ctx.guild).mod_channel.set(None)
            await ctx.send("Done, mod notifications are now disabled.")
        else:
            await self.config.guild(ctx.guild).mod_channel.set(channel.id)
            await ctx.send(f"Done, mod notifications will now be sent to {channel.mention}.")

    @linelimit.command(name="delay")
    async def delay(self, ctx, seconds: int):
        """
        Sets how long the in-channel deletion notice stays before it self-deletes.

        Set `seconds` to 0 to keep the notice from ever auto-deleting.

        `example: [p]linelimit delay 10`
        """
        if seconds < 0:
            return await ctx.send("The delay must be 0 or a positive number of seconds.")
        await self.config.guild(ctx.guild).delete_delay.set(seconds if seconds > 0 else None)
        if seconds > 0:
            await ctx.send(f"Done, the deletion notice will now auto-delete after {seconds} second(s).")
        else:
            await ctx.send("Done, the deletion notice will no longer auto-delete.")

    @linelimit.command(name="list")
    async def list_settings(self, ctx):
        """
        Lists the current line limit settings for this server.
        """
        guild = ctx.guild
        mod_channel_id = await self.config.guild(guild).mod_channel()
        delay = await self.config.guild(guild).delete_delay()
        categories = await self.config.guild(guild).categories()

        channel_lines = []
        for channel in guild.text_channels:
            limit = await self.config.channel(channel).line_limit()
            if limit:
                channel_lines.append(f"{channel.mention}: {limit} line(s)")

        category_lines = []
        for cat_id, limit in categories.items():
            category = guild.get_channel(int(cat_id))
            name = category.name if category else f"Unknown category ({cat_id})"
            category_lines.append(f"**{name}**: {limit} line(s)")

        mod_channel = guild.get_channel(mod_channel_id) if mod_channel_id else None

        e = discord.Embed(color=await ctx.embed_color())
        e.title = f"{guild}'s Line Limit Settings"
        e.add_field(
            name="Mod Notification Channel",
            value=mod_channel.mention if mod_channel else "Not set",
            inline=True,
        )
        e.add_field(name="Deletion Notice Delay", value=f"{delay}s" if delay else "Disabled", inline=True)
        e.add_field(name="Channel Limits", value="\n".join(channel_lines) or "None set", inline=False)
        e.add_field(name="Category Limits", value="\n".join(category_lines) or "None set", inline=False)
        await ctx.send(embed=e)

    # -------------------------------------------------------------------
    # Listeners
    # -------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message):
        if not isinstance(message.guild, discord.Guild):
            return False

        if message.author.bot:
            return False

        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return False

        if isinstance(message.author, discord.Member):
            if await self.bot.is_automod_immune(message.author):
                return False

        if not self.loaded:
            await self.build_cache()

        channel = message.channel
        limit = self.cache["channels"].get(channel.id)
        if limit is None and getattr(channel, "category_id", None) is not None:
            limit = self.cache["categories"].get(channel.category_id)

        if not limit:
            return False

        line_count = len(message.content.split("\n"))
        if line_count <= limit:
            return False

        try:
            await message.delete()
        except discord.NotFound:
            return False
        except discord.Forbidden:
            self.log.debug(f"Forbidden access to delete in {channel}")
            return False
        except discord.HTTPException as e:
            self.log.debug(f"HTTPException deleting in {channel} - {e.status}")
            return False

        await self.notify_channel(message, limit)
        await self.notify_mods(message, limit)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        await self.on_message(after)

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    async def notify_channel(self, message: discord.Message, limit: int):
        """
        Posts a deletion notice in the channel, mentioning the user, then
        self-deletes after the configured delay.
        """
        delay = await self.config.guild(message.guild).delete_delay()
        content = (
            f"{message.author.mention}, your message was removed for exceeding the "
            f"{limit}-line limit in {message.channel.mention}. Please condense it into "
            f"{limit} line(s) or fewer and try again."
        )
        try:
            await message.channel.send(content, delete_after=delay if delay else None)
        except discord.Forbidden:
            self.log.debug(f"Forbidden access to send in {message.channel}")
        except discord.HTTPException as e:
            self.log.debug(f"HTTPException sending in {message.channel} - {e.status}")

    async def notify_mods(self, message: discord.Message, limit: int):
        """
        Sends an embed with the message content to the configured mod channel, if any.
        """
        mod_channel_id = await self.config.guild(message.guild).mod_channel()
        if not mod_channel_id:
            return

        mod_channel = message.guild.get_channel(mod_channel_id)
        if mod_channel is None:
            return

        content = message.content or "*[No text content]*"
        if len(content) > 1000:
            content = content[:1000] + "\N{HORIZONTAL ELLIPSIS}"

        e = discord.Embed(
            title="Removed for Too Many Lines",
            color=await self.bot.get_embed_color(mod_channel),
        )
        e.add_field(name="Mention", value=message.author.mention, inline=True)
        e.add_field(name="UserID", value=f"`{message.author.id}`", inline=True)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
        e.add_field(name="Limit", value=f"{limit} line(s)", inline=True)
        e.add_field(name="Message", value=f"```{content}```", inline=False)

        try:
            await mod_channel.send(embed=e)
        except discord.Forbidden:
            self.log.debug(f"Forbidden access to send in {mod_channel}")
        except discord.HTTPException as e2:
            self.log.debug(f"HTTPException sending in {mod_channel} - {e2.status}")

    async def build_cache(self):
        """
        Builds the in-memory cache of channel and category line limits from Config.
        Channel ids and category ids are unique across the whole bot, so this can
        be built once globally rather than per-guild.
        """
        self.cache["channels"] = {}
        all_channels = await self.config.all_channels()
        for channel_id, data in all_channels.items():
            limit = data.get("line_limit")
            if limit:
                self.cache["channels"][int(channel_id)] = limit

        self.cache["categories"] = {}
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            for category_id, limit in data.get("categories", {}).items():
                if limit:
                    self.cache["categories"][int(category_id)] = limit

        self.loaded = True
