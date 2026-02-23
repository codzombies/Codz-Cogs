from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import box, humanize_list
import discord
from typing import Literal

class StickerControl(commands.Cog):
    """Control where users can post stickers using blacklist/whitelist systems"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)
        
        default_guild = {
            "mode": "blacklist",  # "blacklist" or "whitelist"
            "channels": [],  # List of channel IDs
            "categories": [],  # List of category IDs
            "enabled": False,
            "log_channel": None  # Channel ID to log deleted sticker events
        }
        
        self.config.register_guild(**default_guild)
    
    @commands.group()
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def stickercontrol(self, ctx):
        """Manage sticker posting restrictions"""
        pass
    
    @stickercontrol.command(name="mode")
    async def set_mode(self, ctx, mode: Literal["blacklist", "whitelist"]):
        """
        Set the restriction mode
        
        **Blacklist mode**: Stickers are blocked in specified channels/categories
        **Whitelist mode**: Stickers are only allowed in specified channels/categories
        """
        await self.config.guild(ctx.guild).mode.set(mode)
        await ctx.send(f"✅ Sticker control mode set to **{mode}**")
    
    @stickercontrol.command(name="enable")
    async def enable_control(self, ctx):
        """Enable sticker control"""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("✅ Sticker control is now **enabled**")
    
    @stickercontrol.command(name="disable")
    async def disable_control(self, ctx):
        """Disable sticker control"""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("✅ Sticker control is now **disabled**")
    
    @stickercontrol.command(name="addchannel")
    async def add_channel(self, ctx, *channels: discord.TextChannel):
        """Add channels to the blacklist/whitelist"""
        if not channels:
            await ctx.send("❌ Please specify at least one channel")
            return
        
        async with self.config.guild(ctx.guild).channels() as channel_list:
            added = []
            for channel in channels:
                if channel.id not in channel_list:
                    channel_list.append(channel.id)
                    added.append(channel.mention)
            
            if added:
                mode = await self.config.guild(ctx.guild).mode()
                await ctx.send(f"✅ Added {humanize_list(added)} to the {mode}")
            else:
                await ctx.send("ℹ️ All specified channels were already in the list")
    
    @stickercontrol.command(name="removechannel")
    async def remove_channel(self, ctx, *channels: discord.TextChannel):
        """Remove channels from the blacklist/whitelist"""
        if not channels:
            await ctx.send("❌ Please specify at least one channel")
            return
        
        async with self.config.guild(ctx.guild).channels() as channel_list:
            removed = []
            for channel in channels:
                if channel.id in channel_list:
                    channel_list.remove(channel.id)
                    removed.append(channel.mention)
            
            if removed:
                mode = await self.config.guild(ctx.guild).mode()
                await ctx.send(f"✅ Removed {humanize_list(removed)} from the {mode}")
            else:
                await ctx.send("ℹ️ None of the specified channels were in the list")
    
    @stickercontrol.command(name="addcategory")
    async def add_category(self, ctx, *categories: discord.CategoryChannel):
        """Add categories to the blacklist/whitelist"""
        if not categories:
            await ctx.send("❌ Please specify at least one category")
            return
        
        async with self.config.guild(ctx.guild).categories() as category_list:
            added = []
            for category in categories:
                if category.id not in category_list:
                    category_list.append(category.id)
                    added.append(f"**{category.name}**")
            
            if added:
                mode = await self.config.guild(ctx.guild).mode()
                await ctx.send(f"✅ Added {humanize_list(added)} to the {mode}")
            else:
                await ctx.send("ℹ️ All specified categories were already in the list")
    
    @stickercontrol.command(name="removecategory")
    async def remove_category(self, ctx, *categories: discord.CategoryChannel):
        """Remove categories from the blacklist/whitelist"""
        if not categories:
            await ctx.send("❌ Please specify at least one category")
            return
        
        async with self.config.guild(ctx.guild).categories() as category_list:
            removed = []
            for category in categories:
                if category.id in category_list:
                    category_list.remove(category.id)
                    removed.append(f"**{category.name}**")
            
            if removed:
                mode = await self.config.guild(ctx.guild).mode()
                await ctx.send(f"✅ Removed {humanize_list(removed)} from the {mode}")
            else:
                await ctx.send("ℹ️ None of the specified categories were in the list")
    
    @stickercontrol.command(name="list")
    async def list_settings(self, ctx):
        """Show current sticker control settings"""
        guild_config = await self.config.guild(ctx.guild).all()
        
        enabled = "✅ Enabled" if guild_config["enabled"] else "❌ Disabled"
        mode = guild_config["mode"].capitalize()
        
        channel_mentions = []
        for channel_id in guild_config["channels"]:
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                channel_mentions.append(channel.mention)
            else:
                channel_mentions.append(f"Unknown ({channel_id})")
        
        category_names = []
        for category_id in guild_config["categories"]:
            category = ctx.guild.get_channel(category_id)
            if category:
                category_names.append(f"**{category.name}**")
            else:
                category_names.append(f"Unknown ({category_id})")
        
        embed = discord.Embed(
            title="Sticker Control Settings",
            color=await ctx.embed_color()
        )
        embed.add_field(name="Status", value=enabled, inline=True)
        embed.add_field(name="Mode", value=mode, inline=True)

        log_channel_id = guild_config["log_channel"]
        log_channel = ctx.guild.get_channel(log_channel_id) if log_channel_id else None
        log_channel_text = log_channel.mention if log_channel else "Not set"
        embed.add_field(name="Log Channel", value=log_channel_text, inline=True)
        
        channels_text = humanize_list(channel_mentions) if channel_mentions else "None"
        categories_text = humanize_list(category_names) if category_names else "None"
        
        embed.add_field(name=f"Channels ({len(channel_mentions)})", value=channels_text, inline=False)
        embed.add_field(name=f"Categories ({len(category_names)})", value=categories_text, inline=False)
        
        await ctx.send(embed=embed)
    
    @stickercontrol.command(name="clear")
    async def clear_lists(self, ctx):
        """Clear all channels and categories from the list"""
        await self.config.guild(ctx.guild).channels.set([])
        await self.config.guild(ctx.guild).categories.set([])
        await ctx.send("✅ Cleared all channels and categories from the list")

    @stickercontrol.command(name="logchannel")
    async def set_log_channel(self, ctx, channel: discord.TextChannel = None):
        """
        Set or clear the channel where deleted sticker events are logged.

        Leave `channel` blank to disable logging.
        """
        if channel is None:
            await self.config.guild(ctx.guild).log_channel.set(None)
            await ctx.send("✅ Log channel cleared. Sticker deletions will no longer be logged.")
        else:
            await self.config.guild(ctx.guild).log_channel.set(channel.id)
            await ctx.send(f"✅ Sticker deletions will now be logged to {channel.mention}.")
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Check messages for stickers and delete if necessary"""
        # Ignore DMs and bot messages
        if not message.guild or message.author.bot:
            return
        
        # Check if the message has stickers
        if not message.stickers:
            return
        
        # Check if sticker control is enabled
        guild_config = await self.config.guild(message.guild).all()
        if not guild_config["enabled"]:
            return
        
        # Check if user has manage messages permission (bypass)
        if message.author.guild_permissions.manage_messages:
            return
        
        # Determine if stickers should be blocked
        should_block = await self._should_block_sticker(message.channel, guild_config)
        
        if should_block:
            try:
                await message.delete()
                # Send a brief notification that auto-deletes
                warning = await message.channel.send(
                    f"{message.author.mention}, stickers are not allowed in this channel.",
                    delete_after=5
                )
                # Log to the configured log channel if set
                log_channel_id = guild_config["log_channel"]
                if log_channel_id:
                    log_channel = message.guild.get_channel(log_channel_id)
                    if log_channel:
                        sticker = message.stickers[0]
                        embed = discord.Embed(
                            title="🗑️ Sticker Deleted",
                            color=await self.bot.get_embed_color(log_channel),
                            timestamp=message.created_at
                        )
                        embed.set_author(
                            name=str(message.author),
                            icon_url=message.author.display_avatar.url
                        )
                        embed.add_field(name="User", value=message.author.mention, inline=True)
                        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                        embed.add_field(name="Sticker", value=sticker.name, inline=True)
                        await log_channel.send(embed=embed)
            except discord.Forbidden:
                pass  # Bot lacks permissions to delete
            except discord.HTTPException:
                pass  # Message might already be deleted
    
    async def _should_block_sticker(self, channel, guild_config):
        """Determine if a sticker should be blocked in the given channel"""
        mode = guild_config["mode"]
        channels = guild_config["channels"]
        categories = guild_config["categories"]
        
        # Check if channel is in the list
        channel_in_list = channel.id in channels
        
        # Check if channel's category is in the list
        category_in_list = False
        if hasattr(channel, 'category') and channel.category:
            category_in_list = channel.category.id in categories
        
        in_list = channel_in_list or category_in_list
        
        if mode == "blacklist":
            # Block if in blacklist
            return in_list
        else:  # whitelist
            # Block if NOT in whitelist
            return not in_list